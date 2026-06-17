import json
from copy import deepcopy
from datetime import datetime


DEFAULT_SHIFT_CONFIGURATION = {
    "morning": {"start": "06:00", "end": "14:00", "enabled": True},
    "afternoon": {"start": "14:00", "end": "22:00", "enabled": True},
    "night": {"start": "22:00", "end": "06:00", "enabled": True},
}

SHIFT_MODE_CHOICES = (
    ("1", "1 Shift"),
    ("2", "2 Shifts"),
    ("3", "3 Shifts"),
)

SHIFT_MODE_ENABLED_KEYS = {
    "1": {"morning"},
    "2": {"morning", "afternoon"},
    "3": {"morning", "afternoon", "night"},
}

SHIFT_KEY_ALIASES = {
    "evening": "afternoon",
}

SHIFT_LABELS = {
    "morning": "Morning",
    "afternoon": "Evening",
    "night": "Night",
}


def parse_bool(value, default=False):
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def _coerce_shift_time(value, fallback):
    candidate = str(value or fallback).strip() or fallback
    try:
        datetime.strptime(candidate, "%H:%M")
        return candidate
    except (TypeError, ValueError):
        return fallback


def normalize_shift_mode(value, default="3"):
    raw = str(value or "").strip().lower()
    if raw in {"1", "one", "one_shift", "single", "single_shift"}:
        return "1"
    if raw in {"2", "two", "two_shifts", "2_shift"}:
        return "2"
    if raw in {"3", "three", "three_shifts", "3_shift"}:
        return "3"
    return default


def enabled_shift_keys_for_mode(shift_mode):
    return set(SHIFT_MODE_ENABLED_KEYS.get(normalize_shift_mode(shift_mode), SHIFT_MODE_ENABLED_KEYS["3"]))


def normalize_shift_configuration(raw_config, *, base_config=None, default_enabled=True, shift_mode=None):
    source = raw_config if isinstance(raw_config, dict) else {}
    base = deepcopy(base_config if isinstance(base_config, dict) else DEFAULT_SHIFT_CONFIGURATION)
    normalized = {}
    enabled_keys = enabled_shift_keys_for_mode(shift_mode) if shift_mode is not None else None

    for shift_key, defaults in DEFAULT_SHIFT_CONFIGURATION.items():
        alias_key = next((legacy for legacy, canonical in SHIFT_KEY_ALIASES.items() if canonical == shift_key), None)
        base_entry = (
            base.get(shift_key)
            or (base.get(alias_key) if isinstance(base, dict) and alias_key else None)
            or defaults
        ) if isinstance(base, dict) else defaults
        raw_entry = source.get(shift_key)
        if raw_entry is None and alias_key:
            raw_entry = source.get(alias_key)
        if not isinstance(raw_entry, dict):
            raw_entry = {}

        enabled_default = default_enabled
        if enabled_keys is not None:
            enabled_default = shift_key in enabled_keys

        normalized[shift_key] = {
            "start": _coerce_shift_time(raw_entry.get("start"), base_entry.get("start", defaults["start"])),
            "end": _coerce_shift_time(raw_entry.get("end"), base_entry.get("end", defaults["end"])),
            "enabled": parse_bool(
                raw_entry.get("enabled"),
                default=(
                    enabled_default
                    if "enabled" not in raw_entry
                    else defaults.get("enabled", True)
                ),
            ),
        }
    return normalized


def coerce_shift_configuration_payload(raw_value, *, base_config=None, default_enabled=True, shift_mode=None):
    if isinstance(raw_value, str):
        raw_value = raw_value.strip()
        if not raw_value:
            raw_value = {}
        else:
            raw_value = json.loads(raw_value)
    return normalize_shift_configuration(
        raw_value,
        base_config=base_config,
        default_enabled=default_enabled,
        shift_mode=shift_mode,
    )


def factory_shift_configuration(company_settings=None):
    shift_mode = getattr(company_settings, "shift_mode", "3") if company_settings else "3"
    factory_raw = getattr(company_settings, "shift_configuration", None) if company_settings else None
    return normalize_shift_configuration(factory_raw, default_enabled=True, shift_mode=shift_mode)


def machine_shift_configuration(machine, company_settings=None):
    factory_config = factory_shift_configuration(company_settings)
    if not machine or parse_bool(getattr(machine, "use_factory_shifts", True), default=True):
        return factory_config

    return normalize_shift_configuration(
        getattr(machine, "shift_configuration", None),
        base_config=factory_config,
        default_enabled=False,
    )


def summarize_shift_configuration(config):
    if not isinstance(config, dict):
        return "Factory default"

    segments = []
    for shift_key in ("morning", "afternoon", "night"):
        entry = config.get(shift_key, {})
        if not parse_bool(entry.get("enabled"), default=False):
            continue
        label = SHIFT_LABELS.get(shift_key, shift_key.title())
        start = _coerce_shift_time(entry.get("start"), DEFAULT_SHIFT_CONFIGURATION[shift_key]["start"])
        end = _coerce_shift_time(entry.get("end"), DEFAULT_SHIFT_CONFIGURATION[shift_key]["end"])
        segments.append(f"{label} {start}-{end}")

    return ", ".join(segments) if segments else "No active shifts"
