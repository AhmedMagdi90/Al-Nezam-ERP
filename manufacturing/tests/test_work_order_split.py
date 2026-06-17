import json
from datetime import timedelta

from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from manufacturing.models import (
    BOMOperation,
    BillOfMaterial,
    Machine,
    Notification,
    Product,
    ProductionLog,
    ProductionStage,
    WorkOrder,
)
from manufacturing.services import WorkOrderService
from manufacturing.services import DashboardService
from manufacturing.tests.utils import create_company, create_user_with_role


class WorkOrderSplitTests(TestCase):
    def setUp(self):
        self.company = create_company("Split Co")
        self.planner = create_user_with_role("planner_split_cases", "planner", self.company)
        self.supervisor = create_user_with_role("supervisor_split_cases", "supervisor", self.company)
        self.worker = create_user_with_role("worker_split_cases", "worker", self.company)
        self.client = Client()
        self.client.force_login(self.planner)

        self.machine_1 = Machine.objects.create(
            name="Line 1",
            code="LINE-01",
            type="Assembly",
            category="Assembly",
            status="operational",
            company=self.company,
        )
        self.machine_2 = Machine.objects.create(
            name="Line 2",
            code="LINE-02",
            type="Assembly",
            category="Assembly",
            status="operational",
            company=self.company,
        )

        self.stage_1 = ProductionStage.objects.create(
            name="Op 10: Assembly",
            category="Assembly",
            machine=self.machine_1,
            order=1,
        )
        self.stage_2 = ProductionStage.objects.create(
            name="Op 20: Packing",
            category="Packing",
            order=2,
        )

        self.product = Product.objects.create(name="Split Product", company=self.company)
        self.bom = BillOfMaterial.objects.create(product=self.product, status="active", base_quantity=1)
        BOMOperation.objects.create(
            bom=self.bom,
            stage=self.stage_1,
            machine=self.machine_1,
            order=1,
            duration_minutes=60,
        )
        BOMOperation.objects.create(
            bom=self.bom,
            stage=self.stage_2,
            order=2,
            duration_minutes=45,
        )

    def _create_stage_task(self, *, quantity=100, status="in_progress", parent=None):
        return WorkOrder.objects.create(
            company=self.company,
            product_name=self.product.name,
            bom=self.bom,
            quantity=quantity,
            status=status,
            machine=self.machine_1,
            stage=self.stage_1,
            current_stage=self.stage_1,
            parent=parent,
            assigned_to=self.planner,
            start_date=timezone.now(),
            end_date=timezone.now() + timedelta(hours=2),
            operation_flow_mode="series",
        )

    def test_split_api_requires_target_machine(self):
        wo = self._create_stage_task()

        response = self.client.post(
            reverse("api_split_work_order", args=[wo.id]),
            data=json.dumps({"split_quantity": 20}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400, response.content.decode())
        payload = response.json()
        self.assertFalse(payload["success"])
        self.assertIn("Target machine is required", payload["error"])

    def test_split_api_rejects_quantity_exceeding_remaining(self):
        wo = self._create_stage_task(quantity=100)
        ProductionLog.objects.create(
            work_order=wo,
            worker=self.worker,
            quantity=70,
            status="approved",
        )

        response = self.client.post(
            reverse("api_split_work_order", args=[wo.id]),
            data=json.dumps({"split_quantity": 40, "machine_id": self.machine_2.id}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400, response.content.decode())
        payload = response.json()
        self.assertFalse(payload["success"])
        self.assertIn("exceeds remaining quantity", payload["error"])

    def test_split_api_rejects_fractional_quantity_without_truncating(self):
        wo = self._create_stage_task(quantity=500)

        response = self.client.post(
            reverse("api_split_work_order", args=[wo.id]),
            data=json.dumps({"split_quantity": "199.9", "machine_id": self.machine_2.id}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400, response.content.decode())
        payload = response.json()
        self.assertFalse(payload["success"])
        self.assertIn("whole number", payload["error"])
        wo.refresh_from_db()
        self.assertEqual(wo.quantity, 500)
        self.assertFalse(WorkOrder.objects.filter(source_task=wo).exists())

    def test_split_service_accepts_integral_decimal_quantity(self):
        wo = self._create_stage_task(quantity=500)

        new_wo = WorkOrderService.split_work_order(wo, "200.0", self.machine_2, self.planner)

        wo.refresh_from_db()
        self.assertEqual(wo.quantity, 300)
        self.assertEqual(new_wo.quantity, 200)

    def test_split_service_uses_requested_start_time_for_new_split(self):
        wo = self._create_stage_task(quantity=500)
        requested_start = timezone.now() + timedelta(days=1, hours=2)

        new_wo = WorkOrderService.split_work_order(
            wo,
            200,
            self.machine_2,
            self.planner,
            planned_start=requested_start,
        )

        self.assertGreaterEqual(new_wo.start_date, requested_start)
        self.assertEqual(new_wo.scheduled_start_date, new_wo.start_date)
        self.assertGreater(new_wo.end_date, new_wo.start_date)

    def test_split_service_rejects_parent_work_order(self):
        parent = WorkOrder.objects.create(
            company=self.company,
            product_name=self.product.name,
            bom=self.bom,
            quantity=100,
            status="pending",
            operation_flow_mode="series",
        )
        self._create_stage_task(parent=parent)

        with self.assertRaisesMessage(ValueError, "Cannot split a parent work order"):
            WorkOrderService.split_work_order(parent, 20, self.machine_2, self.planner)

    def test_split_service_stage_task_keeps_parent_and_stage(self):
        parent = WorkOrder.objects.create(
            company=self.company,
            product_name=self.product.name,
            bom=self.bom,
            quantity=100,
            status="in_progress",
            current_stage=self.stage_1,
            operation_flow_mode="series",
        )
        wo = self._create_stage_task(parent=parent, quantity=100)

        new_wo = WorkOrderService.split_work_order(wo, 30, self.machine_2, self.planner)

        wo.refresh_from_db()
        self.assertEqual(wo.quantity, 70)
        self.assertEqual(new_wo.parent_id, parent.id)
        self.assertEqual(new_wo.stage_id, self.stage_1.id)
        self.assertEqual(new_wo.current_stage_id, self.stage_1.id)
        self.assertEqual(new_wo.machine_id, self.machine_2.id)
        self.assertEqual(new_wo.status, "pending")
        self.assertEqual(new_wo.operation_flow_mode, "series")

    def test_split_service_updates_visible_quantities_when_base_quantity_exists(self):
        wo = self._create_stage_task(quantity=100, status="pending")
        wo.base_quantity = 100
        wo.save(update_fields=["base_quantity"])

        new_wo = WorkOrderService.split_work_order(wo, 60, self.machine_2, self.planner)

        wo.refresh_from_db()
        new_wo.refresh_from_db()
        self.assertEqual(wo.quantity, 40)
        self.assertEqual(wo.base_quantity, 40)
        self.assertEqual(new_wo.quantity, 60)
        self.assertEqual(new_wo.base_quantity, 60)

        timeline = DashboardService.get_timeline_data(
            self.company,
            include_unscheduled=True,
            viewer_role="planner",
            viewer=self.planner,
        )
        tasks_by_id = {task["id"]: task for task in timeline["tasks"]}
        self.assertEqual(tasks_by_id[wo.id]["quantity"], 40)
        self.assertEqual(tasks_by_id[wo.id]["base_quantity"], 40)
        self.assertEqual(tasks_by_id[new_wo.id]["quantity"], 60)
        self.assertEqual(tasks_by_id[new_wo.id]["base_quantity"], 60)

    def test_split_service_uses_original_duration_for_new_segment(self):
        wo = self._create_stage_task(quantity=100)
        original_start = timezone.now().replace(hour=10, minute=0, second=0, microsecond=0)
        original_end = original_start + timedelta(hours=2)
        wo.start_date = original_start
        wo.end_date = original_end
        wo.save(update_fields=["start_date", "end_date"])

        new_wo = WorkOrderService.split_work_order(wo, 50, self.machine_2, self.planner)

        wo.refresh_from_db()
        original_minutes = round((wo.end_date - wo.start_date).total_seconds() / 60)
        split_minutes = round((new_wo.end_date - new_wo.start_date).total_seconds() / 60)
        self.assertEqual(original_minutes, 60)
        self.assertEqual(split_minutes, 60)

    def test_split_service_completes_original_when_approved_covers_remaining(self):
        parent = WorkOrder.objects.create(
            company=self.company,
            product_name=self.product.name,
            bom=self.bom,
            quantity=100,
            status="in_progress",
            current_stage=self.stage_1,
            operation_flow_mode="series",
        )
        wo = self._create_stage_task(parent=parent, quantity=100)
        ProductionLog.objects.create(
            work_order=wo,
            worker=self.worker,
            quantity=70,
            status="approved",
        )

        new_wo = WorkOrderService.split_work_order(wo, 30, self.machine_2, self.planner)

        wo.refresh_from_db()
        self.assertEqual(wo.quantity, 70)
        self.assertEqual(wo.status, "completed")
        self.assertEqual(int(wo.progress), 100)
        self.assertEqual(new_wo.status, "pending")
        self.assertEqual(new_wo.quantity, 30)

    def test_split_api_single_stage_root_creates_new_root_work_order(self):
        wo = self._create_stage_task(quantity=100, parent=None)

        response = self.client.post(
            reverse("api_split_work_order", args=[wo.id]),
            data=json.dumps({"split_quantity": 25, "machine_id": self.machine_2.id}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200, response.content.decode())
        payload = response.json()
        self.assertTrue(payload["success"])

        wo.refresh_from_db()
        new_wo = WorkOrder.objects.get(id=payload["new_wo_id"])
        self.assertEqual(wo.quantity, 75)
        self.assertIsNone(new_wo.parent_id)
        self.assertEqual(new_wo.machine_id, self.machine_2.id)
        self.assertEqual(new_wo.quantity, 25)

    def test_split_api_allows_full_remaining_quantity_and_closes_original(self):
        wo = self._create_stage_task(quantity=100, parent=None)

        response = self.client.post(
            reverse("api_split_work_order", args=[wo.id]),
            data=json.dumps({"split_quantity": 100, "machine_id": self.machine_2.id}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200, response.content.decode())
        payload = response.json()
        self.assertTrue(payload["success"])

        wo.refresh_from_db()
        new_wo = WorkOrder.objects.get(id=payload["new_wo_id"])
        self.assertEqual(wo.quantity, 0)
        self.assertEqual(wo.status, "completed")
        self.assertEqual(int(wo.progress), 100)
        self.assertEqual(new_wo.quantity, 100)
        self.assertEqual(new_wo.status, "pending")

    def test_split_api_on_parent_targets_active_stage_task_and_notifies_supervisor(self):
        parent = WorkOrder.objects.create(
            company=self.company,
            product_name=self.product.name,
            bom=self.bom,
            quantity=100,
            base_quantity=100,
            status="in_progress",
            current_stage=self.stage_1,
            operation_flow_mode="series",
        )
        child = self._create_stage_task(parent=parent, quantity=100)
        child.base_quantity = 100
        child.save(update_fields=["base_quantity"])

        response = self.client.post(
            reverse("api_split_work_order", args=[parent.id]),
            data=json.dumps({"split_quantity": 30, "machine_id": self.machine_2.id}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200, response.content.decode())
        payload = response.json()
        self.assertTrue(payload["success"])
        self.assertEqual(payload["source_wo_id"], child.id)

        parent.refresh_from_db()
        child.refresh_from_db()
        new_wo = WorkOrder.objects.get(id=payload["new_wo_id"])
        self.assertEqual(parent.quantity, 70)
        self.assertEqual(parent.base_quantity, 70)
        self.assertEqual(child.quantity, 70)
        self.assertEqual(child.base_quantity, 70)
        self.assertEqual(new_wo.parent_id, parent.id)
        self.assertEqual(new_wo.quantity, 30)
        self.assertEqual(new_wo.base_quantity, 30)
        self.assertIsNone(new_wo.assigned_worker_id)
        self.assertEqual(new_wo.assignment_type, "auto")

        details_response = self.client.get(reverse("get_work_order", args=[parent.id]))
        self.assertEqual(details_response.status_code, 200, details_response.content.decode())
        details_payload = details_response.json()
        self.assertTrue(details_payload["success"])
        self.assertEqual(details_payload["work_order"]["quantity"], 70)
        self.assertEqual(details_payload["work_order"]["base_quantity"], 70)

        notice = Notification.objects.filter(recipient=self.supervisor).order_by("-id").first()
        self.assertIsNotNone(notice)
        self.assertIn("Split work order needs assignment", notice.title)
        self.assertIn("30 units", notice.message)

    def test_split_service_records_source_task_for_cancel_flow(self):
        wo = self._create_stage_task(quantity=100, parent=None)

        new_wo = WorkOrderService.split_work_order(wo, 25, self.machine_2, self.planner)

        self.assertEqual(new_wo.source_task_id, wo.id)

    def test_split_source_task_id_is_exposed_to_timeline_and_details_api(self):
        wo = self._create_stage_task(quantity=100, parent=None)
        new_wo = WorkOrderService.split_work_order(wo, 25, self.machine_2, self.planner)

        timeline = DashboardService.get_timeline_data(
            self.company,
            include_unscheduled=True,
            viewer_role="planner",
            viewer=self.planner,
        )
        split_task = next(task for task in timeline["tasks"] if task["id"] == new_wo.id)
        self.assertEqual(split_task["source_task_id"], wo.id)

        response = self.client.get(reverse("get_work_order", args=[new_wo.id]))
        self.assertEqual(response.status_code, 200, response.content.decode())
        payload = response.json()
        self.assertTrue(payload["success"])
        self.assertEqual(payload["work_order"]["source_task_id"], wo.id)

    def test_parent_route_details_exposes_stage_split_child_ids(self):
        parent = WorkOrder.objects.create(
            company=self.company,
            product_name=self.product.name,
            bom=self.bom,
            quantity=100,
            status="pending",
            current_stage=self.stage_1,
            operation_flow_mode="series",
        )
        source = self._create_stage_task(parent=parent, quantity=100, status="pending")
        split_child = WorkOrderService.split_work_order(source, 25, self.machine_2, self.planner)

        response = self.client.get(reverse("get_work_order", args=[parent.id]))

        self.assertEqual(response.status_code, 200, response.content.decode())
        payload = response.json()
        self.assertTrue(payload["success"])
        assembly_stage = next(
            stage for stage in payload["route_stages"]
            if stage["planned_task_id"] == source.id
        )
        self.assertEqual(assembly_stage["split_child_ids"], [split_child.id])
        self.assertEqual(assembly_stage["split_child_count"], 1)

    def test_cancel_split_service_returns_quantity_to_source_and_cancels_child(self):
        wo = self._create_stage_task(quantity=100, parent=None)
        split_wo = WorkOrderService.split_work_order(wo, 30, self.machine_2, self.planner)

        result = WorkOrderService.cancel_split_work_order(split_wo, self.planner)

        wo.refresh_from_db()
        split_wo.refresh_from_db()
        self.assertEqual(result["target_work_order"].id, wo.id)
        self.assertEqual(result["returned_quantity"], 30)
        self.assertEqual(wo.quantity, 100)
        self.assertEqual(split_wo.status, "canceled")

    def test_cancel_split_api_rejects_child_with_production_logs(self):
        wo = self._create_stage_task(quantity=100, parent=None)
        split_wo = WorkOrderService.split_work_order(wo, 30, self.machine_2, self.planner)
        ProductionLog.objects.create(
            work_order=split_wo,
            worker=self.worker,
            quantity=5,
            status="pending",
        )

        response = self.client.post(
            reverse("api_cancel_split_work_order", args=[split_wo.id]),
            data=json.dumps({}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400, response.content.decode())
        payload = response.json()
        self.assertFalse(payload["success"])
        self.assertIn("production logs exist", payload["error"])

    def test_cancel_split_api_returns_quantity_to_source(self):
        wo = self._create_stage_task(quantity=100, parent=None)
        split_wo = WorkOrderService.split_work_order(wo, 30, self.machine_2, self.planner)

        response = self.client.post(
            reverse("api_cancel_split_work_order", args=[split_wo.id]),
            data=json.dumps({}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200, response.content.decode())
        payload = response.json()
        self.assertTrue(payload["success"])
        self.assertEqual(payload["target_wo_id"], wo.id)
        self.assertEqual(payload["returned_quantity"], 30)

        wo.refresh_from_db()
        split_wo.refresh_from_db()
        self.assertEqual(wo.quantity, 100)
        self.assertEqual(split_wo.status, "canceled")

    def test_combine_service_merges_compatible_split_tasks_into_target(self):
        parent = WorkOrder.objects.create(
            company=self.company,
            product_name=self.product.name,
            bom=self.bom,
            quantity=100,
            base_quantity=100,
            status="in_progress",
            current_stage=self.stage_1,
            operation_flow_mode="series",
        )
        first = self._create_stage_task(parent=parent, quantity=40, status="pending")
        first.base_quantity = 40
        first.save(update_fields=["base_quantity"])
        second = self._create_stage_task(parent=parent, quantity=35, status="pending")
        second.base_quantity = 35
        second.machine = self.machine_2
        second.save(update_fields=["base_quantity", "machine"])

        result = WorkOrderService.combine_work_orders([first, second], self.planner, target_wo=second)

        parent.refresh_from_db()
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(result["target_work_order"].id, second.id)
        self.assertEqual(result["combined_quantity"], 75)
        self.assertEqual(parent.quantity, 75)
        self.assertEqual(parent.base_quantity, 75)
        self.assertEqual(second.quantity, 75)
        self.assertEqual(second.base_quantity, 75)
        self.assertEqual(first.status, "canceled")
        self.assertEqual(result["canceled_work_order_ids"], [first.id])

    def test_combine_service_restores_split_child_to_original_source(self):
        source = self._create_stage_task(quantity=500, status="pending")
        source.base_quantity = 500
        source.save(update_fields=["base_quantity"])
        split_child = WorkOrderService.split_work_order(source, 200, self.machine_2, self.planner)

        source.refresh_from_db()
        self.assertEqual(source.quantity, 300)
        self.assertEqual(source.base_quantity, 300)
        self.assertEqual(split_child.quantity, 200)
        self.assertEqual(split_child.base_quantity, 200)
        self.assertEqual(split_child.source_task_id, source.id)

        result = WorkOrderService.combine_work_orders(
            [source, split_child],
            self.planner,
            target_wo=source,
        )

        source.refresh_from_db()
        split_child.refresh_from_db()
        self.assertEqual(result["target_work_order"].id, source.id)
        self.assertEqual(result["combined_quantity"], 500)
        self.assertEqual(source.quantity, 500)
        self.assertEqual(source.base_quantity, 500)
        self.assertEqual(split_child.status, "canceled")
        self.assertEqual(result["canceled_work_order_ids"], [split_child.id])

    def test_combine_service_rejects_incompatible_stage(self):
        parent = WorkOrder.objects.create(
            company=self.company,
            product_name=self.product.name,
            bom=self.bom,
            quantity=100,
            status="in_progress",
            current_stage=self.stage_1,
            operation_flow_mode="series",
        )
        first = self._create_stage_task(parent=parent, quantity=40, status="pending")
        second = self._create_stage_task(parent=parent, quantity=35, status="pending")
        second.stage = self.stage_2
        second.current_stage = self.stage_2
        second.save(update_fields=["stage", "current_stage"])

        with self.assertRaisesMessage(ValueError, "not compatible"):
            WorkOrderService.combine_work_orders([first, second], self.planner)

    def test_combine_api_merges_compatible_work_orders(self):
        parent = WorkOrder.objects.create(
            company=self.company,
            product_name=self.product.name,
            bom=self.bom,
            quantity=100,
            status="in_progress",
            current_stage=self.stage_1,
            operation_flow_mode="series",
        )
        first = self._create_stage_task(parent=parent, quantity=20, status="pending")
        second = self._create_stage_task(parent=parent, quantity=30, status="pending")

        response = self.client.post(
            reverse("api_combine_work_orders"),
            data=json.dumps({
                "work_order_ids": [first.id, second.id],
                "target_wo_id": first.id,
            }),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200, response.content.decode())
        payload = response.json()
        self.assertTrue(payload["success"])
        self.assertEqual(payload["target_wo_id"], first.id)
        self.assertEqual(payload["combined_quantity"], 50)
        self.assertEqual(payload["canceled_work_order_ids"], [second.id])

        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.quantity, 50)
        self.assertEqual(second.status, "canceled")

    def test_combine_api_restores_split_child_to_source(self):
        source = self._create_stage_task(quantity=500, status="pending")
        split_child = WorkOrderService.split_work_order(source, 200, self.machine_2, self.planner)

        response = self.client.post(
            reverse("api_combine_work_orders"),
            data=json.dumps({
                "work_order_ids": [source.id, split_child.id],
                "target_wo_id": source.id,
            }),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200, response.content.decode())
        payload = response.json()
        self.assertTrue(payload["success"])
        self.assertEqual(payload["target_wo_id"], source.id)
        self.assertEqual(payload["combined_quantity"], 500)
        self.assertEqual(payload["canceled_work_order_ids"], [split_child.id])

        source.refresh_from_db()
        split_child.refresh_from_db()
        self.assertEqual(source.quantity, 500)
        self.assertEqual(split_child.status, "canceled")

    def test_combine_api_can_restore_split_group_from_source_id(self):
        source = self._create_stage_task(quantity=650, status="pending")
        split_child = WorkOrderService.split_work_order(source, 200, self.machine_2, self.planner)

        source.refresh_from_db()
        self.assertEqual(source.quantity, 450)
        self.assertEqual(split_child.quantity, 200)

        response = self.client.post(
            reverse("api_combine_work_orders"),
            data=json.dumps({"source_wo_id": source.id}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200, response.content.decode())
        payload = response.json()
        self.assertTrue(payload["success"])
        self.assertEqual(payload["target_wo_id"], source.id)
        self.assertEqual(payload["combined_quantity"], 650)
        self.assertEqual(payload["canceled_work_order_ids"], [split_child.id])

        source.refresh_from_db()
        split_child.refresh_from_db()
        self.assertEqual(source.quantity, 650)
        self.assertEqual(split_child.status, "canceled")

    def test_combine_api_source_id_is_idempotent_after_split_already_combined(self):
        source = self._create_stage_task(quantity=400, status="pending")
        split_child = WorkOrderService.split_work_order(source, 100, self.machine_2, self.planner)

        first_response = self.client.post(
            reverse("api_combine_work_orders"),
            data=json.dumps({"source_wo_id": source.id}),
            content_type="application/json",
        )
        self.assertEqual(first_response.status_code, 200, first_response.content.decode())

        second_response = self.client.post(
            reverse("api_combine_work_orders"),
            data=json.dumps({"source_wo_id": source.id}),
            content_type="application/json",
        )

        self.assertEqual(second_response.status_code, 200, second_response.content.decode())
        payload = second_response.json()
        self.assertTrue(payload["success"])
        self.assertTrue(payload["already_combined"])
        self.assertEqual(payload["combined_quantity"], 400)
        self.assertEqual(payload["canceled_work_order_ids"], [])

        source.refresh_from_db()
        split_child.refresh_from_db()
        self.assertEqual(source.quantity, 400)
        self.assertEqual(split_child.status, "canceled")
