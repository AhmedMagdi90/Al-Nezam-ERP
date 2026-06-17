import os
import shutil
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.core.management import call_command
from django.contrib.auth import get_user_model
from django.core import mail
from django.contrib.sessions.middleware import SessionMiddleware
from django.conf import settings
from django.db import connections
from django.http import HttpResponse
from django.test import RequestFactory, TestCase, override_settings

from manufacturing.models import BillOfMaterial, Company, Machine, Product, ProductionLog, WorkOrder
from tenancy.context import set_current_tenant_db
from tenancy.db import _TENANT_SCHEMA_READY_ALIASES
from tenancy.middleware import TenantContextMiddleware
from tenancy.models import Organization, Tenant
from tenancy.services import (
    provision_demo_signup,
    provision_organization_environments,
    provision_tenant_environment,
    provision_tenant_with_owner,
)


TEST_TENANT_ROOT = Path.cwd() / ".tmp_tenant_provision_tests"
FOUNDATION_TENANT_DB_PATH = TEST_TENANT_ROOT / "foundation-acme-live.sqlite3"
FOUNDATION_TENANT_ALIAS = "tenant_foundation_acme"
FOUNDATION_DEMO_DB_PATH = TEST_TENANT_ROOT / "foundation-acme-demo.sqlite3"
FOUNDATION_DEMO_ALIAS = "tenant_foundation_acme_demo"
FOUNDATION_TEST_DB_PATH = TEST_TENANT_ROOT / "foundation-acme-test.sqlite3"
FOUNDATION_TEST_ALIAS = "tenant_foundation_acme_test"
FOUNDATION_DEV_DB_PATH = TEST_TENANT_ROOT / "foundation-acme-dev.sqlite3"
FOUNDATION_DEV_ALIAS = "tenant_foundation_acme_dev"
TEST_TENANT_ROOT.mkdir(parents=True, exist_ok=True)

if FOUNDATION_TENANT_ALIAS not in settings.DATABASES:
    settings.DATABASES[FOUNDATION_TENANT_ALIAS] = {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": str(FOUNDATION_TENANT_DB_PATH),
        "OPTIONS": {"timeout": 30},
        "ATOMIC_REQUESTS": False,
        "AUTOCOMMIT": True,
        "CONN_MAX_AGE": 0,
        "CONN_HEALTH_CHECKS": False,
        "TIME_ZONE": None,
        "TEST": {
            "NAME": str(FOUNDATION_TENANT_DB_PATH),
            "MIRROR": None,
            "MIGRATE": True,
            "SERIALIZE": False,
            "DEPENDENCIES": [],
        },
    }
    connections.databases[FOUNDATION_TENANT_ALIAS] = settings.DATABASES[FOUNDATION_TENANT_ALIAS]

if FOUNDATION_DEMO_ALIAS not in settings.DATABASES:
    settings.DATABASES[FOUNDATION_DEMO_ALIAS] = {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": str(FOUNDATION_DEMO_DB_PATH),
        "OPTIONS": {"timeout": 30},
        "ATOMIC_REQUESTS": False,
        "AUTOCOMMIT": True,
        "CONN_MAX_AGE": 0,
        "CONN_HEALTH_CHECKS": False,
        "TIME_ZONE": None,
        "TEST": {
            "NAME": str(FOUNDATION_DEMO_DB_PATH),
            "MIRROR": None,
            "MIGRATE": True,
            "SERIALIZE": False,
            "DEPENDENCIES": [],
        },
    }
    connections.databases[FOUNDATION_DEMO_ALIAS] = settings.DATABASES[FOUNDATION_DEMO_ALIAS]

