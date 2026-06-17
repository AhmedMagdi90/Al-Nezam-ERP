import logging
import os
from urllib.parse import urlparse, parse_qs, unquote
from django.core.management import call_command
from django.db import connections
from django.db import transaction
from django.utils.text import slugify

from accounts.models import Profile, Role
from django.contrib.auth.models import User
from manufacturing.models import Company

from .context import reset_current_tenant_db, set_current_tenant_db
from .db import ensure_tenant_database_ready, ensure_tenant_database_registered
from .demo_seed import DEMO_LOGIN_PASSWORD, seed_demo_tenant_package
from .emails import send_environment_access_email
from .models import Organization, Tenant
from .setup_copy import copy_environment_setup

TENANT_AUTH_BACKEND = "tenancy.auth_backend.TenantModelBackend"
logger = logging.getLogger(__name__)
ENVIRONMENT_BATCH_ORDER = (
    Tenant.EnvironmentType.DEMO,
    Tenant.EnvironmentType.TEST,
    Tenant.EnvironmentType.LIVE,
    Tenant.EnvironmentType.DEV,
)


def _send_environment_access_email_safely(organization: Organization) -> None:
    try:
        send_environment_access_email(organization)
    except Exception:
        logger.exception(
            "Failed to send environment access email for organization=%s",
            getattr(organization, "slug", "unknown"),
        )


def _is_postgres_url(value: str) -> bool:
    value = (value or "").strip().lower()
    return value.startswith("postgres://") or value.startswith("postgresql://")


def _ensure_postgres_database_exists(target_db_url: str, strict: bool = False) -> None:
    """
    Auto-create tenant PostgreSQL DB when TENANT_PG_ADMIN_URL is configured.
    Ensures the tenant DB and public schema are owned by the tenant app user,
    otherwise migrations will fail with permission errors.
    """
    if not _is_postgres_url(target_db_url):
        return

    admin_url = os.getenv("TENANT_PG_ADMIN_URL", "").strip()
    if not admin_url:
        if strict:
            raise RuntimeError(
                "TENANT_PG_ADMIN_URL is required to auto-create a per-company PostgreSQL database."
            )
        return

    try:
        import psycopg
        from psycopg import sql
    except ImportError:
        if strict:
            raise RuntimeError("psycopg is required for PostgreSQL tenant auto-provisioning.")
        logger.warning("psycopg not installed; cannot auto-create PostgreSQL tenant DB.")
        return

    target_parsed = urlparse(target_db_url)
    target_db_name = (target_parsed.path or "").lstrip("/")
    target_db_owner = unquote(target_parsed.username) if target_parsed.username else None
    if not target_db_name:
        return

    admin_parsed = urlparse(admin_url)
    admin_qs = parse_qs(admin_parsed.query or "")
    admin_db_name = (admin_parsed.path or "").lstrip("/") or "postgres"

    sslmode = admin_qs.get("sslmode", [None])[0]
    if not sslmode:
        sslmode = "require" if os.getenv("DB_SSL_REQUIRE", "1") == "1" else "prefer"

    conn_kwargs = {
        "dbname": admin_db_name,
        "host": admin_parsed.hostname,
        "port": admin_parsed.port,
        "user": unquote(admin_parsed.username) if admin_parsed.username else None,
        "password": unquote(admin_parsed.password) if admin_parsed.password else None,
        "sslmode": sslmode,
    }
    conn_kwargs = {k: v for k, v in conn_kwargs.items() if v not in (None, "")}

    with psycopg.connect(**conn_kwargs) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            if target_db_owner:
                cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (target_db_owner,))
                if cur.fetchone() is None:
                    raise RuntimeError(
                        f"PostgreSQL role '{target_db_owner}' does not exist for tenant database provisioning."
                    )

            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (target_db_name,))
            exists = cur.fetchone() is not None
            if not exists:
                if target_db_owner:
                    cur.execute(
                        sql.SQL("CREATE DATABASE {} OWNER {}")
                        .format(sql.Identifier(target_db_name), sql.Identifier(target_db_owner))
                    )
                else:
                    cur.execute(sql.SQL("CREATE DATABASE {}") .format(sql.Identifier(target_db_name)))

            if target_db_owner:
                cur.execute(
                    sql.SQL("ALTER DATABASE {} OWNER TO {}")
                    .format(sql.Identifier(target_db_name), sql.Identifier(target_db_owner))
                )
                cur.execute(
                    sql.SQL("GRANT ALL PRIVILEGES ON DATABASE {} TO {}")
                    .format(sql.Identifier(target_db_name), sql.Identifier(target_db_owner))
                )

    target_conn_kwargs = dict(conn_kwargs)
    target_conn_kwargs["dbname"] = target_db_name
    with psycopg.connect(**target_conn_kwargs) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            if target_db_owner:
                cur.execute(
                    sql.SQL("ALTER SCHEMA public OWNER TO {}")
                    .format(sql.Identifier(target_db_owner))
                )
                cur.execute(
                    sql.SQL("GRANT ALL ON SCHEMA public TO {}")
                    .format(sql.Identifier(target_db_owner))
                )
                cur.execute(
                    sql.SQL("GRANT CREATE ON SCHEMA public TO {}")
                    .format(sql.Identifier(target_db_owner))
                )


