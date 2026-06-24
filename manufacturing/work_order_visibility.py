from datetime import datetime, timedelta

from django.utils import timezone

from accounts.constants import RoleType
from manufacturing.access_control import resolve_user_role
from manufacturing.models import ShiftAssignment, SystemSettings, WorkOrder
from manufacturing.shift_utils import factory_shift_configuration


SHIFT_TYPE_BY_CONFIG_KEY = {
    "morning": "day",
    "afternoon": "middle",
    "night": "night",
}

PROFILE_SHIFT_BY_CONFIG_KEY = {
    "morning": "morning",
    "afternoon": "evening",
    "night": "night",
}


def _role_value(value):
    return str(value.value if hasattr(value, "value") else value or "").strip().lower()


def _parse_shift_time(raw_value, fallback):
    value = str(raw_value or fallback).strip() or fallback
    return datetime.strptime(value, "%H:%M").time()


def get_current_shift_window_for_company(company, now=None, shift_config=None):
    """Return the active factory shift window and assignment date for a company."""
    current = timezone.localtime(now or timezone.now())
    settings = None
    if shift_config is not None:
        settings = type("ShiftSettings", (), {
            "shift_configuration": shift_config,
            "shift_mode": "3",
        })()
    elif company:
        try:
            settings = SystemSettings.objects.filter(company=company).first()
        except Exception:
            settings = None
    config = factory_shift_configuration(settings)

    defaults = {
        "morning": ("06:00", "14:00"),
        "afternoon": ("14:00", "22:00"),
        "night": ("22:00", "06:00"),
    }

    for config_key in ("morning", "afternoon", "night"):
        entry = config.get(config_key, {})
        if not entry.get("enabled", True):
            continue

        default_start, default_end = defaults[config_key]
        start_time = _parse_shift_time(entry.get("start"), default_start)
        end_time = _parse_shift_time(entry.get("end"), default_end)
        start_at = current.replace(
            hour=start_time.hour,
            minute=start_time.minute,
            second=0,
            microsecond=0,
        )
        end_at = current.replace(
            hour=end_time.hour,
            minute=end_time.minute,
            second=0,
            microsecond=0,
        )
        if start_time < end_time:
            if current.time() < start_time:
                start_at -= timedelta(days=1)
                end_at -= timedelta(days=1)
        else:
            if current.time() < end_time:
                start_at -= timedelta(days=1)
            else:
                end_at += timedelta(days=1)

        if start_at <= current < end_at:
            return {
                "is_active": True,
                "shift_type": SHIFT_TYPE_BY_CONFIG_KEY[config_key],
                "config_key": config_key,
                "start": start_at,
                "end": end_at,
                "assignment_date": start_at.date(),
            }

    return {"is_active": False}


def get_active_shift_assignments_for_user(user, company, now=None, shift_config=None):
    window = get_current_shift_window_for_company(company, now=now, shift_config=shift_config)
    if not window.get("is_active"):
        return ShiftAssignment.objects.none(), window

    assignments = ShiftAssignment.objects.filter(
        worker_id=getattr(user, "id", None),
        machine__company=company,
        shift_type=window["shift_type"],
        date=window["assignment_date"],
    ).select_related("machine")
    return assignments, window


def _work_order_overlaps_shift(work_order, shift_window):
    start_at = getattr(work_order, "scheduled_start_date", None) or getattr(work_order, "start_date", None)
    if not start_at:
        return False
    start_at = timezone.localtime(start_at)
    end_at = getattr(work_order, "end_date", None)
    end_at = timezone.localtime(end_at) if end_at else None
    shift_start = shift_window["start"]
    shift_end = shift_window["end"]
    status = str(getattr(work_order, "status", "") or "").strip().lower()

    if status == "in_progress":
        return start_at < shift_end and (end_at is None or end_at > shift_start)
    if status == "pending":
        return shift_start <= start_at < shift_end
    return start_at < shift_end and (end_at or start_at) >= shift_start


def _work_order_matches_machine_ids(work_order, machine_ids):
    if getattr(work_order, "machine_id", None) in machine_ids:
        return True
    stage = getattr(work_order, "stage", None)
    current_stage = getattr(work_order, "current_stage", None)
    return (
        getattr(stage, "machine_id", None) in machine_ids
        or getattr(current_stage, "machine_id", None) in machine_ids
    )


