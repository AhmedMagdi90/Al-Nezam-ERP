import logging
import time
from urllib.parse import urlencode
from pathlib import Path

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, get_user_model, login, logout
from django.contrib.auth.forms import SetPasswordForm
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.validators import validate_email
from django.db import DatabaseError, close_old_connections, connections
from django.db.models import Prefetch, Q
from django.http import Http404
from django.middleware.csrf import get_token
from django.shortcuts import redirect, render
from django.utils import timezone
from django.utils.encoding import force_str
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.http import urlsafe_base64_decode
from django.utils.text import slugify
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import ensure_csrf_cookie
from django.urls import reverse
from django.core.exceptions import ValidationError

from accounts.forms import OrganizationBootstrapForm, TenantLoginForm
from accounts.aws_billing import get_organization_billing_snapshot
from accounts.security import (
    clear_login_failures,
    get_login_throttle_state,
    register_login_failure,
)
from accounts.models import Profile
from manufacturing.forms import CompanyRegistrationForm
from manufacturing.models import Company
from tenancy.access_tokens import tenant_access_token_generator
from tenancy.db import ensure_tenant_database_registered
from tenancy.context import reset_current_tenant_db, set_current_tenant_db
from tenancy.emails import send_environment_access_email
from tenancy.lifecycle import describe_tenant_access, tenant_allows_workspace_access
from tenancy.models import Organization, SupportActionLog, Tenant
from tenancy.portal_urls import portal_environment_url
from tenancy.portal_urls import direct_login_url, tenant_app_url
from tenancy.services import (
    TENANT_AUTH_BACKEND,
    delete_tenant_environment,
    delete_organization_environments,
    ensure_tenant_schema,
    provision_demo_signup,
    provision_organization_environments,
    provision_tenant_environment,
)

logger = logging.getLogger(__name__)
GENERIC_LOGIN_ERROR = "Invalid company code, username/email, or password."


ENVIRONMENT_LABELS = {
    Tenant.EnvironmentType.DEMO: "Demo Server",
    Tenant.EnvironmentType.TEST: "Test Server",
    Tenant.EnvironmentType.LIVE: "Live Server",
    Tenant.EnvironmentType.DEV: "Dev Server",
}


def _is_next_url_allowed_for_role(user, next_url: str) -> bool:
    """Prevent stale deep-links from forcing users into the wrong module after login."""
    if not next_url:
        return False

    try:
        role = (user.profile.role.name or "").lower()
    except Exception:
        return True

    next_lower = next_url.lower()
    blocked_prefixes = {
        "supervisor": ["/manufacturing/planner", "/manufacturing/dashboard"],
        "worker": ["/manufacturing/planner", "/manufacturing/dashboard", "/manufacturing/supervisor"],
        "quality": ["/manufacturing/planner", "/manufacturing/dashboard", "/manufacturing/supervisor"],
        "maintenance": ["/manufacturing/planner", "/manufacturing/dashboard", "/manufacturing/supervisor"],
        "store": ["/manufacturing/planner", "/manufacturing/dashboard", "/manufacturing/supervisor", "/manufacturing/record-output"],
    }
    for prefix in blocked_prefixes.get(role, []):
        if next_lower.startswith(prefix):
            return False
    return True


def _authenticate_tenant_user(login_identifier: str, password: str, tenant_db_alias: str):
    user_model = get_user_model()
    tenant_user = user_model.objects.using(tenant_db_alias).filter(username__iexact=login_identifier).first()
    if tenant_user is None:
        tenant_user = user_model.objects.using(tenant_db_alias).filter(email__iexact=login_identifier).first()
    auth_username = tenant_user.username if tenant_user else login_identifier
    ctx_token = set_current_tenant_db(tenant_db_alias)
    try:
        return authenticate(
            username=auth_username,
            password=password,
            tenant_db_alias=tenant_db_alias,
        )
    finally:
        reset_current_tenant_db(ctx_token)


def _authenticate_tenant_user_with_retry(login_identifier: str, password: str, tenant_db_alias: str, retries: int = 3):
    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            return _authenticate_tenant_user(
                login_identifier=login_identifier,
                password=password,
                tenant_db_alias=tenant_db_alias,
            )
        except DatabaseError as exc:
            last_exc = exc
            is_lock_error = "locked" in str(exc).lower() or "busy" in str(exc).lower()
            if not is_lock_error and attempt >= 2:
                raise
            close_old_connections()
            if tenant_db_alias and tenant_db_alias in connections:
                connections[tenant_db_alias].close()
            time.sleep(0.15 * attempt)
    if last_exc is not None:
        raise last_exc
    return None


def _build_login_redirect(next_url: str = "", **params):
    url = reverse("login")
    query = {}
    if next_url:
        query["next"] = next_url
    query.update({k: v for k, v in params.items() if v})
    if query:
        return f"{url}?{urlencode(query)}"
    return url


def _prefilled_tenant_code(request) -> str:
    candidate = (
        request.POST.get("tenant_code")
        or request.GET.get("tenant_code")
        or getattr(getattr(request, "tenant", None), "code", "")
        or request.session.get("tenant_code", "")
    )
    return str(candidate or "").strip().lower()


def _preferred_active_tenant_for_organization(organization):
    if not organization:
        return None

    environment_priority = {
        Tenant.EnvironmentType.LIVE: 0,
        Tenant.EnvironmentType.DEMO: 1,
        Tenant.EnvironmentType.TEST: 2,
        Tenant.EnvironmentType.DEV: 3,
    }
    active_tenants = list(
        Tenant.objects.using("default")
        .filter(organization=organization, is_active=True)
        .order_by("environment_type", "created_at", "id")
    )
    if not active_tenants:
        return None
    return sorted(
        active_tenants,
        key=lambda item: (
            environment_priority.get(item.environment_type, 99),
            item.created_at or timezone.now(),
            item.id or 0,
        ),
    )[0]


def _resolve_login_tenant(tenant_code: str, login_identifier: str = ""):
    normalized_code = slugify(str(tenant_code or "").strip().lower())
    if not normalized_code:
        normalized_code = ""

    if normalized_code:
        tenant = Tenant.objects.using("default").filter(code=normalized_code, is_active=True).first()
        if tenant:
            return tenant

        organization = (
            Organization.objects.using("default")
            .filter(slug=normalized_code, status=Organization.Status.ACTIVE)
            .first()
        )
        tenant = _preferred_active_tenant_for_organization(organization)
        if tenant:
            return tenant

    owner_email = str(login_identifier or "").strip().lower()
    if not owner_email:
        return None

    organization = (
        Organization.objects.using("default")
        .filter(owner_email__iexact=owner_email, status=Organization.Status.ACTIVE)
        .first()
    )
    tenant = _preferred_active_tenant_for_organization(organization)
    if tenant:
        return tenant

    return (
        Tenant.objects.using("default")
        .filter(owner_email__iexact=owner_email, is_active=True)
        .order_by("environment_type", "created_at", "id")
        .first()
    )


def _trace_login_db_error(stage: str, tenant_code: str, exc: Exception):
    if not settings.DEBUG:
        return
    try:
        trace_path = Path(settings.BASE_DIR) / "login_db_errors.log"
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} | {stage} | tenant={tenant_code} | {exc.__class__.__name__}: {exc}\n"
        with open(trace_path, "a", encoding="utf-8") as fp:
            fp.write(line)
    except Exception:
        return


