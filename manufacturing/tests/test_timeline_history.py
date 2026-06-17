from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from manufacturing.models import Machine, ProductionLog, WorkOrder
from manufacturing.services import DashboardService, WorkOrderService
from manufacturing.tests.utils import create_company


class TimelineHistoryTests(TestCase):
    def setUp(self):
        self.company = create_company("Timeline Co")
        self.machine = Machine.objects.create(
            name="Timeline Machine",
            code="TL-01",
            status="operational",
            company=self.company,
        )

    def test_planner_timeline_hides_canceled_work_orders_by_default(self):
        now = timezone.now()
        completed = WorkOrder.objects.create(
            product_name="Completed WO",
            quantity=10,
            status="completed",
            company=self.company,
            machine=self.machine,
            start_date=now - timedelta(hours=4),
            end_date=now - timedelta(hours=2),
        )
        canceled = WorkOrder.objects.create(
            product_name="Canceled WO",
            quantity=8,
            status="canceled",
            company=self.company,
            machine=self.machine,
            start_date=now - timedelta(hours=2),
            end_date=now - timedelta(hours=1),
        )
        archived = WorkOrder.objects.create(
            product_name="Archived WO",
            quantity=6,
            status="archived",
            company=self.company,
            machine=self.machine,
            start_date=now - timedelta(hours=3),
            end_date=now - timedelta(hours=2),
        )
        ProductionLog.objects.create(work_order=completed, quantity=4, status="approved")
        ProductionLog.objects.create(work_order=completed, quantity=1, status="pending")
        ProductionLog.objects.create(work_order=canceled, quantity=2, status="approved")

        timeline = DashboardService.get_timeline_data(self.company, viewer_role="planner")
        tasks_by_id = {task["id"]: task for task in timeline["tasks"]}

        self.assertIn(completed.id, tasks_by_id)
        self.assertNotIn(canceled.id, tasks_by_id)
        self.assertNotIn(archived.id, tasks_by_id)
        self.assertEqual(tasks_by_id[completed.id]["finished_qty"], 5)
        self.assertEqual(tasks_by_id[completed.id]["remaining_qty"], 5)

    def test_planner_timeline_can_show_canceled_work_orders_when_filtered(self):
        now = timezone.now()
        completed = WorkOrder.objects.create(
            product_name="Completed WO",
            quantity=10,
            status="completed",
            company=self.company,
            machine=self.machine,
            start_date=now - timedelta(hours=4),
            end_date=now - timedelta(hours=2),
        )
        canceled = WorkOrder.objects.create(
            product_name="Canceled WO",
            quantity=8,
            status="canceled",
            company=self.company,
            machine=self.machine,
            start_date=now - timedelta(hours=2),
            end_date=now - timedelta(hours=1),
        )
        archived = WorkOrder.objects.create(
            product_name="Archived WO",
            quantity=6,
            status="archived",
            company=self.company,
            machine=self.machine,
            start_date=now - timedelta(hours=3),
            end_date=now - timedelta(hours=2),
        )
        ProductionLog.objects.create(work_order=completed, quantity=4, status="approved")
        ProductionLog.objects.create(work_order=canceled, quantity=2, status="approved")

        timeline = DashboardService.get_timeline_data(
            self.company,
            viewer_role="planner",
            status_filter="canceled",
        )
        tasks_by_id = {task["id"]: task for task in timeline["tasks"]}

        self.assertNotIn(completed.id, tasks_by_id)
        self.assertIn(canceled.id, tasks_by_id)
        self.assertNotIn(archived.id, tasks_by_id)
        self.assertEqual(tasks_by_id[canceled.id]["finished_qty"], 2)
        self.assertEqual(tasks_by_id[canceled.id]["remaining_qty"], 6)

    def test_dashboard_context_hides_canceled_timeline_history_by_default(self):
        now = timezone.now()
        completed = WorkOrder.objects.create(
            product_name="Completed Dashboard WO",
            quantity=12,
            status="completed",
            company=self.company,
            machine=self.machine,
            start_date=now - timedelta(hours=4),
            end_date=now - timedelta(hours=1),
        )
        canceled = WorkOrder.objects.create(
            product_name="Canceled Dashboard WO",
            quantity=9,
            status="canceled",
            company=self.company,
            machine=self.machine,
            start_date=now - timedelta(hours=3),
            end_date=now - timedelta(hours=2),
        )
        archived = WorkOrder.objects.create(
            product_name="Archived Dashboard WO",
            quantity=5,
            status="archived",
            company=self.company,
            machine=self.machine,
            start_date=now - timedelta(hours=2),
            end_date=now - timedelta(hours=1),
        )
        ProductionLog.objects.create(work_order=completed, quantity=7, status="approved")
        ProductionLog.objects.create(work_order=canceled, quantity=3, status="pending")

        context = DashboardService.get_dashboard_context(self.company, viewer_role="planner")
        tasks_by_id = {task["id"]: task for task in context["tasks_data"]}

        self.assertIn(completed.id, tasks_by_id)
        self.assertNotIn(canceled.id, tasks_by_id)
        self.assertNotIn(archived.id, tasks_by_id)
        self.assertEqual(tasks_by_id[completed.id]["finished_qty"], 7)
        self.assertEqual(tasks_by_id[completed.id]["remaining_qty"], 5)

    def test_find_next_available_slot_ignores_completed_and_canceled_history(self):
        now = timezone.now().replace(second=0, microsecond=0)
        start_after = now

        WorkOrder.objects.create(
            product_name="Completed WO",
            quantity=10,
            status="completed",
            company=self.company,
            machine=self.machine,
            start_date=now - timedelta(hours=2),
            end_date=now + timedelta(hours=2),
        )
        WorkOrder.objects.create(
            product_name="Canceled WO",
            quantity=10,
            status="canceled",
            company=self.company,
            machine=self.machine,
            start_date=now - timedelta(hours=1),
            end_date=now + timedelta(hours=1),
        )

        slot_start, slot_end = WorkOrderService.find_next_available_slot(
            self.machine,
            duration_minutes=60,
            start_after=start_after,
        )

        self.assertEqual(slot_start, start_after)
        self.assertEqual(slot_end, start_after + timedelta(minutes=60))

    def test_find_next_available_slot_still_respects_pending_and_in_progress_work(self):
        now = timezone.now().replace(second=0, microsecond=0)
        start_after = now
        expected_start = now + timedelta(hours=2)

        WorkOrder.objects.create(
            product_name="Pending WO",
            quantity=10,
            status="pending",
            company=self.company,
            machine=self.machine,
            start_date=now,
            end_date=expected_start,
        )

        slot_start, slot_end = WorkOrderService.find_next_available_slot(
            self.machine,
            duration_minutes=30,
            start_after=start_after,
        )

        self.assertEqual(slot_start, expected_start)
        self.assertEqual(slot_end, expected_start + timedelta(minutes=30))

    def test_timeline_payload_includes_completed_item_counts(self):
        now = timezone.now().replace(second=0, microsecond=0)
        wo = WorkOrder.objects.create(
            product_name="Tracked WO",
            quantity=20,
            status="in_progress",
            company=self.company,
            machine=self.machine,
            start_date=now - timedelta(hours=1),
            end_date=now + timedelta(hours=1),
        )
        ProductionLog.objects.create(work_order=wo, quantity=6, status="approved")
        ProductionLog.objects.create(work_order=wo, quantity=3, status="pending")
        ProductionLog.objects.create(work_order=wo, quantity=2, status="rejected")

        timeline = DashboardService.get_timeline_data(self.company, viewer_role="planner")
        task = next(task for task in timeline["tasks"] if task["id"] == wo.id)

        self.assertEqual(task["finished_qty"], 9)
        self.assertEqual(task["approved_qty"], 6)
        self.assertEqual(task["remaining_qty"], 11)
        self.assertEqual(task["progress_stats"]["actual"], 9)
        self.assertEqual(task["progress_stats"]["approved"], 6)
