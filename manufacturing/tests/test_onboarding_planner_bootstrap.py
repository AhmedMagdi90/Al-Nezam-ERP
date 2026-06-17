from io import BytesIO
import json
from pathlib import Path
from unittest.mock import patch

import openpyxl
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.messages.storage.fallback import FallbackStorage
from django.contrib.sessions.middleware import SessionMiddleware
from django.core.management import call_command
from django.db import connections
from django.urls import reverse
from django.test import RequestFactory, TestCase
from django.core.files.uploadedfile import SimpleUploadedFile

from accounts.models import Profile, Role
from manufacturing.models import (
    BillOfMaterial,
    BOMComponent,
    BOMOperation,
    Company,
    Machine,
    Notification,
    ProductionStage,
    WorkOrder,
)
from manufacturing.services import DashboardService
from manufacturing.views.api import TimelineDataView
from manufacturing.views.bulk import HandleBulkImportView
from manufacturing.views.dashboard import PlannerDashboardView
from manufacturing.views.dashboard import require_company
from tenancy.context import reset_current_tenant_db, set_current_tenant_db
from tenancy.db import _TENANT_SCHEMA_READY_ALIASES
from tenancy.models import Tenant
from tenancy.services import provision_tenant_with_owner


TEST_TENANT_ROOT = Path.cwd() / ".tmp_onboarding_planner_tests"
TEST_TENANT_DB_PATH = TEST_TENANT_ROOT / "tower-live.sqlite3"
TEST_TENANT_ALIAS = "tenant_tower_live"
TEST_DEMO_TENANT_DB_PATH = TEST_TENANT_ROOT / "tower-demo.sqlite3"
TEST_DEMO_TENANT_ALIAS = "tenant_tower_live_demo"

TEST_TENANT_ROOT.mkdir(parents=True, exist_ok=True)

if TEST_TENANT_ALIAS not in settings.DATABASES:
    settings.DATABASES[TEST_TENANT_ALIAS] = {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": str(TEST_TENANT_DB_PATH),
        "OPTIONS": {"timeout": 30},
        "ATOMIC_REQUESTS": False,
        "AUTOCOMMIT": True,
        "CONN_MAX_AGE": 0,
        "CONN_HEALTH_CHECKS": False,
        "TIME_ZONE": None,
        "TEST": {
            "NAME": str(TEST_TENANT_DB_PATH),
            "MIRROR": None,
            "MIGRATE": True,
            "SERIALIZE": False,
            "DEPENDENCIES": [],
        },
    }
    connections.databases[TEST_TENANT_ALIAS] = settings.DATABASES[TEST_TENANT_ALIAS]

if TEST_DEMO_TENANT_ALIAS not in settings.DATABASES:
    settings.DATABASES[TEST_DEMO_TENANT_ALIAS] = {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": str(TEST_DEMO_TENANT_DB_PATH),
        "OPTIONS": {"timeout": 30},
        "ATOMIC_REQUESTS": False,
        "AUTOCOMMIT": True,
        "CONN_MAX_AGE": 0,
        "CONN_HEALTH_CHECKS": False,
        "TIME_ZONE": None,
        "TEST": {
            "NAME": str(TEST_DEMO_TENANT_DB_PATH),
            "MIRROR": None,
            "MIGRATE": True,
            "SERIALIZE": False,
            "DEPENDENCIES": [],
        },
    }
    connections.databases[TEST_DEMO_TENANT_ALIAS] = settings.DATABASES[TEST_DEMO_TENANT_ALIAS]


