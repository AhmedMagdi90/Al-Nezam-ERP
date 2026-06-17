from __future__ import annotations

from collections.abc import Iterable
import logging

from django.db import DatabaseError
from django.db.models import Q

from accounts.constants import RoleType
from accounts.models import Profile

logger = logging.getLogger(__name__)


def _role_value(value) -> str:
    role_value = value.value if hasattr(value, "value") else value
    return str(role_value or "").strip().lower()


ROLE_GROUPS = {
    "planner_admin": frozenset({_role_value(RoleType.PLANNER), _role_value(RoleType.ADMIN)}),
    "ops_core": frozenset(
        {_role_value(RoleType.PLANNER), _role_value(RoleType.SUPERVISOR), _role_value(RoleType.ADMIN)}
    ),
    "supervisor_admin": frozenset({_role_value(RoleType.SUPERVISOR), _role_value(RoleType.ADMIN)}),
    "worker_floor": frozenset(
        {_role_value(RoleType.WORKER), _role_value(RoleType.SUPERVISOR), _role_value(RoleType.ADMIN)}
    ),
    "quality_read": frozenset(
        {
            _role_value(RoleType.QUALITY),
            _role_value(RoleType.PLANNER),
            _role_value(RoleType.SUPERVISOR),
            _role_value(RoleType.WORKER),
            _role_value(RoleType.ADMIN),
        }
    ),
    "quality_write": frozenset(
        {
            _role_value(RoleType.QUALITY),
            _role_value(RoleType.SUPERVISOR),
            _role_value(RoleType.WORKER),
            _role_value(RoleType.ADMIN),
        }
    ),
    "maintenance_read": frozenset(
        {
            _role_value(RoleType.MAINTENANCE),
            _role_value(RoleType.PLANNER),
            _role_value(RoleType.SUPERVISOR),
            _role_value(RoleType.WORKER),
            _role_value(RoleType.ADMIN),
        }
    ),
    "maintenance_write": frozenset(
        {
            _role_value(RoleType.MAINTENANCE),
            _role_value(RoleType.SUPERVISOR),
            _role_value(RoleType.WORKER),
            _role_value(RoleType.ADMIN),
        }
    ),
    "factory_setup_view": frozenset(
        {_role_value(RoleType.PLANNER), _role_value(RoleType.SUPERVISOR), _role_value(RoleType.ADMIN)}
    ),
    "store": frozenset({_role_value(RoleType.STORE), _role_value(RoleType.ADMIN)}),
}


# Standardized capability-to-role matrix used by both UI + API checks.
CAPABILITY_ROLE_MATRIX = {
    # UI
    "ui.planner.dashboard": ROLE_GROUPS["planner_admin"],
    "ui.planner.manage": ROLE_GROUPS["planner_admin"],
    "ui.supervisor.dashboard": ROLE_GROUPS["ops_core"],
    "ui.factory_setup.view": ROLE_GROUPS["factory_setup_view"],
    "ui.factory_setup.manage": ROLE_GROUPS["planner_admin"],
    "ui.bom.manage": ROLE_GROUPS["planner_admin"],
    "ui.bulk_import.manage": ROLE_GROUPS["ops_core"],
    "ui.reports.view": ROLE_GROUPS["planner_admin"],
    "ui.shop_floor.view": ROLE_GROUPS["worker_floor"],
    "ui.shop_floor.supervise": ROLE_GROUPS["ops_core"],
    "ui.settings.view": ROLE_GROUPS["factory_setup_view"],
    "ui.quality.dashboard": ROLE_GROUPS["quality_read"],
    "ui.quality.manage": ROLE_GROUPS["quality_write"],
    "ui.maintenance.dashboard": ROLE_GROUPS["maintenance_read"],
    "ui.maintenance.manage": ROLE_GROUPS["maintenance_write"],
    "ui.store.dashboard": ROLE_GROUPS["store"],
    "ui.store.manage": ROLE_GROUPS["store"],
    # API
    "api.work_order.update_status": ROLE_GROUPS["ops_core"],
    "api.work_order.assign_stage": ROLE_GROUPS["planner_admin"],
    "api.work_order.assign_worker": ROLE_GROUPS["ops_core"],
    "api.work_order.close": ROLE_GROUPS["planner_admin"],
    "api.bom.manage": ROLE_GROUPS["planner_admin"],
    "api.production_log.approve": ROLE_GROUPS["supervisor_admin"],
}


