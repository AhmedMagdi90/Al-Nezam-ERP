from __future__ import annotations

import ast
import struct
from pathlib import Path
from django.utils.translation import trans_real


DEFAULT_PO_HEADER = (
    "Project-Id-Version: Kemet ERP 1.1\n"
    "Report-Msgid-Bugs-To: \n"
    "POT-Creation-Date: 2026-01-01 13:30+0300\n"
    "PO-Revision-Date: 2026-01-01 13:35+0300\n"
    "Last-Translator: Antigen <ai@kemet.com>\n"
    "Language-Team: Arabic\n"
    "Language: ar\n"
    "MIME-Version: 1.0\n"
    "Content-Type: text/plain; charset=UTF-8\n"
    "Content-Transfer-Encoding: 8bit\n"
    "Plural-Forms: nplurals=6; plural=n==0 ? 0 : n==1 ? 1 : n==2 ? 2 : n%100>=3 && n%100<=10 ? 3 : n%100>=11 && n%100<=99 ? 4 : 5;\n"
)


def get_catalog_paths(base_dir: Path, language: str = "ar") -> dict[str, Path]:
    handoff_name = "arabic_translation_handoff.po" if language == "ar" else f"{language}_translation_handoff.po"
    return {
        "po": base_dir / "locale" / language / "LC_MESSAGES" / "django.po",
        "mo": base_dir / "locale" / language / "LC_MESSAGES" / "django.mo",
        "handoff": base_dir / "translations" / handoff_name,
    }


def parse_po(po_path: Path) -> dict[str, str]:
    entries: dict[str, str] = {}
    if not po_path.exists():
        entries[""] = DEFAULT_PO_HEADER
        return entries

    section: str | None = None
    fuzzy = False
    msgid = ""
    msgstr = ""

    def commit() -> None:
        nonlocal msgid, msgstr, fuzzy
        if section is not None and not fuzzy:
            entries[msgid] = msgstr
        msgid = ""
        msgstr = ""
        fuzzy = False

    for raw_line in po_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line.startswith("#,") and "fuzzy" in line:
            fuzzy = True
            continue
        if line.startswith("#"):
            continue
        if line.startswith("msgid "):
            commit()
            section = "msgid"
            msgid = ast.literal_eval(line[5:].strip())
            continue
        if line.startswith("msgstr "):
            section = "msgstr"
            msgstr = ast.literal_eval(line[6:].strip())
            continue
        if line.startswith('"'):
            text = ast.literal_eval(line)
            if section == "msgid":
                msgid += text
            elif section == "msgstr":
                msgstr += text
            continue
        if not line:
            commit()
            section = None

    commit()
    entries.setdefault("", DEFAULT_PO_HEADER)
    return entries


def list_entries(po_path: Path) -> list[dict[str, str]]:
    entries = parse_po(po_path)
    return [
        {"msgid": msgid, "msgstr": msgstr}
        for msgid, msgstr in sorted(entries.items(), key=lambda item: item[0].lower())
        if msgid
    ]


def _escape_po(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\t", "\\t")
        .replace("\r", "\\r")
        .replace("\n", "\\n")
    )


def _format_po_field(name: str, value: str) -> list[str]:
    parts = value.splitlines(keepends=True)
    if not parts:
        return [f'{name} ""']
    if len(parts) == 1 and not parts[0].endswith("\n"):
        return [f'{name} "{_escape_po(parts[0])}"']
    return [f'{name} ""', *[f'"{_escape_po(part)}"' for part in parts]]


def write_po(entries: dict[str, str], po_path: Path) -> None:
    po_path.parent.mkdir(parents=True, exist_ok=True)
    normalized = dict(entries)
    normalized.setdefault("", DEFAULT_PO_HEADER)

    lines: list[str] = []
    header = normalized.pop("", DEFAULT_PO_HEADER)
    lines.extend(_format_po_field("msgid", ""))
    lines.extend(_format_po_field("msgstr", header))
    lines.append("")

    for msgid in sorted(normalized.keys(), key=str.lower):
        lines.extend(_format_po_field("msgid", msgid))
        lines.extend(_format_po_field("msgstr", normalized[msgid]))
        lines.append("")

    po_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def compile_mo(entries: dict[str, str], mo_path: Path) -> None:
    mo_path.parent.mkdir(parents=True, exist_ok=True)
    keys = sorted(entries.keys())
    ids = [key.encode("utf-8") for key in keys]
    values = [str(entries[key]).encode("utf-8") for key in keys]

    count = len(keys)
    keystart = 7 * 4
    valuestart = keystart + count * 8
    ids_offset = valuestart + count * 8

    key_offsets: list[tuple[int, int]] = []
    value_offsets: list[tuple[int, int]] = []
    offset = ids_offset

    for item in ids:
        key_offsets.append((len(item), offset))
        offset += len(item) + 1
    for item in values:
        value_offsets.append((len(item), offset))
        offset += len(item) + 1

    output = bytearray()
    output.extend(struct.pack("Iiiiiii", 0x950412DE, 0, count, keystart, valuestart, 0, 0))
    for length, item_offset in key_offsets:
        output.extend(struct.pack("ii", length, item_offset))
    for length, item_offset in value_offsets:
        output.extend(struct.pack("ii", length, item_offset))
    for item in ids:
        output.extend(item + b"\0")
    for item in values:
        output.extend(item + b"\0")

    mo_path.write_bytes(output)


def invalidate_translation_cache(language: str | None = None) -> None:
    cache = getattr(trans_real, "_translations", None)
    if not isinstance(cache, dict):
        return

    if language:
        for key in list(cache.keys()):
            if key == language:
                cache.pop(key, None)
                continue
            if isinstance(key, tuple) and key and key[0] == language:
                cache.pop(key, None)
        return

    cache.clear()


def upsert_translation(base_dir: Path, language: str, msgid: str, msgstr: str) -> dict[str, Path]:
    msgid = str(msgid or "").strip()
    if not msgid:
        raise ValueError("msgid is required")

    paths = get_catalog_paths(base_dir, language)
    entries = parse_po(paths["po"])
    entries[msgid] = str(msgstr or "").strip()
    write_po(entries, paths["po"])
    compile_mo(entries, paths["mo"])

    handoff_path = paths["handoff"]
    handoff_entries = parse_po(handoff_path) if handoff_path.exists() else dict(entries)
    handoff_entries[msgid] = entries[msgid]
    write_po(handoff_entries, handoff_path)
    invalidate_translation_cache(language)
    return paths


def remove_translation(base_dir: Path, language: str, msgid: str) -> dict[str, Path]:
    msgid = str(msgid or "").strip()
    if not msgid:
        raise ValueError("msgid is required")

    paths = get_catalog_paths(base_dir, language)
    entries = parse_po(paths["po"])
    entries.pop(msgid, None)
    write_po(entries, paths["po"])
    compile_mo(entries, paths["mo"])

    handoff_path = paths["handoff"]
    if handoff_path.exists():
        handoff_entries = parse_po(handoff_path)
        handoff_entries.pop(msgid, None)
        write_po(handoff_entries, handoff_path)
    invalidate_translation_cache(language)
    return paths
