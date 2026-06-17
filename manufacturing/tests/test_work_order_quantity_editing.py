import json
from datetime import timedelta

from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from manufacturing.models import (
    BOMOperation,
    BillOfMaterial,
    Machine,
    Product,
    ProductionLog,
    ProductionStage,
    WorkOrder,
)
from manufacturing.tests.utils import create_company, create_user_with_role


class WorkOrderQuantityEditingTests(TestCase):
    def setUp(self):
        self.company = create_company("Quantity Edit Co")
        self.planner = create_user_with_role("planner_quantity_edit", "planner", self.company)
        self.worker = create_user_with_role("worker_quantity_edit", "worker", self.company)
        self.client = Client()
        self.client.force_login(self.planner)

        self.machine = Machine.objects.create(
            name="Assembly Machine",
            code="ASM-01",
            type="Assembly",
            category="Assembly",
            status="operational",
            company=self.company,
        )
        self.stage = ProductionStage.objects.create(
            name="Op 10: Assembly",
            category="Assembly",
            machine=self.machine,
            order=1,
        )
        self.product = Product.objects.create(name="Qty Product", company=self.company)
        self.bom = BillOfMaterial.objects.create(product=self.product, status="active", base_quantity=1)
        BOMOperation.objects.create(
            bom=self.bom,
            stage=self.stage,
            machine=self.machine,
            machine_type="Assembly",
            order=1,
            setup_time=5,
            run_time=10,
            duration_minutes=60,
        )
        self.pack_machine = Machine.objects.create(
            name="Packing Machine",
            code="PACK-QTY-01",
            type="Packing",
            category="Packing",
            status="operational",
            company=self.company,
        )
        self.pack_stage = ProductionStage.objects.create(
            name="Op 20: Packing",
            category="Packing",
            machine=self.pack_machine,
            order=2,
        )
        BOMOperation.objects.create(
            bom=self.bom,
            stage=self.pack_stage,
            machine=self.pack_machine,
            machine_type="Packing",
            order=2,
            setup_time=0,
            run_time=5,
            duration_minutes=30,
        )

    def _create_work_order(self, *, quantity=10, parent=None, status="draft"):
        return WorkOrder.objects.create(
            company=self.company,
            product_name=self.product.name,
            bom=self.bom,
            quantity=quantity,
            base_quantity=quantity,
            status=status,
            machine=self.machine,
            stage=self.stage,
            current_stage=self.stage,
            parent=parent,
            assigned_to=self.planner,
            start_date=timezone.now(),
        )

    def test_work_order_detail_exposes_minimum_editable_quantity(self):
        work_order = self._create_work_order(quantity=12)
        ProductionLog.objects.create(
            work_order=work_order,
            worker=self.worker,
            quantity=5,
            status="pending",
        )

        response = self.client.get(reverse("api_workorder_detail", args=[work_order.id]))

        self.assertEqual(response.status_code, 200, response.content.decode())
        payload = response.json()
        self.assertTrue(payload["success"])
        self.assertEqual(payload["work_order"]["reported_qty"], 5)
        self.assertEqual(payload["work_order"]["minimum_editable_quantity"], 5)

    def test_update_rejects_quantity_below_own_reported_output(self):
        work_order = self._create_work_order(quantity=12)
        ProductionLog.objects.create(
            work_order=work_order,
            worker=self.worker,
            quantity=4,
            status="approved",
        )
        ProductionLog.objects.create(
            work_order=work_order,
            worker=self.worker,
            quantity=3,
            status="pending",
        )

        response = self.client.post(
            reverse("update_work_order_alias", args=[work_order.id]),
            data=json.dumps({"quantity": 6}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400, response.content.decode())
        payload = response.json()
        self.assertFalse(payload["success"])
        self.assertIn("already reported output (7)", payload["error"])

    def test_update_rejects_root_quantity_below_child_reported_output(self):
        root_work_order = self._create_work_order(quantity=10, status="pending")
        child_work_order = self._create_work_order(quantity=10, parent=root_work_order, status="in_progress")
        ProductionLog.objects.create(
            work_order=child_work_order,
            worker=self.worker,
            quantity=6,
            status="pending",
        )

        response = self.client.post(
            reverse("update_work_order_alias", args=[root_work_order.id]),
            data=json.dumps({"quantity": 5}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400, response.content.decode())
        payload = response.json()
        self.assertFalse(payload["success"])
        self.assertIn("already reported output (6)", payload["error"])

    def test_update_allows_quantity_at_reported_output_floor(self):
        work_order = self._create_work_order(quantity=12)
        ProductionLog.objects.create(
            work_order=work_order,
            worker=self.worker,
            quantity=4,
            status="approved",
        )
        ProductionLog.objects.create(
            work_order=work_order,
            worker=self.worker,
            quantity=3,
            status="pending",
        )

        response = self.client.post(
            reverse("update_work_order_alias", args=[work_order.id]),
            data=json.dumps({"quantity": 7}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200, response.content.decode())
        payload = response.json()
        self.assertTrue(payload["success"])
        work_order.refresh_from_db()
        self.assertEqual(work_order.quantity, 7)

    def test_assign_work_order_rejects_quantity_below_reported_output(self):
        work_order = self._create_work_order(quantity=10, status="pending")
        ProductionLog.objects.create(
            work_order=work_order,
            worker=self.worker,
            quantity=5,
            status="pending",
        )

        response = self.client.post(
            reverse("assign_work_order"),
            data={
                "wo_id": work_order.id,
                "quantity": 4,
            },
        )

        self.assertEqual(response.status_code, 400, response.content.decode())
        payload = response.json()
        self.assertFalse(payload["success"])
        self.assertIn("already reported output (5)", payload["error"])

    def test_update_quantity_replans_single_scheduled_work_order(self):
        start_at = timezone.now() + timedelta(hours=1)
        work_order = self._create_work_order(quantity=10, status="pending")
        work_order.scheduled_start_date = start_at
        work_order.start_date = start_at
        work_order.end_date = start_at + timedelta(minutes=105)
        work_order.save(update_fields=["scheduled_start_date", "start_date", "end_date"])

        response = self.client.post(
            reverse("update_work_order_alias", args=[work_order.id]),
            data=json.dumps({"quantity": 20}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200, response.content.decode())
        payload = response.json()
        self.assertTrue(payload["success"])
        self.assertTrue(payload["rescheduled"])
        work_order.refresh_from_db()
        self.assertEqual(work_order.quantity, 20)
        self.assertEqual(work_order.end_date, work_order.start_date + timedelta(minutes=205))

    def test_update_parent_quantity_replans_routed_stage_tasks(self):
        start_at = timezone.now() + timedelta(hours=1)
        parent = WorkOrder.objects.create(
            company=self.company,
            product_name=self.product.name,
            bom=self.bom,
            quantity=10,
            base_quantity=10,
            status="draft",
            assigned_to=self.planner,
        )
        WorkOrder.objects.create(
            company=self.company,
            product_name=f"{self.product.name} - Assembly",
            bom=self.bom,
            quantity=10,
            status="pending",
            machine=self.machine,
            stage=self.stage,
            current_stage=self.stage,
            parent=parent,
            assigned_to=self.planner,
            scheduled_start_date=start_at,
            start_date=start_at,
            end_date=start_at + timedelta(minutes=105),
        )
        WorkOrder.objects.create(
            company=self.company,
            product_name=f"{self.product.name} - Packing",
            bom=self.bom,
            quantity=10,
            status="pending",
            machine=self.pack_machine,
            stage=self.pack_stage,
            current_stage=self.pack_stage,
            parent=parent,
            assigned_to=self.planner,
            scheduled_start_date=start_at + timedelta(minutes=105),
            start_date=start_at + timedelta(minutes=105),
            end_date=start_at + timedelta(minutes=155),
        )

        response = self.client.post(
            reverse("update_work_order_alias", args=[parent.id]),
            data=json.dumps({"quantity": 20}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200, response.content.decode())
        payload = response.json()
        self.assertTrue(payload["success"])
        self.assertTrue(payload["rescheduled"])

        parent.refresh_from_db()
        assembly = WorkOrder.objects.get(parent=parent, stage=self.stage)
        packing = WorkOrder.objects.get(parent=parent, stage=self.pack_stage)
        self.assertEqual(parent.quantity, 20)
        self.assertEqual(assembly.quantity, 20)
        self.assertEqual(packing.quantity, 20)
        self.assertEqual(assembly.end_date, assembly.start_date + timedelta(minutes=205))
        self.assertGreaterEqual(packing.start_date, assembly.end_date)
        packing_duration_seconds = (packing.end_date - packing.start_date).total_seconds()
        self.assertAlmostEqual(packing_duration_seconds, 100 * 60, delta=60)
