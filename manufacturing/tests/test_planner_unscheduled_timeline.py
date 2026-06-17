from django.test import TestCase

from manufacturing.models import WorkOrder
from manufacturing.services import DashboardService
from manufacturing.tests.utils import create_company, create_user_with_role


class PlannerUnscheduledTimelineTests(TestCase):
    def setUp(self):
        self.company = create_company("Timeline Co")
        self.planner = create_user_with_role("planner_unscheduled_timeline", "planner", self.company)

        self.unscheduled_work_order = WorkOrder.objects.create(
            company=self.company,
            product_name="Pending Bottle",
            quantity=12,
            status="pending",
        )

    def test_planner_dashboard_context_keeps_unscheduled_work_orders_in_timeline_payload(self):
        context = DashboardService.get_dashboard_context(
            self.company,
            viewer_role="planner",
            viewer=self.planner,
        )

        task_ids = {item["id"] for item in context["tasks_data"]}

        self.assertIn(self.unscheduled_work_order.id, task_ids)
        pending_task = next(item for item in context["tasks_data"] if item["id"] == self.unscheduled_work_order.id)
        self.assertIsNone(pending_task["machine_id"])
        self.assertIsNone(pending_task["start"])

    def test_timeline_api_only_includes_unscheduled_work_orders_when_requested(self):
        without_unscheduled = DashboardService.get_timeline_data(
            self.company,
            include_unscheduled=False,
            viewer_role="planner",
            viewer=self.planner,
        )
        with_unscheduled = DashboardService.get_timeline_data(
            self.company,
            include_unscheduled=True,
            viewer_role="planner",
            viewer=self.planner,
        )

        without_ids = {item["id"] for item in without_unscheduled["tasks"]}
        with_ids = {item["id"] for item in with_unscheduled["tasks"]}

        self.assertNotIn(self.unscheduled_work_order.id, without_ids)
        self.assertIn(self.unscheduled_work_order.id, with_ids)