def _drop_postgres_database(target_db_url: str, strict: bool = False) -> None:
    if not _is_postgres_url(target_db_url):
        return

    admin_url = os.getenv("TENANT_PG_ADMIN_URL", "").strip()
    if not admin_url:
        if strict:
            raise RuntimeError(
                "TENANT_PG_ADMIN_URL is required to drop a per-company PostgreSQL database."
            )
        return

    try:
        import psycopg
        from psycopg import sql
    except ImportError:
        if strict:
            raise RuntimeError("psycopg is required for PostgreSQL tenant database deletion.")
        logger.warning("psycopg not installed; cannot drop PostgreSQL tenant DB.")
        return

    target_parsed = urlparse(target_db_url)
    target_db_name = (target_parsed.path or "").lstrip("/")
    if not target_db_name:
        return

    admin_parsed = urlparse(admin_url)
    admin_qs = parse_qs(admin_parsed.query or "")
    admin_db_name = (admin_parsed.path or "").lstrip("/") or "postgres"
    if target_db_name == admin_db_name:
        raise RuntimeError("Refusing to drop the admin PostgreSQL database.")

    sslmode = admin_qs.get("sslmode", [None])[0]
    if not sslmode:
        sslmode = "require" if os.getenv("DB_SSL_REQUIRE", "1") == "1" else "prefer"

    conn_kwargs = {
        "dbname": admin_db_name,
        "host": admin_parsed.hostname,
        "port": admin_parsed.port,
        "user": unquote(admin_parsed.username) if admin_parsed.username else None,
        "password": unquote(admin_parsed.password) if admin_parsed.password else None,
        "sslmode": sslmode,
    }
    conn_kwargs = {k: v for k, v in conn_kwargs.items() if v not in (None, "")}

    with psycopg.connect(**conn_kwargs) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (target_db_name,))
            exists = cur.fetchone() is not None
            if not exists:
                return
            cur.execute(
                """
                SELECT pg_terminate_backend(pid)
                FROM pg_stat_activity
                WHERE datname = %s AND pid <> pg_backend_pid()
                """,
                (target_db_name,),
            )
            cur.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(target_db_name)))


def delete_tenant_environment(tenant: Tenant) -> None:
    db_alias = getattr(tenant, "db_alias", "")
    if db_alias and db_alias in connections:
        try:
            connections[db_alias].close()
        except Exception:
            logger.warning("Failed to close tenant connection for alias=%s before deletion.", db_alias, exc_info=True)
    if db_alias and db_alias in connections.databases:
        connections.databases.pop(db_alias, None)

    if _is_postgres_url(tenant.db_name):
        _drop_postgres_database(tenant.db_name, strict=True)
    else:
        db_path = tenant.resolved_db_path
        for suffix in ("", "-wal", "-shm"):
            path = db_path.with_name(f"{db_path.name}{suffix}") if suffix else db_path
            try:
                if path.exists():
                    path.unlink()
            except FileNotFoundError:
                continue

    tenant.delete(using="default")


def delete_organization_environments(organization: Organization) -> None:
    tenants = list(
        Tenant.objects.using("default")
        .filter(organization=organization)
        .order_by("environment_type", "created_at")
    )
    active_tenants = [tenant.code for tenant in tenants if tenant.is_active]
    if active_tenants:
        raise ValueError(
            "Deactivate all customer environments before deleting the company."
        )

    for tenant in tenants:
        delete_tenant_environment(tenant)

    organization.delete(using="default")