if FOUNDATION_TEST_ALIAS not in settings.DATABASES:
    settings.DATABASES[FOUNDATION_TEST_ALIAS] = {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": str(FOUNDATION_TEST_DB_PATH),
        "OPTIONS": {"timeout": 30},
        "ATOMIC_REQUESTS": False,
        "AUTOCOMMIT": True,
        "CONN_MAX_AGE": 0,
        "CONN_HEALTH_CHECKS": False,
        "TIME_ZONE": None,
        "TEST": {
            "NAME": str(FOUNDATION_TEST_DB_PATH),
            "MIRROR": None,
            "MIGRATE": True,
            "SERIALIZE": False,
            "DEPENDENCIES": [],
        },
    }
    connections.databases[FOUNDATION_TEST_ALIAS] = settings.DATABASES[FOUNDATION_TEST_ALIAS]

if FOUNDATION_DEV_ALIAS not in settings.DATABASES:
    settings.DATABASES[FOUNDATION_DEV_ALIAS] = {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": str(FOUNDATION_DEV_DB_PATH),
        "OPTIONS": {"timeout": 30},
        "ATOMIC_REQUESTS": False,
        "AUTOCOMMIT": True,
        "CONN_MAX_AGE": 0,
        "CONN_HEALTH_CHECKS": False,
        "TIME_ZONE": None,
        "TEST": {
            "NAME": str(FOUNDATION_DEV_DB_PATH),
            "MIRROR": None,
            "MIGRATE": True,
            "SERIALIZE": False,
            "DEPENDENCIES": [],
        },
    }
    connections.databases[FOUNDATION_DEV_ALIAS] = settings.DATABASES[FOUNDATION_DEV_ALIAS]


