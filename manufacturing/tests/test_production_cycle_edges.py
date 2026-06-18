from datetime import timedelta

from django.test import TestCase, Client
from django.urls import reverse
from django.utils import timezone

from manufacturing.models import (
    WorkOrder,
    Machine,
    Product,
    BillOfMaterial,
    ProductionStage,
    BOMOperation,
    ProductionLog,
    QualityCheck,
)
from manufacturing.services import WorkOrderService, ProductionLogService
from manufacturing.services import DashboardService
from manufacturing.tests.utils import create_company, create_user_with_role


class ProductionCycleEdgeCaseTests(TestCase):
    def setUp(self):
        self.company = create_company()
        self.planner = create_user_with_role('planner', 'planner', self.company)
        self.supervisor = create_user_with_role('supervisor', 'supervisor', self.company)
        self.worker = create_user_with_role('worker', 'worker', self.company)
        self.client = Client()

        self.machine = Machine.objects.create(
            name="Line 1",
            code="LINE-1",
            status="operational",
            company=self.company
        )
        self.machine2 = Machine.objects.create(
            name="Line 2",
            code="LINE-2",
            status="operational",
            company=self.company
        )

    def _create_bom_with_stages(self, product_name, stages):
        product = Product.objects.create(name=product_name, company=self.company)
        bom = BillOfMaterial.objects.create(product=product, status='active', base_quantity=1)
        stage_objs = []
        for idx, stage_kwargs in enumerate(stages, start=1):
            stage = ProductionStage.objects.create(order=idx, **stage_kwargs)
            BOMOperation.objects.create(
                bom=bom,
                stage=stage,
                order=idx,
                duration_minutes=10
            )
            stage_objs.append(stage)
        return product, bom, stage_objs

    def test_qc_gate_blocks_next_stage_until_checked(self):
        product, bom, stages = self._create_bom_with_stages(
            "QC Product",
            [
                {"name": "Cutting", "machine": self.machine, "is_quality_check": True},
                {"name": "Assembly", "machine": self.machine2},
            ]
        )
        stage1, stage2 = stages

        wo = WorkOrder.objects.create(
            product_name=product.name,
            bom=bom,
            quantity=10,
            status='in_progress',
            machine=self.machine,
            stage=stage1,
            current_stage=stage1,
            company=self.company
        )

        ProductionLog.objects.create(
            work_order=wo,
            worker=self.worker,
            quantity=10,
            status='approved'
        )

        result = WorkOrderService.create_next_stage_task(
            wo,
            created_by=self.supervisor,
            auto_create=False
        )

        self.assertTrue(result.get('qc_pending'))
        self.assertTrue(QualityCheck.objects.filter(work_order=wo, status='new').exists())
        wo.refresh_from_db()
        self.assertIsNotNone(wo.quality_start_at)
        self.assertFalse(WorkOrder.objects.filter(parent=wo, stage=stage2).exists())

    def test_supervisor_approval_does_not_complete_multistage_qc_work_order(self):
        product, bom, stages = self._create_bom_with_stages(
            "QC Multi Stage Approval",
            [
                {"name": "Cutting", "machine": self.machine, "is_quality_check": True},
                {"name": "Assembly", "machine": self.machine2},
            ]
        )
        stage1, _stage2 = stages

        wo = WorkOrder.objects.create(
            product_name=product.name,
            bom=bom,
            quantity=10,
            status='in_progress',
            machine=self.machine,
            stage=stage1,
            current_stage=stage1,
            company=self.company
        )

        log = ProductionLog.objects.create(
            work_order=wo,
            worker=self.worker,
            quantity=10,
            status='pending',
            completion_requested=True
        )

        ProductionLogService.approve_log(log, self.supervisor)

        wo.refresh_from_db()
        self.assertNotEqual(wo.status, 'completed')
        self.assertTrue(QualityCheck.objects.filter(work_order=wo, status='new').exists())

    def test_split_remaining_creates_new_work_order(self):
        product = Product.objects.create(name="Split Product", company=self.company)
        planned_start = timezone.now() - timedelta(minutes=20)
        planned_end = timezone.now() + timedelta(hours=2)
        wo = WorkOrder.objects.create(
            product_name=product.name,
            quantity=100,
            status='in_progress',
            machine=self.machine,
            company=self.company,
            start_date=planned_start,
            end_date=planned_end,
        )

        log = ProductionLog.objects.create(
            work_order=wo,
            worker=self.worker,
            quantity=70,
            status='pending'
        )
        before_approval = timezone.now()
        ProductionLogService.approve_log(log, self.supervisor)
        after_approval = timezone.now()

        wo.refresh_from_db()
        self.assertEqual(wo.quantity, 70)
        self.assertEqual(int(wo.progress), 100)
        self.assertLess(wo.end_date, planned_end)
        self.assertGreaterEqual(wo.end_date, before_approval)
        self.assertLessEqual(wo.end_date, after_approval)

        split = WorkOrder.objects.filter(
            company=self.company,
            product_name=product.name,
            quantity=30,
            status='pending'
        ).exclude(id=wo.id).first()

        self.assertIsNotNone(split)
        self.assertEqual(split.machine, self.machine)
        self.assertGreaterEqual(split.start_date, before_approval)
        self.assertLessEqual(split.start_date, after_approval)

    def test_multi_stage_sets_next_stage_ready_without_auto_create(self):
        product, bom, stages = self._create_bom_with_stages(
            "Multi Stage Product",
            [
                {"name": "Cutting", "machine": self.machine},
                {"name": "Packaging", "machine": self.machine2},
            ]
        )
        stage1, stage2 = stages
        planned_start = timezone.now() - timedelta(minutes=30)
        planned_end = timezone.now() + timedelta(hours=2)

        wo = WorkOrder.objects.create(
            product_name=product.name,
            bom=bom,
            quantity=50,
            status='in_progress',
            machine=self.machine,
            stage=stage1,
            current_stage=stage1,
            company=self.company,
            start_date=planned_start,
            end_date=planned_end,
        )

        log = ProductionLog.objects.create(
            work_order=wo,
            worker=self.worker,
            quantity=50,
            status='pending'
        )
        before_approval = timezone.now()
        ProductionLogService.approve_log(log, self.supervisor)
        after_approval = timezone.now()

        wo.refresh_from_db()
        self.assertTrue(wo.next_stage_ready)
        self.assertLess(wo.end_date, planned_end)
        self.assertGreaterEqual(wo.end_date, before_approval)
        self.assertLessEqual(wo.end_date, after_approval)
        self.assertFalse(WorkOrder.objects.filter(parent=wo, stage=stage2).exists())

    def test_planner_close_final_stage(self):
        product, bom, stages = self._create_bom_with_stages(
            "Final Stage Product",
            [{"name": "Final", "machine": self.machine}]
        )
        stage1 = stages[0]

        wo = WorkOrder.objects.create(
            product_name=product.name,
            bom=bom,
            quantity=20,
            status='in_progress',
            machine=self.machine,
            stage=stage1,
            current_stage=stage1,
            company=self.company
        )

        log = ProductionLog.objects.create(
            work_order=wo,
            worker=self.worker,
            quantity=20,
            status='pending'
        )
        before_approval = timezone.now()
        ProductionLogService.approve_log(log, self.supervisor)
        after_approval = timezone.now()

        wo.refresh_from_db()
        self.assertTrue(wo.planner_action_required)
        self.assertIsNotNone(wo.end_date)
        self.assertGreaterEqual(wo.end_date, before_approval)
        self.assertLessEqual(wo.end_date, after_approval)
        completion_at = wo.end_date
        wo.store_receipt_status = 'received'
        wo.save(update_fields=['store_receipt_status'])

        self.client.force_login(self.planner)
        url = reverse('api_close_work_order', kwargs={'wo_id': wo.id})
        response = self.client.post(url)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json().get('success'))

        wo.refresh_from_db()
        self.assertTrue(wo.closed_by_planner)
        self.assertFalse(wo.planner_action_required)
        self.assertEqual(wo.status, 'completed')
        self.assertEqual(wo.end_date, completion_at)

    def test_planner_close_trims_future_slot_for_immediate_machine_reuse(self):
        product, bom, stages = self._create_bom_with_stages(
            "Early Close Product",
            [{"name": "Final Assembly", "machine": self.machine}]
        )
        stage1 = stages[0]
        now = timezone.now().replace(second=0, microsecond=0)
        original_end = now + timedelta(hours=4)
        parent = WorkOrder.objects.create(
            product_name=product.name,
            bom=bom,
            quantity=20,
            status='completed',
            machine=self.machine,
            current_stage=stage1,
            company=self.company,
            planner_action_required=True,
            store_receipt_status='received',
            start_date=now - timedelta(hours=1),
            end_date=original_end,
        )
        child = WorkOrder.objects.create(
            parent=parent,
            product_name=f"{product.name} - {stage1.name}",
            bom=bom,
            quantity=20,
            status='completed',
            machine=self.machine,
            stage=stage1,
            current_stage=stage1,
            company=self.company,
            start_date=now - timedelta(hours=1),
            end_date=original_end,
        )

        self.client.force_login(self.planner)
        before_close = timezone.now()
        response = self.client.post(reverse('api_close_work_order', kwargs={'wo_id': parent.id}))
        after_close = timezone.now()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json().get('success'))

        parent.refresh_from_db()
        child.refresh_from_db()
        self.machine.refresh_from_db()

        self.assertEqual(parent.status, 'completed')
        self.assertTrue(parent.closed_by_planner)
        self.assertEqual(self.machine.status, 'operational')
        self.assertLess(parent.end_date, original_end)
        self.assertLess(child.end_date, original_end)
        self.assertGreaterEqual(parent.end_date, before_close)
        self.assertLessEqual(parent.end_date, after_close)
        self.assertLessEqual(child.end_date, after_close)

        slot_start, _slot_end = WorkOrderService.find_next_available_slot(
            self.machine,
            duration_minutes=30,
            start_after=after_close,
        )
        self.assertEqual(slot_start, after_close)

        timeline = DashboardService.get_dashboard_context(self.company, viewer_role="planner")
        tasks_by_id = {task["id"]: task for task in timeline["tasks_data"]}
        self.assertIn(child.id, tasks_by_id)
        self.assertLess(tasks_by_id[child.id]["end"], original_end.isoformat())

    def test_supervisor_approval_trims_parent_timeline_before_planner_close(self):
        product, bom, stages = self._create_bom_with_stages(
            "Approval Early Finish Product",
            [{"name": "Cutting", "machine": self.machine}]
        )
        stage1 = stages[0]
        now = timezone.now().replace(second=0, microsecond=0)
        planned_end = now + timedelta(hours=4)
        parent = WorkOrder.objects.create(
            product_name=product.name,
            bom=bom,
            quantity=10,
            status='in_progress',
            machine=self.machine,
            current_stage=stage1,
            company=self.company,
            start_date=now - timedelta(hours=1),
            end_date=planned_end,
        )
        child = WorkOrder.objects.create(
            parent=parent,
            product_name=f"{product.name} - {stage1.name}",
            bom=bom,
            quantity=10,
            status='in_progress',
            machine=self.machine,
            stage=stage1,
            current_stage=stage1,
            company=self.company,
            assigned_worker=self.worker,
            start_date=now - timedelta(hours=1),
            end_date=planned_end,
        )
        log = ProductionLog.objects.create(
            work_order=child,
            worker=self.worker,
            quantity=10,
            status='pending',
            completion_requested=True,
        )

        before_approval = timezone.now()
        ProductionLogService.approve_log(log, self.supervisor)
        after_approval = timezone.now()

        parent.refresh_from_db()
        child.refresh_from_db()

        self.assertEqual(child.status, 'completed')
        self.assertEqual(parent.status, 'completed')
        self.assertTrue(parent.planner_action_required)
        self.assertLess(child.end_date, planned_end)
        self.assertEqual(parent.end_date, child.end_date)
        self.assertGreaterEqual(parent.end_date, before_approval)
        self.assertLessEqual(parent.end_date, after_approval)

        timeline = DashboardService.get_dashboard_context(self.company, viewer_role="planner")
        tasks_by_id = {task["id"]: task for task in timeline["tasks_data"]}
        self.assertEqual(tasks_by_id[child.id]["end"], child.end_date.isoformat())

    def test_planner_close_blocked_when_qc_pending(self):
        product, bom, stages = self._create_bom_with_stages(
            "QC Block Product",
            [{"name": "QC Final", "machine": self.machine, "is_quality_check": True}]
        )
        stage1 = stages[0]

        wo = WorkOrder.objects.create(
            product_name=product.name,
            bom=bom,
            quantity=10,
            status='completed',
            machine=self.machine,
            stage=stage1,
            current_stage=stage1,
            company=self.company,
            planner_action_required=True
        )
        QualityCheck.objects.create(work_order=wo, status='new')

        self.client.force_login(self.planner)
        url = reverse('api_close_work_order', kwargs={'wo_id': wo.id})
        response = self.client.post(url)

        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json().get('success'))
        self.assertIn("QC is pending", response.json().get('error', ''))

    def test_release_to_next_stage_blocked_when_qc_pending(self):
        product, bom, stages = self._create_bom_with_stages(
            "Release QC Product",
            [
                {"name": "Cutting", "machine": self.machine, "is_quality_check": True},
                {"name": "Packing", "machine": self.machine2},
            ]
        )
        stage1 = stages[0]

        wo = WorkOrder.objects.create(
            product_name=product.name,
            bom=bom,
            quantity=10,
            status='in_progress',
            machine=self.machine,
            stage=stage1,
            current_stage=stage1,
            company=self.company
        )

        ProductionLog.objects.create(
            work_order=wo,
            worker=self.worker,
            quantity=10,
            status='approved'
        )
        QualityCheck.objects.create(work_order=wo, status='new')

        with self.assertRaises(ValueError) as ctx:
            WorkOrderService.release_to_next_stage(wo, 5, self.supervisor)

        self.assertIn("QC is pending", str(ctx.exception))

    def test_release_to_next_stage_allows_partial_after_qc_processed(self):
        product, bom, stages = self._create_bom_with_stages(
            "Release After QC",
            [
                {"name": "Cutting", "machine": self.machine, "is_quality_check": True},
                {"name": "Packing", "machine": self.machine2},
            ]
        )
        stage1, stage2 = stages

        wo = WorkOrder.objects.create(
            product_name=product.name,
            bom=bom,
            quantity=10,
            status='in_progress',
            machine=self.machine,
            stage=stage1,
            current_stage=stage1,
            company=self.company
        )

        ProductionLog.objects.create(
            work_order=wo,
            worker=self.worker,
            quantity=10,
            status='approved'
        )
        QualityCheck.objects.create(
            work_order=wo,
            status='processed',
            good_quantity=8,
            repair_quantity=0,
            faulty_quantity=0
        )

        new_task = WorkOrderService.release_to_next_stage(wo, 5, self.supervisor, target_machine=self.machine2)

        self.assertEqual(new_task.quantity, 5)
        self.assertEqual(new_task.stage, stage2)
        self.assertEqual(new_task.parent, wo)
        self.assertEqual(new_task.source_task, wo)
        self.assertEqual(new_task.machine, self.machine2)

        wo.refresh_from_db()
        self.assertEqual(wo.released_qty, 5)
        self.assertEqual(wo.current_stage, stage2)

    def test_qc_processed_stage_child_sets_next_stage_ready_for_manual_start(self):
        product, bom, stages = self._create_bom_with_stages(
            "QC Manual Progression",
            [
                {"name": "Cutting", "machine": self.machine, "is_quality_check": True},
                {"name": "Packing", "machine": self.machine2},
            ]
        )
        stage1, _stage2 = stages

        parent = WorkOrder.objects.create(
            product_name=product.name,
            bom=bom,
            quantity=100,
            status='in_progress',
            machine=self.machine,
            stage=stage1,
            current_stage=stage1,
            company=self.company
        )
        child = WorkOrder.objects.create(
            parent=parent,
            product_name=f"{product.name} - {stage1.name}",
            bom=bom,
            quantity=100,
            status='in_progress',
            machine=self.machine,
            stage=stage1,
            current_stage=stage1,
            qc_requirement=True,
            company=self.company
        )

        ProductionLog.objects.create(
            work_order=child,
            worker=self.worker,
            quantity=100,
            status='approved'
        )
        QualityCheck.objects.create(
            work_order=child,
            status='processed',
            good_quantity=90,
            repair_quantity=0,
            faulty_quantity=10
        )

        result = WorkOrderService.create_next_stage_task(child, self.supervisor, auto_create=False)

        self.assertTrue(result.get('manual_start'))
        parent.refresh_from_db()
        self.assertTrue(parent.next_stage_ready)

    def test_existing_next_stage_without_machine_returns_to_planner(self):
        product, bom, stages = self._create_bom_with_stages(
            "Planner Machine Assignment",
            [
                {"name": "Cutting", "machine": self.machine},
                {"name": "Packing", "machine": self.machine2},
            ]
        )
        stage1, stage2 = stages

        parent = WorkOrder.objects.create(
            product_name=product.name,
            bom=bom,
            quantity=20,
            status='in_progress',
            current_stage=stage1,
            company=self.company
        )
        child = WorkOrder.objects.create(
            parent=parent,
            product_name=f"{product.name} - {stage1.name}",
            bom=bom,
            quantity=20,
            status='completed',
            machine=self.machine,
            stage=stage1,
            current_stage=stage1,
            company=self.company
        )
        next_child = WorkOrder.objects.create(
            parent=parent,
            product_name=f"{product.name} - {stage2.name}",
            bom=bom,
            quantity=20,
            status='pending',
            machine=None,
            stage=stage2,
            current_stage=stage2,
            company=self.company
        )

        result = WorkOrderService.create_next_stage_task(child, self.supervisor, auto_create=False)

        self.assertTrue(result.get('planner_assignment_required'))
        self.assertEqual(result.get('next_task').id, next_child.id)
        parent.refresh_from_db()
        self.assertTrue(parent.next_stage_ready)
        self.assertEqual(parent.current_stage, stage2)

    def test_existing_next_stage_with_machine_auto_progresses_to_supervisor(self):
        product, bom, stages = self._create_bom_with_stages(
            "Auto Progression",
            [
                {"name": "Cutting", "machine": self.machine},
                {"name": "Packing", "machine": self.machine2},
            ]
        )
        stage1, stage2 = stages

        now = timezone.now()
        parent = WorkOrder.objects.create(
            product_name=product.name,
            bom=bom,
            quantity=20,
            status='in_progress',
            current_stage=stage1,
            company=self.company
        )
        child = WorkOrder.objects.create(
            parent=parent,
            product_name=f"{product.name} - {stage1.name}",
            bom=bom,
            quantity=20,
            status='completed',
            machine=self.machine,
            stage=stage1,
            current_stage=stage1,
            start_date=now - timedelta(minutes=30),
            end_date=now,
            company=self.company
        )
        next_child = WorkOrder.objects.create(
            parent=parent,
            product_name=f"{product.name} - {stage2.name}",
            bom=bom,
            quantity=20,
            status='pending',
            machine=self.machine2,
            stage=stage2,
            current_stage=stage2,
            start_date=now + timedelta(minutes=5),
            end_date=now + timedelta(minutes=65),
            company=self.company
        )

        result = WorkOrderService.create_next_stage_task(child, self.supervisor, auto_create=False)

        self.assertFalse(result.get('planner_assignment_required', False))
        self.assertTrue(result.get('activated_existing'))
        parent.refresh_from_db()
        self.assertFalse(parent.next_stage_ready)
        self.assertEqual(parent.current_stage, stage2)

        visible = DashboardService._filter_work_orders_for_viewer(
            WorkOrder.objects.filter(parent=parent).select_related("parent"),
            viewer_role="supervisor",
            shift_config=None,
            viewer=None,
        )
        self.assertEqual(visible, [])

    def test_planner_release_queue_skips_preplanned_next_stage_with_machine(self):
        product, bom, stages = self._create_bom_with_stages(
            "Planner Queue Guard",
            [
                {"name": "Cutting", "machine": self.machine},
                {"name": "Packing", "machine": self.machine2},
            ]
        )
        stage1, stage2 = stages

        now = timezone.now()
        parent = WorkOrder.objects.create(
            product_name=product.name,
            bom=bom,
            quantity=12,
            status='in_progress',
            current_stage=stage1,
            company=self.company
        )
        stage_one = WorkOrder.objects.create(
            parent=parent,
            product_name=f"{product.name} - {stage1.name}",
            bom=bom,
            quantity=12,
            status='completed',
            machine=self.machine,
            stage=stage1,
            current_stage=stage1,
            start_date=now - timedelta(minutes=40),
            end_date=now - timedelta(minutes=5),
            company=self.company
        )
        ProductionLog.objects.create(
            work_order=stage_one,
            worker=self.worker,
            quantity=12,
            status='approved'
        )
        WorkOrder.objects.create(
            parent=parent,
            product_name=f"{product.name} - {stage2.name}",
            bom=bom,
            quantity=12,
            status='pending',
            machine=self.machine2,
            stage=stage2,
            current_stage=stage2,
            start_date=now + timedelta(minutes=10),
            end_date=now + timedelta(minutes=70),
            company=self.company
        )

        context = DashboardService.get_dashboard_context(
            self.company,
            viewer_role='planner',
            viewer=self.planner,
        )
        self.assertEqual(context.get('release_ready_count'), 0)
        self.assertEqual(context.get('release_ready_tasks'), [])

    def test_accept_scrap_reduces_parent_target_without_compensation_task(self):
        product, bom, stages = self._create_bom_with_stages(
            "Accept Scrap Product",
            [
                {"name": "Cutting", "machine": self.machine, "is_quality_check": True},
                {"name": "Packing", "machine": self.machine2},
            ]
        )
        stage1, _stage2 = stages

        parent = WorkOrder.objects.create(
            product_name=product.name,
            bom=bom,
            quantity=100,
            status='in_progress',
            machine=self.machine,
            stage=stage1,
            current_stage=stage1,
            company=self.company
        )
        child = WorkOrder.objects.create(
            parent=parent,
            product_name=f"{product.name} - {stage1.name}",
            bom=bom,
            quantity=100,
            status='completed',
            machine=self.machine,
            stage=stage1,
            current_stage=stage1,
            qc_requirement=True,
            company=self.company
        )
        qc = QualityCheck.objects.create(
            work_order=child,
            status='processed',
            good_quantity=90,
            repair_quantity=0,
            faulty_quantity=10
        )

        result = WorkOrderService.accept_scrap_from_quality_check(qc, 10, self.planner)

        parent.refresh_from_db()
        qc.refresh_from_db()
        self.assertEqual(parent.quantity, 90)
        self.assertEqual(qc.scrap_compensated_qty, 10)
        self.assertEqual(result.get('remaining_scrap'), 0)
        self.assertFalse(
            WorkOrder.objects.filter(
                parent=parent,
                is_scrap_compensation_task=True,
                scrap_source_quality_check=qc
            ).exists()
        )

    def test_create_log_blocks_quantity_above_remaining(self):
        wo = WorkOrder.objects.create(
            product_name="Cap Quantity Product",
            quantity=90,
            status='in_progress',
            machine=self.machine,
            company=self.company,
            assigned_worker=self.worker,
            assignment_type='manual',
            start_date=timezone.now()
        )

        with self.assertRaises(ValueError) as ctx:
            ProductionLogService.create_log(
                work_order=wo,
                worker=self.worker,
                quantity=100,
                shift='morning'
            )

        self.assertIn("exceeds remaining work", str(ctx.exception))

    def test_dashboard_intake_uses_root_work_orders_not_completed_stage_children(self):
        product, bom, stages = self._create_bom_with_stages(
            "Intake Root Product",
            [
                {"name": "Cutting", "machine": self.machine, "is_quality_check": True},
                {"name": "Assembly", "machine": self.machine2},
            ]
        )
        stage1, stage2 = stages

        parent = WorkOrder.objects.create(
            product_name=product.name,
            bom=bom,
            quantity=10,
            status='in_progress',
            current_stage=stage2,
            stage=stage1,
            company=self.company
        )
        child_stage1 = WorkOrder.objects.create(
            parent=parent,
            product_name=f"{product.name} - {stage1.name}",
            bom=bom,
            quantity=10,
            status='completed',
            stage=stage1,
            current_stage=stage1,
            machine=self.machine,
            company=self.company,
            qc_requirement=True
        )
        QualityCheck.objects.create(
            work_order=child_stage1,
            status='processed',
            good_quantity=10,
            repair_quantity=0,
            faulty_quantity=0
        )
        WorkOrder.objects.create(
            parent=parent,
            product_name=f"{product.name} - {stage2.name}",
            bom=bom,
            quantity=10,
            status='pending',
            stage=stage2,
            current_stage=stage2,
            machine=self.machine2,
            company=self.company
        )

        context = DashboardService.get_dashboard_context(self.company)
        intake = context.get('intake_orders_data') or []
        intake_row = next((row for row in intake if row.get('id') == parent.id), {})
        ids = {row.get('id') for row in intake}

        self.assertIn(parent.id, ids)
        self.assertNotIn(child_stage1.id, ids)
        self.assertEqual(intake_row.get('release_source_id'), child_stage1.id)
        self.assertEqual(int(intake_row.get('available_release_qty') or 0), 10)

    def test_work_order_log_api_includes_stage_child_logs_for_parent(self):
        product, bom, stages = self._create_bom_with_stages(
            "WO Log Parent Child",
            [
                {"name": "Cutting", "machine": self.machine},
                {"name": "Assembly", "machine": self.machine2},
            ]
        )
        stage1, _stage2 = stages

        parent = WorkOrder.objects.create(
            product_name=product.name,
            bom=bom,
            quantity=10,
            status='in_progress',
            stage=stage1,
            current_stage=stage1,
            company=self.company
        )
        child = WorkOrder.objects.create(
            parent=parent,
            product_name=f"{product.name} - {stage1.name}",
            bom=bom,
            quantity=10,
            status='in_progress',
            stage=stage1,
            current_stage=stage1,
            machine=self.machine,
            company=self.company
        )

        ProductionLog.objects.create(
            work_order=child,
            worker=self.worker,
            quantity=5,
            status='approved',
            note='Stage child production log'
        )

        self.client.force_login(self.planner)
        url = reverse('api_work_order_log', kwargs={'wo_id': parent.id})
        response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload.get('success'))
        logs = payload.get('logs') or []
        self.assertTrue(any(int(log.get('quantity') or 0) == 5 for log in logs))
        self.assertTrue(any(f"Stage WO #{child.id}" in (log.get('action') or "") for log in logs))

    def test_work_order_details_api_returns_parent_display_id_for_child_stage(self):
        product, bom, stages = self._create_bom_with_stages(
            "WO Details Parent Display",
            [
                {"name": "Cutting", "machine": self.machine},
                {"name": "Assembly", "machine": self.machine2},
            ]
        )
        stage1, _stage2 = stages

        parent = WorkOrder.objects.create(
            product_name=product.name,
            bom=bom,
            quantity=10,
            status='pending',
            stage=stage1,
            current_stage=stage1,
            company=self.company
        )
        child = WorkOrder.objects.create(
            parent=parent,
            product_name=f"{product.name} - {stage1.name}",
            bom=bom,
            quantity=10,
            status='pending',
            stage=stage1,
            current_stage=stage1,
            machine=self.machine,
            company=self.company
        )

        self.client.force_login(self.planner)
        url = reverse('get_work_order', kwargs={'wo_id': child.id})
        response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload.get('success'))
        self.assertEqual(payload['work_order']['id'], child.id)
        self.assertEqual(payload['work_order']['parent_id'], parent.id)
        self.assertEqual(payload['work_order']['display_work_order_id'], parent.id)
