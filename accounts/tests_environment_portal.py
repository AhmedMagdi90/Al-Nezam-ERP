from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.sessions.middleware import SessionMiddleware
from django.http import HttpResponseRedirect
from django.test import Client, RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.views import home_redirect
from tenancy.models import Organization, Tenant


class EnvironmentPortalTests(TestCase):
    databases = {"default"}

    def setUp(self):
        self.client = Client()
        self.factory = RequestFactory()
        self.session_middleware = SessionMiddleware(lambda request: None)
        self.user = get_user_model().objects.create_user(
            username="owner@acme.com",
            email="owner@acme.com",
            password="StrongPass123!",
        )
        self.organization = Organization.objects.create(
            name="Acme Manufacturing",
            slug="acme",
            owner_email="owner@acme.com",
        )
        self.demo_tenant = Tenant.objects.create(
            name="Acme Demo",
            organization=self.organization,
            owner_email="owner@acme.com",
            code="acme-demo",
            environment_type=Tenant.EnvironmentType.DEMO,
            db_alias="tenant_acme_demo",
            db_name="tenant_dbs/acme-demo.sqlite3",
        )
        self.test_tenant = Tenant.objects.create(
            name="Acme Test",
            organization=self.organization,
            owner_email="owner@acme.com",
            code="acme-test",
            environment_type=Tenant.EnvironmentType.TEST,
            db_alias="tenant_acme_test",
            db_name="tenant_dbs/acme-test.sqlite3",
        )
        self.live_tenant = Tenant.objects.create(
            name="Acme Live",
            organization=self.organization,
            owner_email="owner@acme.com",
            code="acme",
            environment_type=Tenant.EnvironmentType.LIVE,
            is_primary=True,
            db_alias="tenant_acme",
            db_name="tenant_dbs/acme.sqlite3",
        )

    def _build_request(self, path="/accounts/home/"):
        request = self.factory.get(path)
        self.session_middleware.process_request(request)
        request.session.save()
        request.user = self.user
        return request

    def test_home_redirect_routes_multi_environment_organization_to_portal(self):
        request = self._build_request()

        response = home_redirect(request)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("environment_portal"))

    def test_home_redirect_skips_portal_when_only_one_active_environment_exists(self):
        self.demo_tenant.is_active = False
        self.demo_tenant.save(update_fields=["is_active"])
        self.test_tenant.is_active = False
        self.test_tenant.save(update_fields=["is_active"])
        request = self._build_request()

        with patch("accounts.views._resolve_profile_and_company", return_value=("planner", SimpleNamespace(id=1))):
            response = home_redirect(request)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("dashboard"))

    def test_environment_portal_lists_available_environments(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse("environment_portal"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Demo Server")
        self.assertContains(response, "Test Server")
        self.assertContains(response, "Live Server")
        self.assertContains(response, self.demo_tenant.code)
        self.assertContains(response, self.test_tenant.code)
        self.assertContains(response, self.live_tenant.code)
        self.assertContains(response, "Active")

    def test_environment_portal_switches_active_environment_and_delegates_redirect(self):
        self.client.force_login(self.user)

        with patch("accounts.views.ensure_tenant_database_registered", return_value="tenant_acme_test"), patch(
            "accounts.views.home_redirect",
            return_value=HttpResponseRedirect(reverse("dashboard")),
        ) as mock_home_redirect:
            response = self.client.post(
                reverse("environment_portal"),
                {"tenant_code": self.test_tenant.code},
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("dashboard"))
        session = self.client.session
        self.assertEqual(session["tenant_code"], self.test_tenant.code)
        self.assertTrue(session["reset_planner_workspace_state"])
        mock_home_redirect.assert_called_once()
        forwarded_request = mock_home_redirect.call_args.args[0]
        self.assertEqual(forwarded_request.tenant.code, self.test_tenant.code)
        self.assertEqual(forwarded_request.tenant_db_alias, "tenant_acme_test")
        self.assertEqual(mock_home_redirect.call_args.kwargs, {"allow_portal": False})

    def test_environment_portal_shows_provision_actions_for_missing_test_and_live(self):
        self.test_tenant.delete()
        self.live_tenant.delete()
        self.client.force_login(self.user)

        response = self.client.get(reverse("environment_portal"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Create Test Server")
        self.assertContains(response, "Create Live Server")
        self.assertContains(response, "copy machines, stages, products, and BOMs from the current demo setup")

    def test_environment_portal_shows_live_copy_from_test_text_when_test_exists(self):
        self.live_tenant.delete()
        self.client.force_login(self.user)

        response = self.client.get(reverse("environment_portal"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Create Live Server")
        self.assertContains(response, "copy approved machines, stages, products, and BOMs from the current Test setup")

    def test_environment_portal_provisions_test_environment_for_owner(self):
        self.test_tenant.delete()
        self.client.force_login(self.user)
        provisioned_tenant = Tenant(
            name="Acme Test",
            organization=self.organization,
            owner_email="owner@acme.com",
            code="acme-test",
            environment_type=Tenant.EnvironmentType.TEST,
            db_alias="tenant_acme_test",
            db_name="tenant_dbs/acme-test.sqlite3",
        )

        with patch(
            "accounts.views.provision_tenant_environment",
            return_value=(provisioned_tenant, None, None),
        ) as mock_provision:
            response = self.client.post(
                reverse("environment_portal"),
                {
                    "action": "provision_environment",
                    "environment_type": Tenant.EnvironmentType.TEST,
                },
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("environment_portal"))
        self.organization.refresh_from_db()
        self.assertTrue(self.organization.wants_test_environment)
        session = self.client.session
        self.assertEqual(session["tenant_code"], "acme-test")
        self.assertTrue(session["reset_planner_workspace_state"])
        mock_provision.assert_called_once_with(
            self.organization,
            Tenant.EnvironmentType.TEST,
            owner_password="",
            owner_password_hash=self.user.password,
            setup_source_tenant=self.demo_tenant,
        )

    def test_environment_portal_provisions_live_environment_from_test_setup_for_owner(self):
        self.live_tenant.delete()
        self.client.force_login(self.user)
        provisioned_tenant = Tenant(
            name="Acme Live",
            organization=self.organization,
            owner_email="owner@acme.com",
            code="acme",
            environment_type=Tenant.EnvironmentType.LIVE,
            is_primary=True,
            db_alias="tenant_acme",
            db_name="tenant_dbs/acme.sqlite3",
        )

        with patch(
            "accounts.views.provision_tenant_environment",
            return_value=(provisioned_tenant, None, None),
        ) as mock_provision:
            response = self.client.post(
                reverse("environment_portal"),
                {
                    "action": "provision_environment",
                    "environment_type": Tenant.EnvironmentType.LIVE,
                },
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("environment_portal"))
        session = self.client.session
        self.assertEqual(session["tenant_code"], "acme")
        self.assertTrue(session["reset_planner_workspace_state"])
        mock_provision.assert_called_once_with(
            self.organization,
            Tenant.EnvironmentType.LIVE,
            owner_password="",
            owner_password_hash=self.user.password,
            setup_source_tenant=self.test_tenant,
        )

    @override_settings(DEMO_TENANT_LIFETIME_DAYS=14)
    def test_environment_portal_shows_demo_days_remaining_badge(self):
        self.live_tenant.delete()
        self.test_tenant.delete()
        Tenant.objects.filter(pk=self.demo_tenant.pk).update(created_at=timezone.now() - timedelta(days=2))
        self.demo_tenant.refresh_from_db()
        self.client.force_login(self.user)

        response = self.client.get(reverse("environment_portal"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "12 Days Left")
        self.assertContains(response, "Available for 12 more days.")

    @override_settings(DEMO_TENANT_LIFETIME_DAYS=14)
    def test_environment_portal_blocks_opening_expired_demo_environment(self):
        self.live_tenant.delete()
        self.test_tenant.delete()
        Tenant.objects.filter(pk=self.demo_tenant.pk).update(created_at=timezone.now() - timedelta(days=16))
        self.demo_tenant.refresh_from_db()
        self.client.force_login(self.user)

        response = self.client.post(
            reverse("environment_portal"),
            {
                "action": "open_environment",
                "tenant_code": self.demo_tenant.code,
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Expired")
        self.assertContains(response, "Demo Server expired")
        self.assertNotEqual(self.client.session.get("tenant_code"), self.demo_tenant.code)

    @override_settings(DEMO_TENANT_LIFETIME_DAYS=14)
    def test_home_redirect_routes_single_expired_demo_to_portal(self):
        self.live_tenant.delete()
        self.test_tenant.delete()
        Tenant.objects.filter(pk=self.demo_tenant.pk).update(created_at=timezone.now() - timedelta(days=20))
        self.demo_tenant.refresh_from_db()
        request = self._build_request()
        request.tenant = self.demo_tenant

        response = home_redirect(request)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("environment_portal"))
