from django.test import TestCase
from django.utils import timezone

from manufacturing.models import (
    BillOfMaterial,
    BOMOperation,
    Customer,
    Notification,
    Product,
    ProductionStage,
    QualityCheck,
    WorkOrder,
)
from manufacturing.services import DashboardService, WorkOrderLifecycle, WorkOrderLifecycleError, WorkOrderService
from manufacturing.tests.utils import create_company, create_user_with_role


class WorkOrderLifecycleTests(TestCase):
    def setUp(self):
        self.company = create_company("Lifecycle Co")
        self.planner = create_user_with_role("planner_lc", "planner", self.company)
        self.supervisor = create_user_with_role("supervisor_lc", "supervisor", self.company)
        self.worker = create_user_with_role("worker_lc", "worker", self.company)

    def _make_wo(self, status="pending"):
        return WorkOrder.objects.create(
            company=self.company,
            product_name="Lifecycle Product",
            quantity=10,
            status=status,
        )

    def test_planner_cannot_start_work_order(self):
        wo = self._make_wo(status="pending")
        with self.assertRaises(WorkOrderLifecycleError):
            WorkOrderLifecycle.apply_transition(
                wo,
                "in_progress",
                actor=self.planner,
                save=False,
            )

    def test_worker_can_start_pending_work_order(self):
        wo = self._make_wo(status="pending")
        changed = WorkOrderLifecycle.apply_transition(
            wo,
            "in_progress",
            actor=self.worker,
        )
        self.assertTrue(changed)
        wo.refresh_from_db()
        self.assertEqual(wo.status, "in_progress")

    def test_in_progress_cannot_roll_back_to_pending(self):
        wo = self._make_wo(status="in_progress")
        with self.assertRaises(WorkOrderLifecycleError):
            WorkOrderLifecycle.apply_transition(
                wo,
                "pending",
                actor=self.supervisor,
                save=False,
            )

    def test_close_requires_planner_or_admin(self):
        wo = self._make_wo(status="completed")
        wo.planner_action_required = True
        wo.store_receipt_status = "received"
        wo.store_received_qty = wo.quantity
        wo.store_scrap_qty = 0
        wo.save(update_fields=["planner_action_required", "store_receipt_status", "store_received_qty", "store_scrap_qty"])

        with self.assertRaises(WorkOrderLifecycleError):
            WorkOrderLifecycle.close(wo, actor=self.supervisor, save=False)

        changed = WorkOrderLifecycle.close(wo, actor=self.planner)
        self.assertTrue(changed)
        wo.refresh_from_db()
        self.assertFalse(wo.planner_action_required)
        self.assertTrue(wo.closed_by_planner)
        self.assertEqual(wo.status, "completed")

    def test_close_blocked_when_qc_pending(self):
        wo = self._make_wo(status="completed")
        wo.planner_action_required = True
        wo.save(update_fields=["planner_action_required"])
        QualityCheck.objects.create(work_order=wo, status="new")

        with self.assertRaises(WorkOrderLifecycleError):
            WorkOrderLifecycle.close(wo, actor=self.planner, save=False)

    def test_dashboard_planner_actions_expose_close_queue_display_fields(self):
        customer = Customer.objects.create(name="Lifecycle Customer", company=self.company)
        stage = ProductionStage.objects.create(name="Packing", order=30)
        wo = WorkOrder.objects.create(
            company=self.company,
            product_name="Close Queue Product",
            quantity=12,
            status="completed",
            current_stage=stage,
            customer=customer,
            planner_action_required=True,
            closed_by_planner=False,
            end_date=timezone.now(),
        )

        context = DashboardService.get_dashboard_context(
            self.company,
            viewer_role="planner",
            viewer=self.planner,
        )

        close_row = next(row for row in context["planner_actions"] if row.id == wo.id)
        self.assertEqual(close_row.close_completed_qty, 12)
        self.assertEqual(close_row.close_final_stage_name, "Packing")
        self.assertEqual(close_row.close_customer_name, "Lifecycle Customer")

    def test_dashboard_notifications_include_unread_system_notifications(self):
        Notification.objects.create(
            recipient=self.planner,
            title="Store receipt confirmed",
            message="WO #42 received.",
            link="/manufacturing/planner/?wo=42",
        )

        context = DashboardService.get_dashboard_context(
            self.company,
            viewer_role="planner",
            viewer=self.planner,
        )

        self.assertEqual(context["system_notifications_count"], 1)
        self.assertEqual(context["system_notifications"][0].title, "Store receipt confirmed")
        self.assertIn("/manufacturing/dashboard/", context["system_notifications"][0].link)
        self.assertGreaterEqual(context["planner_notifications_count"], 1)

    def test_sync_parent_completion_advances_pending_parent_through_valid_states(self):
        product = Product.objects.create(name="Lifecycle Multi Stage", company=self.company)
        bom = BillOfMaterial.objects.create(product=product, status="active", base_quantity=1)
        stage1 = ProductionStage.objects.create(name="Op 01", order=1)
        stage2 = ProductionStage.objects.create(name="Op 02", order=2)
        BOMOperation.objects.create(bom=bom, stage=stage1, order=1, duration_minutes=10)
        BOMOperation.objects.create(bom=bom, stage=stage2, order=2, duration_minutes=10)

        parent = WorkOrder.objects.create(
            company=self.company,
            product_name=product.name,
            bom=bom,
            quantity=10,
            status="pending",
        )
        WorkOrder.objects.create(
            company=self.company,
            parent=parent,
            product_name=f"{product.name} - Op 01",
            bom=bom,
            quantity=10,
            status="completed",
            stage=stage1,
            current_stage=stage1,
        )
        WorkOrder.objects.create(
            company=self.company,
            parent=parent,
            product_name=f"{product.name} - Op 02",
            bom=bom,
            quantity=10,
            status="completed",
            stage=stage2,
            current_stage=stage2,
        )

        WorkOrderService.sync_parent_completion(self.company)

        parent.refresh_from_db()
        self.assertEqual(parent.status, "completed")
        self.assertEqual(float(parent.progress), 100.0)
        self.assertTrue(parent.planner_action_required)
        self.assertEqual(parent.store_receipt_status, "pending")
        self.assertIsNotNone(parent.end_date)