class TenantProvisioningFoundationTests(TestCase):
    databases = {"default", FOUNDATION_TENANT_ALIAS, FOUNDATION_DEMO_ALIAS, FOUNDATION_TEST_ALIAS, FOUNDATION_DEV_ALIAS}

    def setUp(self):
        self.temp_root = TEST_TENANT_ROOT
        self.temp_root.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        _TENANT_SCHEMA_READY_ALIASES.discard(FOUNDATION_TENANT_ALIAS)
        _TENANT_SCHEMA_READY_ALIASES.discard(FOUNDATION_DEMO_ALIAS)
        _TENANT_SCHEMA_READY_ALIASES.discard(FOUNDATION_TEST_ALIAS)
        _TENANT_SCHEMA_READY_ALIASES.discard(FOUNDATION_DEV_ALIAS)
        shutil.rmtree(self.temp_root, ignore_errors=True)
        super().tearDown()

    @patch.dict(os.environ, {"TENANT_BASE_DOMAIN": "nezam.test"}, clear=False)
    @patch("tenancy.services._ensure_postgres_database_exists")
    @patch("tenancy.services._build_tenant_db_url")
    def test_provision_creates_organization_and_live_environment(self, mock_build_tenant_db_url, mock_ensure_postgres):
        mock_build_tenant_db_url.return_value = str(FOUNDATION_TENANT_DB_PATH)

        tenant, company, user = provision_tenant_with_owner(
            company_name="Acme Manufacturing",
            company_code="foundation-acme",
            owner_email="owner@acme.com",
            owner_password="AcmeStrongPass123!",
        )

        tenant.refresh_from_db()
        organization = tenant.organization

        self.assertIsNotNone(organization)
        self.assertEqual(organization.slug, "foundation-acme")
        self.assertEqual(organization.owner_email, "owner@acme.com")
        self.assertEqual(tenant.environment_type, Tenant.EnvironmentType.LIVE)
        self.assertTrue(tenant.is_primary)
        self.assertEqual(tenant.hostname, "foundation-acme.nezam.test")
        self.assertEqual(company.name, "Acme Manufacturing")
        self.assertEqual(user.username, "owner@acme.com")
        self.assertEqual(tenant.db_alias, FOUNDATION_TENANT_ALIAS)

        User = get_user_model()
        self.assertTrue(User.objects.using(tenant.db_alias).filter(username="owner@acme.com").exists())
        self.assertTrue(Company.objects.using(tenant.db_alias).filter(name="Acme Manufacturing").exists())
        self.assertEqual(mock_ensure_postgres.call_count, 2)

    @patch.dict(os.environ, {"TENANT_BASE_DOMAIN": "nezam.test"}, clear=False)
    @patch("tenancy.services._ensure_postgres_database_exists")
    @patch("tenancy.services._build_tenant_db_url")
    def test_provision_demo_environment_creates_seeded_demo_package(self, mock_build_tenant_db_url, mock_ensure_postgres):
        def fake_db_url(tenant_code, _db_alias):
            if tenant_code == "foundation-acme":
                return str(FOUNDATION_TENANT_DB_PATH)
            if tenant_code == "foundation-acme-demo":
                return str(FOUNDATION_DEMO_DB_PATH)
            raise AssertionError(f"Unexpected tenant code: {tenant_code}")

        mock_build_tenant_db_url.side_effect = fake_db_url

        live_tenant, _live_company, _live_user = provision_tenant_with_owner(
            company_name="Acme Manufacturing",
            company_code="foundation-acme",
            owner_email="owner@acme.com",
            owner_password="AcmeStrongPass123!",
        )

        demo_tenant, demo_company, demo_owner = provision_tenant_environment(
            live_tenant.organization,
            Tenant.EnvironmentType.DEMO,
            owner_password="AcmeStrongPass123!",
            seed_demo_package=True,
        )

        self.assertEqual(demo_tenant.code, "foundation-acme-demo")
        self.assertEqual(demo_tenant.hostname, "foundation-acme-demo.nezam.test")
        self.assertFalse(demo_tenant.is_primary)
        self.assertEqual(demo_company.name, "Acme Manufacturing")
        self.assertEqual(demo_owner.username, "owner@acme.com")

        User = get_user_model()
        self.assertTrue(User.objects.using(demo_tenant.db_alias).filter(username="planner@acme-manufacturing.demo.local").exists())
        self.assertTrue(User.objects.using(demo_tenant.db_alias).filter(username="worker@acme-manufacturing.demo.local").exists())
        self.assertEqual(Machine.objects.using(demo_tenant.db_alias).filter(company=demo_company).count(), 5)
        self.assertGreaterEqual(Product.objects.using(demo_tenant.db_alias).filter(company=demo_company).count(), 2)
        self.assertGreaterEqual(WorkOrder.objects.using(demo_tenant.db_alias).filter(company=demo_company).count(), 2)

    @patch.dict(os.environ, {"TENANT_BASE_DOMAIN": "nezam.test"}, clear=False)
    @patch("tenancy.services._ensure_postgres_database_exists")
    @patch("tenancy.services._build_tenant_db_url")
    def test_public_demo_signup_creates_only_demo_environment(self, mock_build_tenant_db_url, mock_ensure_postgres):
        mock_build_tenant_db_url.return_value = str(FOUNDATION_DEMO_DB_PATH)

        demo_tenant, demo_company, demo_owner = provision_demo_signup(
            company_name="Acme Manufacturing",
            company_code="foundation-acme",
            owner_email="owner@acme.com",
            owner_password="AcmeStrongPass123!",
        )

        organization = demo_tenant.organization
        self.assertEqual(demo_tenant.environment_type, Tenant.EnvironmentType.DEMO)
        self.assertEqual(demo_tenant.code, "foundation-acme-demo")
        self.assertEqual(demo_tenant.hostname, "foundation-acme-demo.nezam.test")
        self.assertFalse(Tenant.objects.using("default").filter(organization=organization, environment_type=Tenant.EnvironmentType.LIVE).exists())
        self.assertEqual(demo_company.name, "Acme Manufacturing")
        self.assertEqual(demo_owner.username, "owner@acme.com")

    @override_settings(
        EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
        DEFAULT_FROM_EMAIL="noreply@nezam.test",
    )
    @patch.dict(os.environ, {"TENANT_BASE_DOMAIN": "nezam.test"}, clear=False)
    @patch("tenancy.services._ensure_postgres_database_exists")
    @patch("tenancy.services._build_tenant_db_url")
    def test_public_demo_signup_sends_access_email_with_demo_links(self, mock_build_tenant_db_url, mock_ensure_postgres):
        mock_build_tenant_db_url.return_value = str(FOUNDATION_DEMO_DB_PATH)

        provision_demo_signup(
            company_name="Acme Manufacturing",
            company_code="foundation-acme",
            owner_email="owner@acme.com",
            owner_password="AcmeStrongPass123!",
        )

        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, ["owner@acme.com"])
        self.assertIn("Acme Manufacturing", message.subject)
        self.assertIn("foundation-acme-demo", message.body)
        self.assertIn("https://foundation-acme-demo.nezam.test/accounts/login/", message.body)
        self.assertIn("/accounts/access/foundation-acme-demo/", message.body)
        self.assertIn("https://portal.nezam.test/accounts/portal/", message.body)
        self.assertIn("planner@acme-manufacturing.demo.local", message.body)
        self.assertIn("DemoPass123!", message.body)

    @override_settings(
        EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
        DEFAULT_FROM_EMAIL="noreply@nezam.test",
    )
    @patch.dict(os.environ, {"TENANT_BASE_DOMAIN": "nezam.test"}, clear=False)
    @patch("tenancy.services._ensure_postgres_database_exists")
    @patch("tenancy.services._build_tenant_db_url")
    def test_adding_test_environment_sends_updated_access_email(self, mock_build_tenant_db_url, mock_ensure_postgres):
        def fake_db_url(tenant_code, _db_alias):
            if tenant_code == "foundation-acme-demo":
                return str(FOUNDATION_DEMO_DB_PATH)
            if tenant_code == "foundation-acme-test":
                return str(FOUNDATION_TEST_DB_PATH)
            raise AssertionError(f"Unexpected tenant code: {tenant_code}")

        mock_build_tenant_db_url.side_effect = fake_db_url

        demo_tenant, _demo_company, _demo_owner = provision_demo_signup(
            company_name="Acme Manufacturing",
            company_code="foundation-acme",
            owner_email="owner@acme.com",
            owner_password="AcmeStrongPass123!",
        )
        mail.outbox.clear()

        provision_tenant_environment(
            demo_tenant.organization,
            Tenant.EnvironmentType.TEST,
            owner_password="AcmeStrongPass123!",
        )

        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertIn("foundation-acme-demo", message.body)
        self.assertIn("foundation-acme-test", message.body)
        self.assertIn("https://foundation-acme-test.nezam.test/accounts/login/", message.body)
        self.assertIn("/accounts/access/foundation-acme-test/", message.body)

    @patch.dict(os.environ, {"TENANT_BASE_DOMAIN": "nezam.test"}, clear=False)
    @patch("tenancy.services._ensure_postgres_database_exists")
    @patch("tenancy.services._build_tenant_db_url")
    def test_provision_test_environment_can_reuse_existing_owner_password_hash(self, mock_build_tenant_db_url, mock_ensure_postgres):
        def fake_db_url(tenant_code, _db_alias):
            if tenant_code == "foundation-acme-demo":
                return str(FOUNDATION_DEMO_DB_PATH)
            if tenant_code == "foundation-acme-test":
                return str(FOUNDATION_TEST_DB_PATH)
            raise AssertionError(f"Unexpected tenant code: {tenant_code}")

        mock_build_tenant_db_url.side_effect = fake_db_url

        demo_tenant, _demo_company, demo_owner = provision_demo_signup(
            company_name="Acme Manufacturing",
            company_code="foundation-acme",
            owner_email="owner@acme.com",
            owner_password="AcmeStrongPass123!",
        )

        test_tenant, _test_company, test_owner = provision_tenant_environment(
            demo_tenant.organization,
            Tenant.EnvironmentType.TEST,
            owner_password="",
            owner_password_hash=demo_owner.password,
            send_access_email=False,
        )

        self.assertEqual(test_tenant.code, "foundation-acme-test")
        self.assertEqual(test_tenant.db_alias, FOUNDATION_TEST_ALIAS)
        self.assertEqual(test_owner.username, "owner@acme.com")
        self.assertNotEqual(test_owner.password, "")
        self.assertTrue(test_owner.check_password("AcmeStrongPass123!"))

    @patch.dict(os.environ, {"TENANT_BASE_DOMAIN": "nezam.test"}, clear=False)
    @patch("tenancy.services._ensure_postgres_database_exists")
    @patch("tenancy.services._build_tenant_db_url")
    def test_provision_test_environment_can_copy_demo_setup_without_demo_transactions(self, mock_build_tenant_db_url, mock_ensure_postgres):
        def fake_db_url(tenant_code, _db_alias):
            if tenant_code == "foundation-acme-demo":
                return str(FOUNDATION_DEMO_DB_PATH)
            if tenant_code == "foundation-acme-test":
                return str(FOUNDATION_TEST_DB_PATH)
            raise AssertionError(f"Unexpected tenant code: {tenant_code}")

        mock_build_tenant_db_url.side_effect = fake_db_url

        demo_tenant, demo_company, demo_owner = provision_demo_signup(
            company_name="Acme Manufacturing",
            company_code="foundation-acme",
            owner_email="owner@acme.com",
            owner_password="AcmeStrongPass123!",
        )

        test_tenant, test_company, _test_owner = provision_tenant_environment(
            demo_tenant.organization,
            Tenant.EnvironmentType.TEST,
            owner_password="",
            owner_password_hash=demo_owner.password,
            setup_source_tenant=demo_tenant,
            send_access_email=False,
        )

        self.assertEqual(Machine.objects.using(test_tenant.db_alias).filter(company=test_company).count(), 5)
        self.assertEqual(
            sorted(Machine.objects.using(test_tenant.db_alias).filter(company=test_company).values_list("code", flat=True)),
            ["M-001", "M-002", "M-003", "M-004", "M-005"],
        )
        self.assertGreaterEqual(Product.objects.using(test_tenant.db_alias).filter(company=test_company).count(), 2)
        self.assertGreaterEqual(BillOfMaterial.objects.using(test_tenant.db_alias).filter(product__company=test_company).count(), 2)
        self.assertEqual(WorkOrder.objects.using(test_tenant.db_alias).filter(company=test_company).count(), 0)
        self.assertEqual(ProductionLog.objects.using(test_tenant.db_alias).count(), 0)
        self.assertEqual(demo_company.name, test_company.name)

    @patch.dict(os.environ, {"TENANT_BASE_DOMAIN": "nezam.test"}, clear=False)
    @patch("tenancy.services._ensure_postgres_database_exists")
    @patch("tenancy.services._build_tenant_db_url")
    def test_provision_live_environment_can_copy_test_setup_without_transactions(self, mock_build_tenant_db_url, mock_ensure_postgres):
        def fake_db_url(tenant_code, _db_alias):
            if tenant_code == "foundation-acme-demo":
                return str(FOUNDATION_DEMO_DB_PATH)
            if tenant_code == "foundation-acme-test":
                return str(FOUNDATION_TEST_DB_PATH)
            if tenant_code == "foundation-acme":
                return str(FOUNDATION_TENANT_DB_PATH)
            raise AssertionError(f"Unexpected tenant code: {tenant_code}")

        mock_build_tenant_db_url.side_effect = fake_db_url

        demo_tenant, _demo_company, demo_owner = provision_demo_signup(
            company_name="Acme Manufacturing",
            company_code="foundation-acme",
            owner_email="owner@acme.com",
            owner_password="AcmeStrongPass123!",
        )

        test_tenant, test_company, test_owner = provision_tenant_environment(
            demo_tenant.organization,
            Tenant.EnvironmentType.TEST,
            owner_password="",
            owner_password_hash=demo_owner.password,
            setup_source_tenant=demo_tenant,
            send_access_email=False,
        )

        approved_machine = Machine.objects.using(test_tenant.db_alias).get(company=test_company, code="M-001")
        approved_machine.name = "Approved CNC Machine 01"
        approved_machine.save(using=test_tenant.db_alias, update_fields=["name"])

        approved_product = Product.objects.using(test_tenant.db_alias).create(
            company_id=test_company.id,
            name="Customer Launch Product",
            unit="pcs",
            material_type="finished",
        )
        approved_bom = BillOfMaterial.objects.using(test_tenant.db_alias).create(
            product_id=approved_product.id,
            version="uat-v1",
            status="draft",
            base_quantity=50,
            uom="pcs",
            created_by_id=test_owner.id,
            notes="Approved in UAT",
        )
        test_work_order = WorkOrder.objects.using(test_tenant.db_alias).create(
            company_id=test_company.id,
            product_name=approved_product.name,
            bom_id=approved_bom.id,
            quantity=10,
            machine_id=approved_machine.id,
            status="pending",
            assignment_type="manual",
        )

        live_tenant, live_company, _live_owner = provision_tenant_environment(
            demo_tenant.organization,
            Tenant.EnvironmentType.LIVE,
            owner_password="",
            owner_password_hash=demo_owner.password,
            setup_source_tenant=test_tenant,
            send_access_email=False,
        )

        live_machine = Machine.objects.using(live_tenant.db_alias).get(company=live_company, code="M-001")
        self.assertEqual(live_machine.name, "Approved CNC Machine 01")
        self.assertTrue(Product.objects.using(live_tenant.db_alias).filter(company=live_company, name="Customer Launch Product").exists())
        self.assertTrue(
            BillOfMaterial.objects.using(live_tenant.db_alias).filter(product__company=live_company, version="uat-v1").exists()
        )
        self.assertEqual(WorkOrder.objects.using(live_tenant.db_alias).filter(company=live_company).count(), 0)
        self.assertEqual(ProductionLog.objects.using(live_tenant.db_alias).count(), 0)

    @patch.dict(os.environ, {"TENANT_BASE_DOMAIN": "nezam.test"}, clear=False)
    @patch("tenancy.services._ensure_postgres_database_exists")
    @patch("tenancy.services._build_tenant_db_url")
    def test_batch_provision_can_create_demo_test_and_dev_without_live(self, mock_build_tenant_db_url, mock_ensure_postgres):
        def fake_db_url(tenant_code, _db_alias):
            mapping = {
                "foundation-acme-demo": str(FOUNDATION_DEMO_DB_PATH),
                "foundation-acme-test": str(FOUNDATION_TEST_DB_PATH),
                "foundation-acme-dev": str(FOUNDATION_DEV_DB_PATH),
            }
            return mapping[tenant_code]

        mock_build_tenant_db_url.side_effect = fake_db_url

        organization, created = provision_organization_environments(
            company_name="Acme Manufacturing",
            company_code="foundation-acme",
            owner_email="owner@acme.com",
            owner_password="AcmeStrongPass123!",
            environment_types=[
                Tenant.EnvironmentType.DEV,
                Tenant.EnvironmentType.TEST,
                Tenant.EnvironmentType.DEMO,
            ],
            subscription_plan="pro",
        )

        created_by_type = {tenant.environment_type: tenant for tenant, _company, _user in created}
        self.assertEqual(set(created_by_type.keys()), {Tenant.EnvironmentType.DEMO, Tenant.EnvironmentType.TEST, Tenant.EnvironmentType.DEV})
        self.assertFalse(Tenant.objects.using("default").filter(organization=organization, environment_type=Tenant.EnvironmentType.LIVE).exists())

        demo_tenant = created_by_type[Tenant.EnvironmentType.DEMO]
        test_tenant = created_by_type[Tenant.EnvironmentType.TEST]
        dev_tenant = created_by_type[Tenant.EnvironmentType.DEV]
        demo_company = Company.objects.using(demo_tenant.db_alias).order_by("id").first()
        test_company = Company.objects.using(test_tenant.db_alias).order_by("id").first()
        dev_company = Company.objects.using(dev_tenant.db_alias).order_by("id").first()

        self.assertEqual(Machine.objects.using(demo_tenant.db_alias).filter(company=demo_company).count(), 5)
        self.assertEqual(Machine.objects.using(test_tenant.db_alias).filter(company=test_company).count(), 5)
        self.assertEqual(Machine.objects.using(dev_tenant.db_alias).filter(company=dev_company).count(), 5)
        self.assertEqual(WorkOrder.objects.using(test_tenant.db_alias).filter(company=test_company).count(), 0)
        self.assertEqual(WorkOrder.objects.using(dev_tenant.db_alias).filter(company=dev_company).count(), 0)


