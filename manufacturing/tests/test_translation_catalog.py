from pathlib import Path
import shutil
import uuid

from django.test import SimpleTestCase
from django.utils.translation import trans_real

from manufacturing.translation_catalog import (
    invalidate_translation_cache,
    list_entries,
    parse_po,
    remove_translation,
    upsert_translation,
)


class TranslationCatalogTests(SimpleTestCase):
    def _workspace_temp_dir(self):
        root = Path.cwd() / ".tmp_test_catalog"
        root.mkdir(parents=True, exist_ok=True)
        path = root / f"case_{uuid.uuid4().hex}"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def test_upsert_translation_updates_po_and_mo_and_handoff(self):
        base_dir = self._workspace_temp_dir()
        try:
            po_dir = base_dir / "locale" / "ar" / "LC_MESSAGES"
            po_dir.mkdir(parents=True, exist_ok=True)
            (po_dir / "django.po").write_text(
                'msgid ""\nmsgstr ""\n"Language: ar\\n"\n\nmsgid "Planning"\nmsgstr "التخطيط"\n',
                encoding="utf-8",
            )

            paths = upsert_translation(base_dir, "ar", "Create Work Order", "إنشاء أمر عمل")

            self.assertTrue(paths["po"].exists())
            self.assertTrue(paths["mo"].exists())
            self.assertTrue(paths["handoff"].exists())
            entries = parse_po(paths["po"])
            self.assertEqual(entries["Create Work Order"], "إنشاء أمر عمل")
            self.assertIn("Create Work Order", [entry["msgid"] for entry in list_entries(paths["po"])])
        finally:
            shutil.rmtree(base_dir, ignore_errors=True)

    def test_remove_translation_deletes_entry(self):
        base_dir = self._workspace_temp_dir()
        try:
            po_dir = base_dir / "locale" / "ar" / "LC_MESSAGES"
            po_dir.mkdir(parents=True, exist_ok=True)
            (po_dir / "django.po").write_text(
                'msgid ""\nmsgstr ""\n"Language: ar\\n"\n\nmsgid "Planning"\nmsgstr "التخطيط"\n',
                encoding="utf-8",
            )

            remove_translation(base_dir, "ar", "Planning")
            entries = parse_po(po_dir / "django.po")
            self.assertNotIn("Planning", entries)
        finally:
            shutil.rmtree(base_dir, ignore_errors=True)

    def test_invalidate_translation_cache_clears_cached_language_entries(self):
        original_cache = dict(trans_real._translations)
        try:
            trans_real._translations.clear()
            trans_real._translations["ar"] = object()
            trans_real._translations[("ar", "django")] = object()
            trans_real._translations["en"] = object()

            invalidate_translation_cache("ar")

            self.assertNotIn("ar", trans_real._translations)
            self.assertNotIn(("ar", "django"), trans_real._translations)
            self.assertIn("en", trans_real._translations)
        finally:
            trans_real._translations.clear()
            trans_real._translations.update(original_cache)