def _normalize_shift_value(value):
    normalized = str(value or "").strip().lower()
    aliases = {
        "day": "morning",
        "middle": "evening",
        "afternoon": "evening",
    }
    return aliases.get(normalized, normalized)


def _active_profile_shift(user, effective_date):
    profile = getattr(user, "profile", None)
    if not profile:
        return ""

    planned_shift = _normalize_shift_value(getattr(profile, "planned_shift", None))
    planned_start = getattr(profile, "planned_shift_start_date", None)
    if planned_shift and planned_start and planned_start <= effective_date:
        return planned_shift
    return _normalize_shift_value(getattr(profile, "shift", None))


def _profile_shift_matches_window(user, shift_window):
    if not shift_window.get("is_active"):
        return False
    expected = PROFILE_SHIFT_BY_CONFIG_KEY.get(shift_window.get("config_key"), "")
    if not expected:
        return False
    effective_date = shift_window.get("assignment_date") or timezone.localdate()
    return _active_profile_shift(user, effective_date) == expected


def _normalized_departments_for_user(user):
    profile = getattr(user, "profile", None)
    raw = getattr(profile, "department", None) if profile else None
    return {
        " ".join(part.strip().lower().split())
        for part in str(raw or "").replace(";", "\n").splitlines()
        if part.strip()
    }


def _work_order_department_keys(work_order):
    stage = getattr(work_order, "current_stage", None) or getattr(work_order, "stage", None)
    machine = getattr(work_order, "machine", None)
    raw_values = (
        getattr(stage, "category", None),
        getattr(stage, "name", None),
        getattr(machine, "category", None),
        getattr(machine, "type", None),
    )
    return {
        " ".join(str(value or "").strip().lower().split())
        for value in raw_values
        if str(value or "").strip()
    }


def _is_company_member(user, company):
    if getattr(user, "is_superuser", False):
        return True
    profile = getattr(user, "profile", None)
    return bool(profile and getattr(profile, "company_id", None) == getattr(company, "id", None))


def can_user_see_work_order(user, work_order, now=None, shift_config=None):
    if not user or not getattr(user, "is_authenticated", False) or not work_order:
        return False

    company = getattr(work_order, "company", None)
    if not company or not _is_company_member(user, company):
        return False

    role = resolve_user_role(user)
    privileged_roles = {
        _role_value(RoleType.ADMIN),
        _role_value(RoleType.PLANNER),
        "owner",
    }
    if getattr(user, "is_superuser", False) or role in privileged_roles:
        return True

    if role not in {_role_value(RoleType.SUPERVISOR), _role_value(RoleType.WORKER)}:
        return True

    assignments, shift_window = get_active_shift_assignments_for_user(
        user,
        company,
        now=now,
        shift_config=shift_config,
    )
    machine_ids = set(assignments.values_list("machine_id", flat=True))
    profile_shift_matches = _profile_shift_matches_window(user, shift_window)
    has_active_shift = bool(machine_ids) or profile_shift_matches
    if not has_active_shift or not _work_order_overlaps_shift(work_order, shift_window):
        return False

    if role == _role_value(RoleType.WORKER):
        if getattr(work_order, "assigned_worker_id", None) != getattr(user, "id", None):
            return False
        return profile_shift_matches or _work_order_matches_machine_ids(work_order, machine_ids)

    if _work_order_matches_machine_ids(work_order, machine_ids):
        return True

    viewer_departments = _normalized_departments_for_user(user)
    if viewer_departments:
        return bool(has_active_shift and viewer_departments & _work_order_department_keys(work_order))
    return profile_shift_matches


def get_visible_work_orders_for_user(user, queryset=None, now=None, shift_config=None):
    queryset = queryset if queryset is not None else WorkOrder.objects.all()
    role = resolve_user_role(user)
    privileged_roles = {
        _role_value(RoleType.ADMIN),
        _role_value(RoleType.PLANNER),
        "owner",
    }
    if getattr(user, "is_superuser", False) or role in privileged_roles:
        return queryset

    if role not in {_role_value(RoleType.SUPERVISOR), _role_value(RoleType.WORKER)}:
        return queryset

    rows = list(queryset.select_related("company", "machine", "stage__machine", "current_stage__machine"))
    visible_ids = [
        wo.id for wo in rows
        if can_user_see_work_order(user, wo, now=now, shift_config=shift_config)
    ]
    return queryset.filter(id__in=visible_ids)
