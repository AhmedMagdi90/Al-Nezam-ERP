import os
import tempfile

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import connections

from django.contrib.auth.models import User

from tenancy.db import ensure_tenant_database_registered
from tenancy.models import Tenant


class Command(BaseCommand):
    help = "Migrate one tenant DB from SQLite to PostgreSQL and repoint tenant.db_name to PostgreSQL URL."

    def add_arguments(self, parser):
        parser.add_argument("--tenant-code", required=True, help="Tenant code to migrate, e.g. al-nour")
        parser.add_argument(
            "--target-url",
            required=True,
            help="PostgreSQL DATABASE_URL for target tenant DB, e.g. postgresql://user:pass@host:5432/tenant_al_nour",
        )
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        try:
            import dj_database_url
        except ImportError as exc:
            raise CommandError(
                "dj-database-url is not installed in the active environment. "
                "Install dependencies with: pip install -r requirements.txt"
            ) from exc

        tenant_code = options["tenant_code"].strip().lower()
        target_url = options["target_url"].strip()
        dry_run = bool(options["dry_run"])

        tenant = Tenant.objects.using("default").filter(code=tenant_code, is_active=True).first()
        if not tenant:
            raise CommandError(f"Active tenant not found: {tenant_code}")

        source_alias = ensure_tenant_database_registered(tenant)
        source_engine = connections.databases[source_alias].get("ENGINE")
        if source_engine != "django.db.backends.sqlite3":
            raise CommandError(
                f"Tenant {tenant_code} is not using SQLite source (engine={source_engine}). "
                "This command is for SQLite -> PostgreSQL migration."
            )

        target_alias = "__tenant_pg_target__"
        target_cfg = dj_database_url.parse(
            target_url,
            conn_max_age=int(os.getenv("DB_CONN_MAX_AGE", "600")),
            ssl_require=os.getenv("DB_SSL_REQUIRE", "1") == "1",
        )
        target_cfg.setdefault("TIME_ZONE", None)
        connections.databases[target_alias] = target_cfg

        apps_to_copy = list(getattr(settings, "TENANCY_TENANT_APPS", []))
        if not apps_to_copy:
            raise CommandError("TENANCY_TENANT_APPS is empty; nothing to migrate.")

        self.stdout.write(f"Tenant: {tenant.code}")
        self.stdout.write(f"Source alias: {source_alias}")
        self.stdout.write(f"Target alias: {target_alias}")
        self.stdout.write(f"Apps: {apps_to_copy}")

        if dry_run:
            self.stdout.write(self.style.WARNING("Dry-run mode: no changes applied."))
            return

        # Ensure target schema exists.
        call_command("migrate", database=target_alias, interactive=False, verbosity=1)

        # Dump tenant data from source and load into target.
        with tempfile.NamedTemporaryFile(prefix=f"tenant_{tenant.code}_", suffix=".json", delete=False) as tmp:
            dump_path = tmp.name

        self.stdout.write(f"Dumping source tenant data to {dump_path}")
        call_command(
            "dumpdata",
            *apps_to_copy,
            database=source_alias,
            natural_foreign=False,
            natural_primary=False,
            indent=2,
            output=dump_path,
            verbosity=1,
        )

        self.stdout.write("Flushing target tenant DB before load")
        call_command("flush", database=target_alias, interactive=False, verbosity=0)
        call_command("migrate", database=target_alias, interactive=False, verbosity=0)

        self.stdout.write("Loading tenant data into PostgreSQL target")
        call_command("loaddata", dump_path, database=target_alias, verbosity=1)

        # Validation check: users should now be present in target.
        migrated_user_count = User.objects.using(target_alias).count()
        self.stdout.write(self.style.SUCCESS(f"Migrated auth users: {migrated_user_count}"))

        tenant.db_name = target_url
        tenant.save(using="default", update_fields=["db_name", "updated_at"])
        self.stdout.write(self.style.SUCCESS(f"Tenant {tenant.code} now points to PostgreSQL URL."))
