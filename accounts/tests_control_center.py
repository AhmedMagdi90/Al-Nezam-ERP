from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse
from unittest.mock import patch

from accounts.aws_billing import get_organization_billing_snapshot
from tenancy.models import Organization, SupportActionLog, Tenant


class PlatformControlCenterTests(TestCase):
    databases = {"default"}

    def setUp(self):
        self.client = Client()
        self.staff_user = get_user_model().objects.create_user(
            username="support@example.com",
            email="support@example.com",
            password="StrongPass123!",
            is_staff=True,
        )
        self.normal_user = get_user_model().objects.create_user(
            username="basic@example.com",
            email="basic@example.com",
            password="StrongPass123!",
        )
        self.organization = Organization.objects.create(
            name="Acme Manufacturing",
            slug="acme",
            owner_email="owner@acme.com",
            status=Organization.Status.ACTIVE,
            seat_limit=12,
            wants_test_environment=True,
        )
        self.demo_tenant = Tenant.objects.create(
            name="Acme Demo",
            organization=self.organization,
            owner_email="owner@acme.com",
            code="acme-demo",
            environment_type=Tenant.EnvironmentType.DEMO,
            db_alias="tenant_acme_demo",
            db_name="tenant_dbs/acme-demo.sqlite3",
            hostname="demo.acme.nezam.test",
            is_active=True,
        )
        self.live_tenant = Tenant.objects.create(
            name="Acme Live",
            organization=self.organization,
            owner_email="owner@acme.com",
            code="acme",
            environment_type=Tenant.EnvironmentType.LIVE,
            db_alias="tenant_acme",
            db_name="tenant_dbs/acme.sqlite3",
            hostname="app.acme.nezam.test",
            is_active=True,
        )
        self.url = reverse("platform_control_center")

    def test_staff_can_open_control_center(self):
        self.client.force_login(self.staff_user)

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Al Nezam Control Center")
        self.assertContains(response, "Acme Manufacturing")
        self.assertContains(response, "Support Sidebar")

    @patch.dict("os.environ", {"AWS_COST_EXPLORER_ENABLED": "0"}, clear=False)
    @patch("accounts.aws_billing._aws_credentials_available")
    @patch("accounts.aws_billing._boto3_available")
    def test_billing_snapshot_skips_aws_runtime_checks_when_disabled(self, boto3_available, credentials_available):
        snapshot = get_organization_billing_snapshot(self.organization, [], currency="USD")

        self.assertFalse(snapshot["available"])
        self.assertFalse(snapshot["configured"])
        boto3_available.assert_not_called()
        credentials_available.assert_not_called()

    @patch.dict("os.environ", {"AWS_COST_EXPLORER_ENABLED": "1", "AWS_COST_EXPLORER_TAG_KEY": ""}, clear=False)
    @patch("accounts.aws_billing._aws_credentials_available")
    @patch("accounts.aws_billing._boto3_available")
    def test_billing_snapshot_skips_aws_runtime_checks_without_tag_key(self, boto3_available, credentials_available):
        snapshot = get_organization_billing_snapshot(self.organization, [], currency="USD")

        self.assertFalse(snapshot["available"])
        self.assertFalse(snapshot["configured"])
        self.assertIn("Set AWS_COST_EXPLORER_TAG_KEY", snapshot["reason"])
        boto3_available.assert_not_called()
        credentials_available.assert_not_called()

    def test_non_staff_user_is_forbidden(self):
        self.client.force_login(self.normal_user)

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 403)

    def test_search_filters_by_tenant_code(self):
        self.client.force_login(self.staff_user)
        Organization.objects.create(
            name="Bravo Foods",
            slug="bravo",
            owner_email="owner@bravo.com",
            status=Organization.Status.ACTIVE,
            seat_limit=6,
        )

        response = self.client.get(self.url, {"q": "acme-demo"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Acme Manufacturing")
        self.assertNotContains(response, "Bravo Foods")

    def test_selected_tab_is_rendered(self):
        self.client.force_login(self.staff_user)

        response = self.client.get(self.url, {"org": self.organization.id, "tab": "billing"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["selected_tab"], "billing")
        self.assertContains(response, "Billing Setup Checklist")

    def test_danger_tab_is_rendered(self):
        self.client.force_login(self.staff_user)

        response = self.client.get(self.url, {"org": self.organization.id, "tab": "danger"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["selected_tab"], "danger")
        self.assertContains(response, "Delete Company")

    def test_support_tab_is_rendered(self):
        self.client.force_login(self.staff_user)

        response = self.client.get(self.url, {"org": self.organization.id, "tab": "support"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["selected_tab"], "support")
        self.assertContains(response, "Support Notes")

    def test_workspace_health_strip_is_rendered(self):
        self.client.force_login(self.staff_user)

        response = self.client.get(self.url, {"org": self.organization.id, "tab": "overview"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Last Support Action")
        self.assertContains(response, "Open Risk")

    def test_staff_can_update_customer_controls(self):
        self.client.force_login(self.staff_user)

        response = self.client.post(
            self.url,
            data={
                "action": "update_organization",
                "organization_id": str(self.organization.id),
                "organization_name": "Acme Industrial Group",
                "owner_email": "new.owner@acme.com",
                "organization_status": Organization.Status.SUSPENDED,
                "seat_limit": "25",
                "support_notes": "Customer asked for onboarding support and test sign-off.",
                "tab": "overview",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.organization.refresh_from_db()
        self.demo_tenant.refresh_from_db()
        self.live_tenant.refresh_from_db()
        self.assertEqual(self.organization.name, "Acme Industrial Group")
        self.assertEqual(self.organization.owner_email, "new.owner@acme.com")
        self.assertEqual(self.organization.status, Organization.Status.SUSPENDED)
        self.assertEqual(self.organization.seat_limit, 25)
        self.assertEqual(self.organization.support_notes, "Customer asked for onboarding support and test sign-off.")
        self.assertEqual(self.demo_tenant.owner_email, "new.owner@acme.com")
        self.assertEqual(self.live_tenant.owner_email, "new.owner@acme.com")
        self.assertTrue(
            SupportActionLog.objects.filter(
                organization=self.organization,
                action_type="update_customer_identity",
            ).exists()
        )
        self.assertEqual(response.context["selected_tab"], "overview")

    def test_staff_can_update_support_notes(self):
        self.client.force_login(self.staff_user)

        response = self.client.post(
            self.url,
            data={
                "action": "update_support_notes",
                "organization_id": str(self.organization.id),
                "support_notes": "Customer is blocked on billing setup and needs follow-up tomorrow.",
                "tab": "support",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.organization.refresh_from_db()
        self.assertEqual(self.organization.support_notes, "Customer is blocked on billing setup and needs follow-up tomorrow.")
        self.assertEqual(response.context["selected_tab"], "support")
        self.assertTrue(
            SupportActionLog.objects.filter(
                organization=self.organization,
                action_type="update_support_notes",
            ).exists()
        )

    def test_staff_can_deactivate_environment(self):
        self.client.force_login(self.staff_user)

        response = self.client.post(
            self.url,
            data={
                "action": "set_tenant_state",
                "tenant_id": str(self.demo_tenant.id),
                "organization_id": str(self.organization.id),
                "tenant_state": "inactive",
                "tab": "environments",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.demo_tenant.refresh_from_db()
        self.assertFalse(self.demo_tenant.is_active)
        self.assertEqual(response.context["selected_tab"], "environments")
        self.assertTrue(
            SupportActionLog.objects.filter(
                organization=self.organization,
                action_type="set_environment_state",
                target_label="acme-demo",
            ).exists()
        )

    def test_staff_can_deactivate_company_and_active_environments(self):
        self.client.force_login(self.staff_user)

        response = self.client.post(
            self.url,
            data={
                "action": "deactivate_organization",
                "organization_id": str(self.organization.id),
                "tab": "danger",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.organization.refresh_from_db()
        self.demo_tenant.refresh_from_db()
        self.live_tenant.refresh_from_db()
        self.assertEqual(self.organization.status, Organization.Status.SUSPENDED)
        self.assertFalse(self.demo_tenant.is_active)
        self.assertFalse(self.live_tenant.is_active)
        self.assertEqual(response.context["selected_tab"], "danger")
        self.assertContains(response, "was deactivated and 2 active environments were shut off.")
        self.assertTrue(
            SupportActionLog.objects.filter(
                organization=self.organization,
                action_type="deactivate_organization",
                target_label="acme",
            ).exists()
        )

    def test_staff_can_bulk_deactivate_environments(self):
        self.client.force_login(self.staff_user)

        response = self.client.post(
            self.url,
            data={
                "action": "bulk_set_environment_state",
                "organization_id": str(self.organization.id),
                "tenant_state": "inactive",
                "tab": "environments",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.demo_tenant.refresh_from_db()
        self.live_tenant.refresh_from_db()
        self.assertFalse(self.demo_tenant.is_active)
        self.assertFalse(self.live_tenant.is_active)
        self.assertContains(response, "2 environments were marked inactive for Acme Manufacturing.")
        self.assertTrue(
            SupportActionLog.objects.filter(
                organization=self.organization,
                action_type="bulk_set_environment_state",
            ).exists()
        )

    def test_bulk_activate_requires_active_company(self):
        self.client.force_login(self.staff_user)
        self.organization.status = Organization.Status.SUSPENDED
        self.organization.save(update_fields=["status"])
        self.demo_tenant.is_active = False
        self.demo_tenant.save(update_fields=["is_active"])

        response = self.client.post(
            self.url,
            data={
                "action": "bulk_set_environment_state",
                "organization_id": str(self.organization.id),
                "tenant_state": "active",
                "tab": "environments",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.demo_tenant.refresh_from_db()
        self.assertFalse(self.demo_tenant.is_active)
        self.assertContains(response, "Activate the company first before re-enabling its environments.")

    def test_delete_environment_requires_inactive_state(self):
        self.client.force_login(self.staff_user)

        response = self.client.post(
            self.url,
            data={
                "action": "delete_environment",
                "tenant_id": str(self.demo_tenant.id),
                "organization_id": str(self.organization.id),
                "delete_environment_code": self.demo_tenant.code,
                "acknowledge_environment_delete": "on",
                "tab": "environments",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(Tenant.objects.filter(id=self.demo_tenant.id).exists())
        self.assertContains(response, "Deactivate the environment before deleting it.")

    @patch("accounts.views.delete_tenant_environment")
    def test_staff_can_delete_inactive_environment_when_confirmed(self, mock_delete_tenant_environment):
        self.client.force_login(self.staff_user)
        self.demo_tenant.is_active = False
        self.demo_tenant.save(update_fields=["is_active"])

        def _delete(tenant):
            tenant.delete()

        mock_delete_tenant_environment.side_effect = _delete

        response = self.client.post(
            self.url,
            data={
                "action": "delete_environment",
                "tenant_id": str(self.demo_tenant.id),
                "organization_id": str(self.organization.id),
                "delete_environment_code": self.demo_tenant.code,
                "acknowledge_environment_delete": "on",
                "tab": "environments",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(Tenant.objects.filter(id=self.demo_tenant.id).exists())
        mock_delete_tenant_environment.assert_called_once()
        self.assertContains(response, "Demo Server `acme-demo` was deleted.")
        self.assertTrue(
            SupportActionLog.objects.filter(
                organization=self.organization,
                action_type="delete_environment",
                target_label="acme-demo",
            ).exists()
        )

    def test_staff_can_activate_company_without_reactivating_environments(self):
        self.client.force_login(self.staff_user)
        self.organization.status = Organization.Status.SUSPENDED
        self.organization.save(update_fields=["status"])
        self.demo_tenant.is_active = False
        self.demo_tenant.save(update_fields=["is_active"])
        self.live_tenant.is_active = False
        self.live_tenant.save(update_fields=["is_active"])

        response = self.client.post(
            self.url,
            data={
                "action": "set_organization_status",
                "organization_id": str(self.organization.id),
                "organization_status": Organization.Status.ACTIVE,
                "tab": "danger",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.organization.refresh_from_db()
        self.demo_tenant.refresh_from_db()
        self.live_tenant.refresh_from_db()
        self.assertEqual(self.organization.status, Organization.Status.ACTIVE)
        self.assertFalse(self.demo_tenant.is_active)
        self.assertFalse(self.live_tenant.is_active)
        self.assertContains(response, "was activated. Environments remain in their current state.")
        self.assertTrue(
            SupportActionLog.objects.filter(
                organization=self.organization,
                action_type="activate_organization",
                target_label="acme",
            ).exists()
        )

    def test_staff_can_move_company_to_pending(self):
        self.client.force_login(self.staff_user)

        response = self.client.post(
            self.url,
            data={
                "action": "set_organization_status",
                "organization_id": str(self.organization.id),
                "organization_status": Organization.Status.PENDING,
                "tab": "danger",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.organization.refresh_from_db()
        self.assertEqual(self.organization.status, Organization.Status.PENDING)
        self.assertContains(response, "was moved to pending review.")
        self.assertTrue(
            SupportActionLog.objects.filter(
                organization=self.organization,
                action_type="set_organization_pending",
                target_label="acme",
            ).exists()
        )

    @patch("accounts.views.send_environment_access_email", return_value=True)
    def test_staff_can_resend_access_email(self, mock_send_environment_access_email):
        self.client.force_login(self.staff_user)

        response = self.client.post(
            self.url,
            data={
                "action": "resend_access_email",
                "organization_id": str(self.organization.id),
                "tab": "overview",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        mock_send_environment_access_email.assert_called_once_with(self.organization)
        self.assertContains(response, "Access email resent to owner@acme.com.")
        self.assertTrue(
            SupportActionLog.objects.filter(
                organization=self.organization,
                action_type="resend_access_email",
            ).exists()
        )

    @patch("accounts.views.send_environment_access_email", return_value=True)
    def test_staff_can_send_recovery_access_email(self, mock_send_environment_access_email):
        self.client.force_login(self.staff_user)

        response = self.client.post(
            self.url,
            data={
                "action": "send_recovery_access_email",
                "organization_id": str(self.organization.id),
                "recovery_email": "backup@acme.com",
                "tab": "overview",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        mock_send_environment_access_email.assert_called_once_with(self.organization, recipient_email="backup@acme.com")
        self.assertContains(response, "Recovery access email sent to backup@acme.com.")
        self.assertTrue(
            SupportActionLog.objects.filter(
                organization=self.organization,
                action_type="send_recovery_access_email",
                target_label="backup@acme.com",
            ).exists()
        )

    @patch("accounts.views.provision_tenant_environment")
    @patch("accounts.views._owner_password_hash_for_organization", return_value="pbkdf2_sha256$hash")
    def test_staff_can_provision_missing_environment_from_control_center(self, mock_owner_hash, mock_provision_tenant_environment):
        self.client.force_login(self.staff_user)
        created_tenant = Tenant(
            name="Acme Test",
            organization=self.organization,
            owner_email="owner@acme.com",
            code="acme-test",
            environment_type=Tenant.EnvironmentType.TEST,
            db_alias="tenant_acme_test",
            db_name="tenant_dbs/acme-test.sqlite3",
            hostname="test.acme.nezam.test",
            is_active=True,
        )
        mock_provision_tenant_environment.return_value = (created_tenant, object(), object())

        response = self.client.post(
            self.url,
            data={
                "action": "provision_environment",
                "organization_id": str(self.organization.id),
                "environment_type": Tenant.EnvironmentType.TEST,
                "tab": "environments",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["selected_tab"], "environments")
        mock_owner_hash.assert_called_once_with(self.organization)
        mock_provision_tenant_environment.assert_called_once()
        self.assertTrue(
            SupportActionLog.objects.filter(
                organization=self.organization,
                action_type="provision_environment",
                target_label="acme-test",
            ).exists()
        )

    def test_delete_company_requires_exact_slug_confirmation(self):
        self.client.force_login(self.staff_user)

        response = self.client.post(
            self.url,
            data={
                "action": "delete_organization",
                "organization_id": str(self.organization.id),
                "delete_confirmation_slug": "wrong-slug",
                "acknowledge_delete": "on",
                "tab": "danger",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(Organization.objects.filter(id=self.organization.id).exists())
        self.assertEqual(response.context["selected_tab"], "danger")
        self.assertContains(response, "Type the exact company code before deleting this customer.")

    @patch("accounts.views.delete_organization_environments")
    def test_staff_can_delete_company_when_confirmed(self, mock_delete_organization_environments):
        self.client.force_login(self.staff_user)

        def _delete(org):
            org.delete()

        mock_delete_organization_environments.side_effect = _delete

        response = self.client.post(
            self.url,
            data={
                "action": "delete_organization",
                "organization_id": str(self.organization.id),
                "delete_confirmation_slug": self.organization.slug,
                "acknowledge_delete": "on",
                "tab": "danger",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(Organization.objects.filter(id=self.organization.id).exists())
        mock_delete_organization_environments.assert_called_once()
        self.assertContains(response, "was deleted from the control center.")
