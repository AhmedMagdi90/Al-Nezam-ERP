import json
from datetime import timedelta

from django.apps import apps
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
    ShiftAssignment,
    WorkOrder,
)
from manufacturing.services import DashboardService
from manufacturing.tests.utils import create_company, create_user_with_role
from manufacturing.work_order_visibility import get_current_shift_window_for_company


class MaterialReadinessTests(TestCase):
    def setUp(self):
        self.company = create_company("Material Readiness Co")
        self.planner = create_user_with_role("planner_material", "planner", self.company)
        self.supervisor = create_user_with_role("supervisor_material", "supervisor", self.company)
        self.worker = create_user_with_role("worker_material", "worker", self.company)
        self.product = Product.objects.create(
            company=self.company,
            name="Control Box",
            unit="pcs",
            material_type="finished",
        )
        self.raw = Product.objects.create(
            company=self.company,
            name="Steel Sheet",
            unit="kg",
            material_type="raw",
        )
        self.stage = ProductionStage.objects.create(name="Cutting", category="CNC", order=1)
        self.machine = Machine.objects.create(
            company=self.company,
            name="CNC 01",
            code="CNC-01",
            category="CNC",
            status="operational",
        )
        shift_window = get_current_shift_window_for_company(self.company)
        ShiftAssignment.objects.create(
            worker=self.worker,
            machine=self.machine,
            shift_type=shift_window["shift_type"],
            date=shift_window["assignment_date"],
            created_by=self.planner,
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
            quantity=20,
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
        self.wo = WorkOrder.objects.create(
            company=self.company,
            product_name=self.product.name,
            bom=self.bom,
            quantity=30,
            status="pending",
        )

    def test_planner_updates_material_readiness_and_detail_payload_scales_bom(self):
        client = Client()
        client.force_login(self.planner)

        response = client.post(
            reverse("api_work_order_material_readiness", args=[self.wo.id]),
            data=json.dumps({"status": "shortage", "shortage_note": "Steel sheet missing"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.wo.refresh_from_db()
        self.assertEqual(self.wo.material_readiness_status, "shortage")
        self.assertEqual(self.wo.material_shortage_note, "Steel sheet missing")

        detail = client.get(reverse("get_work_order", args=[self.wo.id]))
        payload = detail.json()["work_order"]["material_readiness"]
        self.assertEqual(payload["status"], "shortage")
        self.assertEqual(payload["materials"][0]["name"], "Steel Sheet")
        self.assertEqual(payload["materials"][0]["required_quantity"], 60.0)

    def test_ready_material_confirmation_moves_work_order_to_planner_pending_queue(self):
        self.wo.status = "pending"
        self.wo.save(update_fields=["status"])
        client = Client()
        client.force_login(self.planner)

        response = client.post(
            reverse("api_work_order_material_readiness", args=[self.wo.id]),
            data=json.dumps({"status": "ready"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200, response.content.decode())
        self.wo.refresh_from_db()
        self.assertEqual(self.wo.material_readiness_status, "ready")
        self.assertEqual(self.wo.material_available_qty, self.wo.quantity)
        self.assertEqual(float(self.wo.material_available_percent), 100.0)

        context = DashboardService.get_dashboard_context(
            self.company,
            viewer_role="planner",
            viewer=self.planner,
        )
        pending_ids = list(context["pending_wos"].values_list("id", flat=True))
        material_action_ids = [wo.id for wo in context["material_actions"]]
        queue_row = next(row for row in context["pending_wos_queue"] if row["action_wo_id"] == self.wo.id)
        intake_row = next(row for row in context["intake_orders_data"] if row["id"] == self.wo.id)

        self.assertIn(self.wo.id, pending_ids)
        self.assertNotIn(self.wo.id, material_action_ids)
        self.assertEqual(queue_row["category"], "ready_plan")
        self.assertEqual(queue_row["status_label"], "Ready to Plan")
        self.assertEqual(queue_row["action_label"], "Open Plan")
        self.assertEqual(context["pending_wos_queue_counts"]["ready_plan"], 1)
        self.assertEqual(intake_row["material_readiness_status"], "ready")
        self.assertTrue(intake_row["material_readiness"]["can_plan"])
        self.assertEqual(intake_row["material_readiness"]["status_label"], "Material OK")
        self.assertEqual(
            intake_row["material_readiness"]["planner_next_action"],
            "Schedule or release the full work order quantity.",
        )

    def test_partial_material_confirmation_returns_clear_planner_next_action(self):
        client = Client()
        client.force_login(self.planner)

        response = client.post(
            reverse("api_work_order_material_readiness", args=[self.wo.id]),
            data=json.dumps({
                "status": "partial",
                "available_percent": 40,
                "expected_delivery_date": "2026-06-15",
                "note": "Only enough steel for first lot",
            }),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200, response.content.decode())
        self.wo.refresh_from_db()
        self.assertEqual(self.wo.material_available_qty, 12)
        self.assertEqual(float(self.wo.material_available_percent), 40.0)
        self.assertEqual(self.wo.material_expected_delivery_date.isoformat(), "2026-06-15")
        payload = response.json()["material_readiness"]
        self.assertEqual(payload["status"], "partial")
        self.assertEqual(payload["status_label"], "Partially OK")
        self.assertEqual(payload["available_qty"], 12)
        self.assertEqual(payload["available_percent"], 40.0)
        self.assertEqual(payload["shortfall_qty"], 18)
        self.assertEqual(payload["expected_delivery_date"], "2026-06-15")
        self.assertEqual(
            payload["planner_next_action"],
            "Split or reduce the work order before scheduling the shortfall quantity.",
        )

    def test_supervisor_cannot_update_material_readiness(self):
        client = Client()
        client.force_login(self.supervisor)

        response = client.post(
            reverse("api_work_order_material_readiness", args=[self.wo.id]),
            data=json.dumps({"status": "ready"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 403)

    def test_material_shortage_blocks_scheduling_until_store_updates_status(self):
        self.wo.material_readiness_status = "shortage"
        self.wo.material_shortage_note = "Steel sheet missing"
        self.wo.save(update_fields=["material_readiness_status", "material_shortage_note"])
        client = Client()
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
            reverse("api_schedule_work_order", args=[self.wo.id]),
            data=json.dumps(payload),
            content_type="application/json",
        )
        self.assertEqual(blocked.status_code, 409)
        self.assertTrue(blocked.json()["requires_store_material_action"])

        payload["material_shortage_acknowledged"] = True
        still_blocked = client.post(
            reverse("api_schedule_work_order", args=[self.wo.id]),
            data=json.dumps(payload),
            content_type="application/json",
        )
        self.assertEqual(still_blocked.status_code, 409)

        self.wo.material_readiness_status = "ready"
        self.wo.material_available_qty = self.wo.quantity
        self.wo.save(update_fields=["material_readiness_status", "material_available_qty"])

        allowed = client.post(
            reverse("api_schedule_work_order", args=[self.wo.id]),
            data=json.dumps(payload),
            content_type="application/json",
        )
        self.assertEqual(allowed.status_code, 200)
        self.assertTrue(allowed.json()["success"])

    def test_worker_start_is_blocked_by_material_shortage_and_audited(self):
        self.wo.status = "pending"
        self.wo.machine = self.machine
        self.wo.assigned_worker = self.worker
        self.wo.start_date = timezone.now() - timedelta(minutes=5)
        self.wo.material_readiness_status = "shortage"
        self.wo.material_shortage_note = "Steel sheet missing"
        self.wo.save(
            update_fields=[
                "status",
                "machine",
                "assigned_worker",
                "start_date",
                "material_readiness_status",
                "material_shortage_note",
            ]
        )
        client = Client()
        client.force_login(self.worker)

        response = client.post(
            reverse("api_update_work_order_status", args=[self.wo.id]),
            data=json.dumps({"status": "in_progress"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 409)
        payload = response.json()
        self.assertEqual(payload["reason_code"], "material_shortage")
        self.wo.refresh_from_db()
        self.assertEqual(self.wo.status, "pending")
        AuditLog = apps.get_model("manufacturing", "AuditLog")
        self.assertTrue(
            AuditLog.objects.filter(
                company=self.company,
                details__event="production_start_blocked",
                details__reason_code="material_shortage",
            ).exists()
        )

    def test_worker_start_ready_work_order_sets_in_progress_and_audits(self):
        self.wo.status = "pending"
        self.wo.machine = self.machine
        self.wo.assigned_worker = self.worker
        self.wo.start_date = timezone.now() - timedelta(minutes=5)
        self.wo.material_readiness_status = "ready"
        self.wo.save(
            update_fields=[
                "status",
                "machine",
                "assigned_worker",
                "start_date",
                "material_readiness_status",
            ]
        )
        client = Client()
        client.force_login(self.worker)

        response = client.post(
            reverse("api_update_work_order_status", args=[self.wo.id]),
            data=json.dumps({"status": "in_progress"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.wo.refresh_from_db()
        self.assertEqual(self.wo.status, "in_progress")
        self.assertIsNotNone(self.wo.worker_start_at)
        AuditLog = apps.get_model("manufacturing", "AuditLog")
        self.assertTrue(
            AuditLog.objects.filter(
                company=self.company,
                details__event="production_start_approved",
            ).exists()
        )
