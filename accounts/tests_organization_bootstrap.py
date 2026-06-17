from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from tenancy.models import Organization, Tenant


class OrganizationBootstrapViewTests(TestCase):
    databases = {"default"}

    def setUp(self):
        self.client = Client()
        self.staff_user = get_user_model().objects.create_user(
            username="staff@example.com",
            email="staff@example.com",
            password="StrongPass123!",
            is_staff=True,
        )
        self.normal_user = get_user_model().objects.create_user(
            username="user@example.com",
            email="user@example.com",
            password="StrongPass123!",
        )

    def test_staff_can_open_bootstrap_page(self):
        self.client.force_login(self.staff_user)

        response = self.client.get(reverse("organization_bootstrap"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Bootstrap Organization")
        self.assertContains(response, "Create Organization And Environments")

    def test_non_staff_user_is_forbidden(self):
        self.client.force_login(self.normal_user)

        response = self.client.get(reverse("organization_bootstrap"))

        self.assertEqual(response.status_code, 403)

    @patch("accounts.views.provision_organization_environments")
    def test_staff_post_bootstraps_selected_environments(self, mock_provision):
        self.client.force_login(self.staff_user)
        organization = Organization(name="Acme Manufacturing", slug="acme", owner_email="owner@acme.com")
        demo_tenant = Tenant(
            name="Acme Demo",
            organization=organization,
            owner_email="owner@acme.com",
            code="acme-demo",
            environment_type=Tenant.EnvironmentType.DEMO,
            db_alias="tenant_acme_demo",
            db_name="tenant_dbs/acme-demo.sqlite3",
            hostname="acme-demo.nezam.test",
        )
        dev_tenant = Tenant(
            name="Acme Dev",
            organization=organization,
            owner_email="owner@acme.com",
            code="acme-dev",
            environment_type=Tenant.EnvironmentType.DEV,
            db_alias="tenant_acme_dev",
            db_name="tenant_dbs/acme-dev.sqlite3",
            hostname="acme-dev.nezam.test",
        )
        mock_provision.return_value = (
            organization,
            [
                (demo_tenant, SimpleNamespace(id=1), SimpleNamespace(id=2)),
                (dev_tenant, SimpleNamespace(id=1), SimpleNamespace(id=2)),
            ],
        )

        response = self.client.post(
            reverse("organization_bootstrap"),
            {
                "company_name": "Acme Manufacturing",
                "company_code": "acme",
                "owner_email": "owner@acme.com",
                "owner_password": "StrongPass123!",
                "subscription_plan": "pro",
                "environments": ["demo", "dev"],
                "demo_password": "DemoPass123!",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Organization acme created successfully.")
        self.assertContains(response, "acme-demo")
        self.assertContains(response, "acme-dev")
        mock_provision.assert_called_once_with(
            company_name="Acme Manufacturing",
            company_code="acme",
            owner_email="owner@acme.com",
            owner_password="StrongPass123!",
            environment_types=["demo", "dev"],
            subscription_plan="pro",
            demo_password="DemoPass123!",
        )