def _build_tenant_db_url(tenant_code: str, db_alias: str) -> str:
    tenant_db_url_template = os.getenv("TENANT_DB_URL_TEMPLATE", "").strip()
    if not tenant_db_url_template:
        raise ValueError(
            "TENANT_DB_URL_TEMPLATE is required. Configure PostgreSQL tenant database template first."
        )

    db_name = tenant_db_url_template.format(
        code=tenant_code,
        code_underscore=tenant_code.replace("-", "_"),
        alias=db_alias,
    ).strip()
    if not _is_postgres_url(db_name):
        raise ValueError("TENANT_DB_URL_TEMPLATE must generate a PostgreSQL URL.")
    return db_name


def _owner_email_exists_globally(owner_email: str) -> bool:
    if Organization.objects.using("default").filter(owner_email__iexact=owner_email).exists():
        return True
    if Tenant.objects.using("default").filter(owner_email__iexact=owner_email, organization__isnull=True).exists():
        return True

    for tenant in Tenant.objects.using("default").filter(is_active=True).only("id", "db_alias", "db_name"):
        try:
            alias = ensure_tenant_database_registered(tenant)
            exists = User.objects.using(alias).filter(username__iexact=owner_email).exists() or User.objects.using(
                alias
            ).filter(email__iexact=owner_email).exists()
            if exists:
                if tenant.organization_id:
                    Organization.objects.using("default").filter(id=tenant.organization_id, owner_email__isnull=True).update(
                        owner_email=owner_email
                    )
                elif not tenant.owner_email:
                    Tenant.objects.using("default").filter(id=tenant.id).update(owner_email=owner_email)
                return True
        except Exception:
            logger.warning("Skipping tenant %s during global owner-email check.", tenant.code, exc_info=True)
            continue
    return False


def _organization_slug_exists(organization_slug: str) -> bool:
    if Organization.objects.using("default").filter(slug=organization_slug).exists():
        return True
    return Tenant.objects.using("default").filter(code=organization_slug, organization__isnull=True).exists()


def create_organization(
    company_name: str,
    company_code: str,
    owner_email: str,
) -> Organization:
    organization_slug = slugify((company_code or "").strip().lower())
    if not organization_slug:
        raise ValueError("A valid company code is required.")

    normalized_email = (owner_email or "").strip().lower()
    if not normalized_email:
        raise ValueError("Owner email is required.")

    if _organization_slug_exists(organization_slug):
        raise ValueError("This company code is already in use.")
    if _owner_email_exists_globally(normalized_email):
        raise ValueError("This owner email is already used by another company.")

    return Organization.objects.using("default").create(
        name=(company_name or "").strip(),
        slug=organization_slug,
        owner_email=normalized_email,
        status=Organization.Status.ACTIVE,
    )


def _build_default_hostname(organization_slug: str, environment_type: str) -> str | None:
    base_domain = os.getenv("TENANT_BASE_DOMAIN", "").strip().lower().lstrip(".")
    if not base_domain:
        return None
    label = organization_slug
    if environment_type != Tenant.EnvironmentType.LIVE:
        label = f"{organization_slug}-{environment_type}"
    return f"{label}.{base_domain}"


def _build_environment_code(organization_slug: str, environment_type: str) -> str:
    organization_slug = slugify((organization_slug or "").strip().lower())
    if environment_type == Tenant.EnvironmentType.LIVE:
        return organization_slug
    return f"{organization_slug}-{environment_type}"


def _build_tenant_runtime_identifiers(organization_slug: str, environment_type: str) -> tuple[str, str, str | None]:
    tenant_code = _build_environment_code(organization_slug, environment_type)
    db_alias = f"tenant_{tenant_code.replace('-', '_')}"
    hostname = _build_default_hostname(organization_slug, environment_type)
    return tenant_code, db_alias, hostname


