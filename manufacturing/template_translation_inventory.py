from __future__ import annotations

from pathlib import Path
import re


TRANSLATION_TAG_PATTERN = re.compile(
    r"""\{%\s*(?:tenant_trans|trans)\s+(['"])(?P<msgid>.+?)\1\s*%}""",
    re.DOTALL,
)

IGNORED_NAME_PARTS = (
    "backup",
    "_old",
    "_test",
    "fragment",
    "debug",
    "_simple",
    "_clean",
    "_basic",
    "_minimal",
    "_embed",
    "_final",
    "_fixed",
)


def _is_active_template(path: Path) -> bool:
    stem = path.stem.lower()
    return not any(part in stem for part in IGNORED_NAME_PARTS)


def _humanize_template_part(value: str) -> str:
    label = str(value or "").replace("_", " ").replace("-", " ").strip()
    return " ".join(label.split()).title()


def _describe_template(relative_path: Path) -> str:
    stem = relative_path.stem
    parent_parts = list(relative_path.parent.parts)

    if parent_parts and parent_parts[0].lower() == "partials":
        return f"Shared UI / {_humanize_template_part(stem)}"

    labels = [_humanize_template_part(part) for part in parent_parts if str(part).strip()]
    labels.append(_humanize_template_part(stem))
    return " / ".join(label for label in labels if label)


def get_template_translation_inventory(base_dir: Path | None = None) -> dict[str, dict[str, list[str]]]:
    root = Path(base_dir or Path.cwd()) / "templates" / "manufacturing"
    if not root.exists():
        return {}

    raw_inventory: dict[str, dict[str, set[str]]] = {}
    for template_path in root.rglob("*.html"):
        if not _is_active_template(template_path):
            continue

        relative_path = template_path.relative_to(root)
        description = _describe_template(relative_path)
        content = template_path.read_text(encoding="utf-8")

        for match in TRANSLATION_TAG_PATTERN.finditer(content):
            msgid = " ".join(match.group("msgid").split()).strip()
            if not msgid:
                continue

            entry = raw_inventory.setdefault(
                msgid,
                {"descriptions": set(), "files": set()},
            )
            entry["descriptions"].add(description)
            entry["files"].add(relative_path.as_posix())

    inventory: dict[str, dict[str, list[str]]] = {}
    for msgid, payload in raw_inventory.items():
        inventory[msgid] = {
            "descriptions": sorted(payload["descriptions"]),
            "files": sorted(payload["files"]),
        }
    return inventory


def list_template_translation_sources(base_dir: Path | None = None) -> list[str]:
    return sorted(get_template_translation_inventory(base_dir).keys(), key=str.lower)
