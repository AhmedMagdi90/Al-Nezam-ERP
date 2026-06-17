from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from manufacturing.models import Company, Machine, ProductionLog, QualityCheck, WorkOrder
from manufacturing.services import DashboardService, WorkOrderCycleService
from manufacturing.tests.utils import create_user_with_role


class WorkOrderCycleStateTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Cycle State Co")
        self.planner = create_user_with_role("cycle_planner", "planner", self.company)
        self.supervisor = create_user_with_role("cycle_supervisor", "supervisor", self.company)
        self.worker = create_user_with_role("cycle_worker", "worker", self.company)
        self.machine = Machine.objects.create(
            company=self.company,
            name="Cutting Machine",
            code="CUT-01",
            status="operational",
        )

    def _make_wo(self, **overrides):
        defaults = {
            "company": self.company,
            "product_name": "Cycle Product",
            "quantity": 10,
            "status": "pending",
            "machine": self.machine,
        }
        defaults.update(overrides)
        return WorkOrder.objects.create(**defaults)

    def test_pending_work_order_without_worker_needs_supervisor_dispatch(self):
        wo = self._make_wo(start_date=timezone.now())

        cycle_state = WorkOrderCycleService.describe(wo)

        self.assertEqual(cycle_state["step"], "supervisor_dispatch")
        self.assertEqual(cycle_state["owner_role"], "supervisor")
        self.assertTrue(cycle_state["blocked"])
        self.assertEqual(cycle_state["blocker_reason"], "Worker is not assigned.")

    def test_pending_work_order_without_machine_is_blocked_in_planning(self):
        wo = self._make_wo(machine=None)

        cycle_state = WorkOrderCycleService.describe(wo)

        self.assertEqual(cycle_state["step"], "planning")
        self.assertEqual(cycle_state["owner_role"], "planner")
        self.assertTrue(cycle_state["blocked"])
        self.assertEqual(cycle_state["blocker_reason"], "Machine is not assigned.")

    def test_pending_work_order_without_schedule_is_blocked_in_planning(self):
        wo = self._make_wo(assigned_worker=self.worker)

        cycle_state = WorkOrderCycleService.describe(wo)

        self.assertEqual(cycle_state["step"], "planning")
        self.assertEqual(cycle_state["owner_role"], "planner")
        self.assertTrue(cycle_state["blocked"])
        self.assertEqual(cycle_state["blocker_reason"], "Start time is not scheduled.")

    def test_work_order_with_unavailable_machine_is_blocked(self):
        self.machine.status = "broken"
        self.machine.save(update_fields=["status"])
        wo = self._make_wo(assigned_worker=self.worker, start_date=timezone.now())

        cycle_state = WorkOrderCycleService.describe(wo)

        self.assertEqual(cycle_state["step"], "machine_unavailable")
        self.assertEqual(cycle_state["owner_role"], "planner")
        self.assertTrue(cycle_state["blocked"])
        self.assertEqual(cycle_state["next_action"], "Assign another machine or resolve fault")
        self.assertIn("Cutting Machine", cycle_state["blocker_reason"])

    def test_pending_qc_blocks_cycle_until_quality_inspection(self):
        wo = self._make_wo(status="in_progress", qc_requirement=True)
        QualityCheck.objects.create(work_order=wo, status="new")

        cycle_state = WorkOrderCycleService.describe(wo)

        self.assertEqual(cycle_state["step"], "quality_inspection")
        self.assertEqual(cycle_state["owner_role"], "quality")
        self.assertTrue(cycle_state["blocked"])

    def test_pending_production_log_needs_supervisor_approval(self):
        wo = self._make_wo(status="in_progress", assigned_worker=self.worker)
        ProductionLog.objects.create(
            work_order=wo,
            worker=self.worker,
            quantity=4,
            status="pending",
        )

        cycle_state = WorkOrderCycleService.describe(wo)

        self.assertEqual(cycle_state["step"], "production_approval")
        self.assertEqual(cycle_state["owner_role"], "supervisor")
        self.assertTrue(cycle_state["blocked"])
        self.assertEqual(cycle_state["blocker_reason"], "Production output is waiting for supervisor approval.")

    def test_next_stage_ready_is_blocked_for_planner_release(self):
        wo = self._make_wo(next_stage_ready=True, assigned_worker=self.worker, start_date=timezone.now())

        cycle_state = WorkOrderCycleService.describe(wo)

        self.assertEqual(cycle_state["step"], "next_stage_release")
        self.assertEqual(cycle_state["owner_role"], "planner")
        self.assertTrue(cycle_state["blocked"])
        self.assertEqual(cycle_state["blocker_reason"], "Next stage is ready and waiting for planner release.")

    def test_completed_work_order_is_blocked_for_planner_close(self):
        wo = self._make_wo(status="completed", assigned_worker=self.worker, start_date=timezone.now())

        cycle_state = WorkOrderCycleService.describe(wo)

        self.assertEqual(cycle_state["step"], "planner_close")
        self.assertEqual(cycle_state["owner_role"], "planner")
        self.assertTrue(cycle_state["blocked"])
        self.assertEqual(cycle_state["blocker_reason"], "Completed work order is waiting for planner close.")

    def test_work_order_detail_api_exposes_cycle_state(self):
        self.client.force_login(self.planner)
        wo = self._make_wo(machine=None)

        response = self.client.get(reverse("api_workorder_detail", args=[wo.id]))

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["work_order"]["cycle_state"]["step"], "planning")
        self.assertTrue(payload["work_order"]["cycle_state"]["blocked"])

    def test_work_order_modal_api_exposes_cycle_state(self):
        self.client.force_login(self.planner)
        wo = self._make_wo(machine=None)

        response = self.client.get(reverse("get_work_order", args=[wo.id]))

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["work_order"]["cycle_state"]["step"], "planning")
        self.assertEqual(payload["work_order"]["cycle_state"]["next_action"], "Assign machine and schedule work")

    def test_dashboard_context_exposes_cycle_state_for_planner_intake(self):
        wo = self._make_wo(machine=None)

        context = DashboardService.get_dashboard_context(
            self.company,
            viewer_role="planner",
            viewer=self.planner,
        )

        intake_row = next(row for row in context["intake_orders_data"] if row["id"] == wo.id)
        self.assertEqual(intake_row["cycle_state"]["step"], "planning")
        self.assertEqual(intake_row["cycle_state"]["next_action"], "Assign machine and schedule work")

    def test_supervisor_dashboard_renders_cycle_next_action(self):
        wo = self._make_wo(assigned_worker=self.worker, start_date=timezone.now())
        self.client.force_login(self.supervisor)

        response = self.client.get(reverse("supervisor_dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, wo.display_work_order_code)
        self.assertContains(response, "Start production")
