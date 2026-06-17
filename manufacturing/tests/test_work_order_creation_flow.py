import json
from pathlib import Path

from django.conf import settings
from django.test import Client, TestCase
from django.urls import reverse

from manufacturing.models import BillOfMaterial, BOMComponent, Product, SystemSettings, WorkOrder
from manufacturing.tests.utils import create_company, create_user_with_role


class WorkOrderCreationFlowTests(TestCase):
    def setUp(self):
        self.company = create_company()
        self.planner = create_user_with_role("planner_create", "planner", self.company)
        self.client = Client()
        self.client.force_login(self.planner)

        self.product = Product.objects.create(name="Create Flow Product", company=self.company)
        self.bom = BillOfMaterial.objects.create(
            product=self.product,
            status="active",
            base_quantity=1,
        )

    def test_single_work_order_defaults_to_pending(self):
        response = self.client.post(
            reverse("api_create_work_order"),
            data=json.dumps({
                "bom_id": self.bom.id,
                "quantity": 12,
                "priority": "Normal",
            }),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["success"])
        self.assertEqual(payload["status"], "pending")

        work_order = WorkOrder.objects.get(id=payload["wo_id"])
        self.assertEqual(work_order.status, "pending")
        self.assertIsNone(work_order.machine)
        self.assertIsNone(work_order.start_date)

    def test_work_order_model_no_longer_exposes_draft_status(self):
        status_values = {value for value, _label in WorkOrder.STATUS_CHOICES}

        self.assertNotIn("draft", status_values)
        self.assertEqual(WorkOrder._meta.get_field("status").default, "pending")

    def test_single_work_order_draft_request_is_normalized_to_pending(self):
        response = self.client.post(
            reverse("api_create_work_order"),
            data=json.dumps({
                "bom_id": self.bom.id,
                "quantity": 5,
                "status": "draft",
            }),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "pending")

    def test_single_work_order_uses_company_default_operation_flow_mode(self):
        settings, _ = SystemSettings.objects.get_or_create(company=self.company)
        settings.default_operation_flow_mode = "parallel"
        settings.save(update_fields=["default_operation_flow_mode"])

        response = self.client.post(
            reverse("api_create_work_order"),
            data=json.dumps({
                "bom_id": self.bom.id,
                "quantity": 7,
            }),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        work_order = WorkOrder.objects.get(id=payload["wo_id"])
        self.assertEqual(work_order.operation_flow_mode, "parallel")

    def test_single_work_order_captures_bom_snapshot(self):
        self.bom.status = "draft"
        self.bom.save(update_fields=["status"])
        BOMComponent.objects.create(
            bom=self.bom,
            material_name="Snapshot Steel",
            quantity=2,
            unit="kg",
            cost_per_unit=3,
        )
        self.bom.status = "active"
        self.bom.save(update_fields=["status"])

        response = self.client.post(
            reverse("api_create_work_order"),
            data=json.dumps({
                "bom_id": self.bom.id,
                "quantity": 12,
            }),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200, response.content.decode())
        payload = response.json()
        work_order = WorkOrder.objects.get(id=payload["wo_id"])
        self.assertEqual(work_order.bom_version, self.bom.version)
        self.assertEqual(work_order.bom_snapshot["bom_id"], self.bom.id)
        self.assertEqual(work_order.bom_snapshot["components"][0]["material_name"], "Snapshot Steel")


class PlannerDashboardCreateWorkOrderEntryTests(TestCase):
    def setUp(self):
        self.company = create_company()
        self.planner = create_user_with_role("planner_dashboard_create", "planner", self.company)
        self.client = Client()
        self.client.force_login(self.planner)

    def test_schedule_header_exposes_create_work_order_trigger(self):
        template_path = (
            Path(settings.BASE_DIR)
            / "templates"
            / "manufacturing"
            / "planner_dashboard.html"
        )
        template_source = template_path.read_text(encoding="utf-8")

        self.assertIn('data-schedule-create-wo="true"', template_source)
        self.assertIn("$dispatch('open-create-work-order')", template_source)

    def test_pending_work_order_card_renders_plain_product_name_template_tag(self):
        template_path = (
            Path(settings.BASE_DIR)
            / "templates"
            / "manufacturing"
            / "planner_dashboard.html"
        )
        template_source = template_path.read_text(encoding="utf-8")

        self.assertIn('{{ wo.product_name|default:"Work Order" }}', template_source)
        self.assertNotIn('wo.product_name|default:wo.bom.product.name|default:"Work Order"', template_source)

    def test_schedule_layout_standardizes_queue_rail_at_lg_breakpoint(self):
        template_path = (
            Path(settings.BASE_DIR)
            / "templates"
            / "manufacturing"
            / "planner_dashboard.html"
        )
        template_source = template_path.read_text(encoding="utf-8")

        self.assertIn('grid grid-cols-1 lg:grid-cols-12', template_source)
        self.assertIn('class="lg:col-span-9', template_source)
        self.assertIn('class="lg:col-span-3', template_source)
        self.assertNotIn('grid grid-cols-1 xl:grid-cols-12', template_source)