def _trace_login_event(stage: str, detail: str):
    if not settings.DEBUG:
        return
    try:
        trace_path = Path(settings.BASE_DIR) / "login_trace.log"
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} | {stage} | {detail}\n"
        with open(trace_path, "a", encoding="utf-8") as fp:
            fp.write(line)
    except Exception:
        return


def _resolve_profile_and_company(request):
    user = request.user
    db_alias = getattr(request, "tenant_db_alias", None) or getattr(getattr(user, "_state", None), "db", None) or "default"
    try:
        profile_row = (
            Profile.objects.using(db_alias)
            .filter(user_id=user.id)
            .values("id", "role__name", "company_id")
            .first()
        )
        if not profile_row:
            return None, None

        role_name = (profile_row.get("role__name") or "").lower() or None
        company = None
        company_id = profile_row.get("company_id")
        if company_id:
            company = Company.objects.using(db_alias).filter(id=company_id).first()
            if company:
                return role_name, company

        company = Company.objects.using(db_alias).order_by("-created_at").first()
        if company:
            Profile.objects.using(db_alias).filter(id=profile_row["id"], company_id__isnull=True).update(company_id=company.id)
        return role_name, company
    except Exception:
        return None, None


def _organization_for_user(request):
    tenant = getattr(request, "tenant", None)
    if tenant and tenant.organization_id:
        return tenant.organization

    session_tenant_code = request.session.get("tenant_code")
    if session_tenant_code:
        tenant = Tenant.objects.using("default").filter(code=session_tenant_code, is_active=True).select_related("organization").first()
        if tenant and tenant.organization_id:
            return tenant.organization

    user_email = (getattr(request.user, "email", None) or getattr(request.user, "username", None) or "").strip().lower()
    if not user_email:
        return None

    organization = Organization.objects.using("default").filter(owner_email__iexact=user_email).first()
    if organization:
        return organization

    tenant = (
        Tenant.objects.using("default")
        .filter(owner_email__iexact=user_email, is_active=True, organization__isnull=False)
        .select_related("organization")
        .first()
    )
    return tenant.organization if tenant and tenant.organization_id else None


def _environment_cards_for_organization(organization):
    tenants = list(
        Tenant.objects.using("default")
        .filter(organization=organization, is_active=True)
        .order_by("environment_type", "created_at")
    )
    cards = []
    for tenant in tenants:
        label = ENVIRONMENT_LABELS.get(tenant.environment_type, tenant.get_environment_type_display())
        lifecycle = describe_tenant_access(tenant)
        cards.append(
            {
                "tenant": tenant,
                "label": label,
                "is_live": tenant.environment_type == Tenant.EnvironmentType.LIVE,
                "is_demo": tenant.environment_type == Tenant.EnvironmentType.DEMO,
                "is_test": tenant.environment_type == Tenant.EnvironmentType.TEST,
                "is_dev": tenant.environment_type == Tenant.EnvironmentType.DEV,
                "lifecycle": lifecycle,
            }
        )
    return cards


def _user_can_manage_organization(request, organization) -> bool:
    user_email = (getattr(request.user, "email", None) or getattr(request.user, "username", None) or "").strip().lower()
    owner_email = (getattr(organization, "owner_email", None) or "").strip().lower()
    return bool(user_email and owner_email and user_email == owner_email)


def _user_can_access_bootstrap(request) -> bool:
    user = getattr(request, "user", None)
    return bool(user and user.is_authenticated and (user.is_staff or user.is_superuser))


def _control_center_redirect(base_url: str, q: str = "", status_filter: str = "", organization_id=None, tab: str = ""):
    params = {}
    if q:
        params["q"] = q
    if status_filter:
        params["status"] = status_filter
    if organization_id:
        params["org"] = organization_id
    if tab:
        params["tab"] = tab
    if params:
        return f"{base_url}?{urlencode(params)}"
    return base_url


def _resolve_control_center_tab(raw_tab: str) -> str:
    allowed_tabs = {"overview", "environments", "billing", "activity", "support", "danger"}
    candidate = (raw_tab or "").strip().lower()
    return candidate if candidate in allowed_tabs else "overview"


def _platform_control_center_cards(organizations):
    cards = []
    for organization in organizations:
        tenants = sorted(
            list(getattr(organization, "prefetched_tenants", list(organization.tenants.all()))),
            key=lambda tenant: (tenant.environment_type, tenant.created_at or timezone.now()),
        )
        tenant_cards = []
        for tenant in tenants:
            lifecycle = describe_tenant_access(tenant)
            tenant_cards.append(
                {
                    "tenant": tenant,
                    "lifecycle": lifecycle,
                    "login_url": direct_login_url(tenant),
                    "app_url": tenant_app_url(tenant),
                    "support_tag_value": tenant.code,
                }
            )
        active_tenants = [item for item in tenant_cards if item["tenant"].is_active]
        cards.append(
            {
                "organization": organization,
                "tenant_cards": tenant_cards,
                "tenant_count": len(tenant_cards),
                "active_tenant_count": len(active_tenants),
                "inactive_tenant_count": len(tenant_cards) - len(active_tenants),
                "owner_label": organization.owner_email or "No owner email",
            }
        )
    return cards


def _platform_control_timeline(organization, tenant_cards):
    timeline = [
        {
            "title": "Customer record created",
            "detail": f"{organization.name} entered the control plane.",
            "timestamp": organization.created_at,
            "tone": "slate",
        },
        {
            "title": "Customer profile last updated",
            "detail": f"Status is {organization.status} with seat limit {organization.seat_limit}.",
            "timestamp": organization.updated_at,
            "tone": "cyan",
        },
    ]
    for item in tenant_cards:
        tenant = item["tenant"]
        label = ENVIRONMENT_LABELS.get(tenant.environment_type, tenant.get_environment_type_display())
        timeline.append(
            {
                "title": f"{label} provisioned",
                "detail": tenant.code if not tenant.hostname else f"{tenant.code} · {tenant.hostname}",
                "timestamp": tenant.created_at,
                "tone": "emerald" if tenant.is_active else "rose",
            }
        )
        if tenant.environment_type == Tenant.EnvironmentType.DEMO and item["lifecycle"].get("expires_at"):
            timeline.append(
                {
                    "title": "Demo access expires",
                    "detail": item["lifecycle"].get("summary") or "Demo lifecycle is active.",
                    "timestamp": item["lifecycle"]["expires_at"],
                    "tone": "amber" if not item["lifecycle"].get("is_expired") else "rose",
                }
            )
    timeline.sort(key=lambda entry: entry["timestamp"] or timezone.now(), reverse=True)
    return timeline


def _open_risk_summary(selected_card, billing_snapshot, support_environment_actions, latest_support_entry):
    if not selected_card:
        return {"label": "No customer selected", "tone": "slate"}

    demo_risk = next(
        (
            tenant_card for tenant_card in selected_card["tenant_cards"]
            if tenant_card["tenant"].environment_type == Tenant.EnvironmentType.DEMO
            and tenant_card["lifecycle"].get("days_remaining") is not None
            and tenant_card["lifecycle"].get("days_remaining") <= 3
        ),
        None,
    )
    if demo_risk:
        return {"label": "Demo expires soon", "tone": "amber"}
    if not (selected_card["organization"].owner_email or "").strip():
        return {"label": "Owner email missing", "tone": "amber"}
    if selected_card["active_tenant_count"] == 0:
        return {"label": "No active environment", "tone": "rose"}
    if support_environment_actions:
        return {"label": "Environment missing", "tone": "amber"}
    if billing_snapshot and not billing_snapshot.get("available"):
        return {"label": "Billing visibility offline", "tone": "slate"}
    if latest_support_entry and latest_support_entry.action_type in {
        "delete_organization",
        "set_environment_state",
        "bulk_set_environment_state",
        "deactivate_organization",
        "activate_organization",
        "set_organization_pending",
        "update_customer_identity",
        "send_recovery_access_email",
        "delete_environment",
    }:
        return {"label": "Recent support change", "tone": "cyan"}
    return {"label": "No immediate risk", "tone": "emerald"}


