from __future__ import annotations

from pathlib import Path

from django.utils.translation import get_language, gettext

from manufacturing.models import Company, SystemSettings
from manufacturing.translation_catalog import get_catalog_paths, list_entries


TRAILING_PUNCTUATION = ".!?:;\u2026\u061f\u060c"


def _normalize_language(language: str | None) -> str:
    value = str(language or "").strip().lower()
    if not value:
        return "en"
    return value.split("-")[0]


def _clean_entry_map(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}

    cleaned: dict[str, str] = {}
    for msgid, msgstr in raw.items():
        source = str(msgid or "").strip()
        target = str(msgstr or "").strip()
        if source and target:
            cleaned[source] = target
    return cleaned


def canonicalize_translation_key(value: str | None) -> str:
    source = " ".join(str(value or "").split()).strip()
    if not source:
        return ""
    source = source.rstrip(TRAILING_PUNCTUATION).rstrip()
    return source.casefold()


def normalize_translation_overrides(raw: object) -> dict[str, dict[str, str]]:
    if not isinstance(raw, dict):
        return {}

    normalized: dict[str, dict[str, str]] = {}
    for language, entries in raw.items():
        language_key = _normalize_language(str(language or ""))
        if not language_key:
            continue
        cleaned_entries = _clean_entry_map(entries)
        if cleaned_entries:
            normalized[language_key] = cleaned_entries
    return normalized


def _system_settings_for_company(company: Company | None) -> SystemSettings | None:
    if not company:
        return None

    db_alias = getattr(getattr(company, "_state", None), "db", None) or "default"
    return (
        SystemSettings.objects.using(db_alias)
        .filter(company_id=company.pk)
        .first()
    )


def _system_settings_manager(company: Company):
    db_alias = getattr(getattr(company, "_state", None), "db", None) or "default"
    return SystemSettings.objects.using(db_alias)


def _resolve_override_entry(scoped: dict[str, str], msgid: str) -> tuple[str | None, str | None]:
    exact = scoped.get(msgid)
    if exact:
        return exact, msgid

    canonical_source = canonicalize_translation_key(msgid)
    if not canonical_source:
        return None, None

    matches = [
        existing_key
        for existing_key in scoped.keys()
        if canonicalize_translation_key(existing_key) == canonical_source
    ]
    if len(matches) == 1:
        matched_key = matches[0]
        return scoped.get(matched_key), matched_key

    return None, None


def get_company_translation_map(company: Company | None, language: str | None) -> dict[str, str]:
    settings = _system_settings_for_company(company)
    if not settings:
        return {}

    overrides = normalize_translation_overrides(settings.translation_overrides)
    return dict(overrides.get(_normalize_language(language), {}))


def list_translation_entries_for_company(
    company: Company | None,
    language: str,
    base_dir: Path | None = None,
) -> list[dict[str, str]]:
    merged: dict[str, dict[str, str]] = {}

    if base_dir:
        po_path = get_catalog_paths(base_dir, language)["po"]
        for entry in list_entries(po_path):
            merged[entry["msgid"]] = {
                "msgid": entry["msgid"],
                "msgstr": entry["msgstr"],
                "origin": "global",
            }

    for msgid, msgstr in get_company_translation_map(company, language).items():
        merged[msgid] = {
            "msgid": msgid,
            "msgstr": msgstr,
            "origin": "company",
        }

    return [merged[key] for key in sorted(merged.keys(), key=str.lower)]


def upsert_company_translation(company: Company, language: str, msgid: str, msgstr: str) -> SystemSettings:
    source = str(msgid or "").strip()
    target = str(msgstr or "").strip()
    if not source:
        raise ValueError("msgid is required")
    if not target:
        raise ValueError("msgstr is required")

    settings, _ = _system_settings_manager(company).get_or_create(company=company)
    overrides = normalize_translation_overrides(settings.translation_overrides)
    language_key = _normalize_language(language)
    scoped = dict(overrides.get(language_key, {}))
    _existing_value, matched_key = _resolve_override_entry(scoped, source)
    scoped[matched_key or source] = target
    overrides[language_key] = scoped
    settings.translation_overrides = overrides
    settings.save(update_fields=["translation_overrides"])
    return settings


def remove_company_translation(company: Company, language: str, msgid: str) -> SystemSettings:
    source = str(msgid or "").strip()
    if not source:
        raise ValueError("msgid is required")

    settings, _ = _system_settings_manager(company).get_or_create(company=company)
    overrides = normalize_translation_overrides(settings.translation_overrides)
    language_key = _normalize_language(language)
    scoped = dict(overrides.get(language_key, {}))
    _existing_value, matched_key = _resolve_override_entry(scoped, source)
    scoped.pop(matched_key or source, None)
    if scoped:
        overrides[language_key] = scoped
    else:
        overrides.pop(language_key, None)
    settings.translation_overrides = overrides
    settings.save(update_fields=["translation_overrides"])
    return settings


def resolve_runtime_translation(company: Company | None, msgid: str, language: str | None = None) -> str:
    source = str(msgid or "").strip()
    if not source:
        return ""

    language_key = _normalize_language(language or get_language())
    if language_key != "en":
        override, _matched_key = _resolve_override_entry(get_company_translation_map(company, language_key), source)
        if override:
            return override

    return gettext(source)

