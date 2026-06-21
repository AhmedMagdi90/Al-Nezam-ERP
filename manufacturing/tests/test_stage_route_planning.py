import json
from datetime import timedelta
from unittest.mock import patch

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
    ShiftAssignment,
    SystemSettings,
    WorkOrder,
)
from manufacturing.services import DashboardService
from manufacturing.tests.utils import create_company, create_user_with_role
from manufacturing.work_order_visibility import get_current_shift_window_for_company


class StageRoutePlanningTests(TestCase):
    def setUp(self):
        self.company = create_company()
        self.planner = create_user_with_role("planner_route", "planner", self.company)
        self.supervisor_cut = create_user_with_role("supervisor_cut", "supervisor", self.company)
        self.supervisor_pack = create_user_with_role("supervisor_pack", "supervisor", self.company)
        self.supervisor_production = create_user_with_role("supervisor_production", "supervisor", self.company)
        self.supervisor_cut.profile.department = "Cutting"
        self.supervisor_cut.profile.save(update_fields=["department"])
        self.supervisor_pack.profile.department = "Packing"
        self.supervisor_pack.profile.save(update_fields=["department"])
        self.supervisor_production.profile.department = "Production"
        self.supervisor_production.profile.save(update_fields=["department"])
        self.client = Client()
        self.client.force_login(self.planner)
        self.material_gate_patch = patch(
            "manufacturing.views.schedule.get_material_readiness_planning_blocker",
            return_value=None,
        )
        self.material_gate_patch.start()
        self.addCleanup(self.material_gate_patch.stop)

        self.machine_cut = Machine.objects.create(
            name="Cutting Machine",
            code="CUT-01",
            type="Cutting",
            category="Cutting",
            status="operational",
            company=self.company,
        )
        self.machine_cut_alt = Machine.objects.create(
            name="Cutting Machine Backup",
            code="CUT-02",
            type="Cutting",
            category="Cutting",
            status="operational",
            company=self.company,
        )
        self.machine_pack = Machine.objects.create(
            name="Packing Machine",
            code="PACK-01",
            type="Packing",
            category="Packing",
            status="operational",
            company=self.company,
        )
        self.machine_cnc = Machine.objects.create(
            name="CNC Machine",
            code="CNC-99",
            type="CNC",
            category="CNC",
            status="operational",
            company=self.company,
        )
        settings, _ = SystemSettings.objects.get_or_create(company=self.company)
        settings.shift_mode = "1"
        settings.shift_configuration = self._active_shift_config()
        settings.save(update_fields=["shift_mode", "shift_configuration"])
        shift_window = get_current_shift_window_for_company(self.company)
        ShiftAssignment.objects.create(
            worker=self.supervisor_cut,
            machine=self.machine_cut,
            shift_type=shift_window["shift_type"],
            date=shift_window["assignment_date"],
            created_by=self.planner,
        )
        ShiftAssignment.objects.create(
            worker=self.supervisor_pack,
            machine=self.machine_pack,
            shift_type=shift_window["shift_type"],
            date=shift_window["assignment_date"],
            created_by=self.planner,
        )
        ShiftAssignment.objects.create(
            worker=self.supervisor_production,
            machine=self.machine_cut,
            shift_type=shift_window["shift_type"],
            date=shift_window["assignment_date"],
            created_by=self.planner,
        )
        self.stage_cut = ProductionStage.objects.create(
            name="Cutting",
            category="Cutting",
            machine=self.machine_cut,
        )
        self.stage_pack = ProductionStage.objects.create(
            name="Packing",
            category="Packing",
        )
        self.product = Product.objects.create(name="Widget", company=self.company)
        self.bom = BillOfMaterial.objects.create(
            product=self.product,
            status="active",
            base_quantity=1,
        )
        BOMOperation.objects.create(
            bom=self.bom,
            stage=self.stage_cut,
            machine=self.machine_cut,
            order=1,
            duration_minutes=60,
        )
        BOMOperation.objects.create(
            bom=self.bom,
            stage=self.stage_pack,
            machine_type="Packing",
            order=2,
            duration_minutes=45,
        )

    def _active_shift_config(self):
        now = timezone.localtime()
        return {
            "morning": {
                "start": (now - timedelta(hours=1)).strftime("%H:%M"),
                "end": (now + timedelta(hours=4)).strftime("%H:%M"),
            },
            "afternoon": {"start": "23:00", "end": "23:30"},
            "night": {"start": "23:30", "end": "05:30"},
        }

    def test_planner_schedule_api_auto_schedules_full_route(self):
        work_order = WorkOrder.objects.create(
            product_name="Widget Batch",
            bom=self.bom,
            quantity=25,
            status="pending",
            company=self.company,
        )
        start_at = timezone.now() + timedelta(minutes=20)

        response = self.client.post(
            reverse("api_schedule_work_order", args=[work_order.id]),
            data=json.dumps({
                "stage_id": self.stage_cut.id,
                "machine_id": self.machine_cut.id,
                "start_date": start_at.isoformat(),
            }),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200, response.content.decode())
        payload = response.json()
        self.assertTrue(payload["success"])

        work_order.refresh_from_db()
        route_tasks = list(
            WorkOrder.objects.filter(parent=work_order).select_related("stage", "machine").order_by("start_date", "id")
        )
        self.assertEqual(len(route_tasks), 2)
        self.assertEqual(route_tasks[0].stage, self.stage_cut)
        self.assertEqual(route_tasks[0].machine, self.machine_cut)
        self.assertEqual(route_tasks[1].stage, self.stage_pack)
        self.assertEqual(route_tasks[1].machine, self.machine_pack)
        self.assertGreaterEqual(route_tasks[1].start_date, route_tasks[0].end_date)
        self.assertEqual(work_order.current_stage, self.stage_cut)
        self.assertEqual(work_order.status, "pending")

    def test_planner_schedule_api_applies_quantity_before_planning_full_route(self):
        work_order = WorkOrder.objects.create(
            product_name="Widget Batch",
            bom=self.bom,
            quantity=25,
            base_quantity=25,
            status="pending",
            company=self.company,
        )
        start_at = timezone.now() + timedelta(minutes=20)

        response = self.client.post(
            reverse("api_schedule_work_order", args=[work_order.id]),
            data=json.dumps({
                "stage_id": self.stage_cut.id,
                "machine_id": self.machine_cut.id,
                "start_date": start_at.isoformat(),
                "quantity": 40,
                "route_assignments": [
                    {
                        "stage_id": self.stage_cut.id,
                        "machine_id": self.machine_cut.id,
                        "selection_mode": "manual",
                    },
                    {
                        "stage_id": self.stage_pack.id,
                        "machine_id": self.machine_pack.id,
                        "selection_mode": "manual",
                    },
                ],
            }),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200, response.content.decode())
        payload = response.json()
        self.assertTrue(payload["success"])
        self.assertTrue(payload["quantity_changed"])
        self.assertEqual(payload["quantity"], 40)

        work_order.refresh_from_db()
        route_quantities = list(
            WorkOrder.objects.filter(parent=work_order).order_by("id").values_list("quantity", flat=True)
        )
        self.assertEqual(work_order.quantity, 40)
        self.assertEqual(work_order.base_quantity, 40)
        self.assertEqual(route_quantities, [40, 40])

    def test_planner_schedule_api_allows_canceling_routed_work_order_without_start_date(self):
        worker = create_user_with_role("worker_route_cancel", "worker", self.company)
        work_order = WorkOrder.objects.create(
            product_name="Widget Batch",
            bom=self.bom,
            quantity=25,
            status="pending",
            company=self.company,
        )
        start_at = timezone.now() + timedelta(minutes=20)

        schedule_response = self.client.post(
            reverse("api_schedule_work_order", args=[work_order.id]),
            data=json.dumps({
                "stage_id": self.stage_cut.id,
                "machine_id": self.machine_cut.id,
                "start_date": start_at.isoformat(),
                "route_assignments": [
                    {
                        "stage_id": self.stage_cut.id,
                        "machine_id": self.machine_cut.id,
                        "selection_mode": "manual",
                    },
                    {
                        "stage_id": self.stage_pack.id,
                        "machine_id": self.machine_pack.id,
                        "selection_mode": "manual",
                    },
                ],
            }),
            content_type="application/json",
        )

        self.assertEqual(schedule_response.status_code, 200, schedule_response.content.decode())
        route_tasks = list(WorkOrder.objects.filter(parent=work_order).order_by("id"))
        self.assertEqual(len(route_tasks), 2)

        work_order.assigned_worker = worker
        work_order.assignment_type = "manual"
        work_order.save(update_fields=["assigned_worker", "assignment_type"])
        for task in route_tasks:
            task.assigned_worker = worker
            task.assignment_type = "manual"
            task.save(update_fields=["assigned_worker", "assignment_type"])

        cancel_response = self.client.post(
            reverse("api_schedule_work_order", args=[work_order.id]),
            data=json.dumps({
                "status": "canceled",
            }),
            content_type="application/json",
        )

        self.assertEqual(cancel_response.status_code, 200, cancel_response.content.decode())
        payload = cancel_response.json()
        self.assertTrue(payload["success"])
        self.assertEqual(payload["status"], "canceled")

        work_order.refresh_from_db()
        self.assertEqual(work_order.status, "canceled")
        self.assertIsNone(work_order.assigned_worker_id)
        self.assertEqual(work_order.assignment_type, "auto")
        self.assertIsNotNone(work_order.end_date)

        for task in WorkOrder.objects.filter(parent=work_order):
            self.assertEqual(task.status, "canceled")
            self.assertIsNone(task.assigned_worker_id)
            self.assertEqual(task.assignment_type, "auto")
            self.assertIsNotNone(task.end_date)

    def test_planner_schedule_api_allows_canceling_in_progress_routed_work_order(self):
        worker = create_user_with_role("worker_route_cancel_active", "worker", self.company)
        work_order = WorkOrder.objects.create(
            product_name="Widget Batch",
            bom=self.bom,
            quantity=25,
            status="in_progress",
            current_stage=self.stage_cut,
            company=self.company,
            assigned_worker=worker,
            assignment_type="manual",
        )
        stage_one = WorkOrder.objects.create(
            parent=work_order,
            product_name="Widget Batch - Cutting",
            bom=self.bom,
            quantity=25,
            machine=self.machine_cut,
            stage=self.stage_cut,
            current_stage=self.stage_cut,
            status="in_progress",
            start_date=timezone.now() - timedelta(minutes=20),
            end_date=timezone.now() + timedelta(minutes=40),
            company=self.company,
            assigned_worker=worker,
            assignment_type="manual",
        )
        stage_two = WorkOrder.objects.create(
            parent=work_order,
            product_name="Widget Batch - Packing",
            bom=self.bom,
            quantity=25,
            machine=self.machine_pack,
            stage=self.stage_pack,
            current_stage=self.stage_pack,
            status="pending",
            start_date=timezone.now() + timedelta(minutes=45),
            end_date=timezone.now() + timedelta(minutes=90),
            company=self.company,
            assigned_worker=worker,
            assignment_type="manual",
        )

        cancel_response = self.client.post(
            reverse("api_schedule_work_order", args=[work_order.id]),
            data=json.dumps({
                "status": "canceled",
            }),
            content_type="application/json",
        )

        self.assertEqual(cancel_response.status_code, 200, cancel_response.content.decode())
        payload = cancel_response.json()
        self.assertTrue(payload["success"])
        self.assertEqual(payload["status"], "canceled")

        work_order.refresh_from_db()
        stage_one.refresh_from_db()
        stage_two.refresh_from_db()
        self.assertEqual(work_order.status, "canceled")
        self.assertEqual(stage_one.status, "canceled")
        self.assertEqual(stage_two.status, "canceled")
        self.assertIsNone(work_order.assigned_worker_id)
        self.assertIsNone(stage_one.assigned_worker_id)
        self.assertIsNone(stage_two.assigned_worker_id)

    def test_work_order_plan_details_only_include_bom_candidate_machines(self):
        work_order = WorkOrder.objects.create(
            product_name="Widget Batch",
            bom=self.bom,
            quantity=25,
            status="pending",
            company=self.company,
        )

        response = self.client.get(reverse("api_workorder_detail", args=[work_order.id]))

        self.assertEqual(response.status_code, 200, response.content.decode())
        payload = response.json()
        self.assertTrue(payload["success"])

        all_machine_ids = {item["id"] for item in payload["all_machines"]}
        self.assertIn(self.machine_cut.id, all_machine_ids)
        self.assertIn(self.machine_cut_alt.id, all_machine_ids)
        self.assertIn(self.machine_pack.id, all_machine_ids)
        self.assertNotIn(self.machine_cnc.id, all_machine_ids)

        stages = {stage["id"]: stage for stage in payload["stages"]}
        self.assertEqual(stages[self.stage_cut.id]["duration_minutes"], 60)
        self.assertGreater(stages[self.stage_cut.id]["estimated_duration_minutes"], 0)
        self.assertIn("setup_time", stages[self.stage_cut.id])
        self.assertIn("run_time", stages[self.stage_cut.id])
        cut_candidates = {item["id"] for item in stages[self.stage_cut.id]["candidate_machines"]}
        pack_candidates = {item["id"] for item in stages[self.stage_pack.id]["candidate_machines"]}

        self.assertIn(self.machine_cut.id, cut_candidates)
        self.assertIn(self.machine_cut_alt.id, cut_candidates)
        self.assertNotIn(self.machine_pack.id, cut_candidates)
        self.assertNotIn(self.machine_cnc.id, cut_candidates)

        self.assertIn(self.machine_pack.id, pack_candidates)
        self.assertNotIn(self.machine_cut.id, pack_candidates)
        self.assertNotIn(self.machine_cnc.id, pack_candidates)

    def test_work_order_plan_details_infer_bom_from_product_name(self):
        work_order = WorkOrder.objects.create(
            product_name="Widget",
            quantity=25,
            status="pending",
            company=self.company,
        )

        response = self.client.get(reverse("get_work_order", args=[work_order.id]))

        self.assertEqual(response.status_code, 200, response.content.decode())
        payload = response.json()
        self.assertTrue(payload["success"])
        self.assertTrue(payload["work_order"]["route_container"])
        self.assertEqual(
            [stage["id"] for stage in payload["route_stages"]],
            [self.stage_cut.id, self.stage_pack.id],
        )

    def test_planner_schedule_api_infers_bom_from_product_name(self):
        work_order = WorkOrder.objects.create(
            product_name="Widget",
            quantity=25,
            status="pending",
            company=self.company,
        )
        start_at = timezone.now() + timedelta(minutes=20)

        response = self.client.post(
            reverse("api_schedule_work_order", args=[work_order.id]),
            data=json.dumps({
                "stage_id": self.stage_cut.id,
                "machine_id": self.machine_cut.id,
                "start_date": start_at.isoformat(),
                "route_assignments": [
                    {
                        "stage_id": self.stage_cut.id,
                        "machine_id": self.machine_cut.id,
                        "selection_mode": "manual",
                    },
                    {
                        "stage_id": self.stage_pack.id,
                        "machine_id": self.machine_pack.id,
                        "selection_mode": "manual",
                    },
                ],
            }),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200, response.content.decode())
        payload = response.json()
        self.assertTrue(payload["success"])
        work_order.refresh_from_db()
        self.assertEqual(work_order.bom, self.bom)
        self.assertEqual(WorkOrder.objects.filter(parent=work_order).count(), 2)

    def test_recommended_route_assignment_avoids_busy_first_machine(self):
        work_order = WorkOrder.objects.create(
            product_name="Widget Batch",
            bom=self.bom,
            quantity=25,
            status="pending",
            company=self.company,
        )
        start_at = timezone.now() + timedelta(minutes=20)
        WorkOrder.objects.create(
            product_name="Blocking WO",
            bom=self.bom,
            quantity=5,
            status="pending",
            company=self.company,
            machine=self.machine_cut,
            stage=self.stage_cut,
            current_stage=self.stage_cut,
            start_date=start_at,
            end_date=start_at + timedelta(hours=2),
        )

        response = self.client.post(
            reverse("api_schedule_work_order", args=[work_order.id]),
            data=json.dumps({
                "stage_id": self.stage_cut.id,
                "machine_id": self.machine_cut.id,
                "start_date": start_at.isoformat(),
                "route_assignments": [
                    {
                        "stage_id": self.stage_cut.id,
                        "machine_id": self.machine_cut.id,
                        "selection_mode": "recommended",
                    },
                    {
                        "stage_id": self.stage_pack.id,
                        "machine_id": self.machine_pack.id,
                        "selection_mode": "recommended",
                    },
                ],
            }),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200, response.content.decode())
        payload = response.json()
        self.assertTrue(payload["success"])

        route_tasks = list(
            WorkOrder.objects.filter(parent=work_order).select_related("stage", "machine").order_by("start_date", "id")
        )
        self.assertEqual(route_tasks[0].stage, self.stage_cut)
        self.assertEqual(route_tasks[0].machine, self.machine_cut_alt)
        self.assertEqual(route_tasks[0].start_date, start_at)

    def test_planner_undo_restore_can_remove_new_route_tasks_and_restore_parent(self):
        work_order = WorkOrder.objects.create(
            product_name="Widget Batch",
            bom=self.bom,
            quantity=25,
            status="pending",
            company=self.company,
        )
        start_at = timezone.now() + timedelta(minutes=20)

        schedule_response = self.client.post(
            reverse("api_schedule_work_order", args=[work_order.id]),
            data=json.dumps({
                "stage_id": self.stage_cut.id,
                "machine_id": self.machine_cut.id,
                "start_date": start_at.isoformat(),
                "route_assignments": [
                    {
                        "stage_id": self.stage_cut.id,
                        "machine_id": self.machine_cut.id,
                        "selection_mode": "manual",
                    },
                    {
                        "stage_id": self.stage_pack.id,
                        "machine_id": self.machine_pack.id,
                        "selection_mode": "manual",
                    },
                ],
            }),
            content_type="application/json",
        )

        self.assertEqual(schedule_response.status_code, 200, schedule_response.content.decode())
        schedule_payload = schedule_response.json()
        self.assertTrue(schedule_payload["success"])
        self.assertEqual(WorkOrder.objects.filter(parent=work_order).count(), 2)

        undo_response = self.client.post(
            reverse("api_planner_undo"),
            data=json.dumps({
                "items": [
                    {
                        "id": work_order.id,
                        "fields": {
                            "status": "draft",
                            "machine_id": None,
                            "stage_id": None,
                            "current_stage_id": None,
                            "start_date": None,
                            "end_date": None,
                            "scheduled_start_date": None,
                            "operation_flow_mode": "series",
                            "next_stage_ready": False,
                            "planner_action_required": False,
                            "closed_by_planner": False,
                            "assigned_worker_id": None,
                            "assignment_type": "auto",
                            "planner_start_at": None,
                        },
                    },
                    *[
                        {"id": item["id"], "delete": True}
                        for item in schedule_payload["tasks"]
                    ],
                ],
            }),
            content_type="application/json",
        )

        self.assertEqual(undo_response.status_code, 200, undo_response.content.decode())
        undo_payload = undo_response.json()
        self.assertTrue(undo_payload["success"])

        work_order.refresh_from_db()
        self.assertEqual(work_order.status, "pending")
        self.assertIsNone(work_order.current_stage_id)
        self.assertEqual(WorkOrder.objects.filter(parent=work_order).count(), 0)

    def test_planner_undo_restore_rejects_started_work_orders(self):
        work_order = WorkOrder.objects.create(
            product_name="Widget Batch",
            bom=self.bom,
            quantity=10,
            status="pending",
            company=self.company,
            machine=self.machine_cut,
            stage=self.stage_cut,
            current_stage=self.stage_cut,
            start_date=timezone.now(),
            end_date=timezone.now() + timedelta(hours=1),
        )
        ProductionLog.objects.create(
            work_order=work_order,
            quantity=1,
            status="approved",
        )

        undo_response = self.client.post(
            reverse("api_planner_undo"),
            data=json.dumps({
                "items": [
                    {
                        "id": work_order.id,
                        "fields": {
                            "status": "pending",
                            "machine_id": None,
                            "stage_id": self.stage_cut.id,
                            "current_stage_id": self.stage_cut.id,
                            "start_date": None,
                            "end_date": None,
                            "scheduled_start_date": None,
                        },
                    },
                ],
            }),
            content_type="application/json",
        )

        self.assertEqual(undo_response.status_code, 400)
        self.assertIn("cannot be undone", undo_response.json()["error"].lower())

    def test_planner_undo_restore_allows_in_progress_without_production_logs(self):
        start_at = timezone.now() + timedelta(minutes=15)
        original_start = start_at - timedelta(hours=2)
        original_end = original_start + timedelta(hours=1)
        work_order = WorkOrder.objects.create(
            product_name="Widget Batch",
            bom=self.bom,
            quantity=10,
            status="in_progress",
            company=self.company,
            machine=self.machine_cut_alt,
            stage=self.stage_cut,
            current_stage=self.stage_cut,
            start_date=start_at,
            end_date=start_at + timedelta(hours=1),
        )

        undo_response = self.client.post(
            reverse("api_planner_undo"),
            data=json.dumps({
                "items": [
                    {
                        "id": work_order.id,
                        "fields": {
                            "status": "in_progress",
                            "machine_id": self.machine_cut.id,
                            "stage_id": self.stage_cut.id,
                            "current_stage_id": self.stage_cut.id,
                            "start_date": original_start.isoformat(),
                            "end_date": original_end.isoformat(),
                            "scheduled_start_date": original_start.isoformat(),
                        },
                    },
                ],
            }),
            content_type="application/json",
        )

        self.assertEqual(undo_response.status_code, 200, undo_response.content.decode())
        self.assertTrue(undo_response.json()["success"])

        work_order.refresh_from_db()
        self.assertEqual(work_order.status, "in_progress")
        self.assertEqual(work_order.machine_id, self.machine_cut.id)
        self.assertEqual(work_order.start_date, original_start)
        self.assertEqual(work_order.end_date, original_end)

    def test_manual_route_assignment_keeps_busy_machine_error(self):
        work_order = WorkOrder.objects.create(
            product_name="Widget Batch",
            bom=self.bom,
            quantity=25,
            status="pending",
            company=self.company,
        )
        start_at = timezone.now() + timedelta(minutes=20)
        WorkOrder.objects.create(
            product_name="Blocking WO",
            bom=self.bom,
            quantity=5,
            status="pending",
            company=self.company,
            machine=self.machine_cut,
            stage=self.stage_cut,
            current_stage=self.stage_cut,
            start_date=start_at,
            end_date=start_at + timedelta(hours=2),
        )

        response = self.client.post(
            reverse("api_schedule_work_order", args=[work_order.id]),
            data=json.dumps({
                "stage_id": self.stage_cut.id,
                "machine_id": self.machine_cut.id,
                "start_date": start_at.isoformat(),
                "route_assignments": [
                    {
                        "stage_id": self.stage_cut.id,
                        "machine_id": self.machine_cut.id,
                        "selection_mode": "manual",
                    },
                    {
                        "stage_id": self.stage_pack.id,
                        "machine_id": self.machine_pack.id,
                        "selection_mode": "recommended",
                    },
                ],
            }),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400, response.content.decode())
        payload = response.json()
        self.assertFalse(payload["success"])
        self.assertIn("Machine occupied at this time", payload["error"])

    def test_manual_route_assignment_allows_machine_freed_by_early_close(self):
        work_order = WorkOrder.objects.create(
            product_name="Widget Batch",
            bom=self.bom,
            quantity=25,
            status="pending",
            company=self.company,
        )
        start_at = timezone.now().replace(second=0, microsecond=0) + timedelta(minutes=20)
        closed_end = start_at + timedelta(hours=3)
        WorkOrder.objects.create(
            product_name="Early Closed Blocking WO",
            bom=self.bom,
            quantity=5,
            status="completed",
            closed_by_planner=True,
            company=self.company,
            machine=self.machine_cut,
            stage=self.stage_cut,
            current_stage=self.stage_cut,
            start_date=start_at - timedelta(hours=1),
            end_date=start_at - timedelta(minutes=1),
        )
        WorkOrder.objects.create(
            product_name="Historical Future Plan",
            bom=self.bom,
            quantity=5,
            status="completed",
            closed_by_planner=True,
            company=self.company,
            machine=self.machine_cut,
            stage=self.stage_cut,
            current_stage=self.stage_cut,
            start_date=start_at - timedelta(hours=1),
            end_date=closed_end,
        )

        response = self.client.post(
            reverse("api_schedule_work_order", args=[work_order.id]),
            data=json.dumps({
                "stage_id": self.stage_cut.id,
                "machine_id": self.machine_cut.id,
                "start_date": start_at.isoformat(),
                "route_assignments": [
                    {
                        "stage_id": self.stage_cut.id,
                        "machine_id": self.machine_cut.id,
                        "selection_mode": "manual",
                    },
                    {
                        "stage_id": self.stage_pack.id,
                        "machine_id": self.machine_pack.id,
                        "selection_mode": "recommended",
                    },
                ],
            }),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200, response.content.decode())
        self.assertTrue(response.json()["success"])
        first_task = WorkOrder.objects.get(parent=work_order, stage=self.stage_cut)
        self.assertEqual(first_task.machine_id, self.machine_cut.id)
        self.assertEqual(first_task.start_date, start_at)

    def test_manual_route_stage_start_override_is_honored_for_later_stage(self):
        work_order = WorkOrder.objects.create(
            product_name="Widget Batch",
            bom=self.bom,
            quantity=25,
            status="pending",
            company=self.company,
        )
        start_at = timezone.now() + timedelta(minutes=20)
        stage_two_start = start_at + timedelta(hours=3)

        response = self.client.post(
            reverse("api_schedule_work_order", args=[work_order.id]),
            data=json.dumps({
                "stage_id": self.stage_cut.id,
                "machine_id": self.machine_cut.id,
                "start_date": start_at.isoformat(),
                "route_assignments": [
                    {
                        "stage_id": self.stage_cut.id,
                        "machine_id": self.machine_cut.id,
                        "selection_mode": "manual",
                    },
                    {
                        "stage_id": self.stage_pack.id,
                        "machine_id": self.machine_pack.id,
                        "selection_mode": "manual",
                        "start_date": stage_two_start.isoformat(),
                    },
                ],
            }),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200, response.content.decode())
        payload = response.json()
        self.assertTrue(payload["success"])

        stage_one = WorkOrder.objects.get(parent=work_order, stage=self.stage_cut)
        stage_two = WorkOrder.objects.get(parent=work_order, stage=self.stage_pack)
        self.assertEqual(stage_one.start_date, start_at)
        self.assertEqual(stage_two.start_date, stage_two_start)
        self.assertGreaterEqual(stage_two.start_date, stage_one.end_date)

    def test_manual_route_stage_start_override_before_previous_stage_is_rejected(self):
        work_order = WorkOrder.objects.create(
            product_name="Widget Batch",
            bom=self.bom,
            quantity=25,
            status="pending",
            company=self.company,
        )
        start_at = timezone.now() + timedelta(minutes=20)
        invalid_stage_two_start = start_at + timedelta(minutes=30)

        response = self.client.post(
            reverse("api_schedule_work_order", args=[work_order.id]),
            data=json.dumps({
                "stage_id": self.stage_cut.id,
                "machine_id": self.machine_cut.id,
                "start_date": start_at.isoformat(),
                "route_assignments": [
                    {
                        "stage_id": self.stage_cut.id,
                        "machine_id": self.machine_cut.id,
                        "selection_mode": "manual",
                    },
                    {
                        "stage_id": self.stage_pack.id,
                        "machine_id": self.machine_pack.id,
                        "selection_mode": "manual",
                        "start_date": invalid_stage_two_start.isoformat(),
                    },
                ],
            }),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400, response.content.decode())
        payload = response.json()
        self.assertFalse(payload["success"])
        self.assertIn("cannot start before", payload["error"].lower())

    def test_timeline_move_of_first_stage_shifts_following_stage_for_series_flow(self):
        work_order = WorkOrder.objects.create(
            product_name="Widget Batch",
            bom=self.bom,
            quantity=25,
            status="pending",
            company=self.company,
        )
        start_at = timezone.now() + timedelta(minutes=20)

        schedule_response = self.client.post(
            reverse("api_schedule_work_order", args=[work_order.id]),
            data=json.dumps({
                "stage_id": self.stage_cut.id,
                "machine_id": self.machine_cut.id,
                "start_date": start_at.isoformat(),
                "route_assignments": [
                    {
                        "stage_id": self.stage_cut.id,
                        "machine_id": self.machine_cut.id,
                        "selection_mode": "manual",
                    },
                    {
                        "stage_id": self.stage_pack.id,
                        "machine_id": self.machine_pack.id,
                        "selection_mode": "manual",
                    },
                ],
            }),
            content_type="application/json",
        )
        self.assertEqual(schedule_response.status_code, 200, schedule_response.content.decode())

        stage_one = WorkOrder.objects.get(parent=work_order, stage=self.stage_cut)
        stage_two = WorkOrder.objects.get(parent=work_order, stage=self.stage_pack)
        original_stage_two_start = stage_two.start_date

        moved_start = stage_one.start_date + timedelta(hours=2)
        moved_end = stage_one.end_date + timedelta(hours=2)

        response = self.client.post(
            reverse("update_work_order", args=[stage_one.id]),
            data={
                "id": stage_one.id,
                "machine_id": self.machine_cut.id,
                "stage_id": self.stage_cut.id,
                "status": "pending",
                "start_date": moved_start.isoformat(),
                "end_date": moved_end.isoformat(),
            },
        )

        self.assertEqual(response.status_code, 200, response.content.decode())
        payload = response.json()
        self.assertTrue(payload["success"])
        self.assertTrue(payload["rescheduled"])

        stage_one.refresh_from_db()
        stage_two.refresh_from_db()

        self.assertEqual(stage_one.start_date, moved_start)
        self.assertEqual(stage_one.end_date, moved_end)
        self.assertEqual(stage_two.start_date, original_stage_two_start + timedelta(hours=2))
        self.assertGreaterEqual(stage_two.start_date, stage_one.end_date)

    def test_timeline_move_of_later_stage_before_previous_stage_end_is_rejected(self):
        work_order = WorkOrder.objects.create(
            product_name="Widget Batch",
            bom=self.bom,
            quantity=25,
            status="pending",
            company=self.company,
        )
        start_at = timezone.now() + timedelta(minutes=20)

        schedule_response = self.client.post(
            reverse("api_schedule_work_order", args=[work_order.id]),
            data=json.dumps({
                "stage_id": self.stage_cut.id,
                "machine_id": self.machine_cut.id,
                "start_date": start_at.isoformat(),
                "route_assignments": [
                    {
                        "stage_id": self.stage_cut.id,
                        "machine_id": self.machine_cut.id,
                        "selection_mode": "manual",
                    },
                    {
                        "stage_id": self.stage_pack.id,
                        "machine_id": self.machine_pack.id,
                        "selection_mode": "manual",
                    },
                ],
            }),
            content_type="application/json",
        )
        self.assertEqual(schedule_response.status_code, 200, schedule_response.content.decode())

        stage_one = WorkOrder.objects.get(parent=work_order, stage=self.stage_cut)
        stage_two = WorkOrder.objects.get(parent=work_order, stage=self.stage_pack)
        original_stage_two_start = stage_two.start_date
        original_stage_two_end = stage_two.end_date

        invalid_start = stage_one.start_date + timedelta(minutes=15)
        invalid_end = stage_two.end_date + timedelta(minutes=15)

        response = self.client.post(
            reverse("update_work_order", args=[stage_two.id]),
            data={
                "id": stage_two.id,
                "machine_id": self.machine_pack.id,
                "stage_id": self.stage_pack.id,
                "status": "pending",
                "start_date": invalid_start.isoformat(),
                "end_date": invalid_end.isoformat(),
            },
        )

        self.assertEqual(response.status_code, 400, response.content.decode())
        payload = response.json()
        self.assertFalse(payload["success"])
        self.assertIn("cannot start before", payload["error"].lower())

        stage_two.refresh_from_db()
        self.assertEqual(stage_two.start_date, original_stage_two_start)
        self.assertEqual(stage_two.end_date, original_stage_two_end)

    def test_supervisor_filter_only_shows_current_stage_tasks_for_matching_department(self):
        parent = WorkOrder.objects.create(
            product_name="Widget Batch",
            bom=self.bom,
            quantity=25,
            status="pending",
            current_stage=self.stage_cut,
            company=self.company,
        )
        start_at = timezone.now()
        stage_one = WorkOrder.objects.create(
            parent=parent,
            product_name="Widget Batch - Cutting",
            bom=self.bom,
            quantity=25,
            machine=self.machine_cut,
            stage=self.stage_cut,
            current_stage=self.stage_cut,
            status="pending",
            start_date=start_at,
            end_date=start_at + timedelta(hours=1),
            company=self.company,
        )
        stage_two = WorkOrder.objects.create(
            parent=parent,
            product_name="Widget Batch - Packing",
            bom=self.bom,
            quantity=25,
            machine=self.machine_pack,
            stage=self.stage_pack,
            current_stage=self.stage_pack,
            status="pending",
            start_date=stage_one.end_date + timedelta(minutes=5),
            end_date=stage_one.end_date + timedelta(hours=1),
            company=self.company,
        )

        visible = DashboardService._filter_work_orders_for_viewer(
            WorkOrder.objects.filter(parent=parent).select_related("parent"),
            viewer_role="supervisor",
            shift_config=self._active_shift_config(),
            viewer=self.supervisor_cut,
        )
        self.assertEqual([wo.id for wo in visible], [stage_one.id])
        not_visible = DashboardService._filter_work_orders_for_viewer(
            WorkOrder.objects.filter(parent=parent).select_related("parent"),
            viewer_role="supervisor",
            shift_config=self._active_shift_config(),
            viewer=self.supervisor_pack,
        )
        self.assertEqual(not_visible, [])

        stage_one.status = "completed"
        stage_one.save(update_fields=["status"])
        parent.current_stage = self.stage_pack
        parent.save(update_fields=["current_stage"])

        visible_after_advance = DashboardService._filter_work_orders_for_viewer(
            WorkOrder.objects.filter(parent=parent).select_related("parent"),
            viewer_role="supervisor",
            shift_config=self._active_shift_config(),
            viewer=self.supervisor_pack,
        )
        self.assertEqual([wo.id for wo in visible_after_advance], [stage_two.id])
        no_longer_visible = DashboardService._filter_work_orders_for_viewer(
            WorkOrder.objects.filter(parent=parent).select_related("parent"),
            viewer_role="supervisor",
            shift_config=self._active_shift_config(),
            viewer=self.supervisor_cut,
        )
        self.assertEqual(no_longer_visible, [])

    def test_planner_filter_keeps_all_planned_route_stages_visible(self):
        parent = WorkOrder.objects.create(
            product_name="Widget Batch",
            bom=self.bom,
            quantity=25,
            status="pending",
            current_stage=self.stage_cut,
            company=self.company,
        )
        start_at = timezone.now() + timedelta(minutes=30)
        stage_one = WorkOrder.objects.create(
            parent=parent,
            product_name="Widget Batch - Cutting",
            bom=self.bom,
            quantity=25,
            machine=self.machine_cut,
            stage=self.stage_cut,
            current_stage=self.stage_cut,
            status="pending",
            start_date=start_at,
            end_date=start_at + timedelta(hours=1),
            company=self.company,
        )
        stage_two = WorkOrder.objects.create(
            parent=parent,
            product_name="Widget Batch - Packing",
            bom=self.bom,
            quantity=25,
            machine=self.machine_pack,
            stage=self.stage_pack,
            current_stage=self.stage_pack,
            status="pending",
            start_date=stage_one.end_date + timedelta(minutes=15),
            end_date=stage_one.end_date + timedelta(hours=1),
            company=self.company,
        )

        visible = DashboardService._filter_work_orders_for_viewer(
            WorkOrder.objects.filter(parent=parent).select_related("parent"),
            viewer_role="planner",
            shift_config=self._active_shift_config(),
            viewer=self.planner,
        )

        self.assertEqual({wo.id for wo in visible}, {stage_one.id, stage_two.id})

    def test_supervisor_filter_ignores_parent_stage_pointer_when_earlier_stage_still_open(self):
        parent = WorkOrder.objects.create(
            product_name="Widget Batch",
            bom=self.bom,
            quantity=25,
            status="in_progress",
            current_stage=self.stage_pack,
            company=self.company,
        )
        start_at = timezone.now() + timedelta(minutes=15)
        stage_one = WorkOrder.objects.create(
            parent=parent,
            product_name="Widget Batch - Cutting",
            bom=self.bom,
            quantity=25,
            machine=self.machine_cut,
            stage=self.stage_cut,
            current_stage=self.stage_cut,
            status="in_progress",
            start_date=start_at,
            end_date=start_at + timedelta(hours=1),
            company=self.company,
        )
        WorkOrder.objects.create(
            parent=parent,
            product_name="Widget Batch - Packing",
            bom=self.bom,
            quantity=10,
            machine=self.machine_pack,
            stage=self.stage_pack,
            current_stage=self.stage_pack,
            status="pending",
            start_date=stage_one.end_date + timedelta(minutes=5),
            end_date=stage_one.end_date + timedelta(hours=1),
            company=self.company,
        )

        visible = DashboardService._filter_work_orders_for_viewer(
            WorkOrder.objects.filter(parent=parent).select_related("parent"),
            viewer_role="supervisor",
            shift_config=self._active_shift_config(),
            viewer=self.supervisor_cut,
        )

        self.assertEqual([wo.id for wo in visible], [stage_one.id])

    def test_supervisor_filter_after_future_route_schedule_hides_stages_until_start_time(self):
        work_order = WorkOrder.objects.create(
            product_name="Widget Batch",
            bom=self.bom,
            quantity=25,
            status="pending",
            company=self.company,
        )
        start_at = timezone.now() + timedelta(minutes=20)

        response = self.client.post(
            reverse("api_schedule_work_order", args=[work_order.id]),
            data=json.dumps({
                "stage_id": self.stage_cut.id,
                "machine_id": self.machine_cut.id,
                "start_date": start_at.isoformat(),
                "route_assignments": [
                    {
                        "stage_id": self.stage_cut.id,
                        "machine_id": self.machine_cut.id,
                        "selection_mode": "manual",
                    },
                    {
                        "stage_id": self.stage_pack.id,
                        "machine_id": self.machine_pack.id,
                        "selection_mode": "manual",
                    },
                ],
            }),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200, response.content.decode())

        visible_cut = DashboardService._filter_work_orders_for_viewer(
            WorkOrder.objects.filter(parent=work_order).select_related("parent"),
            viewer_role="supervisor",
            shift_config=self._active_shift_config(),
            viewer=self.supervisor_cut,
        )
        visible_pack = DashboardService._filter_work_orders_for_viewer(
            WorkOrder.objects.filter(parent=work_order).select_related("parent"),
            viewer_role="supervisor",
            shift_config=self._active_shift_config(),
            viewer=self.supervisor_pack,
        )

        self.assertEqual(visible_cut, [])
        self.assertEqual(visible_pack, [])

    def test_production_supervisor_sees_stage_when_no_exact_department_supervisor_exists(self):
        parent = WorkOrder.objects.create(
            product_name="Widget Batch",
            bom=self.bom,
            quantity=25,
            status="pending",
            current_stage=self.stage_cut,
            company=self.company,
        )
        start_at = timezone.now()
        stage_one = WorkOrder.objects.create(
            parent=parent,
            product_name="Widget Batch - Cutting",
            bom=self.bom,
            quantity=25,
            machine=self.machine_cut,
            stage=self.stage_cut,
            current_stage=self.stage_cut,
            status="pending",
            start_date=start_at,
            end_date=start_at + timedelta(hours=1),
            company=self.company,
        )
        WorkOrder.objects.create(
            parent=parent,
            product_name="Widget Batch - Packing",
            bom=self.bom,
            quantity=25,
            machine=self.machine_pack,
            stage=self.stage_pack,
            current_stage=self.stage_pack,
            status="pending",
            start_date=stage_one.end_date + timedelta(minutes=5),
            end_date=stage_one.end_date + timedelta(hours=1),
            company=self.company,
        )

        self.supervisor_cut.profile.department = "Other"
        self.supervisor_cut.profile.save(update_fields=["department"])

        visible_prod = DashboardService._filter_work_orders_for_viewer(
            WorkOrder.objects.filter(parent=parent).select_related("parent"),
            viewer_role="supervisor",
            shift_config=self._active_shift_config(),
            viewer=self.supervisor_production,
        )
        visible_pack = DashboardService._filter_work_orders_for_viewer(
            WorkOrder.objects.filter(parent=parent).select_related("parent"),
            viewer_role="supervisor",
            shift_config=self._active_shift_config(),
            viewer=self.supervisor_pack,
        )

        self.assertEqual([wo.id for wo in visible_prod], [stage_one.id])
        self.assertEqual(visible_pack, [])

    def test_supervisor_filter_matches_any_department_assignment_value(self):
        parent = WorkOrder.objects.create(
            product_name="Widget Batch",
            bom=self.bom,
            quantity=25,
            status="pending",
            current_stage=self.stage_cut,
            company=self.company,
        )
        start_at = timezone.now()
        stage_one = WorkOrder.objects.create(
            parent=parent,
            product_name="Widget Batch - Cutting",
            bom=self.bom,
            quantity=25,
            machine=self.machine_cut,
            stage=self.stage_cut,
            current_stage=self.stage_cut,
            status="pending",
            start_date=start_at,
            end_date=start_at + timedelta(hours=1),
            company=self.company,
        )

        self.supervisor_pack.profile.department = "Other\nCutting"
        self.supervisor_pack.profile.save(update_fields=["department"])

        visible = DashboardService._filter_work_orders_for_viewer(
            WorkOrder.objects.filter(parent=parent).select_related("parent"),
            viewer_role="supervisor",
            shift_config=self._active_shift_config(),
            viewer=self.supervisor_pack,
        )

        self.assertEqual([wo.id for wo in visible], [stage_one.id])

    def test_planner_schedule_api_can_override_operation_flow_mode_to_parallel(self):
        work_order = WorkOrder.objects.create(
            product_name="Widget Batch",
            bom=self.bom,
            quantity=25,
            status="pending",
            company=self.company,
        )
        start_at = timezone.now() + timedelta(minutes=20)

        response = self.client.post(
            reverse("api_schedule_work_order", args=[work_order.id]),
            data=json.dumps({
                "stage_id": self.stage_cut.id,
                "machine_id": self.machine_cut.id,
                "start_date": start_at.isoformat(),
                "operation_flow_mode": "parallel",
                "route_assignments": [
                    {
                        "stage_id": self.stage_cut.id,
                        "machine_id": self.machine_cut.id,
                        "selection_mode": "manual",
                    },
                    {
                        "stage_id": self.stage_pack.id,
                        "machine_id": self.machine_pack.id,
                        "selection_mode": "manual",
                    },
                ],
            }),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200, response.content.decode())
        work_order.refresh_from_db()
        self.assertEqual(work_order.operation_flow_mode, "parallel")

        route_tasks = list(
            WorkOrder.objects.filter(parent=work_order).select_related("stage", "machine").order_by("stage__name", "id")
        )
        self.assertEqual(len(route_tasks), 2)
        for child in route_tasks:
            self.assertEqual(child.operation_flow_mode, "parallel")
            self.assertEqual(child.start_date, start_at)

    def test_route_schedule_keeps_unassigned_future_stage_with_planner(self):
        work_order = WorkOrder.objects.create(
            product_name="Widget Batch",
            bom=self.bom,
            quantity=25,
            status="pending",
            company=self.company,
        )
        start_at = timezone.now() + timedelta(minutes=20)

        response = self.client.post(
            reverse("api_schedule_work_order", args=[work_order.id]),
            data=json.dumps({
                "stage_id": self.stage_cut.id,
                "machine_id": self.machine_cut.id,
                "start_date": start_at.isoformat(),
                "route_assignments": [
                    {
                        "stage_id": self.stage_cut.id,
                        "machine_id": self.machine_cut.id,
                        "selection_mode": "manual",
                    },
                    {
                        "stage_id": self.stage_pack.id,
                        "machine_id": "",
                        "selection_mode": "auto",
                    },
                ],
            }),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200, response.content.decode())
        stage_one = WorkOrder.objects.get(parent=work_order, stage=self.stage_cut)
        stage_two = WorkOrder.objects.get(parent=work_order, stage=self.stage_pack)

        self.assertEqual(stage_one.machine, self.machine_cut)
        self.assertIsNotNone(stage_one.start_date)
        self.assertIsNone(stage_two.machine)
        self.assertIsNone(stage_two.start_date)
        self.assertIsNone(stage_two.end_date)

    def test_planner_schedule_api_full_route_still_works_when_root_stage_is_already_set(self):
        work_order = WorkOrder.objects.create(
            product_name="Widget Batch",
            bom=self.bom,
            quantity=25,
            status="pending",
            company=self.company,
            stage=self.stage_cut,
            current_stage=self.stage_cut,
        )
        start_at = timezone.now() + timedelta(minutes=20)

        response = self.client.post(
            reverse("api_schedule_work_order", args=[work_order.id]),
            data=json.dumps({
                "stage_id": self.stage_cut.id,
                "machine_id": self.machine_cut.id,
                "start_date": start_at.isoformat(),
                "route_assignments": [
                    {
                        "stage_id": self.stage_cut.id,
                        "machine_id": self.machine_cut.id,
                        "selection_mode": "manual",
                    },
                    {
                        "stage_id": self.stage_pack.id,
                        "machine_id": self.machine_pack.id,
                        "selection_mode": "manual",
                    },
                ],
            }),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200, response.content.decode())
        payload = response.json()
        self.assertTrue(payload["success"])

        route_tasks = list(
            WorkOrder.objects.filter(parent=work_order).select_related("stage", "machine").order_by("start_date", "id")
        )
        self.assertEqual(len(route_tasks), 2)
        self.assertEqual(route_tasks[0].stage, self.stage_cut)
        self.assertEqual(route_tasks[1].stage, self.stage_pack)

    def test_parallel_flow_shows_each_stage_to_its_supervisor_without_waiting(self):
        parent = WorkOrder.objects.create(
            product_name="Widget Batch",
            bom=self.bom,
            quantity=25,
            status="pending",
            current_stage=self.stage_cut,
            operation_flow_mode="parallel",
            company=self.company,
        )
        settings, _ = SystemSettings.objects.get_or_create(company=self.company)
        settings.default_operation_flow_mode = "parallel"
        settings.save(update_fields=["default_operation_flow_mode"])

        start_at = timezone.now() + timedelta(minutes=15)
        stage_one = WorkOrder.objects.create(
            parent=parent,
            product_name="Widget Batch - Cutting",
            bom=self.bom,
            quantity=25,
            machine=self.machine_cut,
            stage=self.stage_cut,
            current_stage=self.stage_cut,
            status="pending",
            start_date=start_at,
            end_date=start_at + timedelta(hours=1),
            operation_flow_mode="parallel",
            company=self.company,
        )
        stage_two = WorkOrder.objects.create(
            parent=parent,
            product_name="Widget Batch - Packing",
            bom=self.bom,
            quantity=25,
            machine=self.machine_pack,
            stage=self.stage_pack,
            current_stage=self.stage_pack,
            status="pending",
            start_date=start_at,
            end_date=start_at + timedelta(hours=1),
            operation_flow_mode="parallel",
            company=self.company,
        )

        visible_cut = DashboardService._filter_work_orders_for_viewer(
            WorkOrder.objects.filter(parent=parent).select_related("parent"),
            viewer_role="supervisor",
            shift_config=self._active_shift_config(),
            viewer=self.supervisor_cut,
        )
        visible_pack = DashboardService._filter_work_orders_for_viewer(
            WorkOrder.objects.filter(parent=parent).select_related("parent"),
            viewer_role="supervisor",
            shift_config=self._active_shift_config(),
            viewer=self.supervisor_pack,
        )

        self.assertEqual([wo.id for wo in visible_cut], [stage_one.id])
        self.assertEqual([wo.id for wo in visible_pack], [stage_two.id])

    def test_work_order_details_api_only_allows_route_planning_on_root_work_order(self):
        parent = WorkOrder.objects.create(
            product_name="Widget Batch",
            bom=self.bom,
            quantity=25,
            status="pending",
            current_stage=self.stage_cut,
            company=self.company,
        )
        child = WorkOrder.objects.create(
            parent=parent,
            product_name="Widget Batch - Cutting",
            bom=self.bom,
            quantity=25,
            machine=self.machine_cut,
            stage=self.stage_cut,
            current_stage=self.stage_cut,
            status="pending",
            company=self.company,
        )

        root_response = self.client.get(reverse("get_work_order", args=[parent.id]))
        self.assertEqual(root_response.status_code, 200, root_response.content.decode())
        root_payload = root_response.json()
        self.assertTrue(root_payload["success"])
        self.assertTrue(root_payload["work_order"]["route_container"])
        self.assertEqual(len(root_payload["route_stages"]), 2)

        child_response = self.client.get(reverse("get_work_order", args=[child.id]))
        self.assertEqual(child_response.status_code, 200, child_response.content.decode())
        child_payload = child_response.json()
        self.assertTrue(child_payload["success"])
        self.assertFalse(child_payload["work_order"]["route_container"])
        self.assertEqual(child_payload["route_stages"], [])

    def test_supervisor_dashboard_hides_future_next_stage_until_scheduled_time(self):
        settings, _ = SystemSettings.objects.get_or_create(company=self.company)
        now = timezone.localtime()
        settings.shift_configuration = {
            "morning": {
                "start": (now - timedelta(minutes=30)).strftime("%H:%M"),
                "end": (now + timedelta(minutes=30)).strftime("%H:%M"),
            },
            "afternoon": {
                "start": (now + timedelta(hours=3)).strftime("%H:%M"),
                "end": (now + timedelta(hours=5)).strftime("%H:%M"),
            },
            "night": {"start": "23:30", "end": "05:30"},
        }
        settings.save(update_fields=["shift_configuration"])

        parent = WorkOrder.objects.create(
            product_name="Widget Batch",
            bom=self.bom,
            quantity=25,
            status="in_progress",
            current_stage=self.stage_pack,
            company=self.company,
        )
        stage_one = WorkOrder.objects.create(
            parent=parent,
            product_name="Widget Batch - Cutting",
            bom=self.bom,
            quantity=25,
            machine=self.machine_cut,
            stage=self.stage_cut,
            current_stage=self.stage_cut,
            status="completed",
            start_date=timezone.now() - timedelta(hours=2),
            end_date=timezone.now() - timedelta(hours=1),
            company=self.company,
        )
        stage_two = WorkOrder.objects.create(
            parent=parent,
            product_name="Widget Batch - Packing",
            bom=self.bom,
            quantity=25,
            machine=self.machine_pack,
            stage=self.stage_pack,
            current_stage=self.stage_pack,
            status="pending",
            start_date=timezone.now() + timedelta(hours=3),
            end_date=timezone.now() + timedelta(hours=4),
            company=self.company,
        )

        visible_in_shift_only = DashboardService._filter_work_orders_for_viewer(
            WorkOrder.objects.filter(parent=parent).select_related("parent"),
            viewer_role="supervisor",
            shift_config=settings.shift_configuration,
            viewer=self.supervisor_pack,
        )
        self.assertEqual(visible_in_shift_only, [])

        self.client.force_login(self.supervisor_pack)
        response = self.client.get(reverse("supervisor_dashboard"))

        self.assertEqual(response.status_code, 200)
        pending_ids = {wo.id for wo in response.context["pending_tasks"]}
        self.assertNotIn(stage_two.id, pending_ids)
        self.assertNotIn(stage_one.id, pending_ids)
        self.assertEqual(response.context["upcoming_pending_tasks_count"], 0)

    def test_supervisor_shift_filter_keeps_previous_shift_open_work_order_visible(self):
        now = timezone.localtime()
        shift_config = {
            "morning": {
                "start": (now - timedelta(minutes=30)).strftime("%H:%M"),
                "end": (now + timedelta(hours=4)).strftime("%H:%M"),
            },
            "afternoon": {"start": "23:00", "end": "23:30"},
            "night": {"start": "23:30", "end": "05:30"},
        }
        inherited_open_wo = WorkOrder.objects.create(
            product_name="Inherited Cutting Batch",
            bom=self.bom,
            quantity=25,
            machine=self.machine_cut,
            stage=self.stage_cut,
            current_stage=self.stage_cut,
            status="in_progress",
            start_date=timezone.now() - timedelta(hours=3),
            company=self.company,
        )

        visible = DashboardService._filter_work_orders_for_viewer(
            WorkOrder.objects.filter(id=inherited_open_wo.id).select_related("stage", "current_stage"),
            viewer_role="supervisor",
            shift_config=shift_config,
            viewer=self.supervisor_cut,
        )

        self.assertEqual([wo.id for wo in visible], [inherited_open_wo.id])

    def test_supervisor_timeline_includes_previous_shift_open_work_order(self):
        settings, _ = SystemSettings.objects.get_or_create(company=self.company)
        now = timezone.localtime()
        settings.shift_configuration = {
            "morning": {
                "start": (now - timedelta(minutes=30)).strftime("%H:%M"),
                "end": (now + timedelta(hours=4)).strftime("%H:%M"),
            },
            "afternoon": {"start": "23:00", "end": "23:30"},
            "night": {"start": "23:30", "end": "05:30"},
        }
        settings.save(update_fields=["shift_configuration"])

        inherited_open_wo = WorkOrder.objects.create(
            product_name="Inherited Timeline Batch",
            bom=self.bom,
            quantity=25,
            machine=self.machine_cut,
            stage=self.stage_cut,
            current_stage=self.stage_cut,
            status="in_progress",
            start_date=timezone.now() - timedelta(days=1, hours=3),
            company=self.company,
        )

        timeline = DashboardService.get_timeline_data(
            self.company,
            viewer_role="supervisor",
            viewer=self.supervisor_cut,
        )
        tasks_by_id = {task["id"]: task for task in timeline["tasks"]}

        self.assertIn(inherited_open_wo.id, tasks_by_id)
        inherited_task = tasks_by_id[inherited_open_wo.id]
        self.assertTrue(inherited_task["timeline_inherited"])
        self.assertEqual(inherited_task["start"][:10], timezone.localdate().isoformat())
        self.assertEqual(
            inherited_task["original_start"][:10],
            inherited_open_wo.start_date.date().isoformat(),
        )

    def test_supervisor_department_match_falls_back_to_stage_name_and_machine_type(self):
        self.stage_pack.category = ""
        self.stage_pack.save(update_fields=["category"])
        self.machine_pack.category = ""
        self.machine_pack.save(update_fields=["category"])

        parent = WorkOrder.objects.create(
            product_name="Widget Batch",
            bom=self.bom,
            quantity=25,
            status="pending",
            current_stage=self.stage_pack,
            company=self.company,
        )
        stage_two = WorkOrder.objects.create(
            parent=parent,
            product_name="Widget Batch - Packing",
            bom=self.bom,
            quantity=25,
            machine=self.machine_pack,
            stage=self.stage_pack,
            current_stage=self.stage_pack,
            status="pending",
            start_date=timezone.now() + timedelta(minutes=15),
            end_date=timezone.now() + timedelta(hours=1),
            company=self.company,
        )

        visible_pack = DashboardService._filter_work_orders_for_viewer(
            WorkOrder.objects.filter(parent=parent).select_related("parent"),
            viewer_role="supervisor",
            shift_config=self._active_shift_config(),
            viewer=self.supervisor_pack,
        )

        self.assertEqual([wo.id for wo in visible_pack], [stage_two.id])
