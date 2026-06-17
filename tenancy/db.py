from copy import deepcopy
import logging
import os
from pathlib import Path
import threading

from django.db import connections
from django.conf import settings
from django.core.management import call_command
from django.db.migrations.executor import MigrationExecutor

from .context import reset_current_tenant_db, set_current_tenant_db

try:
    import dj_database_url
except ImportError:  # pragma: no cover - optional import for non-postgres setups
    dj_database_url = None


logger = logging.getLogger(__name__)
_TENANT_SCHEMA_LOCK = threading.Lock()
_TENANT_SCHEMA_READY_ALIASES: set[str] = set()


def _is_database_url(value: str) -> bool:
    value = (value or "").strip().lower()
    return value.startswith("postgres://") or value.startswith("postgresql://")


def _tenant_sqlite_config_from_default(db_path: Path) -> dict:
    """
    Build a tenant DB config by cloning default DB settings so required
    backend keys (e.g. TIME_ZONE) are always present.
    """
    cfg = deepcopy(settings.DATABASES.get("default", {}))
    cfg["ENGINE"] = "django.db.backends.sqlite3"
    cfg["NAME"] = str(db_path)
    options = deepcopy(cfg.get("OPTIONS") or {})
    options.setdefault("timeout", int(getattr(settings, "SQLITE_TIMEOUT_SECONDS", 30)))
    cfg["OPTIONS"] = options
    return _normalize_runtime_db_config(cfg)


def _configure_sqlite_runtime_pragmas(db_alias: str):
    timeout_ms = max(1000, int(getattr(settings, "SQLITE_TIMEOUT_SECONDS", 30)) * 1000)
    enable_wal = bool(getattr(settings, "SQLITE_ENABLE_WAL", True))
    try:
        with connections[db_alias].cursor() as cursor:
            cursor.execute(f"PRAGMA busy_timeout={timeout_ms};")
            if enable_wal:
                cursor.execute("PRAGMA journal_mode=WAL;")
    except Exception:
        # Never block tenant resolution on PRAGMA tuning.
        return


def _tenant_db_config_from_entry(db_entry: str) -> dict:
    db_entry = (db_entry or "").strip()
    if _is_database_url(db_entry):
        if dj_database_url is None:
            raise RuntimeError("dj-database-url is required for PostgreSQL tenant databases.")
        cfg = dj_database_url.parse(
            db_entry,
            conn_max_age=int(os.getenv("DB_CONN_MAX_AGE", "600")),
            ssl_require=os.getenv("DB_SSL_REQUIRE", "1") == "1",
        )
        return _normalize_runtime_db_config(cfg)

    db_path = Path(db_entry)
    if not db_path.is_absolute():
        db_path = Path(settings.BASE_DIR) / db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return _tenant_sqlite_config_from_default(db_path)


def _normalize_runtime_db_config(cfg: dict) -> dict:
    """
    Django expects several backend keys to exist on runtime-added aliases.
    Missing keys can crash request handling before the real database error
    reaches the user.
    """
    cfg = deepcopy(cfg)
    cfg.setdefault("ATOMIC_REQUESTS", False)
    cfg.setdefault("AUTOCOMMIT", True)
    cfg.setdefault("CONN_MAX_AGE", 0)
    cfg.setdefault("CONN_HEALTH_CHECKS", False)
    cfg.setdefault("TIME_ZONE", None)
    cfg.setdefault("OPTIONS", {})
    return cfg


def ensure_tenant_database_registered(tenant) -> str:
    """
    Add tenant DB config to Django connections at runtime if missing.
    Returns the tenant db alias.
    """
    db_alias = tenant.db_alias
    if db_alias in connections.databases:
        connections.databases[db_alias] = _normalize_runtime_db_config(connections.databases[db_alias])
        if connections.databases[db_alias].get("ENGINE") == "django.db.backends.sqlite3":
            _configure_sqlite_runtime_pragmas(db_alias)
        return db_alias

    connections.databases[db_alias] = _tenant_db_config_from_entry(tenant.db_name)
    if connections.databases[db_alias].get("ENGINE") == "django.db.backends.sqlite3":
        _configure_sqlite_runtime_pragmas(db_alias)
    return db_alias


def _tenant_has_pending_migrations(db_alias: str) -> bool:
    executor = MigrationExecutor(connections[db_alias])
    targets = executor.loader.graph.leaf_nodes()
    return bool(executor.migration_plan(targets))


def ensure_tenant_database_ready(tenant) -> str:
    """
    Register the tenant DB alias and apply any missing tenant migrations once
    per process before request code touches tenant-scoped models.
    """
    db_alias = ensure_tenant_database_registered(tenant)

    with _TENANT_SCHEMA_LOCK:
        if db_alias in _TENANT_SCHEMA_READY_ALIASES:
            return db_alias

        if _tenant_has_pending_migrations(db_alias):
            logger.warning("Applying pending migrations for tenant db alias %s", db_alias)
            ctx_token = set_current_tenant_db(db_alias)
            try:
                call_command("migrate", database=db_alias, interactive=False, verbosity=0)
            finally:
                reset_current_tenant_db(ctx_token)

        _TENANT_SCHEMA_READY_ALIASES.add(db_alias)

    return db_alias