def _log_support_action(request, organization, action_type: str, *, target_label: str = "", notes: str = "", metadata: dict | None = None):
    if not organization:
        return
    actor = (getattr(request.user, "email", None) or getattr(request.user, "username", None) or "").strip().lower()
    SupportActionLog.objects.using("default").create(
        organization=organization,
        actor_email=actor,
        action_type=action_type,
        target_label=target_label or "",
        notes=notes or "",
        metadata=metadata or {},
    )


def _owner_password_hash_for_organization(organization):
    owner_email = (getattr(organization, "owner_email", None) or "").strip().lower()
    if not owner_email:
        return None
    candidates = (
        Tenant.objects.using("default")
        .filter(organization=organization, is_active=True)
        .order_by("environment_type", "created_at")
    )
    user_model = get_user_model()
    for tenant in candidates:
        alias = ensure_tenant_database_registered(tenant)
        owner_user = user_model.objects.using(alias).filter(username__iexact=owner_email).first()
        if owner_user is None:
            owner_user = user_model.objects.using(alias).filter(email__iexact=owner_email).first()
        if owner_user and getattr(owner_user, "password", ""):
            return owner_user.password
    return None


def _available_support_environment_provision_actions(organization):
    existing_types = set(
        Tenant.objects.using("default")
        .filter(organization=organization, is_active=True)
        .values_list("environment_type", flat=True)
    )
    action_specs = {
        Tenant.EnvironmentType.DEMO: {
            "label": "Create Demo Server",
            "description": "Provision a fresh demo workspace for guided evaluation and first-touch support.",
            "tone": "cyan",
        },
        Tenant.EnvironmentType.TEST: {
            "label": "Create Test Server",
            "description": "Provision a customer validation environment from the latest demo or available setup.",
            "tone": "amber",
        },
        Tenant.EnvironmentType.LIVE: {
            "label": "Create Live Server",
            "description": "Provision a production environment using the latest approved setup source.",
            "tone": "emerald",
        },
        Tenant.EnvironmentType.DEV: {
            "label": "Create Dev Server",
            "description": "Provision an internal engineering/support environment for troubleshooting or preview work.",
            "tone": "slate",
        },
    }
    actions = []
    for environment_type, spec in action_specs.items():
        if environment_type in existing_types:
            continue
        actions.append({"environment_type": environment_type, **spec})
    return actions


