from datetime import timedelta

from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from manufacturing.models import (
    BillOfMaterial,
    BOMOperation,
    Machine,
    Product,
    ProductionStage,
    WorkOrder,
)
from manufacturing.services import WorkOrderService
from manufacturing.tests.utils import create_company, create_user_with_role


class TimelineSnapTests(TestCase):
    def setUp(self):
        self.company = create_company("Snap Co")
        self.planner = create_user_with_role("snap_planner", "planner", self.company)
        self.client = Client()
        self.machine_a = Machine.objects.create(
            name="Machine A",
            code="MA-01",
            status="operational",
            company=self.company,
        )
        self.machine_b = Machine.objects.create(
            name="Machine B",
            code="MB-01",
            status="operational",
            company=self.company,
        )

    def _dt(self, hour, minute):
        base = timezone.now().replace(hour=hour, minute=minute, second=0, microsecond=0)
        return base + timedelta(days=1)

    def test_snap_scheduled_work_orders_aligns_pending_machine_schedule(self):
        first = WorkOrder.objects.create(
            product_name="WO-1",
            quantity=10,
            status="pending",
            company=self.company,
            machine=self.machine_a,
            start_date=self._dt(9, 3),
            end_date=self._dt(9, 33),
        )
        second = WorkOrder.objects.create(
            product_name="WO-2",
            quantity=10,
            status="pending",
            company=self.company,
            machine=self.machine_a,
            start_date=self._dt(9, 34),
            end_date=self._dt(10, 4),
        )

        result = WorkOrderService.snap_scheduled_work_orders(self.company, 5)

        first.refresh_from_db()
        second.refresh_from_db()

        self.assertEqual(result["changed_count"], 2)
        self.assertEqual(first.start_date, self._dt(9, 5))
        self.assertEqual(first.end_date, self._dt(9, 35))
        self.assertEqual(second.start_date, self._dt(9, 35))
        self.assertEqual(second.end_date, self._dt(10, 5))

    def test_snap_scheduled_work_orders_closes_idle_gap_on_same_machine(self):
        first = WorkOrder.objects.create(
            product_name="WO-1",
            quantity=10,
            status="pending",
            company=self.company,
            machine=self.machine_a,
            start_date=self._dt(9, 3),
            end_date=self._dt(9, 33),
        )
        second = WorkOrder.objects.create(
            product_name="WO-2",
            quantity=10,
            status="pending",
            company=self.company,
            machine=self.machine_a,
            start_date=self._dt(11, 11),
            end_date=self._dt(11, 41),
        )

        result = WorkOrderService.snap_scheduled_work_orders(self.company, 5)

        first.refresh_from_db()
        second.refresh_from_db()

        self.assertEqual(result["changed_count"], 2)
        self.assertEqual(first.start_date, self._dt(9, 5))
        self.assertEqual(first.end_date, self._dt(9, 35))
        self.assertEqual(second.start_date, self._dt(9, 35))
        self.assertEqual(second.end_date, self._dt(10, 5))

    def test_snap_scheduled_work_orders_keeps_in_progress_and_completed_untouched(self):
        in_progress = WorkOrder.objects.create(
            product_name="Running WO",
            quantity=10,
            status="in_progress",
            company=self.company,
            machine=self.machine_a,
            start_date=self._dt(8, 7),
            end_date=self._dt(8, 37),
        )
        completed = WorkOrder.objects.create(
            product_name="Done WO",
            quantity=10,
            status="completed",
            company=self.company,
            machine=self.machine_a,
            start_date=self._dt(10, 7),
            end_date=self._dt(10, 37),
        )

        result = WorkOrderService.snap_scheduled_work_orders(self.company, 5)

        in_progress.refresh_from_db()
        completed.refresh_from_db()

        self.assertEqual(result["changed_count"], 0)
        self.assertEqual(in_progress.start_date, self._dt(8, 7))
        self.assertEqual(completed.start_date, self._dt(10, 7))

    def test_snap_scheduled_work_orders_cascades_series_stage_children(self):
        product = Product.objects.create(name="Series Product", company=self.company)
        bom = BillOfMaterial.objects.create(product=product, status="active", base_quantity=1)
        stage1 = ProductionStage.objects.create(name="Op 10", order=10)
        stage2 = ProductionStage.objects.create(name="Op 20", order=20)
        BOMOperation.objects.create(bom=bom, stage=stage1, order=10, duration_minutes=30)
        BOMOperation.objects.create(bom=bom, stage=stage2, order=20, duration_minutes=30)

        parent = WorkOrder.objects.create(
            product_name=product.name,
            quantity=10,
            status="pending",
            company=self.company,
            bom=bom,
            operation_flow_mode="series",
        )
        child1 = WorkOrder.objects.create(
            parent=parent,
            product_name=f"{product.name} - {stage1.name}",
            quantity=10,
            status="pending",
            company=self.company,
            bom=bom,
            machine=self.machine_a,
            stage=stage1,
            current_stage=stage1,
            start_date=self._dt(9, 3),
            end_date=self._dt(9, 33),
            scheduled_start_date=self._dt(9, 3),
        )
        child2 = WorkOrder.objects.create(
            parent=parent,
            product_name=f"{product.name} - {stage2.name}",
            quantity=10,
            status="pending",
            company=self.company,
            bom=bom,
            machine=self.machine_b,
            stage=stage2,
            current_stage=stage2,
            start_date=self._dt(9, 40),
            end_date=self._dt(10, 10),
            scheduled_start_date=self._dt(9, 40),
        )

        result = WorkOrderService.snap_scheduled_work_orders(self.company, 15)

        child1.refresh_from_db()
        child2.refresh_from_db()

        self.assertGreaterEqual(result["changed_count"], 2)
        self.assertEqual(child1.start_date, self._dt(9, 15))
        self.assertEqual(child1.end_date, self._dt(9, 45))
        self.assertEqual(child2.start_date, self._dt(9, 45))
        self.assertEqual(child2.end_date, self._dt(10, 15))

    def test_timeline_snap_api_requires_planner_and_returns_changes(self):
        WorkOrder.objects.create(
            product_name="API WO",
            quantity=10,
            status="pending",
            company=self.company,
            machine=self.machine_a,
            start_date=self._dt(11, 2),
            end_date=self._dt(11, 32),
        )

        self.client.force_login(self.planner)
        response = self.client.post(
            reverse("api_timeline_snap"),
            data='{"snap_minutes": 5}',
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200, response.content.decode())
        payload = response.json()
        self.assertTrue(payload["success"])
        self.assertEqual(payload["changed_count"], 1)
