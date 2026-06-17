from pathlib import Path
import shutil
import uuid

from django.contrib.auth.models import AnonymousUser, User
from django.template import RequestContext, Template
from django.test import RequestFactory, TestCase
from django.utils import translation

from accounts.models import Profile
from manufacturing.models import Company
from manufacturing.runtime_translations import (
    list_translation_entries_for_company,
    resolve_runtime_translation,
    upsert_company_translation,
)


class RuntimeTranslationTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Ora")
        self.user = User.objects.create_user(username="ora", email="ora@example.com", password="pass12345")
        profile, _ = Profile.objects.get_or_create(user=self.user)
        profile.company = self.company
        profile.save(update_fields=["company"])
        self.factory = RequestFactory()

    def _workspace_temp_dir(self):
        root = Path.cwd() / ".tmp_test_tenant_translations"
        root.mkdir(parents=True, exist_ok=True)
        path = root / f"case_{uuid.uuid4().hex}"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def test_resolve_runtime_translation_prefers_company_override(self):
        upsert_company_translation(self.company, "ar", "Actual vs Planned", "translated-company")

        with translation.override("ar"):
            self.assertEqual(
                resolve_runtime_translation(self.company, "Actual vs Planned"),
                "translated-company",
            )

    def test_list_translation_entries_merges_company_overrides_over_global(self):
        base_dir = self._workspace_temp_dir()
        try:
            po_dir = base_dir / "locale" / "ar" / "LC_MESSAGES"
            po_dir.mkdir(parents=True, exist_ok=True)
            (po_dir / "django.po").write_text(
                'msgid ""\nmsgstr ""\n"Language: ar\\n"\n\n'
                'msgid "Actual vs Planned"\nmsgstr "translated-global"\n',
                encoding="utf-8",
            )

            upsert_company_translation(self.company, "ar", "Actual vs Planned", "translated-company")
            entries = list_translation_entries_for_company(self.company, "ar", base_dir)
            lookup = {entry["msgid"]: entry for entry in entries}

            self.assertEqual(lookup["Actual vs Planned"]["msgstr"], "translated-company")
            self.assertEqual(lookup["Actual vs Planned"]["origin"], "company")
        finally:
            shutil.rmtree(base_dir, ignore_errors=True)

    def test_tenant_trans_template_tag_renders_company_override(self):
        upsert_company_translation(self.company, "ar", "Actual vs Planned", "translated-company")
        request = self.factory.get("/manufacturing/reports/")
        request.user = self.user

        with translation.override("ar"):
            rendered = Template(
                '{% load tenant_i18n %}{% tenant_trans "Actual vs Planned" %}'
            ).render(RequestContext(request, {}))

        self.assertEqual(rendered, "translated-company")

    def test_tenant_trans_template_tag_handles_anonymous_user(self):
        request = self.factory.get("/accounts/login/")
        request.user = AnonymousUser()

        with translation.override("ar"):
            rendered = Template(
                '{% load tenant_i18n %}{% tenant_trans "Language" %}'
            ).render(RequestContext(request, {}))

        self.assertTrue(rendered)

    def test_resolve_runtime_translation_matches_common_source_variations(self):
        upsert_company_translation(
            self.company,
            "ar",
            "Compare production time and material consumption by Work Order",
            "runtime-override",
        )

        with translation.override("ar"):
            self.assertEqual(
                resolve_runtime_translation(
                    self.company,
                    "Compare production time and material consumption by Work Order.",
                ),
                "runtime-override",
            )