@login_required
def platform_control_center(request):
    if not _user_can_access_bootstrap(request):
        raise PermissionDenied("Only staff users can access the platform control center.")

    base_url = reverse("platform_control_center")
    q = (request.POST.get("q") or request.GET.get("q") or "").strip()
    status_filter = (request.POST.get("status") or request.GET.get("status") or "").strip().lower()
    selected_org_id = request.POST.get("organization_id") or request.GET.get("org")
    selected_tab = _resolve_control_center_tab(request.POST.get("tab") or request.GET.get("tab"))

    if request.method == "POST":
        action = (request.POST.get("action") or "").strip().lower()
        if action == "update_organization":
            organization = (
                Organization.objects.using("default")
                .filter(pk=request.POST.get("organization_id"))
                .first()
            )
            if not organization:
                messages.error(request, "Customer was not found.")
            else:
                status_value = (request.POST.get("organization_status") or "").strip().lower()
                seat_limit_raw = (request.POST.get("seat_limit") or "").strip()
                organization_name = (request.POST.get("organization_name") or "").strip()
                owner_email_raw = (request.POST.get("owner_email") or "").strip().lower()
                owner_email_value = owner_email_raw or None
                valid_statuses = {choice for choice, _label in Organization.Status.choices}
                if status_value not in valid_statuses:
                    messages.error(request, "Invalid customer status.")
                elif not organization_name:
                    messages.error(request, "Company name is required.")
                elif owner_email_value and Organization.objects.using("default").filter(owner_email__iexact=owner_email_value).exclude(pk=organization.pk).exists():
                    messages.error(request, "Owner email is already assigned to another customer.")
                else:
                    try:
                        seat_limit = int(seat_limit_raw or organization.seat_limit or 1)
                    except (TypeError, ValueError):
                        seat_limit = 0
                    if seat_limit <= 0:
                        messages.error(request, "Seat limit must be a positive number.")
                    else:
                        previous_owner_email = organization.owner_email
                        organization.status = status_value
                        organization.name = organization_name
                        organization.owner_email = owner_email_value
                        organization.seat_limit = seat_limit
                        organization.wants_test_environment = request.POST.get("wants_test_environment") == "on"
                        update_fields = ["status", "name", "owner_email", "seat_limit", "wants_test_environment", "updated_at"]
                        if "support_notes" in request.POST:
                            organization.support_notes = (request.POST.get("support_notes") or "").strip()
                            update_fields.append("support_notes")
                        organization.save(update_fields=update_fields)
                        if previous_owner_email != organization.owner_email:
                            Tenant.objects.using("default").filter(organization=organization).update(
                                owner_email=organization.owner_email,
                                updated_at=timezone.now(),
                            )
                        _log_support_action(
                            request,
                            organization,
                            "update_customer_identity",
                            target_label=organization.slug,
                            notes=organization.support_notes,
                            metadata={
                                "name": organization.name,
                                "owner_email": organization.owner_email or "",
                                "status": organization.status,
                                "seat_limit": organization.seat_limit,
                                "wants_test_environment": organization.wants_test_environment,
                            },
                        )
                        messages.success(request, f"{organization.name} updated successfully.")
            return redirect(_control_center_redirect(base_url, q=q, status_filter=status_filter, organization_id=request.POST.get("organization_id"), tab=selected_tab))

        if action == "update_support_notes":
            organization = (
                Organization.objects.using("default")
                .filter(pk=request.POST.get("organization_id"))
                .first()
            )
            if not organization:
                messages.error(request, "Customer was not found.")
            else:
                organization.support_notes = (request.POST.get("support_notes") or "").strip()
                organization.save(update_fields=["support_notes", "updated_at"])
                _log_support_action(
                    request,
                    organization,
                    "update_support_notes",
                    target_label=organization.slug,
                    notes=organization.support_notes,
                )
                messages.success(request, f"Support notes saved for {organization.name}.")
            return redirect(_control_center_redirect(base_url, q=q, status_filter=status_filter, organization_id=request.POST.get("organization_id"), tab="support"))

        if action == "set_organization_status":
            organization = (
                Organization.objects.using("default")
                .filter(pk=request.POST.get("organization_id"))
                .first()
            )
            desired_status = (request.POST.get("organization_status") or "").strip().lower()
            if not organization:
                messages.error(request, "Customer was not found.")
            elif desired_status not in {Organization.Status.ACTIVE, Organization.Status.PENDING}:
                messages.error(request, "Selected company lifecycle status is invalid.")
            else:
                now = timezone.now()
                reactivated_count = 0
                organization.status = desired_status
                organization.save(update_fields=["status", "updated_at"])
                if desired_status == Organization.Status.ACTIVE and request.POST.get("reactivate_environments") == "on":
                    reactivated_count = (
                        Tenant.objects.using("default")
                        .filter(organization=organization, is_active=False)
                        .update(is_active=True, updated_at=now)
                    )
                action_type = "activate_organization" if desired_status == Organization.Status.ACTIVE else "set_organization_pending"
                _log_support_action(
                    request,
                    organization,
                    action_type,
                    target_label=organization.slug,
                    metadata={
                        "status": desired_status,
                        "reactivated_environment_count": reactivated_count,
                    },
                )
                if desired_status == Organization.Status.ACTIVE:
                    if reactivated_count:
                        messages.success(
                            request,
                            f"{organization.name} was activated and {reactivated_count} environments were re-enabled.",
                        )
                    else:
                        messages.success(
                            request,
                            f"{organization.name} was activated. Environments remain in their current state.",
                        )
                else:
                    messages.success(request, f"{organization.name} was moved to pending review.")
            return redirect(_control_center_redirect(base_url, q=q, status_filter=status_filter, organization_id=request.POST.get("organization_id"), tab="danger"))

        if action == "deactivate_organization":
            organization = (
                Organization.objects.using("default")
                .filter(pk=request.POST.get("organization_id"))
                .first()
            )
            if not organization:
                messages.error(request, "Customer was not found.")
            else:
                now = timezone.now()
                deactivated_count = (
                    Tenant.objects.using("default")
                    .filter(organization=organization, is_active=True)
                    .update(is_active=False, updated_at=now)
                )
                organization.status = Organization.Status.SUSPENDED
                organization.save(update_fields=["status", "updated_at"])
                _log_support_action(
                    request,
                    organization,
                    "deactivate_organization",
                    target_label=organization.slug,
                    metadata={"deactivated_environment_count": deactivated_count},
                )
                messages.success(
                    request,
                    f"{organization.name} was deactivated and {deactivated_count} active environments were shut off.",
                )
            return redirect(_control_center_redirect(base_url, q=q, status_filter=status_filter, organization_id=request.POST.get("organization_id"), tab="danger"))

        if action == "set_tenant_state":
            tenant = (
                Tenant.objects.using("default")
                .select_related("organization")
                .filter(pk=request.POST.get("tenant_id"))
                .first()
            )
            if not tenant:
                messages.error(request, "Environment was not found.")
            else:
                desired_state = (request.POST.get("tenant_state") or "").strip().lower()
                tenant.is_active = desired_state == "active"
                tenant.save(update_fields=["is_active", "updated_at"])
                _log_support_action(
                    request,
                    tenant.organization,
                    "set_environment_state",
                    target_label=tenant.code,
                    metadata={"tenant_state": "active" if tenant.is_active else "inactive"},
                )
                label = ENVIRONMENT_LABELS.get(tenant.environment_type, tenant.get_environment_type_display())
                messages.success(
                    request,
                    f"{tenant.organization.name} {label} marked {'active' if tenant.is_active else 'inactive'}.",
                )
            return redirect(_control_center_redirect(base_url, q=q, status_filter=status_filter, organization_id=request.POST.get("organization_id"), tab=selected_tab))

        if action == "bulk_set_environment_state":
            organization = (
                Organization.objects.using("default")
                .filter(pk=request.POST.get("organization_id"))
                .first()
            )
            desired_state = (request.POST.get("tenant_state") or "").strip().lower()
            if not organization:
                messages.error(request, "Customer was not found.")
            elif desired_state not in {"active", "inactive"}:
                messages.error(request, "Selected environment state is invalid.")
            elif desired_state == "active" and organization.status != Organization.Status.ACTIVE:
                messages.error(request, "Activate the company first before re-enabling its environments.")
            else:
                now = timezone.now()
                updated_count = (
                    Tenant.objects.using("default")
                    .filter(organization=organization, is_active=(desired_state != "active"))
                    .update(is_active=(desired_state == "active"), updated_at=now)
                )
                _log_support_action(
                    request,
                    organization,
                    "bulk_set_environment_state",
                    target_label=organization.slug,
                    metadata={"tenant_state": desired_state, "updated_count": updated_count},
                )
                if updated_count:
                    messages.success(
                        request,
                        f"{updated_count} environments were marked {desired_state} for {organization.name}.",
                    )
                else:
                    messages.info(
                        request,
                        f"No environments needed to change for {organization.name}.",
                    )
            return redirect(_control_center_redirect(base_url, q=q, status_filter=status_filter, organization_id=request.POST.get("organization_id"), tab="environments"))

        if action == "resend_access_email":
            organization = (
                Organization.objects.using("default")
                .filter(pk=request.POST.get("organization_id"))
                .first()
            )
            if not organization:
                messages.error(request, "Customer was not found.")
            elif not (organization.owner_email or "").strip():
                messages.error(request, "Owner email is missing for this customer.")
            else:
                try:
                    sent = send_environment_access_email(organization)
                except Exception:
                    logger.exception("Control center access email resend failed for org=%s", organization.slug)
                    messages.error(request, "Access email resend failed. Check mail settings and logs.")
                else:
                    if sent:
                        _log_support_action(
                            request,
                            organization,
                            "resend_access_email",
                            target_label=organization.owner_email,
                        )
                        messages.success(request, f"Access email resent to {organization.owner_email}.")
                    else:
                        messages.error(request, "Access email was not sent because mail delivery is not configured.")
            return redirect(_control_center_redirect(base_url, q=q, status_filter=status_filter, organization_id=request.POST.get("organization_id"), tab=selected_tab))

        if action == "send_recovery_access_email":
            organization = (
                Organization.objects.using("default")
                .filter(pk=request.POST.get("organization_id"))
                .first()
            )
            recovery_email = (request.POST.get("recovery_email") or "").strip().lower()
            if not organization:
                messages.error(request, "Customer was not found.")
            elif not recovery_email:
                messages.error(request, "Recovery email is required.")
            else:
                try:
                    validate_email(recovery_email)
                except ValidationError:
                    messages.error(request, "Recovery email is invalid.")
                else:
                    try:
                        sent = send_environment_access_email(organization, recipient_email=recovery_email)
                    except Exception:
                        logger.exception("Control center recovery email send failed for org=%s recipient=%s", organization.slug, recovery_email)
                        messages.error(request, "Recovery email send failed. Check mail settings and logs.")
                    else:
                        if sent:
                            _log_support_action(
                                request,
                                organization,
                                "send_recovery_access_email",
                                target_label=recovery_email,
                                metadata={"owner_email": organization.owner_email or ""},
                            )
                            messages.success(request, f"Recovery access email sent to {recovery_email}.")
                        else:
                            messages.error(request, "Recovery email was not sent because mail delivery is not configured.")
            return redirect(_control_center_redirect(base_url, q=q, status_filter=status_filter, organization_id=request.POST.get("organization_id"), tab=selected_tab))

        if action == "provision_environment":
            organization = (
                Organization.objects.using("default")
                .filter(pk=request.POST.get("organization_id"))
                .first()
            )
            environment_type = (request.POST.get("environment_type") or "").strip().lower()
            if not organization:
                messages.error(request, "Customer was not found.")
            elif environment_type not in {choice for choice, _label in Tenant.EnvironmentType.choices}:
                messages.error(request, "Selected environment type is invalid.")
            else:
                owner_password_hash = _owner_password_hash_for_organization(organization)
                setup_source_tenant = _setup_source_tenant_for_environment(organization, environment_type)
                try:
                    tenant, _company, _user = provision_tenant_environment(
                        organization,
                        environment_type,
                        owner_password="",
                        owner_password_hash=owner_password_hash,
                        setup_source_tenant=setup_source_tenant,
                    )
                except ValueError as exc:
                    messages.error(request, str(exc))
                except Exception:
                    logger.exception(
                        "Control center environment provisioning failed for org=%s env=%s",
                        organization.slug,
                        environment_type,
                    )
                    messages.error(request, "Environment provisioning failed. Check database and mail settings, then retry.")
                else:
                    _log_support_action(
                        request,
                        organization,
                        "provision_environment",
                        target_label=tenant.code,
                        metadata={"environment_type": environment_type, "hostname": tenant.hostname},
                    )
                    messages.success(request, f"{ENVIRONMENT_LABELS.get(environment_type, environment_type.title())} created successfully.")
            return redirect(_control_center_redirect(base_url, q=q, status_filter=status_filter, organization_id=request.POST.get("organization_id"), tab=selected_tab))

        if action == "delete_environment":
            tenant = (
                Tenant.objects.using("default")
                .select_related("organization")
                .filter(pk=request.POST.get("tenant_id"))
                .first()
            )
            confirmation_code = slugify((request.POST.get("delete_environment_code") or "").strip().lower())
            acknowledge_delete = request.POST.get("acknowledge_environment_delete") == "on"
            if not tenant:
                messages.error(request, "Environment was not found.")
                return redirect(_control_center_redirect(base_url, q=q, status_filter=status_filter, organization_id=request.POST.get("organization_id"), tab="environments"))
            if tenant.is_active:
                messages.error(request, "Deactivate the environment before deleting it.")
                return redirect(_control_center_redirect(base_url, q=q, status_filter=status_filter, organization_id=tenant.organization_id, tab="environments"))
            if confirmation_code != tenant.code:
                messages.error(request, f"Type the exact environment code `{tenant.code}` before deleting it.")
                return redirect(_control_center_redirect(base_url, q=q, status_filter=status_filter, organization_id=tenant.organization_id, tab="environments"))
            if not acknowledge_delete:
                messages.error(request, "You must confirm the environment delete checklist before continuing.")
                return redirect(_control_center_redirect(base_url, q=q, status_filter=status_filter, organization_id=tenant.organization_id, tab="environments"))
            if tenant.environment_type == Tenant.EnvironmentType.LIVE and tenant.organization and tenant.organization.status == Organization.Status.ACTIVE:
                messages.error(request, "Move the company out of active status before deleting its live environment.")
                return redirect(_control_center_redirect(base_url, q=q, status_filter=status_filter, organization_id=tenant.organization_id, tab="environments"))

            tenant_code = tenant.code
            tenant_label = ENVIRONMENT_LABELS.get(tenant.environment_type, tenant.get_environment_type_display())
            organization = tenant.organization
            try:
                delete_tenant_environment(tenant)
            except Exception:
                logger.exception("Control center environment deletion failed for tenant=%s", tenant_code)
                messages.error(request, "Environment deletion failed. Check database teardown access and try again.")
            else:
                _log_support_action(
                    request,
                    organization,
                    "delete_environment",
                    target_label=tenant_code,
                    metadata={"environment_type": tenant.environment_type},
                )
                messages.success(request, f"{tenant_label} `{tenant_code}` was deleted.")
            return redirect(_control_center_redirect(base_url, q=q, status_filter=status_filter, organization_id=request.POST.get("organization_id"), tab="environments"))

        if action == "delete_organization":
            organization = (
                Organization.objects.using("default")
                .filter(pk=request.POST.get("organization_id"))
                .first()
            )
            confirmation_slug = slugify((request.POST.get("delete_confirmation_slug") or "").strip().lower())
            acknowledge_delete = request.POST.get("acknowledge_delete") == "on"
            if not organization:
                messages.error(request, "Customer was not found.")
                return redirect(_control_center_redirect(base_url, q=q, status_filter=status_filter, tab="danger"))
            if confirmation_slug != organization.slug:
                messages.error(request, "Type the exact company code before deleting this customer.")
                return redirect(_control_center_redirect(base_url, q=q, status_filter=status_filter, organization_id=organization.id, tab="danger"))
            if not acknowledge_delete:
                messages.error(request, "You must confirm the delete checklist before deleting this customer.")
                return redirect(_control_center_redirect(base_url, q=q, status_filter=status_filter, organization_id=organization.id, tab="danger"))
            organization_name = organization.name
            organization_slug = organization.slug
            try:
                delete_organization_environments(organization)
            except ValueError as exc:
                messages.error(request, str(exc))
                return redirect(_control_center_redirect(base_url, q=q, status_filter=status_filter, organization_id=request.POST.get("organization_id"), tab="danger"))
            except Exception:
                logger.exception("Control center company deletion failed for org=%s", organization_slug)
                messages.error(request, "Company deletion failed. Check database teardown access and try again.")
                return redirect(_control_center_redirect(base_url, q=q, status_filter=status_filter, organization_id=request.POST.get("organization_id"), tab="danger"))

            logger.warning(
                "Control center deleted organization slug=%s name=%s by=%s",
                organization_slug,
                organization_name,
                getattr(request.user, "email", None) or getattr(request.user, "username", None) or "unknown",
            )
            messages.success(request, f"{organization_name} was deleted from the control center.")
            return redirect(_control_center_redirect(base_url, q=q, status_filter=status_filter, tab="overview"))

    organizations_qs = Organization.objects.using("default").all().prefetch_related(
        Prefetch("tenants", queryset=Tenant.objects.using("default").all().order_by("environment_type", "created_at"), to_attr="prefetched_tenants")
    )
    if q:
        organizations_qs = organizations_qs.filter(
            Q(name__icontains=q)
            | Q(slug__icontains=q)
            | Q(owner_email__icontains=q)
            | Q(tenants__code__icontains=q)
            | Q(tenants__hostname__icontains=q)
        ).distinct()
    if status_filter in {choice for choice, _label in Organization.Status.choices}:
        organizations_qs = organizations_qs.filter(status=status_filter)

    organizations = list(organizations_qs.order_by("-updated_at", "name"))
    organization_cards = _platform_control_center_cards(organizations)
    selected_card = None
    if selected_org_id:
        for card in organization_cards:
            if str(card["organization"].id) == str(selected_org_id):
                selected_card = card
                break
    if selected_card is None and organization_cards:
        selected_card = organization_cards[0]

    total_tenants = sum(card["tenant_count"] for card in organization_cards)
    inactive_tenants = sum(
        1 for card in organization_cards for tenant_card in card["tenant_cards"] if not tenant_card["tenant"].is_active
    )
    expiring_demo_count = sum(
        1
        for card in organization_cards
        for tenant_card in card["tenant_cards"]
        if tenant_card["tenant"].environment_type == Tenant.EnvironmentType.DEMO
        and tenant_card["lifecycle"].get("days_remaining") is not None
        and tenant_card["lifecycle"].get("days_remaining") <= 3
    )
    billing_snapshot = None
    support_log_entries = []
    latest_support_entry = None
    open_risk_summary = {"label": "No customer selected", "tone": "slate"}
    if selected_card:
        platform_settings = None
        try:
            from tenancy.models import PlatformSettings

            platform_settings = PlatformSettings.get_solo()
        except Exception:
            platform_settings = None
        currency = getattr(platform_settings, "subscription_currency", "USD") if platform_settings else "USD"
        billing_snapshot = get_organization_billing_snapshot(
            selected_card["organization"],
            selected_card["tenant_cards"],
            currency=currency,
        )
        if billing_snapshot:
            for item in selected_card["tenant_cards"]:
                item["billing_amount"] = billing_snapshot.get("tenant_amounts", {}).get(item["support_tag_value"])
        support_log_entries = list(
            SupportActionLog.objects.using("default")
            .filter(organization=selected_card["organization"])
            .order_by("-created_at", "-id")[:20]
        )
        latest_support_entry = support_log_entries[0] if support_log_entries else None
        open_risk_summary = _open_risk_summary(
            selected_card,
            billing_snapshot,
            _available_support_environment_provision_actions(selected_card["organization"]),
            latest_support_entry,
        )

    return render(
        request,
        "accounts/platform_control_center.html",
        {
            "organization_cards": organization_cards,
            "selected_card": selected_card,
            "selected_timeline": _platform_control_timeline(selected_card["organization"], selected_card["tenant_cards"]) if selected_card else [],
            "selected_billing": billing_snapshot,
            "support_log_entries": support_log_entries,
            "support_environment_actions": _available_support_environment_provision_actions(selected_card["organization"]) if selected_card else [],
            "latest_support_entry": latest_support_entry,
            "open_risk_summary": open_risk_summary,
            "search_query": q,
            "status_filter": status_filter,
            "selected_tab": selected_tab,
            "portal_login_url": portal_environment_url(),
            "summary": {
                "customer_count": len(organization_cards),
                "active_customer_count": sum(1 for card in organization_cards if card["organization"].status == Organization.Status.ACTIVE),
                "suspended_customer_count": sum(1 for card in organization_cards if card["organization"].status == Organization.Status.SUSPENDED),
                "tenant_count": total_tenants,
                "inactive_tenant_count": inactive_tenants,
                "expiring_demo_count": expiring_demo_count,
            },
        },
    )