class ProvisionTenantCommandTests(TestCase):
    databases = {"default"}

    @patch("tenancy.management.commands.provision_tenant.provision_organization_environments")
    def test_command_can_bootstrap_exact_environment_list(self, mock_provision):
        organization = Organization(name="Acme Manufacturing", slug="acme", owner_email="owner@acme.com")
        company = SimpleNamespace(id=11)
        user = SimpleNamespace(id=22)
        demo_tenant = Tenant(
            name="Acme Demo",
            organization=organization,
            owner_email="owner@acme.com",
            code="acme-demo",
            environment_type=Tenant.EnvironmentType.DEMO,
            db_alias="tenant_acme_demo",
            db_name="tenant_dbs/acme-demo.sqlite3",
        )
        mock_provision.return_value = (
            organization,
            [(demo_tenant, company, user)],
        )

        call_command(
            "provision_tenant",
            "--company", "Acme Manufacturing",
            "--code", "acme",
            "--email", "owner@acme.com",
            "--password", "StrongPass123!",
            "--environment", "demo",
            "--environment", "test",
            "--environment", "dev",
        )

        mock_provision.assert_called_once_with(
            company_name="Acme Manufacturing",
            company_code="acme",
            owner_email="owner@acme.com",
            owner_password="StrongPass123!",
            environment_types=["demo", "test", "dev"],
            subscription_plan="free_trial",
            demo_password="DemoPass123!",
        )


