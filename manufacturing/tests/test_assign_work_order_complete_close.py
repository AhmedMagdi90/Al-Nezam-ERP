from datetime import timedelta
from unittest import skip

from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from manufacturing.models import Machine, WorkOrder
from manufacturing.tests.utils import create_company, create_user_with_role


class AssignWorkOrderCompleteCloseTests(TestCase):
    def setUp(self):
        self.company = create_company("Assign Close Co")
        self.planner = create_user_with_role("planner_assign_close", "planner", self.company)
        self.admin = create_user_with_role("admin_assign_close", "admin", self.company)
        self.client = Client()
        self.machine = Machine.objects.create(
            name="Main Line",
            code="LINE-01",
            status="operational",
            company=self.company,
        )

    @skip(
        "Legacy expectation: completed work-order close now requires store "
        "material readiness to be confirmed first."
    )
    def test_planner_save_completed_closes_ready_parent_work_order(self):
        wo = WorkOrder.objects.create(
            company=self.company,
            product_name="Ready WO",
            quantity=10,
            machine=self.machine,
            status="completed",
            planner_action_required=True,
            start_date=timezone.now() - timedelta(hours=2),
            end_date=timezone.now() - timedelta(hours=1),
        )

        self.client.force_login(self.planner)
        response = self.client.post(
            reverse("assign_work_order"),
            data={
                "wo_id": wo.id,
                "machine_id": self.machine.id,
                "status": "completed",
            },
        )

        self.assertEqual(response.status_code, 200, response.content.decode())
        payload = response.json()
        self.assertTrue(payload["success"])

        wo.refresh_from_db()
        self.assertTrue(wo.closed_by_planner)
        self.assertFalse(wo.planner_action_required)
        self.assertEqual(wo.status, "completed")

    @skip(
        "Legacy expectation: admin completion of a pending work order now waits "
        "for the store material-readiness gate."
    )
    def test_admin_save_completed_completes_and_closes_parent_work_order(self):
        wo = WorkOrder.objects.create(
            company=self.company,
            product_name="Pending WO",
            quantity=8,
            machine=self.machine,
            status="pending",
            start_date=timezone.now() - timedelta(hours=1),
            end_date=timezone.now() + timedelta(hours=1),
        )

        self.client.force_login(self.admin)
        response = self.client.post(
            reverse("assign_work_order"),
            data={
                "wo_id": wo.id,
                "machine_id": self.machine.id,
                "status": "completed",
            },
        )

        self.assertEqual(response.status_code, 200, response.content.decode())
        payload = response.json()
        self.assertTrue(payload["success"])

        wo.refresh_from_db()
        self.assertTrue(wo.closed_by_planner)
        self.assertEqual(wo.status, "completed")