def _available_environment_provision_actions(organization, can_manage: bool):
    if not can_manage:
        return []

    existing_types = set(
        Tenant.objects.using("default")
        .filter(organization=organization, is_active=True)
        .values_list("environment_type", flat=True)
    )
    has_demo = Tenant.EnvironmentType.DEMO in existing_types
    has_test = Tenant.EnvironmentType.TEST in existing_types
    actions = []
    if Tenant.EnvironmentType.TEST not in existing_types:
        actions.append(
            {
                "environment_type": Tenant.EnvironmentType.TEST,
                "label": "Test Server",
                "description": (
                    "Create a customer validation environment and copy machines, stages, products, and BOMs from the current demo setup."
                    if has_demo
                    else "Create a customer validation environment for onboarding, training, and sign-off before go-live."
                ),
                "button_label": "Create Test Server",
                "tone": "amber",
            }
        )
    if Tenant.EnvironmentType.LIVE not in existing_types:
        actions.append(
            {
                "environment_type": Tenant.EnvironmentType.LIVE,
                "label": "Live Server",
                "description": (
                    "Create the production environment and copy approved machines, stages, products, and BOMs from the current Test setup."
                    if has_test
                    else "Create the production environment for real orders, execution, reporting, and daily operations."
                ),
                "button_label": "Create Live Server",
                "tone": "emerald",
            }
        )
    return actions