@override_settings(ALLOWED_HOSTS=["testserver", "localhost", "127.0.0.1", "acme.nezam.com", "acme-test.nezam.com", "acme-demo.nezam.com"])
class TenantContextMiddlewareHostResolutionTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.session_middleware = SessionMiddleware(lambda request: HttpResponse("ok"))
        self.middleware = TenantContextMiddleware(lambda request: HttpResponse("ok"))

        self.organization = Organization.objects.create(
            name="Acme Manufacturing",
            slug="acme",
            owner_email="owner@acme.com",
        )
        self.live_tenant = Tenant.objects.create(
            name="Acme Manufacturing",
            organization=self.organization,
            owner_email="owner@acme.com",
            code="acme",
            environment_type=Tenant.EnvironmentType.LIVE,
            hostname="acme.nezam.com",
            is_primary=True,
            db_alias="tenant_acme",
            db_name="tenant_dbs/acme.sqlite3",
        )
        self.test_tenant = Tenant.objects.create(
            name="Acme Manufacturing Test",
            organization=self.organization,
            owner_email="owner@acme.com",
            code="acme-test",
            environment_type=Tenant.EnvironmentType.TEST,
            hostname=None,
            is_primary=False,
            db_alias="tenant_acme_test",
            db_name="tenant_dbs/acme-test.sqlite3",
        )
        self.demo_tenant = Tenant.objects.create(
            name="Acme Manufacturing Demo",
            organization=self.organization,
            owner_email="owner@acme.com",
            code="acme-demo",
            environment_type=Tenant.EnvironmentType.DEMO,
            hostname=None,
            is_primary=False,
            db_alias="tenant_acme_demo",
            db_name="tenant_dbs/acme-demo.sqlite3",
        )

    def _build_request(self, host="testserver"):
        request = self.factory.get("/manufacturing/dashboard/", HTTP_HOST=host)
        self.session_middleware.process_request(request)
        request.session.save()
        return request

    def tearDown(self):
        set_current_tenant_db("default")
        super().tearDown()

    @patch("tenancy.middleware.ensure_tenant_database_ready", return_value="tenant_acme")
    def test_hostname_resolution_takes_precedence_over_session(self, mock_ready):
        request = self._build_request(host="acme.nezam.com")
        request.session["tenant_code"] = "acme-test"
        request.session.save()

        self.middleware.process_request(request)

        self.assertEqual(request.tenant.id, self.live_tenant.id)
        self.assertEqual(request.tenant_db_alias, "tenant_acme")
        self.assertEqual(request.session["tenant_code"], "acme")
        mock_ready.assert_called_once()

    @patch.dict(os.environ, {"TENANT_BASE_DOMAIN": "nezam.com"}, clear=False)
    @patch("tenancy.middleware.ensure_tenant_database_ready", return_value="tenant_acme_test")
    def test_subdomain_fallback_resolves_tenant_by_code(self, mock_ready):
        request = self._build_request(host="acme-test.nezam.com")

        self.middleware.process_request(request)

        self.assertEqual(request.tenant.id, self.test_tenant.id)
        self.assertEqual(request.tenant_db_alias, "tenant_acme_test")
        self.assertEqual(request.session["tenant_code"], "acme-test")
        mock_ready.assert_called_once()

    @patch.dict(os.environ, {"TENANT_BASE_DOMAIN": "nezam.com"}, clear=False)
    @patch("tenancy.middleware.ensure_tenant_database_ready", return_value="tenant_acme_demo")
    def test_subdomain_fallback_resolves_demo_tenant_by_code(self, mock_ready):
        request = self._build_request(host="acme-demo.nezam.com")

        self.middleware.process_request(request)

        self.assertEqual(request.tenant.id, self.demo_tenant.id)
        self.assertEqual(request.tenant_db_alias, "tenant_acme_demo")
        self.assertEqual(request.session["tenant_code"], "acme-demo")
        mock_ready.assert_called_once()

    @patch("tenancy.middleware.ensure_tenant_database_ready", return_value="tenant_acme_test")
    def test_session_resolution_still_works_when_host_is_generic(self, mock_ready):
        request = self._build_request()
        request.session["tenant_code"] = "acme-test"
        request.session.save()

        self.middleware.process_request(request)

        self.assertEqual(request.tenant.id, self.test_tenant.id)
        self.assertEqual(request.tenant_db_alias, "tenant_acme_test")
        mock_ready.assert_called_once()

    @patch("tenancy.middleware.ensure_tenant_database_ready", side_effect=RuntimeError("boom"))
    def test_invalid_tenant_bootstrap_falls_back_to_default_instead_of_crashing(self, mock_ready):
        request = self.factory.get("/accounts/login/", HTTP_HOST="testserver")
        self.session_middleware.process_request(request)
        request.session["tenant_code"] = "acme-test"
        request.session.save()

        self.middleware.process_request(request)

        self.assertIsNone(request.tenant)
        self.assertEqual(request.tenant_db_alias, "default")
        self.assertNotIn("tenant_code", request.session)
        mock_ready.assert_called_once()
