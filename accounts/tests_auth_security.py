from django.core.cache import cache
from django.test import Client, RequestFactory, SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from accounts.forms import TenantLoginForm
from accounts.security import clear_login_failures, get_login_throttle_state, register_login_failure
from accounts.views import _resolve_login_tenant
from manufacturing.forms import CompanyRegistrationForm
from tenancy.models import Organization, Tenant


class TenantLoginFormTests(SimpleTestCase):
    def test_tenant_code_is_normalized(self):
        form = TenantLoginForm(
            data={
                "tenant_code": "  Al-Nour ",
                "username": "admin@example.com",
                "password": "StrongPass123!",
            }
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["tenant_code"], "al-nour")


class LoginThrottleTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()
        cache.clear()
        self.request = self.factory.post("/accounts/login/")
        self.request.META["REMOTE_ADDR"] = "203.0.113.10"

    @override_settings(
        LOGIN_MAX_ATTEMPTS=3,
        LOGIN_MAX_ATTEMPTS_PER_IP=10,
        LOGIN_ATTEMPT_WINDOW_SECONDS=120,
        LOGIN_LOCKOUT_SECONDS=60,
    )
    def test_lockout_after_failed_attempt_limit(self):
        tenant_code = "al-nour"
        login_identifier = "admin@example.com"

        for _ in range(3):
            register_login_failure(self.request, tenant_code, login_identifier)

        state = get_login_throttle_state(self.request, tenant_code, login_identifier)
        self.assertTrue(state.is_blocked)
        self.assertGreater(state.retry_after_seconds, 0)

        clear_login_failures(self.request, tenant_code, login_identifier)
        state_after_clear = get_login_throttle_state(self.request, tenant_code, login_identifier)
        self.assertFalse(state_after_clear.is_blocked)


class CompanyRegistrationFormTests(TestCase):
    def test_weak_password_rejected(self):
        form = CompanyRegistrationForm(
            data={
                "company_name": "Acme",
                "company_code": "acme",
                "owner_email": "owner@acme.com",
                "owner_password": "12345678",
            }
        )
        self.assertFalse(form.is_valid())
        self.assertIn("owner_password", form.errors)

    def test_manual_billing_registration_has_no_card_fields(self):
        form = CompanyRegistrationForm()
        self.assertNotIn("card_number", form.fields)
        self.assertNotIn("expiry", form.fields)
        self.assertNotIn("cvc", form.fields)

    def test_legacy_payment_post_fields_are_ignored(self):
        form = CompanyRegistrationForm(
            data={
                "company_name": "Acme",
                "company_code": "acme",
                "owner_email": "owner@acme.com",
                "owner_password": "AcmeStrongPass123!",
                "card_number": "4242 4242 4242 4242",
                "expiry": "12/28",
                "cvc": "123",
            }
        )
        self.assertTrue(form.is_valid(), form.errors)

    def test_duplicate_company_code_rejected(self):
        Tenant.objects.using("default").create(
            name="Existing",
            code="acme",
            db_alias="tenant_acme",
            db_name="tenant_dbs/acme.sqlite3",
            owner_email="existing@acme.com",
        )
        form = CompanyRegistrationForm(
            data={
                "company_name": "Acme 2",
                "company_code": "acme",
                "owner_email": "owner2@acme.com",
                "owner_password": "AcmeStrongPass123!",
            }
        )
        self.assertFalse(form.is_valid())
        self.assertIn("company_code", form.errors)

    def test_duplicate_company_code_rejected_when_organization_slug_exists(self):
        Organization.objects.using("default").create(
            name="Existing Org",
            slug="acme",
            owner_email="existing@acme.com",
        )
        form = CompanyRegistrationForm(
            data={
                "company_name": "Acme 2",
                "company_code": "acme",
                "owner_email": "owner2@acme.com",
                "owner_password": "AcmeStrongPass123!",
            }
        )
        self.assertFalse(form.is_valid())
        self.assertIn("company_code", form.errors)

    def test_duplicate_owner_email_rejected(self):
        Tenant.objects.using("default").create(
            name="Existing",
            code="acme",
            db_alias="tenant_acme",
            db_name="tenant_dbs/acme.sqlite3",
            owner_email="owner@acme.com",
        )
        form = CompanyRegistrationForm(
            data={
                "company_name": "Acme 2",
                "company_code": "acme-2",
                "owner_email": "owner@acme.com",
                "owner_password": "AcmeStrongPass123!",
            }
        )
        self.assertFalse(form.is_valid())
        self.assertIn("owner_email", form.errors)

    def test_duplicate_owner_email_rejected_when_organization_exists(self):
        Organization.objects.using("default").create(
            name="Existing Org",
            slug="acme",
            owner_email="owner@acme.com",
        )
        form = CompanyRegistrationForm(
            data={
                "company_name": "Acme 2",
                "company_code": "acme-2",
                "owner_email": "owner@acme.com",
                "owner_password": "AcmeStrongPass123!",
            }
        )
        self.assertFalse(form.is_valid())
        self.assertIn("owner_email", form.errors)