def _setup_source_tenant_for_environment(organization, environment_type: str):
    if environment_type == Tenant.EnvironmentType.TEST:
        return (
            Tenant.objects.using("default")
            .filter(
                organization=organization,
                environment_type=Tenant.EnvironmentType.DEMO,
                is_active=True,
            )
            .first()
        )
    if environment_type == Tenant.EnvironmentType.LIVE:
        return (
            Tenant.objects.using("default")
            .filter(
                organization=organization,
                environment_type=Tenant.EnvironmentType.TEST,
                is_active=True,
            )
            .first()
        )
    return None


def environment_portal(request):
    if not request.user.is_authenticated:
        return redirect("login")

    organization = _organization_for_user(request)
    if not organization:
        return home_redirect(request, allow_portal=False)

    environment_cards = _environment_cards_for_organization(organization)
    can_manage_environments = _user_can_manage_organization(request, organization)
    provisioning_actions = _available_environment_provision_actions(organization, can_manage_environments)
    if not environment_cards:
        messages.error(request, "No environments are available for your organization yet.")
        return redirect("logout")

    if request.method == "POST":
        action = (request.POST.get("action") or "open_environment").strip().lower()
        if action == "provision_environment":
            if not can_manage_environments:
                messages.error(request, "Only the organization owner can create new environments.")
            else:
                environment_type = (request.POST.get("environment_type") or "").strip().lower()
                allowed_types = {Tenant.EnvironmentType.TEST, Tenant.EnvironmentType.LIVE}
                if environment_type not in allowed_types:
                    messages.error(request, "Selected environment type cannot be provisioned from the portal.")
                elif Tenant.objects.using("default").filter(
                    organization=organization,
                    environment_type=environment_type,
                    is_active=True,
                ).exists():
                    messages.error(request, f"{environment_type.title()} environment already exists.")
                else:
                    owner_password_hash = getattr(request.user, "password", "")
                    setup_source_tenant = _setup_source_tenant_for_environment(organization, environment_type)
                    try:
                        tenant, _company, _user = provision_tenant_environment(
                            organization,
                            environment_type,
                            owner_password="",
                            owner_password_hash=owner_password_hash,
                            setup_source_tenant=setup_source_tenant,
                        )
                    except ValueError as exc:
                        messages.error(request, str(exc))
                    except Exception:
                        logger.exception(
                            "Portal environment provisioning failed for org=%s env=%s",
                            organization.slug,
                            environment_type,
                        )
                        messages.error(request, "Environment provisioning failed. Check database and mail settings, then retry.")
                    else:
                        if environment_type == Tenant.EnvironmentType.TEST and not organization.wants_test_environment:
                            organization.wants_test_environment = True
                            organization.save(update_fields=["wants_test_environment"])
                        if environment_type == Tenant.EnvironmentType.TEST and setup_source_tenant:
                            messages.success(request, "Test Server created and seeded from the current Demo setup.")
                        elif environment_type == Tenant.EnvironmentType.LIVE and setup_source_tenant:
                            messages.success(request, "Live Server created and seeded from the current Test setup.")
                        else:
                            messages.success(request, f"{ENVIRONMENT_LABELS.get(environment_type, environment_type.title())} created successfully.")
                        request.session["tenant_code"] = tenant.code
                        request.session["reset_planner_workspace_state"] = True
                        return redirect("environment_portal")
        else:
            target_code = (request.POST.get("tenant_code") or "").strip().lower()
            target_tenant = (
                Tenant.objects.using("default")
                .filter(organization=organization, code=target_code, is_active=True)
                .first()
            )
            if not target_tenant:
                messages.error(request, "Selected environment is not available.")
            else:
                target_access = describe_tenant_access(target_tenant)
                if not target_access["is_accessible"]:
                    messages.error(request, target_access["blocked_message"])
                    return redirect("environment_portal")
                request.session["tenant_code"] = target_tenant.code
                request.session["reset_planner_workspace_state"] = True
                request.tenant = target_tenant
                request.tenant_db_alias = ensure_tenant_database_registered(target_tenant)
                return home_redirect(request, allow_portal=False)

    active_tenant_code = request.session.get("tenant_code") or getattr(getattr(request, "tenant", None), "code", "")
    return render(
        request,
        "accounts/environment_portal.html",
        {
            "organization": organization,
            "environment_cards": environment_cards,
            "active_tenant_code": active_tenant_code,
            "can_manage_environments": can_manage_environments,
            "provisioning_actions": provisioning_actions,
        },
    )


