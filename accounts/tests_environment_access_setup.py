import os
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.sessions.middleware import SessionMiddleware
from django.core.management import call_command
from django.db import connections
from django.http import HttpResponseRedirect
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from tenancy.access_tokens import tenant_access_token_generator
from tenancy.db import _TENANT_SCHEMA_READY_ALIASES
from tenancy.models import Tenant
from tenancy.services import provision_demo_signup


TEST_TENANT_ROOT = Path.cwd() / ".tmp_environment_access_setup_tests"
ACCESS_DEMO_DB_PATH = TEST_TENANT_ROOT / "tower-access-demo.sqlite3"
ACCESS_DEMO_ALIAS = "tenant_tower_access_demo"
TEST_TENANT_ROOT.mkdir(parents=True, exist_ok=True)

if ACCESS_DEMO_ALIAS not in settings.DATABASES:
    settings.DATABASES[ACCESS_DEMO_ALIAS] = {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": str(ACCESS_DEMO_DB_PATH),
        "OPTIONS": {"timeout": 30},
        "ATOMIC_REQUESTS": False,
        "AUTOCOMMIT": True,
        "CONN_MAX_AGE": 0,
        "CONN_HEALTH_CHECKS": False,
        "TIME_ZONE": None,
        "TEST": {
            "NAME": str(ACCESS_DEMO_DB_PATH),
            "MIRROR": None,
            "MIGRATE": True,
            "SERIALIZE": False,
            "DEPENDENCIES": [],
        },
    }
    connections.databases[ACCESS_DEMO_ALIAS] = settings.DATABASES[ACCESS_DEMO_ALIAS]


@override_settings(ALLOWED_HOSTS=["testserver", "localhost", "127.0.0.1"])
class EnvironmentAccessSetupTests(TestCase):
    databases = {"default", ACCESS_DEMO_ALIAS}

    def setUp(self):
        self.client = Client()
        TEST_TENANT_ROOT.mkdir(parents=True, exist_ok=True)
        _TENANT_SCHEMA_READY_ALIASES.discard(ACCESS_DEMO_ALIAS)
        connections[ACCESS_DEMO_ALIAS].ensure_connection()
        call_command("flush", database=ACCESS_DEMO_ALIAS, interactive=False, verbosity=0)

    def tearDown(self):
        _TENANT_SCHEMA_READY_ALIASES.discard(ACCESS_DEMO_ALIAS)
        super().tearDown()

    @patch.dict(os.environ, {"TENANT_BASE_DOMAIN": "nezam.test"}, clear=False)
    @patch("tenancy.services._ensure_postgres_database_exists")
    @patch("tenancy.services._build_tenant_db_url", return_value=str(ACCESS_DEMO_DB_PATH))
    def test_access_setup_link_renders_password_form(self, _mock_db_url, _mock_ensure_postgres):
        demo_tenant, _company, demo_owner = provision_demo_signup(
            company_name="Tower Manufacturing",
            company_code="tower-access",
            owner_email="owner@tower.com",
            owner_password="OldStrongPass123!",
        )

        response = self.client.get(self._setup_url(demo_tenant, demo_owner))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Save Password And Open Workspace")
        self.assertContains(response, demo_tenant.name)

    @patch.dict(os.environ, {"TENANT_BASE_DOMAIN": "nezam.test"}, clear=False)
    @patch("tenancy.services._ensure_postgres_database_exists")
    @patch("tenancy.services._build_tenant_db_url", return_value=str(ACCESS_DEMO_DB_PATH))
    def test_access_setup_link_sets_password_and_logs_user_in(self, _mock_db_url, _mock_ensure_postgres):
        demo_tenant, _company, demo_owner = provision_demo_signup(
            company_name="Tower Manufacturing",
            company_code="tower-access",
            owner_email="owner@tower.com",
            owner_password="OldStrongPass123!",
        )
        setup_url = self._setup_url(demo_tenant, demo_owner)

        with patch("accounts.views.home_redirect", return_value=HttpResponseRedirect("/workspace/")):
            response = self.client.post(
                setup_url,
                {
                    "new_password1": "FreshStrongPass456!",
                    "new_password2": "FreshStrongPass456!",
                },
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, "/workspace/")
        session = self.client.session
        self.assertEqual(session["tenant_code"], demo_tenant.code)
        self.assertTrue(session.get("_auth_user_id"))

        reused_response = self.client.get(setup_url)
        self.assertEqual(reused_response.status_code, 400)
        self.assertContains(reused_response, "invalid or has already been used", status_code=400)

    @override_settings(DEMO_TENANT_LIFETIME_DAYS=14)
    @patch.dict(os.environ, {"TENANT_BASE_DOMAIN": "nezam.test"}, clear=False)
    @patch("tenancy.services._ensure_postgres_database_exists")
    @patch("tenancy.services._build_tenant_db_url", return_value=str(ACCESS_DEMO_DB_PATH))
    def test_access_setup_link_is_blocked_for_expired_demo(self, _mock_db_url, _mock_ensure_postgres):
        demo_tenant, _company, demo_owner = provision_demo_signup(
            company_name="Tower Manufacturing",
            company_code="tower-access",
            owner_email="owner@tower.com",
            owner_password="OldStrongPass123!",
        )
        Tenant.objects.filter(pk=demo_tenant.pk).update(created_at=timezone.now() - timedelta(days=20))
        demo_tenant.refresh_from_db()

        response = self.client.get(self._setup_url(demo_tenant, demo_owner))

        self.assertEqual(response.status_code, 410)
        self.assertIn("Demo Server expired", response.content.decode("utf-8"))

    def _setup_url(self, tenant, user):
        return reverse(
            "environment_access_setup",
            kwargs={
                "tenant_code": tenant.code,
                "uidb64": urlsafe_base64_encode(force_bytes(user.pk)),
                "token": tenant_access_token_generator.make_token(user),
            },
        )