class CsrfRecoveryTests(SimpleTestCase):
    def test_login_post_without_csrf_redirects_to_fresh_login(self):
        client = Client(enforce_csrf_checks=True)
        response = client.post(
            reverse("login"),
            {
                "action": "login",
                "tenant_code": "al-nour",
                "username": "admin@example.com",
                "password": "bad-pass",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"{reverse('login')}?cookie_required=1")

    def test_login_post_without_csrf_preserves_next_parameter(self):
        client = Client(enforce_csrf_checks=True)
        response = client.post(
            f"{reverse('login')}?next=/manufacturing/dashboard/",
            {
                "action": "login",
                "tenant_code": "al-nour",
                "username": "admin@example.com",
                "password": "bad-pass",
                "next": "/manufacturing/dashboard/",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response["Location"],
            "/accounts/login/?cookie_required=1&next=%2Fmanufacturing%2Fdashboard%2F",
        )


class LoginPrefillTests(TestCase):
    def test_login_get_prefills_tenant_code_from_query_string(self):
        client = Client()

        response = client.get(f"{reverse('login')}?tenant_code=acme-demo")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'value="acme-demo"', html=False)


class LoginTenantResolutionTests(TestCase):
    databases = {"default"}

    def test_exact_tenant_code_still_resolves_directly(self):
        organization = Organization.objects.using("default").create(
            name="Acme",
            slug="acme",
            owner_email="owner@acme.com",
        )
        tenant = Tenant.objects.using("default").create(
            name="Acme Demo",
            organization=organization,
            owner_email="owner@acme.com",
            code="acme-demo",
            environment_type=Tenant.EnvironmentType.DEMO,
            db_alias="tenant_acme_demo",
            db_name="tenant_dbs/acme-demo.sqlite3",
        )

        resolved = _resolve_login_tenant("Acme-Demo")

        self.assertEqual(resolved.id, tenant.id)

    def test_company_code_resolves_to_demo_for_fresh_demo_signup(self):
        organization = Organization.objects.using("default").create(
            name="Youniss",
            slug="youniss",
            owner_email="youniss@gmail.com",
        )
        tenant = Tenant.objects.using("default").create(
            name="Youniss Demo",
            organization=organization,
            owner_email="youniss@gmail.com",
            code="youniss-demo",
            environment_type=Tenant.EnvironmentType.DEMO,
            db_alias="tenant_youniss_demo",
            db_name="tenant_dbs/youniss-demo.sqlite3",
        )

        resolved = _resolve_login_tenant("Youniss")

        self.assertEqual(resolved.id, tenant.id)

    def test_company_code_prefers_live_when_live_exists(self):
        organization = Organization.objects.using("default").create(
            name="Acme",
            slug="acme",
            owner_email="owner@acme.com",
        )
        Tenant.objects.using("default").create(
            name="Acme Demo",
            organization=organization,
            owner_email="owner@acme.com",
            code="acme-demo",
            environment_type=Tenant.EnvironmentType.DEMO,
            db_alias="tenant_acme_demo",
            db_name="tenant_dbs/acme-demo.sqlite3",
        )
        live_tenant = Tenant.objects.using("default").create(
            name="Acme Live",
            organization=organization,
            owner_email="owner@acme.com",
            code="acme",
            environment_type=Tenant.EnvironmentType.LIVE,
            is_primary=True,
            db_alias="tenant_acme",
            db_name="tenant_dbs/acme.sqlite3",
        )

        resolved = _resolve_login_tenant("acme")

        self.assertEqual(resolved.id, live_tenant.id)

    def test_owner_email_resolves_workspace_when_company_code_is_wrong(self):
        organization = Organization.objects.using("default").create(
            name="Youniss Manufacturing",
            slug="youniss-mfg",
            owner_email="youniss@gmail.com",
        )
        tenant = Tenant.objects.using("default").create(
            name="Youniss Manufacturing Demo",
            organization=organization,
            owner_email="youniss@gmail.com",
            code="youniss-mfg-demo",
            environment_type=Tenant.EnvironmentType.DEMO,
            db_alias="tenant_youniss_mfg_demo",
            db_name="tenant_dbs/youniss-mfg-demo.sqlite3",
        )

        resolved = _resolve_login_tenant("Youniss", "Youniss@gmail.com")

        self.assertEqual(resolved.id, tenant.id)