@login_required
def organization_bootstrap(request):
    if not _user_can_access_bootstrap(request):
        raise PermissionDenied("Only staff users can bootstrap organizations.")

    created_environments = []
    if request.method == "POST":
        form = OrganizationBootstrapForm(request.POST)
        if form.is_valid():
            data = form.cleaned_data
            try:
                organization, created = provision_organization_environments(
                    company_name=data["company_name"],
                    company_code=data["company_code"],
                    owner_email=data["owner_email"],
                    owner_password=data["owner_password"],
                    environment_types=data["environments"],
                    subscription_plan=data["subscription_plan"],
                    demo_password=data["demo_password"] or "DemoPass123!",
                )
            except ValueError as exc:
                messages.error(request, str(exc))
            except Exception:
                logger.exception("Organization bootstrap failed for company_code=%s", data["company_code"])
                messages.error(request, "Organization bootstrap failed. Check database and mail settings, then retry.")
            else:
                created_environments = [
                    {
                        "label": ENVIRONMENT_LABELS.get(tenant.environment_type, tenant.get_environment_type_display()),
                        "tenant_code": tenant.code,
                        "hostname": tenant.hostname,
                        "db_alias": tenant.db_alias,
                    }
                    for tenant, _company, _user in created
                ]
                messages.success(request, f"Organization {organization.slug} created successfully.")
                form = OrganizationBootstrapForm(
                    initial={
                        "subscription_plan": data["subscription_plan"],
                        "environments": data["environments"],
                        "demo_password": data["demo_password"] or "DemoPass123!",
                    }
                )
        else:
            messages.error(request, "Review the bootstrap form and try again.")
    else:
        form = OrganizationBootstrapForm()

    return render(
        request,
        "accounts/organization_bootstrap.html",
        {
            "form": form,
            "created_environments": created_environments,
        },
    )


@never_cache
def environment_access_setup(request, tenant_code: str, uidb64: str, token: str):
    tenant = (
        Tenant.objects.using("default")
        .filter(code=(tenant_code or "").strip().lower(), is_active=True)
        .first()
    )
    if not tenant:
        raise Http404("Environment not found.")

    lifecycle = describe_tenant_access(tenant)
    if not lifecycle["is_accessible"]:
        return render(
            request,
            "accounts/environment_access_setup.html",
            {
                "tenant": tenant,
                "portal_environment_url": portal_environment_url(),
                "invalid_link": True,
                "error_message": lifecycle["blocked_message"],
                "form": None,
            },
            status=410,
        )

    tenant_db_alias = ensure_tenant_database_registered(tenant)
    User = get_user_model()
    try:
        user_id = force_str(urlsafe_base64_decode(uidb64))
        user = User.objects.using(tenant_db_alias).get(pk=user_id)
    except (TypeError, ValueError, OverflowError, User.DoesNotExist):
        user = None

    is_valid_link = bool(user and tenant_access_token_generator.check_token(user, token))
    if not is_valid_link:
        return render(
            request,
            "accounts/environment_access_setup.html",
            {
                "tenant": tenant,
                "portal_environment_url": portal_environment_url(),
                "invalid_link": True,
                "error_message": "This setup link is invalid or has already been used. Request a fresh access email from the environment portal.",
                "form": None,
            },
            status=400,
        )

    if request.method == "POST":
        form = SetPasswordForm(user, request.POST)
        if form.is_valid():
            save_ctx_token = set_current_tenant_db(tenant_db_alias)
            try:
                user = form.save()
            finally:
                reset_current_tenant_db(save_ctx_token)
            request.session["tenant_code"] = tenant.code
            request.tenant = tenant
            request.tenant_db_alias = tenant_db_alias
            ctx_token = set_current_tenant_db(tenant_db_alias)
            try:
                login(request, user, backend=TENANT_AUTH_BACKEND)
            finally:
                reset_current_tenant_db(ctx_token)
            request.session["tenant_code"] = tenant.code
            request.session["reset_planner_workspace_state"] = True
            messages.success(request, f"{ENVIRONMENT_LABELS.get(tenant.environment_type, tenant.get_environment_type_display())} access is ready.")
            redirect_ctx_token = set_current_tenant_db(tenant_db_alias)
            try:
                return home_redirect(request)
            finally:
                reset_current_tenant_db(redirect_ctx_token)
    else:
        form = SetPasswordForm(user)

    return render(
        request,
        "accounts/environment_access_setup.html",
        {
            "tenant": tenant,
            "portal_environment_url": portal_environment_url(),
            "invalid_link": False,
            "error_message": "",
            "form": form,
        },
    )


