import json
from datetime import timedelta

from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from manufacturing.models import BillOfMaterial, Machine, Product, ShiftAssignment, WorkOrder
from manufacturing.tests.utils import create_company, create_user_with_role
from manufacturing.work_order_visibility import (
    can_user_see_work_order,
    get_current_shift_window_for_company,
    get_visible_work_orders_for_user,
)


class WorkOrderVisibilityTests(TestCase):
    def setUp(self):
        self.company = create_company("Visibility Co")
        self.other_company = create_company("Other Visibility Co")
        self.planner = create_user_with_role("visibility_planner", "planner", self.company)
        self.admin = create_user_with_role("visibility_admin", "admin", self.company)
        self.supervisor = create_user_with_role("visibility_supervisor", "supervisor", self.company)
        self.worker = create_user_with_role("visibility_worker", "worker", self.company)
        self.other_worker = create_user_with_role("visibility_other_worker", "worker", self.company)
        self.other_supervisor = create_user_with_role("visibility_other_supervisor", "supervisor", self.other_company)

        self.machine = Machine.objects.create(
            name="Assigned Machine",
            code="VIS-01",
            status="operational",
            company=self.company,
        )
        self.other_machine = Machine.objects.create(
            name="Other Machine",
            code="VIS-02",
            status="operational",
            company=self.company,
        )
        self.external_machine = Machine.objects.create(
            name="External Machine",
            code="EXT-01",
            status="operational",
            company=self.other_company,
        )
        self.product = Product.objects.create(name="Visibility Product", company=self.company)
        self.bom = BillOfMaterial.objects.create(product=self.product, status="active", base_quantity=1)
        self.now = timezone.now()
        self.shift_window = get_current_shift_window_for_company(self.company, now=self.now)

        self._assign_current_shift(self.supervisor, self.machine)
        self._assign_current_shift(self.worker, self.machine)

    def _assign_current_shift(self, user, machine):
        return ShiftAssignment.objects.create(
            worker=user,
            machine=machine,
            shift_type=self.shift_window["shift_type"],
            date=self.shift_window["assignment_date"],
            created_by=self.planner,
        )

    def _work_order(self, *, machine=None, worker=None, company=None, start_date=None, status="pending"):
        company = company or self.company
        return WorkOrder.objects.create(
            product_name="Visibility WO",
            bom=self.bom if company == self.company else None,
            quantity=10,
            status=status,
            company=company,
            machine=machine or self.machine,
            assigned_worker=worker,
            start_date=start_date or self.now,
            material_readiness_status="ready",
            material_available_qty=10,
        )

    def test_supervisor_sees_assigned_machine_work_order_during_active_shift(self):
        wo = self._work_order(machine=self.machine)

        visible = get_visible_work_orders_for_user(
            self.supervisor,
            WorkOrder.objects.filter(company=self.company),
            now=self.now,
        )

        self.assertEqual(list(visible), [wo])
        self.assertTrue(can_user_see_work_order(self.supervisor, wo, now=self.now))

    def test_supervisor_does_not_see_other_machine_work_order(self):
        self._work_order(machine=self.machine)
        other_wo = self._work_order(machine=self.other_machine)

        visible_ids = set(
            get_visible_work_orders_for_user(
                self.supervisor,
                WorkOrder.objects.filter(company=self.company),
                now=self.now,
            ).values_list("id", flat=True)
        )

        self.assertNotIn(other_wo.id, visible_ids)

    def test_supervisor_does_not_see_assigned_machine_work_order_outside_shift(self):
        outside_shift = self.shift_window["end"] + timedelta(minutes=5)
        wo = self._work_order(machine=self.machine, start_date=outside_shift)

        self.assertFalse(can_user_see_work_order(self.supervisor, wo, now=self.now))

    def test_worker_sees_assigned_task_during_active_shift(self):
        wo = self._work_order(machine=self.machine, worker=self.worker)
        client = Client()
        client.force_login(self.worker)

        response = client.get(reverse("shop_floor"))

        self.assertEqual(response.status_code, 200)
        self.assertIn(wo.id, [task.id for task in response.context["assigned_tasks"]])

    def test_worker_cannot_start_task_outside_active_shift(self):
        outside_shift = self.shift_window["end"] + timedelta(minutes=5)
        wo = self._work_order(machine=self.machine, worker=self.worker, start_date=outside_shift)
        client = Client()
        client.force_login(self.worker)

        response = client.post(
            reverse("api_update_work_order_status", kwargs={"wo_id": wo.id}),
            data=json.dumps({"status": "in_progress"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 403)
        wo.refresh_from_db()
        self.assertEqual(wo.status, "pending")

    def test_direct_api_access_to_unauthorized_work_order_returns_403(self):
        wo = self._work_order(machine=self.other_machine, worker=self.other_worker)
        client = Client()
        client.force_login(self.worker)

        response = client.post(
            reverse("log_production"),
            data=json.dumps({"work_order_id": wo.id, "quantity": 1, "shift": "morning"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 403)

    def test_planner_and_admin_still_see_all_company_work_orders(self):
        first = self._work_order(machine=self.machine)
        second = self._work_order(machine=self.other_machine)
        qs = WorkOrder.objects.filter(company=self.company).order_by("id")

        self.assertEqual(list(get_visible_work_orders_for_user(self.planner, qs)), [first, second])
        self.assertEqual(list(get_visible_work_orders_for_user(self.admin, qs)), [first, second])

    def test_cross_company_work_order_is_never_visible(self):
        wo = self._work_order(company=self.other_company, machine=self.external_machine)

        self.assertFalse(can_user_see_work_order(self.supervisor, wo, now=self.now))
