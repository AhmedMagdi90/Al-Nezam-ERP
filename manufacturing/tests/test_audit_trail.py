import json
from datetime import timedelta

from django.apps import apps
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from manufacturing.models import BillOfMaterial, Machine, Product, ProductionLog, ShiftAssignment, SystemSettings, WorkOrder
from manufacturing.tests.utils import create_company, create_user_with_role
from manufacturing.work_order_visibility import get_current_shift_window_for_company


AuditLog = apps.get_model("manufacturing", "AuditLog")


class AuditTrailTests(TestCase):
    def setUp(self):
        self.company = create_company("Audit Company")
        self.planner = create_user_with_role("planner_audit", "planner", self.company)
        self.supervisor = create_user_with_role("supervisor_audit", "supervisor", self.company)
        self.worker = create_user_with_role("worker_audit", "worker", self.company)

        self.product = Product.objects.create(name="Audit Product", company=self.company)
        self.bom = BillOfMaterial.objects.create(
            product=self.product,
            status="active",
            base_quantity=1,
        )
        self.machine = Machine.objects.create(
            name="Audit Machine",
            code="AUD-01",
            status="operational",
            company=self.company,
        )
        settings, _ = SystemSettings.objects.get_or_create(company=self.company)
        now_local = timezone.localtime()
        settings.shift_mode = "1"
        settings.shift_configuration = {
            "morning": {
                "enabled": True,
                "start": (now_local - timedelta(hours=1)).strftime("%H:%M"),
                "end": (now_local + timedelta(hours=6)).strftime("%H:%M"),
            },
            "afternoon": {"enabled": False, "start": "14:00", "end": "22:00"},
            "night": {"enabled": False, "start": "22:00", "end": "06:00"},
        }
        settings.save(update_fields=["shift_mode", "shift_configuration"])
        shift_window = get_current_shift_window_for_company(self.company)
        for user in (self.supervisor, self.worker):
            ShiftAssignment.objects.create(
                worker=user,
                machine=self.machine,
                shift_type=shift_window["shift_type"],
                date=shift_window["assignment_date"],
                created_by=self.planner,
            )

    def test_create_work_order_writes_audit_log(self):
        client = Client()
        client.force_login(self.planner)

        response = client.post(
            reverse("api_create_work_order"),
            data=json.dumps({
                "bom_id": self.bom.id,
                "quantity": 12,
                "priority": "High",
            }),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200, response.content.decode())
        payload = response.json()
        self.assertTrue(payload["success"])

        entry = AuditLog.objects.latest("timestamp")
        self.assertEqual(entry.user_id, self.planner.id)
        self.assertEqual(entry.company_id, self.company.id)
        self.assertEqual(entry.action, "create")
        self.assertEqual(entry.model_name, "WorkOrder")
        self.assertEqual(entry.object_id, payload["wo_id"])
        self.assertEqual(entry.details.get("event"), "work_order_created")

    def test_schedule_work_order_writes_audit_log(self):
        client = Client()
        client.force_login(self.planner)
        work_order = WorkOrder.objects.create(
            product_name="Audit Batch",
            quantity=10,
            status="pending",
            company=self.company,
            material_readiness_status="ready",
            material_available_qty=10,
            material_available_percent=100,
        )
        start_at = timezone.now() + timedelta(hours=2)

        response = client.post(
            reverse("api_schedule_work_order", args=[work_order.id]),
            data=json.dumps({
                "machine_id": self.machine.id,
                "start_date": start_at.isoformat(),
            }),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200, response.content.decode())
        payload = response.json()
        self.assertTrue(payload["success"])

        entry = AuditLog.objects.latest("timestamp")
        self.assertEqual(entry.user_id, self.planner.id)
        self.assertEqual(entry.action, "update")
        self.assertEqual(entry.model_name, "WorkOrder")
        self.assertEqual(entry.object_id, work_order.id)
        self.assertEqual(entry.details.get("event"), "work_order_scheduled")
        self.assertEqual(entry.details.get("machine_id"), self.machine.id)

    def test_log_production_writes_audit_log(self):
        client = Client()
        client.force_login(self.worker)
        work_order = WorkOrder.objects.create(
            product_name="Worker Batch",
            quantity=20,
            status="in_progress",
            company=self.company,
            machine=self.machine,
            assigned_worker=self.worker,
            assignment_type="manual",
            start_date=timezone.now(),
        )

        response = client.post(
            reverse("log_production"),
            data=json.dumps({
                "work_order_id": work_order.id,
                "quantity": 5,
                "shift": "morning",
                "note": "Audit output",
            }),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200, response.content.decode())
        payload = response.json()
        self.assertTrue(payload["success"])

        entry = AuditLog.objects.latest("timestamp")
        self.assertEqual(entry.user_id, self.worker.id)
        self.assertEqual(entry.action, "create")
        self.assertEqual(entry.model_name, "ProductionLog")
        self.assertEqual(entry.object_id, payload["log_id"])
        self.assertEqual(entry.details.get("event"), "production_logged")
        self.assertEqual(entry.details.get("work_order_id"), work_order.id)

    def test_approve_log_writes_audit_log(self):
        client = Client()
        client.force_login(self.supervisor)
        work_order = WorkOrder.objects.create(
            product_name="Approval Batch",
            quantity=30,
            status="in_progress",
            company=self.company,
            machine=self.machine,
            assigned_worker=self.worker,
            assignment_type="manual",
            start_date=timezone.now(),
        )
        log = ProductionLog.objects.create(
            work_order=work_order,
            worker=self.worker,
            quantity=7,
            shift="morning",
            status="pending",
        )

        response = client.post(
            reverse("approve_log", args=[log.id]),
            data={"action": "approve"},
        )

        self.assertEqual(response.status_code, 200, response.content.decode())
        self.assertTrue(response.json()["success"])

        entry = AuditLog.objects.latest("timestamp")
        self.assertEqual(entry.user_id, self.supervisor.id)
        self.assertEqual(entry.action, "approve")
        self.assertEqual(entry.model_name, "ProductionLog")
        self.assertEqual(entry.object_id, log.id)
        self.assertEqual(entry.details.get("event"), "production_log_approved")
        self.assertEqual(entry.details.get("work_order_id"), work_order.id)

    def test_assign_worker_to_wo_view_writes_audit_log(self):
        client = Client()
        client.force_login(self.supervisor)
        work_order = WorkOrder.objects.create(
            product_name="Assignment Batch",
            quantity=15,
            status="pending",
            company=self.company,
            machine=self.machine,
            start_date=timezone.now(),
        )

        response = client.post(
            reverse("assign_worker_to_wo"),
            data=json.dumps({
                "wo_id": work_order.id,
                "worker_id": self.worker.id,
                "notes": "Assign from legacy supervisor flow",
            }),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200, response.content.decode())
        self.assertTrue(response.json()["success"])

        entry = AuditLog.objects.latest("timestamp")
        self.assertEqual(entry.user_id, self.supervisor.id)
        self.assertEqual(entry.company_id, self.company.id)
        self.assertEqual(entry.action, "update")
        self.assertEqual(entry.model_name, "WorkOrder")
        self.assertEqual(entry.object_id, work_order.id)
        self.assertEqual(entry.details.get("event"), "worker_assigned")
        self.assertEqual(entry.details.get("worker_id"), self.worker.id)

    def test_work_order_log_api_includes_audit_history(self):
        client = Client()
        client.force_login(self.planner)
        work_order = WorkOrder.objects.create(
            product_name="History Batch",
            quantity=8,
            status="pending",
            company=self.company,
            machine=self.machine,
        )

        AuditLog.objects.create(
            user=self.supervisor,
            action="update",
            model_name="WorkOrder",
            object_id=work_order.id,
            object_repr=str(work_order),
            company=self.company,
            details={
                "event": "worker_assigned",
                "work_order_id": work_order.id,
                "machine_id": self.machine.id,
                "worker_username": self.worker.username,
            },
        )

        response = client.get(reverse("api_work_order_log", args=[work_order.id]))

        self.assertEqual(response.status_code, 200, response.content.decode())
        payload = response.json()
        self.assertTrue(payload["success"])
        logs = payload.get("logs") or []
        self.assertTrue(any(log.get("source") == "audit" for log in logs))
        self.assertTrue(any(log.get("event") == "worker_assigned" for log in logs))
        self.assertTrue(any("Worker:" in (log.get("details") or "") for log in logs))

    def test_machine_log_api_returns_machine_history(self):
        client = Client()
        client.force_login(self.planner)
        work_order = WorkOrder.objects.create(
            product_name="Machine History Batch",
            quantity=9,
            status="in_progress",
            company=self.company,
            machine=self.machine,
            assigned_worker=self.worker,
            assignment_type="manual",
            start_date=timezone.now(),
        )

        AuditLog.objects.create(
            user=self.supervisor,
            action="update",
            model_name="WorkOrder",
            object_id=work_order.id,
            object_repr=str(work_order),
            company=self.company,
            details={
                "event": "worker_assigned",
                "work_order_id": work_order.id,
                "machine_id": self.machine.id,
                "worker_username": self.worker.username,
                "status": "pending",
            },
        )

        response = client.get(reverse("api_machine_log", args=[self.machine.id]))

        self.assertEqual(response.status_code, 200, response.content.decode())
        payload = response.json()
        self.assertTrue(payload["success"])
        self.assertEqual(payload["machine"]["id"], self.machine.id)
        logs = payload.get("logs") or []
        self.assertTrue(logs)
        self.assertTrue(any(log.get("event") == "worker_assigned" for log in logs))
        self.assertTrue(any("WO:" in (log.get("details") or "") for log in logs))

    def test_work_order_log_api_uses_readable_audit_history_labels(self):
        client = Client()
        client.force_login(self.planner)
        work_order = WorkOrder.objects.create(
            product_name="Readable History Batch",
            quantity=12,
            status="pending",
            company=self.company,
            machine=self.machine,
        )

        AuditLog.objects.create(
            user=self.planner,
            action="update",
            model_name="WorkOrder",
            object_id=work_order.id,
            object_repr=str(work_order),
            company=self.company,
            details={
                "event": "material_readiness_updated",
                "work_order_id": work_order.id,
                "status": "partial",
                "available_qty": 6,
                "available_percent": "50.00",
                "expected_delivery_date": "2026-06-20",
                "shortage_note": "Only half available",
            },
        )

        response = client.get(reverse("api_work_order_log", args=[work_order.id]))

        self.assertEqual(response.status_code, 200, response.content.decode())
        logs = response.json().get("logs") or []
        readable_entry = next(log for log in logs if log.get("event") == "material_readiness_updated")
        self.assertEqual(readable_entry["action"], "Material Readiness Updated")
        self.assertIn("Status: Partially OK", readable_entry["details"])
        self.assertIn("Available Qty: 6", readable_entry["details"])
        self.assertIn("Available %: 50.00", readable_entry["details"])
        self.assertIn("Expected Delivery: 2026-06-20", readable_entry["details"])
        self.assertIn("Material Note: Only half available", readable_entry["details"])