@never_cache
@ensure_csrf_cookie
def user_login(request):
    get_token(request)
    requested_tab = (request.GET.get("tab") or "").strip().lower()
    login_active = requested_tab != "register"
    cookie_required = request.GET.get("cookie_required") == "1"
    initial_tenant_code = _prefilled_tenant_code(request)
    if request.method == "GET":
        request.session.set_test_cookie()

    login_form = TenantLoginForm(initial={"tenant_code": initial_tenant_code} if initial_tenant_code else None)
    register_form = CompanyRegistrationForm()

    if request.method == "POST":
        next_url = (request.POST.get("next") or request.GET.get("next") or "").strip()
        _trace_login_event("post_start", f"path={request.path} next={next_url!r}")
        cookies_present = bool(request.COOKIES.get("csrftoken") or request.COOKIES.get(settings.SESSION_COOKIE_NAME))
        if not request.session.test_cookie_worked() and not cookies_present:
            _trace_login_event("cookie_check", "test_cookie_worked=False")
            return redirect(_build_login_redirect(next_url=next_url, cookie_required="1"))
        if request.session.test_cookie_worked():
            request.session.delete_test_cookie()
            _trace_login_event("cookie_check", "test_cookie_worked=True")
        else:
            _trace_login_event("cookie_check", "test_cookie_worked=False_but_request_cookies_present")

        action = (request.POST.get("action") or "").strip().lower()

        if action == "login":
            login_form = TenantLoginForm(request.POST)
            if login_form.is_valid():
                tenant_code = login_form.cleaned_data["tenant_code"]
                login_identifier = login_form.cleaned_data["username"]
                password = login_form.cleaned_data["password"]
                remember_me = login_form.cleaned_data["remember_me"]

                throttle_state = get_login_throttle_state(request, tenant_code, login_identifier)
                if throttle_state.is_blocked:
                    _trace_login_event("throttle_block", f"tenant={tenant_code} user={login_identifier}")
                    retry_minutes = max(1, (throttle_state.retry_after_seconds + 59) // 60)
                    messages.error(
                        request,
                        f"Too many failed login attempts. Try again in {retry_minutes} minute(s).",
                    )
                else:
                    tenant = _resolve_login_tenant(tenant_code, login_identifier)
                    if not tenant:
                        _trace_login_event("tenant_invalid", f"tenant={tenant_code} user={login_identifier}")
                        register_login_failure(request, tenant_code, login_identifier)
                        messages.error(request, GENERIC_LOGIN_ERROR)
                    else:
                        tenant_db_alias = None
                        user = None
                        db_failure_message_sent = False
                        try:
                            close_old_connections()
                            tenant_db_alias = ensure_tenant_database_registered(tenant)
                            user = _authenticate_tenant_user_with_retry(
                                login_identifier=login_identifier,
                                password=password,
                                tenant_db_alias=tenant_db_alias,
                            )
                        except DatabaseError as exc:
                            _trace_login_db_error("initial_auth", tenant.code, exc)
                            logger.exception(
                                "Tenant login DB error for tenant=%s. Running one-time schema repair and retry.",
                                tenant.code,
                            )
                            try:
                                close_old_connections()
                                if tenant_db_alias and tenant_db_alias in connections:
                                    connections[tenant_db_alias].close()
                                tenant_db_alias = ensure_tenant_schema(tenant)
                                user = _authenticate_tenant_user_with_retry(
                                    login_identifier=login_identifier,
                                    password=password,
                                    tenant_db_alias=tenant_db_alias,
                                )
                            except DatabaseError as retry_exc:
                                _trace_login_db_error("post_repair_auth", tenant.code, retry_exc)
                                logger.exception("Tenant login still failing after schema repair for tenant=%s", tenant.code)
                                register_login_failure(request, tenant_code, login_identifier)
                                db_failure_message_sent = True
                                if settings.DEBUG:
                                    messages.error(
                                        request,
                                        f"Login is temporarily unavailable for this company "
                                        f"({retry_exc.__class__.__name__}: {retry_exc}).",
                                    )
                                else:
                                    messages.error(
                                        request,
                                        "Login is temporarily unavailable for this company. Please contact support.",
                                    )

                        if user is not None:
                            _trace_login_event("auth_success", f"tenant={tenant.code} user={user.username}")
                            request.session["tenant_code"] = tenant.code
                            request.tenant = tenant
                            request.tenant_db_alias = tenant_db_alias
                            ctx_token = set_current_tenant_db(tenant_db_alias)
                            try:
                                login(request, user, backend=TENANT_AUTH_BACKEND)
                            finally:
                                reset_current_tenant_db(ctx_token)
                            # Re-assert tenant in case session key rotation drops custom keys.
                            request.session["tenant_code"] = tenant.code
                            request.session.set_expiry(settings.SESSION_COOKIE_AGE if remember_me else 0)
                            clear_login_failures(request, tenant_code, login_identifier)
                            if next_url and next_url.startswith("/admin") and not user.is_staff:
                                next_url = ""
                            if next_url and not _is_next_url_allowed_for_role(user, next_url):
                                _trace_login_event(
                                    "next_url_blocked",
                                    f"role={(user.profile.role.name if hasattr(user, 'profile') and user.profile and user.profile.role else 'unknown')} next={next_url}",
                                )
                                next_url = ""
                            if next_url and url_has_allowed_host_and_scheme(
                                next_url,
                                allowed_hosts={request.get_host()},
                                require_https=request.is_secure(),
                            ):
                                return redirect(next_url)
                            redirect_ctx_token = set_current_tenant_db(tenant_db_alias)
                            try:
                                return home_redirect(request)
                            finally:
                                reset_current_tenant_db(redirect_ctx_token)

                        if not db_failure_message_sent:
                            _trace_login_event("auth_failed", f"tenant={tenant.code} login_identifier={login_identifier}")
                            register_login_failure(request, tenant_code, login_identifier)
                            messages.error(request, GENERIC_LOGIN_ERROR)
            else:
                messages.error(request, "Please provide a valid company code, username/email, and password.")

        elif action == "register":
            login_active = False
            register_form = CompanyRegistrationForm(request.POST)
            if register_form.is_valid():
                data = register_form.cleaned_data
                try:
                    tenant, company, user = provision_demo_signup(
                        company_name=data["company_name"],
                        company_code=data["company_code"],
                        owner_email=data["owner_email"],
                        owner_password=data["owner_password"],
                        seed_demo_package=False,
                    )
                except ValueError as exc:
                    messages.error(request, str(exc))
                except Exception as exc:
                    logger.exception("Registration failed during tenant provisioning.")
                    error_text = str(exc).strip()
                    postgres_hint = (
                        "Company setup failed while preparing its database. "
                        "Check PostgreSQL connection, control database migrations, and tenant DB template settings."
                    )
                    if settings.DEBUG:
                        messages.error(request, f"Registration failed: {error_text or postgres_hint}")
                    else:
                        messages.error(request, postgres_hint)
                else:
                    request.session["tenant_code"] = tenant.code
                    request.tenant = tenant
                    request.tenant_db_alias = tenant.db_alias
                    ctx_token = set_current_tenant_db(tenant.db_alias)
                    try:
                        login(request, user, backend=TENANT_AUTH_BACKEND)
                    finally:
                        reset_current_tenant_db(ctx_token)
                    # Session key rotation during login can drop custom keys.
                    # Re-assert tenant context for subsequent requests (onboarding/uploads).
                    request.session["tenant_code"] = tenant.code
                    request.session["reset_planner_workspace_state"] = True
                    # Flag to show company setup before dashboard on first login
                    request.session["first_time_company_setup"] = True
                    messages.success(
                        request,
                        f"Welcome to Nezam! Your workspace for {company.name} is ready. Complete the setup wizard before opening planner. Sign-in code: {tenant.code}",
                    )
                    redirect_ctx_token = set_current_tenant_db(tenant.db_alias)
                    try:
                        return home_redirect(request)
                    finally:
                        reset_current_tenant_db(redirect_ctx_token)
            else:
                messages.error(request, "Please review your registration details and try again.")

    return render(
        request,
        "accounts/login.html",
        {
            "register_form": register_form,
            "login_form": login_form,
            "login_active": login_active,
            "cookie_required": cookie_required,
        },
    )


def user_logout(request):
    logout(request)
    return redirect("/")  # Redirect to landing page


@login_required
def home_redirect(request, allow_portal: bool = True):
    """Redirect user to the correct page based on their role."""
    if allow_portal:
        current_tenant = getattr(request, "tenant", None)
        if current_tenant and not tenant_allows_workspace_access(current_tenant):
            return redirect("environment_portal")
        organization = _organization_for_user(request)
        if organization:
            active_tenants = Tenant.objects.using("default").filter(organization=organization, is_active=True)
            if active_tenants.count() > 1:
                return redirect("environment_portal")
            only_tenant = active_tenants.first()
            if only_tenant and not tenant_allows_workspace_access(only_tenant):
                return redirect("environment_portal")

    role, company = _resolve_profile_and_company(request)
    if not role:
        return redirect('onboarding_data')

    if not company:
        if role in {'admin', 'planner', 'supervisor'}:
            return redirect('dashboard')
        return redirect('login')

    # Keep setup gating active until onboarding is actually completed.
    if request.session.get('first_time_company_setup') and role in {'admin', 'planner'}:
        return redirect('onboarding_data')

    if role == 'admin':
        return redirect('dashboard')
    elif role == 'planner':
        return redirect('dashboard')
    elif role == 'supervisor':
        return redirect('supervisor_dashboard')
    elif role == 'worker':
        return redirect('record_output')
    elif role == 'maintenance':
        return redirect('maintenance_dashboard') 
    elif role == 'quality':
        return redirect('quality_check')
    elif role == 'store':
        return redirect('store_dashboard')
    else:
        return redirect('login')
