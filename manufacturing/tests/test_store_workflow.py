import json
from datetime import timedelta

from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from manufacturing.models import (
    BOMComponent,
    BOMOperation,
    BillOfMaterial,
    Machine,
    Product,
    ProductionStage,
    WorkOrder,
)
from manufacturing.tests.utils import create_company, create_user_with_role


class StoreWorkflowTests(TestCase):
    def setUp(self):
        self.company = create_company("Store Workflow Co")
        self.planner = create_user_with_role("planner_store_flow", "planner", self.company)
        self.store = create_user_with_role("store_flow", "store", self.company)
        self.product = Product.objects.create(
            company=self.company,
            name="Store Product",
            unit="pcs",
            material_type="finished",
        )
        self.raw = Product.objects.create(
            company=self.company,
            name="Store Raw",
            unit="kg",
            material_type="raw",
        )
        self.stage = ProductionStage.objects.create(name="Cutting", category="CNC", order=1)
        self.machine = Machine.objects.create(
            company=self.company,
            name="CNC Store",
            code="CNC-S",
            category="CNC",
            status="operational",
        )
        self.bom = BillOfMaterial.objects.create(
            product=self.product,
            status="draft",
            base_quantity=10,
            created_by=self.planner,
        )
        BOMComponent.objects.create(
            bom=self.bom,
            product=self.raw,
            material_name=self.raw.name,
            quantity=5,
            unit="kg",
        )
        BOMOperation.objects.create(
            bom=self.bom,
            stage=self.stage,
            machine=self.machine,
            order=10,
            duration_minutes=60,
        )
        self.bom.status = "active"
        self.bom.save(update_fields=["status"])

    def _make_wo(self, quantity=10, status="pending"):
        return WorkOrder.objects.create(
            company=self.company,
            product_name=self.product.name,
            bom=self.bom,
            quantity=quantity,
            status=status,
        )

    def test_store_partial_material_response_blocks_full_plan_but_allows_available_qty(self):
        wo = self._make_wo(quantity=10)
        client = Client()
        client.force_login(self.store)

        response = client.post(
            reverse("api_work_order_material_readiness", args=[wo.id]),
            data=json.dumps({
                "status": "partial",
                "available_percent": 40,
                "expected_delivery_date": "2026-06-20",
                "note": "Missing one item",
            }),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        wo.refresh_from_db()
        self.assertEqual(wo.material_readiness_status, "partial")
        self.assertEqual(wo.material_available_qty, 4)
        self.assertEqual(float(wo.material_available_percent), 40.0)
        self.assertEqual(wo.material_expected_delivery_date.isoformat(), "2026-06-20")

        client.force_login(self.planner)
        start = timezone.now() + timedelta(hours=1)
        payload = {
            "stage_id": self.stage.id,
            "machine_id": self.machine.id,
            "start_date": start.isoformat(),
            "route_assignments": [
                {
                    "stage_id": self.stage.id,
                    "machine_id": self.machine.id,
                    "selection_mode": "manual",
                    "start_date": start.isoformat(),
                }
            ],
        }
        blocked = client.post(
            reverse("api_schedule_work_order", args=[wo.id]),
            data=json.dumps(payload),
            content_type="application/json",
        )
        self.assertEqual(blocked.status_code, 409)
        self.assertTrue(blocked.json()["requires_store_material_action"])

        payload["quantity"] = 4
        allowed = client.post(
            reverse("api_schedule_work_order", args=[wo.id]),
            data=json.dumps(payload),
            content_type="application/json",
        )
        self.assertEqual(allowed.status_code, 200)
        self.assertTrue(allowed.json()["success"])

    def test_store_receipt_is_required_before_planner_close(self):
        wo = self._make_wo(quantity=10, status="completed")
        wo.planner_action_required = True
        wo.store_receipt_status = "pending"
        wo.store_receipt_requested_at = timezone.now()
        wo.save(update_fields=["planner_action_required", "store_receipt_status", "store_receipt_requested_at"])

        client = Client()
        client.force_login(self.planner)
        blocked = client.post(reverse("api_close_work_order", args=[wo.id]))
        self.assertEqual(blocked.status_code, 400)
        self.assertIn("Store must confirm", blocked.json()["error"])

        client.force_login(self.store)
        receipt = client.post(
            reverse("api_store_receipt_confirm", args=[wo.id]),
            data=json.dumps({"received_qty": 9, "scrap_qty": 1, "note": "One damaged"}),
            content_type="application/json",
        )
        self.assertEqual(receipt.status_code, 200)

        client.force_login(self.planner)
        closed = client.post(reverse("api_close_work_order", args=[wo.id]))
        self.assertEqual(closed.status_code, 200)
        wo.refresh_from_db()
        self.assertTrue(wo.closed_by_planner)

    def test_store_dashboard_uses_store_only_shell_for_admin_access(self):
        admin = create_user_with_role("admin_store_shell", "admin", self.company)
        client = Client()
        client.force_login(admin)

        response = client.get(reverse("store_dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["current_role_name"], "store")
        self.assertTrue(response.context["store_shell"])
        self.assertContains(response, "Store Workspace")
        self.assertNotContains(response, 'data-system-tour-target="factory"')
        self.assertNotContains(response, 'data-system-tour-target="reports"')
        self.assertNotContains(response, 'data-system-tour-target="settings"')