def _create_owner_and_company_in_tenant_db(
    alias: str,
    company_name: str,
    owner_email: str,
    owner_password: str,
    owner_password_hash: str | None = None,
    subscription_plan: str = "free_trial",
) -> tuple[Company, User]:
    with transaction.atomic(using=alias):
        user, created = User.objects.using(alias).get_or_create(
            username=owner_email,
            defaults={
                "email": owner_email,
            },
        )
        if created:
            if owner_password_hash:
                user.password = owner_password_hash
            else:
                user.set_password(owner_password)
            user.email = owner_email
            user.save(using=alias, update_fields=["password", "email"])
        else:
            update_fields = []
            if owner_password_hash and user.password != owner_password_hash:
                user.password = owner_password_hash
                update_fields.append("password")
            if user.email != owner_email:
                user.email = owner_email
                update_fields.append("email")
            if update_fields:
                user.save(using=alias, update_fields=update_fields)

        company = Company.objects.using(alias).order_by("id").first()
        if company is None:
            company = Company.objects.using(alias).create(
                name=(company_name or "").strip(),
                subscription_plan=subscription_plan,
            )
        else:
            update_fields = []
            if company.name != (company_name or "").strip():
                company.name = (company_name or "").strip()
                update_fields.append("name")
            if company.subscription_plan != subscription_plan:
                company.subscription_plan = subscription_plan
                update_fields.append("subscription_plan")
            if update_fields:
                company.save(using=alias, update_fields=update_fields)

        admin_role, _ = Role.objects.using(alias).get_or_create(name="admin")
        Profile.objects.using(alias).update_or_create(
            user_id=user.id,
            defaults={
                "company_id": company.id,
                "role_id": admin_role.id,
                "app_scope": "planner",
                "department": "Management",
            },
        )

    return company, user


def provision_tenant_environment(
    organization: Organization,
    environment_type: str,
    owner_password: str,
    owner_password_hash: str | None = None,
    setup_source_tenant: Tenant | None = None,
    subscription_plan: str = "free_trial",
    seed_demo_package: bool = False,
    demo_password: str = DEMO_LOGIN_PASSWORD,
    send_access_email: bool = True,
) -> tuple[Tenant, Company, User]:
    """
    Provision an additional tenant environment (demo/test/live/dev) under an existing organization.
    The application stack stays shared; each tenant environment gets its own isolated database.
    """
    if not organization:
        raise ValueError("Organization is required.")

    normalized_email = (organization.owner_email or "").strip().lower()
    if not normalized_email:
        raise ValueError("Organization owner email is required before provisioning environments.")

    environment_type = (environment_type or "").strip().lower()
    allowed_types = {choice for choice, _label in Tenant.EnvironmentType.choices}
    if environment_type not in allowed_types:
        raise ValueError("Unsupported environment type.")

    existing_tenant = Tenant.objects.using("default").filter(
        organization=organization,
        environment_type=environment_type,
        is_active=True,
    ).first()
    if existing_tenant:
        raise ValueError(f"{environment_type.title()} environment already exists for this organization.")

    tenant_code, db_alias, hostname = _build_tenant_runtime_identifiers(organization.slug, environment_type)
    db_name = _build_tenant_db_url(tenant_code, db_alias)
    _ensure_postgres_database_exists(db_name, strict=True)

    tenant_name = organization.name if environment_type == Tenant.EnvironmentType.LIVE else f"{organization.name} {environment_type.title()}"
    tenant = Tenant.objects.using("default").create(
        name=tenant_name,
        organization=organization,
        owner_email=normalized_email,
        code=tenant_code,
        environment_type=environment_type,
        hostname=hostname,
        is_primary=environment_type == Tenant.EnvironmentType.LIVE,
        db_alias=db_alias,
        db_name=db_name,
        is_active=True,
    )

    try:
        alias = ensure_tenant_schema(tenant)
        ctx_token = set_current_tenant_db(alias)
        try:
            company, user = _create_owner_and_company_in_tenant_db(
                alias,
                organization.name,
                normalized_email,
                owner_password,
                owner_password_hash=owner_password_hash,
                subscription_plan=subscription_plan,
            )
            if environment_type == Tenant.EnvironmentType.DEMO and seed_demo_package:
                seed_demo_tenant_package(alias, company, user, password=demo_password)
            if setup_source_tenant:
                copy_environment_setup(setup_source_tenant, tenant)
        finally:
            reset_current_tenant_db(ctx_token)
    except Exception:
        try:
            Tenant.objects.using("default").filter(id=tenant.id).delete()
        except Exception:
            logger.exception("Failed to cleanup partial tenant environment id=%s during rollback.", tenant.id)
        raise

    if send_access_email:
        _send_environment_access_email_safely(organization)

    return tenant, company, user


def ensure_tenant_schema(tenant: Tenant) -> str:
    """
    Ensure tenant DB file exists and all migrations are applied.
    """
    _ensure_postgres_database_exists(tenant.db_name)
    return ensure_tenant_database_ready(tenant)