def resolve_user_role(user) -> str | None:
    if not getattr(user, "is_authenticated", False):
        return None
    if getattr(user, "is_superuser", False):
        return _role_value(RoleType.ADMIN)

    db_alias = getattr(getattr(user, "_state", None), "db", None) or "default"
    cache_key = "_mf_role_cache"
    cache_value = getattr(user, cache_key, None)
    if cache_value and isinstance(cache_value, tuple) and len(cache_value) == 2:
        cached_db, cached_role = cache_value
        if cached_db == db_alias:
            return cached_role

    try:
        role_name = (
            Profile.objects.using(db_alias)
            .filter(user_id=user.id, role_id__isnull=False)
            .values_list("role__name", flat=True)
            .first()
        )
    except DatabaseError:
        logger.exception(
            "Role resolution failed for user_id=%s on db=%s",
            getattr(user, "id", None),
            db_alias,
        )
        return None

    normalized = _role_value(role_name) if role_name else None
    setattr(user, cache_key, (db_alias, normalized))
    return normalized


def is_worker_mode_enabled(user) -> bool:
    if not getattr(user, "is_authenticated", False):
        return False
    if resolve_user_role(user) != _role_value(RoleType.SUPERVISOR):
        return False

    db_alias = getattr(getattr(user, "_state", None), "db", None) or "default"
    cache_key = "_mf_worker_mode_enabled_cache"
    cache_value = getattr(user, cache_key, None)
    if cache_value and isinstance(cache_value, tuple) and len(cache_value) == 2:
        cached_db, cached_enabled = cache_value
        if cached_db == db_alias:
            return bool(cached_enabled)

    try:
        enabled = bool(
            Profile.objects.using(db_alias)
            .filter(user_id=user.id)
            .values_list("worker_mode_enabled", flat=True)
            .first()
        )
    except DatabaseError:
        logger.exception(
            "Worker-mode resolution failed for user_id=%s on db=%s",
            getattr(user, "id", None),
            db_alias,
        )
        return False

    setattr(user, cache_key, (db_alias, enabled))
    return enabled


def acts_as_worker(user) -> bool:
    role_name = resolve_user_role(user)
    return bool(
        role_name == _role_value(RoleType.WORKER)
        or (role_name == _role_value(RoleType.SUPERVISOR) and is_worker_mode_enabled(user))
    )


def worker_eligible_user_q(prefix: str = "") -> Q:
    role_key = f"{prefix}profile__role__name"
    worker_mode_key = f"{prefix}profile__worker_mode_enabled"
    return (
        Q(**{role_key: _role_value(RoleType.WORKER)})
        | Q(**{role_key: _role_value(RoleType.SUPERVISOR), worker_mode_key: True})
    )


def _required_roles(requirement) -> set[str]:
    if requirement is None:
        return set()

    if isinstance(requirement, str):
        req_key = requirement.strip().lower()
        capability_roles = CAPABILITY_ROLE_MATRIX.get(req_key)
        if capability_roles is not None:
            return set(capability_roles)
        return {req_key}

    if isinstance(requirement, Iterable):
        return {_role_value(role) for role in requirement if _role_value(role)}

    return set()


def user_has_access(user, requirement) -> bool:
    if not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_superuser", False):
        return True

    required_roles = _required_roles(requirement)
    if not required_roles:
        return False

    role_name = resolve_user_role(user)
    return bool(role_name and role_name in required_roles)


def user_has_capability(user, capability: str) -> bool:
    return user_has_access(user, capability)