class OnboardingPlannerBootstrapTests(TestCase):
    databases = {"default", TEST_TENANT_ALIAS, TEST_DEMO_TENANT_ALIAS}

    def setUp(self):
        self.factory = RequestFactory()
        self.session_middleware = SessionMiddleware(lambda request: None)
        self.temp_root = TEST_TENANT_ROOT
        self.temp_root.mkdir(parents=True, exist_ok=True)
        self.tenant_db_path = TEST_TENANT_DB_PATH
        self.demo_tenant_db_path = TEST_DEMO_TENANT_DB_PATH
        _TENANT_SCHEMA_READY_ALIASES.discard(TEST_TENANT_ALIAS)
        _TENANT_SCHEMA_READY_ALIASES.discard(TEST_DEMO_TENANT_ALIAS)
        connections[TEST_TENANT_ALIAS].ensure_connection()
        connections[TEST_DEMO_TENANT_ALIAS].ensure_connection()
        call_command("flush", database=TEST_TENANT_ALIAS, interactive=False, verbosity=0)
        call_command("flush", database=TEST_DEMO_TENANT_ALIAS, interactive=False, verbosity=0)

    def tearDown(self):
        _TENANT_SCHEMA_READY_ALIASES.discard(TEST_TENANT_ALIAS)
        _TENANT_SCHEMA_READY_ALIASES.discard(TEST_DEMO_TENANT_ALIAS)
        super().tearDown()

    def _build_workbook_upload(self, name, headers, rows):
        wb = openpyxl.Workbook()
        sheet = wb.active
        sheet.append(headers)
        for row in rows:
            sheet.append(row)
        buffer = BytesIO()
        wb.save(buffer)
        return SimpleUploadedFile(
            name,
            buffer.getvalue(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    def _build_request(self, user, tenant_code, import_type, uploaded_file):
        request = self.factory.post(
            "/manufacturing/bulk-import/upload/",
            {
                "import_type": import_type,
                "next": "onboarding_data",
                "tenant_code": tenant_code,
                "file": uploaded_file,
            },
        )
        request.user = user
        request.tenant_db_alias = TEST_TENANT_ALIAS
        self.session_middleware.process_request(request)
        request.session["tenant_code"] = tenant_code
        request.session.save()
        setattr(request, "_messages", FallbackStorage(request))
        return request

    @patch("tenancy.services._ensure_postgres_database_exists")
    @patch("tenancy.services._build_tenant_db_url")
    def test_new_company_bulk_import_populates_planner_resources(self, mock_build_tenant_db_url, mock_ensure_postgres):
        mock_build_tenant_db_url.return_value = str(self.tenant_db_path)

        tenant, _company, _user = provision_tenant_with_owner(
            company_name="Tower",
            company_code="tower-live",
            owner_email="owner@tower.com",
            owner_password="TowerStrongPass123!",
        )

        user_model = get_user_model()
        tenant_user = user_model.objects.using(TEST_TENANT_ALIAS).get(username="owner@tower.com")

        machines_file = self._build_workbook_upload(
            "machines_template.xlsx",
            ["Machine Name", "Code", "Status", "Type"],
            [
                ["Filler", "FIL-01", "operational", "Fill"],
                ["Mixer", "MIX-01", "operational", "Mix"],
            ],
        )
        stages_file = self._build_workbook_upload(
            "stages_template.xlsx",
            ["Stage Name", "Machine Code", "Order", "Category", "Is QC", "Color"],
            [
                ["Fill Stage", "FIL-01", 1, "Fill", False, "#90CAF9"],
                ["Mix Stage", "MIX-01", 2, "Mix", False, "#90CAF9"],
            ],
        )

        machines_request = self._build_request(
            tenant_user,
            tenant.code,
            "machines",
            machines_file,
        )
        machines_response = HandleBulkImportView().post(machines_request)
        self.assertEqual(machines_response.status_code, 302)

        stages_request = self._build_request(
            tenant_user,
            tenant.code,
            "stages",
            stages_file,
        )
        stages_response = HandleBulkImportView().post(stages_request)
        self.assertEqual(stages_response.status_code, 302)

        ctx_token = set_current_tenant_db(TEST_TENANT_ALIAS)
        try:
            company = require_company(tenant_user)
            self.assertIsNotNone(company)
            self.assertEqual(Machine.objects.filter(company=company, is_active=True).count(), 2)
            self.assertEqual(ProductionStage.objects.filter(machine__company=company).distinct().count(), 2)

            dashboard_context = DashboardService.get_dashboard_context(
                company,
                viewer_role="planner",
                viewer=tenant_user,
            )
            timeline_payload = DashboardService.get_timeline_data(
                company,
                include_unscheduled=True,
                viewer_role="planner",
                viewer=tenant_user,
            )

            self.assertEqual(len(dashboard_context["machines_data"]), 2)
            self.assertEqual(len(timeline_payload["machines"]), 2)
            self.assertEqual({item["code"] for item in timeline_payload["machines"]}, {"FIL-01", "MIX-01"})

            dashboard_request = self.factory.get("/manufacturing/dashboard/")
            dashboard_request.user = tenant_user
            dashboard_request.tenant = tenant
            dashboard_request.tenant_db_alias = TEST_TENANT_ALIAS
            self.session_middleware.process_request(dashboard_request)
            dashboard_request.session["tenant_code"] = tenant.code
            dashboard_request.session["reset_planner_workspace_state"] = True
            dashboard_request.session["first_workspace_tour_pending"] = True
            dashboard_request.session.save()

            planner_view = PlannerDashboardView()
            planner_context = planner_view.get_context_data(dashboard_request, company)

            self.assertTrue(planner_context["reset_planner_workspace_state"])
            self.assertTrue(planner_context["first_workspace_tour"])
            self.assertNotIn("reset_planner_workspace_state", dashboard_request.session)
            self.assertNotIn("first_workspace_tour_pending", dashboard_request.session)
        finally:
            reset_current_tenant_db(ctx_token)

    @patch("tenancy.services._ensure_postgres_database_exists")
    @patch("tenancy.services._build_tenant_db_url")
    def test_stage_import_accepts_blank_machine_for_generic_routing_stage(self, mock_build_tenant_db_url, mock_ensure_postgres):
        mock_build_tenant_db_url.return_value = str(self.tenant_db_path)
        tenant, _company, _user = provision_tenant_with_owner(
            company_name="Generic Stage Co",
            company_code="tower-live",
            owner_email="owner@generic-stage.com",
            owner_password="GenericStrongPass123!",
        )
        tenant_user = get_user_model().objects.using(TEST_TENANT_ALIAS).get(username="owner@generic-stage.com")
        stages_file = self._build_workbook_upload(
            "stages_template.xlsx",
            ["Stage Name", "Machine Code", "Order", "Category", "Is QC", "Color"],
            [["Assembly", "", 1, "Assembly", False, "#90CAF9"]],
        )

        response = HandleBulkImportView().post(
            self._build_request(tenant_user, tenant.code, "stages", stages_file)
        )

        self.assertEqual(response.status_code, 302)
        ctx_token = set_current_tenant_db(TEST_TENANT_ALIAS)
        try:
            stage = ProductionStage.objects.get(name="Assembly")
            self.assertIsNone(stage.machine_id)
            self.assertEqual(stage.category, "Assembly")
            self.assertEqual(ProductionStage.objects.filter(name="Assembly").count(), 1)
        finally:
            reset_current_tenant_db(ctx_token)

    @patch("tenancy.services._ensure_postgres_database_exists")
    @patch("tenancy.services._build_tenant_db_url")
    def test_stage_import_matches_machine_name_when_code_cell_uses_name(self, mock_build_tenant_db_url, mock_ensure_postgres):
        mock_build_tenant_db_url.return_value = str(self.tenant_db_path)
        tenant, _company, _user = provision_tenant_with_owner(
            company_name="Machine Name Stage Co",
            company_code="tower-live",
            owner_email="owner@machine-name-stage.com",
            owner_password="MachineNameStrongPass123!",
        )
        tenant_user = get_user_model().objects.using(TEST_TENANT_ALIAS).get(username="owner@machine-name-stage.com")
        machines_file = self._build_workbook_upload(
            "machines_template.xlsx",
            ["Machine Name", "Code", "Status", "Type"],
            [["Main Cutter", "CUT-01", "operational", "Cutting"]],
        )
        stages_file = self._build_workbook_upload(
            "stages_template.xlsx",
            ["Stage Name", "Machine Code", "Order", "Category", "Is QC", "Color"],
            [["Cutting", "Main Cutter", 1, "Cutting", False, "#90CAF9"]],
        )

        self.assertEqual(
            HandleBulkImportView().post(self._build_request(tenant_user, tenant.code, "machines", machines_file)).status_code,
            302,
        )
        response = HandleBulkImportView().post(
            self._build_request(tenant_user, tenant.code, "stages", stages_file)
        )

        self.assertEqual(response.status_code, 302)
        ctx_token = set_current_tenant_db(TEST_TENANT_ALIAS)
        try:
            stage = ProductionStage.objects.get(name="Cutting")
            self.assertEqual(stage.machine.name, "Main Cutter")
            self.assertEqual(stage.machine.code, "CUT-01")
        finally:
            reset_current_tenant_db(ctx_token)

    @patch("tenancy.services._ensure_postgres_database_exists")
    @patch("tenancy.services._build_tenant_db_url")
    def test_production_bulk_import_creates_active_bom_and_store_gated_work_order(
        self,
        mock_build_tenant_db_url,
        mock_ensure_postgres,
    ):
        mock_build_tenant_db_url.return_value = str(self.tenant_db_path)
        tenant, company, _user = provision_tenant_with_owner(
            company_name="Bulk Production Co",
            company_code="tower-live",
            owner_email="owner@bulk-production.com",
            owner_password="BulkStrongPass123!",
        )
        tenant_user = get_user_model().objects.using(TEST_TENANT_ALIAS).get(username="owner@bulk-production.com")
        store_user = get_user_model().objects.db_manager(TEST_TENANT_ALIAS).create_user(
            username="store@bulk-production.com",
            password="StoreStrongPass123!",
        )
        store_role, _ = Role.objects.using(TEST_TENANT_ALIAS).get_or_create(name="store")
        Profile.objects.using(TEST_TENANT_ALIAS).filter(user_id=store_user.id).update(
            company=company,
            role=store_role,
            app_scope="store",
        )

        machines_file = self._build_workbook_upload(
            "machines_template.xlsx",
            ["Machine Name", "Code", "Status", "Type"],
            [["CNC Mill", "CNC-01", "operational", "CNC"]],
        )
        bom_file = self._build_workbook_upload(
            "bom_template.xlsx",
            ["Product Name", "Component Name", "Quantity", "Operation Name", "Duration (mins)", "Machine Code"],
            [
                ["Control Box", "Steel Sheet", 2, "Cutting", 30, "CNC-01"],
                ["Control Box", "Screws", 8, "Assembly", 20, ""],
            ],
        )
        work_orders_file = self._build_workbook_upload(
            "work_orders_template.xlsx",
            [
                "Product Name",
                "Quantity",
                "Start Date (YYYY-MM-DD)",
                "End Date (YYYY-MM-DD)",
                "Status (pending/in_progress)",
                "Assigned To (Email)",
            ],
            [["Control Box", 10, "2026-05-20", "2026-05-22", "pending", "owner@bulk-production.com"]],
        )

        self.assertEqual(
            HandleBulkImportView().post(self._build_request(tenant_user, tenant.code, "machines", machines_file)).status_code,
            302,
        )
        self.assertEqual(
            HandleBulkImportView().post(self._build_request(tenant_user, tenant.code, "bom", bom_file)).status_code,
            302,
        )
        self.assertEqual(
            HandleBulkImportView().post(
                self._build_request(tenant_user, tenant.code, "work_orders", work_orders_file)
            ).status_code,
            302,
        )

        ctx_token = set_current_tenant_db(TEST_TENANT_ALIAS)
        try:
            company = Company.objects.get(id=company.id)
            bom = BillOfMaterial.objects.get(product__company=company, product__name="Control Box")
            self.assertEqual(bom.status, "active")
            self.assertEqual(bom.version, "v1.0")
            self.assertEqual(BOMComponent.objects.filter(bom=bom).count(), 2)
            self.assertEqual(BOMOperation.objects.filter(bom=bom).count(), 2)

            work_order = WorkOrder.objects.get(company=company, product_name="Control Box")
            self.assertEqual(work_order.bom_id, bom.id)
            self.assertEqual(work_order.bom_version, "v1.0")
            self.assertEqual(work_order.material_readiness_status, "not_checked")
            self.assertEqual(work_order.status, "pending")
            self.assertEqual(work_order.quantity, 10)
            self.assertEqual(work_order.bom_snapshot["product_name"], "Control Box")
            self.assertEqual(
                Notification.objects.filter(title="Material check requested", message__contains=f"WO #{work_order.id}").count(),
                1,
            )
        finally:
            reset_current_tenant_db(ctx_token)

    @patch("tenancy.services._ensure_postgres_database_exists")
    @patch("tenancy.services._build_tenant_db_url")
    def test_work_order_bulk_import_rejects_products_without_active_bom(
        self,
        mock_build_tenant_db_url,
        mock_ensure_postgres,
    ):
        mock_build_tenant_db_url.return_value = str(self.tenant_db_path)
        tenant, _company, _user = provision_tenant_with_owner(
            company_name="Bulk Gate Co",
            company_code="tower-live",
            owner_email="owner@bulk-gate.com",
            owner_password="BulkGateStrongPass123!",
        )
        tenant_user = get_user_model().objects.using(TEST_TENANT_ALIAS).get(username="owner@bulk-gate.com")
        products_file = self._build_workbook_upload(
            "products_template.xlsx",
            ["Name", "Type", "Unit", "Description"],
            [["Loose Product", "finished", "pcs", "No BOM yet"]],
        )
        work_orders_file = self._build_workbook_upload(
            "work_orders_template.xlsx",
            ["Product Name", "Quantity", "Start Date", "End Date", "Status"],
            [["Loose Product", 5, "", "", "pending"]],
        )

        HandleBulkImportView().post(self._build_request(tenant_user, tenant.code, "products", products_file))
        HandleBulkImportView().post(self._build_request(tenant_user, tenant.code, "work_orders", work_orders_file))

        ctx_token = set_current_tenant_db(TEST_TENANT_ALIAS)
        try:
            self.assertFalse(WorkOrder.objects.filter(product_name="Loose Product").exists())
        finally:
            reset_current_tenant_db(ctx_token)

    @patch("tenancy.services._ensure_postgres_database_exists")
    @patch("tenancy.services._build_tenant_db_url")
    def test_registered_company_enters_setup_wizard_first(self, mock_build_tenant_db_url, mock_ensure_postgres):
        def fake_db_url(tenant_code, _db_alias):
            if tenant_code == "tower-live-demo":
                return str(self.demo_tenant_db_path)
            if tenant_code == "tower-live":
                return str(self.tenant_db_path)
            raise AssertionError(f"Unexpected tenant code: {tenant_code}")

        mock_build_tenant_db_url.side_effect = fake_db_url

        register_response = self.client.post(
            reverse("register_company"),
            {
                "company_name": "Tower",
                "company_code": "tower-live",
                "owner_email": "owner@tower.com",
                "owner_password": "TowerStrongPass123!",
            },
            follow=True,
        )

        self.assertEqual(register_response.status_code, 200)
        self.assertEqual(self.client.session["tenant_code"], "tower-live-demo")
        self.assertContains(register_response, "Initialize Your Factory", html=False)
        self.assertContains(register_response, "Production Setup Checklist", html=False)
        self.assertNotContains(register_response, "M-001", html=False)

        dashboard_response = self.client.get(reverse("dashboard"))
        planner_response = self.client.get(reverse("planner_dashboard"))

        self.assertEqual(dashboard_response.status_code, 302)
        self.assertEqual(dashboard_response.url, reverse("onboarding_data"))
        self.assertEqual(planner_response.status_code, 302)
        self.assertEqual(planner_response.url, reverse("onboarding_data"))

        timeline_response = self.client.get(reverse("get_timeline_data"), {"include_unscheduled": 1})
        self.assertEqual(timeline_response.status_code, 200)
        timeline_payload = json.loads(timeline_response.content)
        self.assertTrue(timeline_payload["success"])
        self.assertEqual(timeline_payload["machines"], [])

    @patch("tenancy.services._ensure_postgres_database_exists")
    @patch("tenancy.services._build_tenant_db_url")
    def test_public_signup_bulk_uploads_persist_in_clean_workspace(self, mock_build_tenant_db_url, mock_ensure_postgres):
        def fake_db_url(tenant_code, _db_alias):
            if tenant_code == "tower-live-demo":
                return str(self.demo_tenant_db_path)
            if tenant_code == "tower-live":
                return str(self.tenant_db_path)
            raise AssertionError(f"Unexpected tenant code: {tenant_code}")

        mock_build_tenant_db_url.side_effect = fake_db_url

        register_response = self.client.post(
            reverse("register_company"),
            {
                "company_name": "Tower",
                "company_code": "tower-live",
                "owner_email": "owner@tower.com",
                "owner_password": "TowerStrongPass123!",
            },
            follow=True,
        )

        self.assertEqual(register_response.status_code, 200)
        self.assertEqual(self.client.session["tenant_code"], "tower-live-demo")

        machines_file = self._build_workbook_upload(
            "machines_template.xlsx",
            ["Machine Name", "Code", "Status", "Type"],
            [
                ["Filler", "FIL-01", "operational", "Fill"],
                ["Mixer", "MIX-01", "operational", "Mix"],
            ],
        )
        stages_file = self._build_workbook_upload(
            "stages_template.xlsx",
            ["Stage Name", "Machine Code", "Order", "Category", "Is QC", "Color"],
            [
                ["Fill Stage", "FIL-01", 10, "Fill", False, "#90CAF9"],
                ["Mix Stage", "MIX-01", 20, "Mix", False, "#90CAF9"],
            ],
        )
        employees_file = self._build_workbook_upload(
            "employees_template.xlsx",
            ["Employee Name", "Email", "Role"],
            [
                ["Line Worker", "worker1@tower.com", "worker"],
            ],
        )

        machines_response = self.client.post(
            reverse("handle_bulk_import"),
            {
                "import_type": "machines",
                "next": "onboarding_data",
                "tenant_code": "tower-live-demo",
                "file": machines_file,
            },
        )
        self.assertEqual(machines_response.status_code, 302)
        self.assertEqual(machines_response.url, reverse("onboarding_data"))

        stages_response = self.client.post(
            reverse("handle_bulk_import"),
            {
                "import_type": "stages",
                "next": "onboarding_data",
                "tenant_code": "tower-live-demo",
                "file": stages_file,
            },
        )
        self.assertEqual(stages_response.status_code, 302)
        self.assertEqual(stages_response.url, reverse("onboarding_data"))

        team_screen = self.client.get(reverse("onboarding_users"))
        self.assertEqual(team_screen.status_code, 200)
        self.assertContains(team_screen, 'name="tenant_code" value="tower-live-demo"', html=False)

        employees_response = self.client.post(
            reverse("handle_bulk_import"),
            {
                "import_type": "employees",
                "next": "dashboard",
                "tenant_code": "tower-live-demo",
                "file": employees_file,
            },
        )
        self.assertEqual(employees_response.status_code, 302)
        self.assertEqual(employees_response.url, reverse("dashboard"))

        self.assertEqual(self.client.session["tenant_code"], "tower-live-demo")

        onboarding_data = self.client.get(reverse("onboarding_data"))
        self.assertEqual(onboarding_data.status_code, 200)
        self.assertEqual(onboarding_data.context["upload_counts"]["machines"], 2)
        self.assertEqual(onboarding_data.context["upload_counts"]["stages"], 2)
        self.assertEqual(onboarding_data.context["upload_counts"]["employees"], 2)

        demo_tenant_row = Tenant.objects.using("default").get(code="tower-live-demo")
        demo_alias = demo_tenant_row.db_alias
        demo_company_id = Profile.objects.using(demo_alias).get(
            user_id=get_user_model().objects.using(demo_alias).get(username="owner@tower.com").id
        ).company_id
        demo_company = Company.objects.using(demo_alias).get(id=demo_company_id)

        self.assertEqual(Machine.objects.using(demo_alias).filter(company=demo_company, is_active=True).count(), 2)
        self.assertEqual(ProductionStage.objects.using(demo_alias).filter(machine__company=demo_company).distinct().count(), 2)

        imported_employee = get_user_model().objects.using(demo_alias).get(username="worker1@tower.com")
        imported_profile = Profile.objects.using(demo_alias).get(user_id=imported_employee.id)
        self.assertEqual(imported_profile.company_id, demo_company.id)

        dashboard_response = self.client.get(reverse("dashboard"))
        self.assertEqual(dashboard_response.status_code, 302)
        self.assertEqual(dashboard_response.url, reverse("onboarding_data"))

    @patch("tenancy.services._ensure_postgres_database_exists")
    @patch("tenancy.services._build_tenant_db_url")
    def test_timeline_api_resolves_company_from_active_tenant_context(self, mock_build_tenant_db_url, mock_ensure_postgres):
        mock_build_tenant_db_url.return_value = str(self.tenant_db_path)

        tenant, company, _user = provision_tenant_with_owner(
            company_name="Tower",
            company_code="tower-live",
            owner_email="owner@tower.com",
            owner_password="TowerStrongPass123!",
        )

        ctx_token = set_current_tenant_db(TEST_TENANT_ALIAS)
        try:
            Machine.objects.create(
                company=company,
                name="Filler",
                code="FIL-01",
                status="operational",
                type="Fill",
                category="Fill",
                is_active=True,
            )
            tenant_user = get_user_model().objects.using(TEST_TENANT_ALIAS).get(username="owner@tower.com")
            tenant_user._state.db = "default"

            request = self.factory.get("/manufacturing/api/timeline/?include_unscheduled=1")
            request.user = tenant_user
            request.tenant = tenant
            request.tenant_db_alias = TEST_TENANT_ALIAS

            response = TimelineDataView().get(request)
        finally:
            reset_current_tenant_db(ctx_token)

        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.content)
        self.assertTrue(payload["success"])
        self.assertEqual([item["code"] for item in payload["machines"]], ["FIL-01"])