def provision_tenant_with_owner(
    company_name: str,
    company_code: str,
    owner_email: str,
    owner_password: str,
    subscription_plan: str = "free_trial",
):
    """
    Create control-plane tenant row, provision tenant DB, and create owner + company inside tenant DB.
    Returns (tenant, company, user).
    """
    organization = create_organization(
        company_name=company_name,
        company_code=company_code,
        owner_email=owner_email,
    )

    try:
        tenant, company, user = provision_tenant_environment(
            organization,
            Tenant.EnvironmentType.LIVE,
            owner_password=owner_password,
            subscription_plan=subscription_plan,
        )
    except Exception:
        # Prevent half-provisioned control-plane tenants when tenant DB setup fails.
        try:
            Organization.objects.using("default").filter(id=organization.id, tenants__isnull=True).delete()
        except Exception:
            logger.exception("Failed to cleanup partial organization id=%s during provisioning rollback.", organization.id)
        raise

    return tenant, company, user


def provision_demo_signup(
    company_name: str,
    company_code: str,
    owner_email: str,
    owner_password: str,
    subscription_plan: str = "free_trial",
    demo_password: str = DEMO_LOGIN_PASSWORD,
    seed_demo_package: bool = True,
):
    """
    Public signup path:
    - creates the organization
    - provisions only a demo environment
    - does not create live/test environments yet
    """
    organization = create_organization(
        company_name=company_name,
        company_code=company_code,
        owner_email=owner_email,
    )

    try:
        return provision_tenant_environment(
            organization,
            Tenant.EnvironmentType.DEMO,
            owner_password=owner_password,
            subscription_plan=subscription_plan,
            seed_demo_package=seed_demo_package,
            demo_password=demo_password,
        )
    except Exception:
        try:
            Organization.objects.using("default").filter(id=organization.id, tenants__isnull=True).delete()
        except Exception:
            logger.exception("Failed to cleanup partial demo signup organization id=%s.", organization.id)
        raise


def _setup_source_for_environment_batch(environment_type: str, created_tenants: dict[str, Tenant]) -> Tenant | None:
    if environment_type == Tenant.EnvironmentType.TEST:
        return created_tenants.get(Tenant.EnvironmentType.DEMO)
    if environment_type == Tenant.EnvironmentType.LIVE:
        return created_tenants.get(Tenant.EnvironmentType.TEST)
    if environment_type == Tenant.EnvironmentType.DEV:
        return (
            created_tenants.get(Tenant.EnvironmentType.DEMO)
            or created_tenants.get(Tenant.EnvironmentType.TEST)
            or created_tenants.get(Tenant.EnvironmentType.LIVE)
        )
    return None


def provision_organization_environments(
    company_name: str,
    company_code: str,
    owner_email: str,
    owner_password: str,
    environment_types: list[str] | tuple[str, ...],
    subscription_plan: str = "free_trial",
    demo_password: str = DEMO_LOGIN_PASSWORD,
) -> tuple[Organization, list[tuple[Tenant, Company, User]]]:
    normalized_types = []
    for environment_type in environment_types:
        normalized = (environment_type or "").strip().lower()
        if normalized and normalized not in normalized_types:
            normalized_types.append(normalized)

    allowed_types = {choice for choice, _label in Tenant.EnvironmentType.choices}
    invalid_types = [value for value in normalized_types if value not in allowed_types]
    if invalid_types:
        invalid_text = ", ".join(sorted(invalid_types))
        raise ValueError(f"Unsupported environment type(s): {invalid_text}")
    if not normalized_types:
        raise ValueError("At least one environment type is required.")

    organization = create_organization(
        company_name=company_name,
        company_code=company_code,
        owner_email=owner_email,
    )

    created: list[tuple[Tenant, Company, User]] = []
    created_by_type: dict[str, Tenant] = {}
    try:
        requested_types = set(normalized_types)
        for environment_type in ENVIRONMENT_BATCH_ORDER:
            if environment_type not in requested_types:
                continue
            tenant, company, user = provision_tenant_environment(
                organization,
                environment_type,
                owner_password=owner_password,
                subscription_plan=subscription_plan,
                seed_demo_package=environment_type == Tenant.EnvironmentType.DEMO,
                demo_password=demo_password,
                setup_source_tenant=_setup_source_for_environment_batch(environment_type, created_by_type),
                send_access_email=False,
            )
            created.append((tenant, company, user))
            created_by_type[environment_type] = tenant
    except Exception:
        try:
            Organization.objects.using("default").filter(id=organization.id, tenants__isnull=True).delete()
        except Exception:
            logger.exception("Failed to cleanup partial organization id=%s during environment batch provisioning.", organization.id)
        raise

    _send_environment_access_email_safely(organization)
    return organization, created
