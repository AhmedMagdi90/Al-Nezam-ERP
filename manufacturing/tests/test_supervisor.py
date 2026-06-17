from datetime import timedelta
from unittest.mock import patch

from django.http import HttpResponse
from django.test import TestCase, Client, RequestFactory
from django.urls import reverse
from django.utils import timezone

from manufacturing.models import WorkOrder, Machine, Product, BillOfMaterial, ProductionLog, ProductionStage, SystemSettings
from manufacturing.tests.utils import create_company, create_user_with_role
from manufacturing.views.dashboard import SupervisorDashboardView

class SupervisorLogicTests(TestCase):
    def setUp(self):
        self.company = create_company()
        # Assign planner role for assignment API
        self.planner = create_user_with_role('planner', 'planner', self.company)
        
        # Create Data
        self.product = Product.objects.create(name="Test Product", company=self.company)
        self.bom = BillOfMaterial.objects.create(product=self.product, status='active')
        self.machine = Machine.objects.create(
            name="Test Machine",
            code="M001",
            status="operational",
            company=self.company
        )
        
        # Create Pending WO
        self.wo = WorkOrder.objects.create(
            product_name="Test Product",
            quantity=100,
            status='pending',
            company=self.company,
            material_readiness_status='ready',
            material_available_qty=100,
        )
        
        self.client = Client()
        self.client.force_login(self.planner)
        self.factory = RequestFactory()

    def test_assign_work_order_api(self):
        """Test the API endpoint used by Drag & Drop."""
        url = reverse('assign_work_order')
        data = {
            'wo_id': self.wo.id,
            'machine_id': self.machine.id
        }
        
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['success'])
        
        # Verify DB updates
        self.wo.refresh_from_db()
        
        self.assertEqual(self.wo.status, 'pending')
        self.assertEqual(self.wo.machine, self.machine)

    def test_supervisor_dashboard_context(self):
        """Test that the dashboard loads the correct context."""
        url = reverse('supervisor_dashboard')
        response = self.client.get(url)
        
        self.assertEqual(response.status_code, 200)
        self.assertIn('pending_tasks', response.context)
        self.assertIn('active_tasks', response.context)
        self.assertIn('assignment_tasks', response.context)
        self.assertIn('pending_tasks_count', response.context)
        self.assertIn('active_tasks_count', response.context)
        self.assertIn('assignment_tasks_count', response.context)
        self.assertIn('assigned_pending_tasks', response.context)
        self.assertIn('assigned_pending_count', response.context)
        self.assertIn('ready_dispatch_tasks', response.context)
        self.assertIn('ready_dispatch_count', response.context)

    def test_supervisor_dashboard_splits_pending_active_and_assignment_queues(self):
        worker = create_user_with_role('worker_for_supervisor', 'worker', self.company)

        pending_unassigned = WorkOrder.objects.create(
            product_name="Pending Unassigned",
            bom=self.bom,
            quantity=10,
            status='pending',
            company=self.company,
            machine=self.machine,
            start_date=timezone.now(),
        )
        pending_assigned = WorkOrder.objects.create(
            product_name="Pending Assigned",
            bom=self.bom,
            quantity=8,
            status='pending',
            company=self.company,
            machine=self.machine,
            start_date=timezone.now(),
            assigned_worker=worker,
        )
        active_started = WorkOrder.objects.create(
            product_name="Active Started",
            bom=self.bom,
            quantity=6,
            status='in_progress',
            company=self.company,
            machine=self.machine,
            start_date=timezone.now(),
            assigned_worker=worker,
        )

        response = self.client.get(reverse('supervisor_dashboard'))
        self.assertEqual(response.status_code, 200)

        pending_ids = {wo.id for wo in response.context['pending_tasks']}
        active_ids = {wo.id for wo in response.context['active_tasks']}
        assignment_ids = {wo.id for wo in response.context['assignment_tasks']}
        assigned_pending_ids = {wo.id for wo in response.context['assigned_pending_tasks']}

        self.assertIn(pending_unassigned.id, pending_ids)
        self.assertIn(pending_assigned.id, pending_ids)
        self.assertNotIn(active_started.id, pending_ids)

        self.assertIn(active_started.id, active_ids)
        self.assertNotIn(pending_unassigned.id, active_ids)

        self.assertIn(pending_unassigned.id, assignment_ids)
        self.assertNotIn(pending_assigned.id, assignment_ids)
        self.assertNotIn(active_started.id, assignment_ids)
        ready_dispatch_ids = {wo.id for wo in response.context['ready_dispatch_tasks']}
        self.assertIn(pending_unassigned.id, ready_dispatch_ids)
        self.assertNotIn(pending_assigned.id, ready_dispatch_ids)
        self.assertNotIn(active_started.id, ready_dispatch_ids)

        self.assertIn(pending_assigned.id, assigned_pending_ids)
        self.assertNotIn(pending_unassigned.id, assigned_pending_ids)
        self.assertNotIn(active_started.id, assigned_pending_ids)

    def test_supervisor_ready_dispatch_includes_future_and_excludes_fault_machines(self):
        future_pending = WorkOrder.objects.create(
            product_name="Future Dispatch",
            bom=self.bom,
            quantity=4,
            status='pending',
            company=self.company,
            machine=self.machine,
            start_date=timezone.now() + timedelta(hours=2),
        )
        fault_machine = Machine.objects.create(
            name="Fault Machine",
            code="F001",
            status="breakdown",
            company=self.company,
        )
        fault_pending = WorkOrder.objects.create(
            product_name="Fault Dispatch",
            bom=self.bom,
            quantity=4,
            status='pending',
            company=self.company,
            machine=fault_machine,
            start_date=timezone.now() - timedelta(minutes=30),
        )
        ready_pending = WorkOrder.objects.create(
            product_name="Ready Dispatch",
            bom=self.bom,
            quantity=4,
            status='pending',
            company=self.company,
            machine=self.machine,
            start_date=timezone.now() - timedelta(minutes=10),
        )

        response = self.client.get(reverse('supervisor_dashboard'))
        self.assertEqual(response.status_code, 200)

        ready_dispatch_ids = {wo.id for wo in response.context['ready_dispatch_tasks']}
        self.assertIn(ready_pending.id, ready_dispatch_ids)
        self.assertIn(future_pending.id, ready_dispatch_ids)
        self.assertNotIn(fault_pending.id, ready_dispatch_ids)

    def test_supervisor_dashboard_keeps_upcoming_scheduled_work_orders_in_pending(self):
        stage = ProductionStage.objects.create(name="Cutting", category="Cutting", order=1)
        later_pending = WorkOrder.objects.create(
            product_name="Later Pending",
            bom=self.bom,
            quantity=12,
            status='pending',
            company=self.company,
            machine=self.machine,
            stage=stage,
            start_date=timezone.now() + timedelta(hours=3),
        )
        earlier_pending = WorkOrder.objects.create(
            product_name="Earlier Pending",
            bom=self.bom,
            quantity=8,
            status='pending',
            company=self.company,
            machine=self.machine,
            stage=stage,
            start_date=timezone.now() + timedelta(hours=1),
        )

        request = self.factory.get(reverse('supervisor_dashboard'))
        request.user = self.planner

        with patch('manufacturing.views.dashboard.render') as render_mock:
            render_mock.return_value = HttpResponse("ok")
            response = SupervisorDashboardView.as_view()(request)

        self.assertEqual(response.status_code, 200)
        context = render_mock.call_args.args[2]

        pending_ids = {wo.id for wo in context['pending_tasks']}
        self.assertIn(later_pending.id, pending_ids)
        self.assertIn(earlier_pending.id, pending_ids)
        self.assertEqual(context['upcoming_pending_tasks_count'], 2)
        self.assertEqual(
            [wo.id for wo in context['upcoming_tasks']],
            [earlier_pending.id, later_pending.id],
        )
        self.assertEqual(context['upcoming_tasks'][0].upcoming_section_name, "Cutting")

    def test_supervisor_template_contains_upcoming_tab(self):
        with open("templates/manufacturing/supervisor_dashboard.html", encoding="utf-8") as handle:
            content = handle.read()

        self.assertIn("tab === 'upcoming'", content)
        self.assertIn("Upcoming Work Orders", content)
        self.assertIn("upcoming_tasks", content)
        self.assertIn("upcoming_section_name", content)
        self.assertIn("upcoming_worker_name", content)

    def test_supervisor_upcoming_tab_is_scoped_to_section(self):
        supervisor = create_user_with_role('supervisor_cutting_upcoming', 'supervisor', self.company)
        supervisor.profile.department = "Cutting"
        supervisor.profile.save(update_fields=["department"])
        worker = create_user_with_role('worker_cutting_upcoming', 'worker', self.company)
        cut_stage = ProductionStage.objects.create(name="Cutting", category="Cutting", order=1)
        pack_stage = ProductionStage.objects.create(name="Packing", category="Packing", order=2)
        cut_machine = Machine.objects.create(
            name="Cut Line",
            code="CUT-US21",
            status="operational",
            category="Cutting",
            company=self.company,
        )
        pack_machine = Machine.objects.create(
            name="Pack Line",
            code="PACK-US21",
            status="operational",
            category="Packing",
            company=self.company,
        )
        cut_future = WorkOrder.objects.create(
            product_name="Cut Future",
            bom=self.bom,
            quantity=10,
            status='pending',
            company=self.company,
            machine=cut_machine,
            stage=cut_stage,
            assigned_worker=worker,
            start_date=timezone.now() + timedelta(hours=1),
        )
        pack_future = WorkOrder.objects.create(
            product_name="Pack Future",
            bom=self.bom,
            quantity=10,
            status='pending',
            company=self.company,
            machine=pack_machine,
            stage=pack_stage,
            start_date=timezone.now() + timedelta(hours=2),
        )

        request = self.factory.get(reverse('supervisor_dashboard'), {"tab": "upcoming"})
        request.user = supervisor

        with patch('manufacturing.views.dashboard.render') as render_mock:
            render_mock.return_value = HttpResponse("ok")
            response = SupervisorDashboardView.as_view()(request)

        self.assertEqual(response.status_code, 200)
        context = render_mock.call_args.args[2]
        upcoming_ids = [wo.id for wo in context['upcoming_tasks']]
        self.assertEqual(upcoming_ids, [cut_future.id])
        self.assertNotIn(pack_future.id, upcoming_ids)
        self.assertEqual(context['upcoming_tasks'][0].upcoming_section_name, "Cutting")
        self.assertEqual(context['upcoming_tasks'][0].upcoming_worker_name, worker.username)
        self.assertEqual(context['upcoming_section_filter_label'], "Cutting")

    def test_supervisor_pending_approvals_are_department_scoped(self):
        supervisor = create_user_with_role('supervisor_cutting_approvals', 'supervisor', self.company)
        supervisor.profile.department = "Cutting"
        supervisor.profile.save(update_fields=["department"])
        worker = create_user_with_role('worker_for_scoped_approvals', 'worker', self.company)
        cut_stage = ProductionStage.objects.create(name="Cutting", category="Cutting", order=1)
        pack_stage = ProductionStage.objects.create(name="Packing", category="Packing", order=2)
        cut_machine = Machine.objects.create(
            name="Cut Machine",
            code="CUT-1",
            category="Cutting",
            status="operational",
            company=self.company,
        )
        pack_machine = Machine.objects.create(
            name="Pack Machine",
            code="PACK-1",
            category="Packing",
            status="operational",
            company=self.company,
        )
        cut_wo = WorkOrder.objects.create(
            product_name="Cut Approval",
            bom=self.bom,
            quantity=10,
            status='in_progress',
            company=self.company,
            machine=cut_machine,
            stage=cut_stage,
            current_stage=cut_stage,
            start_date=timezone.now(),
        )
        pack_wo = WorkOrder.objects.create(
            product_name="Pack Approval",
            bom=self.bom,
            quantity=10,
            status='in_progress',
            company=self.company,
            machine=pack_machine,
            stage=pack_stage,
            current_stage=pack_stage,
            start_date=timezone.now(),
        )
        cut_log = ProductionLog.objects.create(work_order=cut_wo, worker=worker, quantity=4, status='pending')
        pack_log = ProductionLog.objects.create(work_order=pack_wo, worker=worker, quantity=4, status='pending')

        self.client.force_login(supervisor)
        response = self.client.get(reverse('supervisor_dashboard'))
        self.assertEqual(response.status_code, 200)

        approval_ids = {log.id for log in response.context['pending_logs']}
        self.assertIn(cut_log.id, approval_ids)
        self.assertNotIn(pack_log.id, approval_ids)


    def test_supervisor_dashboard_renders_three_distinct_execution_columns(self):
        worker = create_user_with_role('worker_for_supervisor_colors', 'worker', self.company)

        WorkOrder.objects.create(
            product_name="Needs Assignment WO",
            bom=self.bom,
            quantity=10,
            status='pending',
            company=self.company,
            machine=self.machine,
            start_date=timezone.now(),
        )
        WorkOrder.objects.create(
            product_name="Assigned Waiting WO",
            bom=self.bom,
            quantity=9,
            status='pending',
            company=self.company,
            machine=self.machine,
            start_date=timezone.now(),
            assigned_worker=worker,
        )
        WorkOrder.objects.create(
            product_name="Running WO",
            bom=self.bom,
            quantity=8,
            status='in_progress',
            company=self.company,
            machine=self.machine,
            start_date=timezone.now(),
            assigned_worker=worker,
        )

        response = self.client.get(reverse('supervisor_dashboard'))
        self.assertEqual(response.status_code, 200)

        content = response.content.decode()
        self.assertIn('lg:grid-cols-3', content)
        self.assertIn('Ready to Dispatch', content)
        self.assertIn('Waiting Start', content)
        self.assertIn('Running', content)
        self.assertIn('border-l-rose-500', content)
        self.assertIn('border-l-amber-500', content)
        self.assertIn('border-l-cyan-500', content)
        self.assertNotIn('Due now and waiting for a worker', content)

    def test_supervisor_dashboard_persists_active_tab_state(self):
        response = self.client.get(reverse('supervisor_dashboard'))
        self.assertEqual(response.status_code, 200)

        content = response.content.decode()
        self.assertIn('SUPERVISOR_DASHBOARD_TAB_STORAGE_KEY', content)
        self.assertIn("supervisor.dashboard.activeTab", content)
        self.assertIn('getSupervisorDashboardInitialTab()', content)
        self.assertIn('persistSupervisorDashboardTab(nextTab)', content)
        self.assertIn('reloadSupervisorDashboardPreservingTab', content)
        self.assertIn("x-init=\"persistTab(tab)\"", content)

    def test_supervisor_intake_hides_unscheduled_scrap_compensation(self):
        """Unscheduled scrap compensation tasks must stay planner-side until scheduled."""
        scheduled = WorkOrder.objects.create(
            product_name="Scheduled WO",
            bom=self.bom,
            quantity=10,
            status='pending',
            company=self.company,
            machine=self.machine,
            start_date=timezone.now(),
        )
        unscheduled_scrap = WorkOrder.objects.create(
            product_name="Scrap Compensation WO",
            bom=self.bom,
            quantity=2,
            status='pending',
            company=self.company,
            machine=self.machine,
            is_scrap_compensation_task=True,
        )

        url = reverse('supervisor_dashboard')
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

        pending_ids = {wo.id for wo in response.context['pending_tasks']}
        self.assertIn(scheduled.id, pending_ids)
        self.assertNotIn(unscheduled_scrap.id, pending_ids)

    def test_supervisor_dashboard_uses_db_aware_worker_mode_flag(self):
        supervisor = create_user_with_role('supervisor_worker_mode', 'supervisor', self.company)
        supervisor.profile.worker_mode_enabled = True
        supervisor.profile.save(update_fields=['worker_mode_enabled'])

        stale_user = type(supervisor).objects.get(pk=supervisor.pk)
        _ = stale_user.profile
        stale_user.profile.worker_mode_enabled = False

        request = self.factory.get(reverse('supervisor_dashboard'))
        request.user = stale_user

        response = SupervisorDashboardView.as_view()(request)
        self.assertEqual(response.status_code, 200)
        self.assertIn('Open Worker Station', response.content.decode())

    def test_admin_viewing_supervisor_dashboard_is_not_department_filtered(self):
        admin = create_user_with_role('admin_supervisor_view', 'admin', self.company)
        admin.profile.department = 'Management'
        admin.profile.save(update_fields=['department'])

        visible_wo = WorkOrder.objects.create(
            product_name="Assembly Queue",
            bom=self.bom,
            quantity=5,
            status='pending',
            company=self.company,
            machine=self.machine,
            start_date=timezone.now(),
        )

        self.client.force_login(admin)
        response = self.client.get(reverse('supervisor_dashboard'))
        self.assertEqual(response.status_code, 200)

        pending_ids = {wo.id for wo in response.context['pending_tasks']}
        self.assertIn(visible_wo.id, pending_ids)
        self.assertEqual(response.context['open_wos_count'], 1)

    def test_supervisor_context_includes_shift_handover_for_previous_shift_open_work(self):
        supervisor = create_user_with_role('supervisor_handover', 'supervisor', self.company)
        worker = create_user_with_role('worker_handover', 'worker', self.company)
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

        inherited_wo = WorkOrder.objects.create(
            product_name="Previous Shift WO",
            bom=self.bom,
            quantity=10,
            status='in_progress',
            company=self.company,
            machine=self.machine,
            assigned_worker=worker,
            start_date=timezone.now() - timedelta(hours=3),
            end_date=timezone.now() - timedelta(hours=2),
        )
        current_shift_wo = WorkOrder.objects.create(
            product_name="Current Shift WO",
            bom=self.bom,
            quantity=6,
            status='in_progress',
            company=self.company,
            machine=self.machine,
            assigned_worker=worker,
            start_date=timezone.now(),
        )

        request = self.factory.get(reverse('supervisor_dashboard'))
        request.user = supervisor

        with patch('manufacturing.views.dashboard.render') as render_mock:
            render_mock.return_value = HttpResponse("ok")
            response = SupervisorDashboardView.as_view()(request)

        self.assertEqual(response.status_code, 200)
        context = render_mock.call_args.args[2]
        handover_ids = {wo.id for wo in context['shift_handover_tasks']}
        self.assertIn(inherited_wo.id, handover_ids)
        self.assertNotIn(current_shift_wo.id, handover_ids)
        self.assertEqual(context['shift_handover_count'], 1)
        handover_wo = context['shift_handover_tasks'][0]
        self.assertEqual(handover_wo.handover_worker_name, worker.username)
        self.assertEqual(handover_wo.handover_status_label, "In Progress")

    def test_supervisor_template_contains_shift_handover_banner_fields(self):
        with open("templates/manufacturing/supervisor_dashboard.html", encoding="utf-8") as handle:
            content = handle.read()

        self.assertIn("Shift Handover", content)
        self.assertIn("shift_handover_tasks", content)
        self.assertIn("handover_worker_name", content)
        self.assertIn("handover_status_label", content)
        self.assertIn("logReviewRemainingAction", content)
        self.assertIn("will return to Dispatch for worker assignment", content)

    def test_supervisor_context_marks_partial_log_remaining_for_reassignment(self):
        supervisor = create_user_with_role('supervisor_partial_remaining', 'supervisor', self.company)
        worker = create_user_with_role('worker_partial_remaining', 'worker', self.company)
        wo = WorkOrder.objects.create(
            product_name="Partial Remaining WO",
            bom=self.bom,
            quantity=10,
            status='in_progress',
            company=self.company,
            machine=self.machine,
            assigned_worker=worker,
            start_date=timezone.now(),
        )
        ProductionLog.objects.create(
            work_order=wo,
            worker=worker,
            quantity=4,
            status='pending',
        )

        request = self.factory.get(reverse('supervisor_dashboard'), {"tab": "approvals"})
        request.user = supervisor

        with patch('manufacturing.views.dashboard.render') as render_mock:
            render_mock.return_value = HttpResponse("ok")
            response = SupervisorDashboardView.as_view()(request)

        self.assertEqual(response.status_code, 200)
        context = render_mock.call_args.args[2]
        pending_log = context['pending_logs'][0]
        self.assertEqual(pending_log.review_remaining_before_log, 10)
        self.assertEqual(pending_log.review_remaining_after_log, 6)
        self.assertTrue(pending_log.review_has_remaining_after_approval)

    def test_supervisor_ready_dispatch_includes_future_split_remainder(self):
        supervisor = create_user_with_role('supervisor_future_split', 'supervisor', self.company)
        worker = create_user_with_role('worker_future_split', 'worker', self.company)
        source = WorkOrder.objects.create(
            product_name="mac",
            bom=self.bom,
            quantity=100,
            status='completed',
            company=self.company,
            machine=self.machine,
            assigned_worker=worker,
            start_date=timezone.now() - timedelta(hours=2),
        )
        remainder = WorkOrder.objects.create(
            product_name="mac",
            bom=self.bom,
            quantity=100,
            status='pending',
            company=self.company,
            machine=self.machine,
            source_task=source,
            start_date=timezone.now() + timedelta(hours=2),
            scheduled_start_date=timezone.now() + timedelta(hours=2),
        )
        future_planned = WorkOrder.objects.create(
            product_name="future planned",
            bom=self.bom,
            quantity=25,
            status='pending',
            company=self.company,
            machine=self.machine,
            start_date=timezone.now() + timedelta(hours=3),
            scheduled_start_date=timezone.now() + timedelta(hours=3),
        )

        request = self.factory.get(reverse('supervisor_dashboard'))
        request.user = supervisor

        with patch('manufacturing.views.dashboard.render') as render_mock:
            render_mock.return_value = HttpResponse("ok")
            response = SupervisorDashboardView.as_view()(request)

        self.assertEqual(response.status_code, 200)
        context = render_mock.call_args.args[2]
        ready_dispatch_ids = {wo.id for wo in context['ready_dispatch_tasks']}
        assignment_ids = {wo.id for wo in context['assignment_tasks']}
        upcoming_ids = {wo.id for wo in context['upcoming_tasks']}

        self.assertIn(remainder.id, assignment_ids)
        self.assertIn(remainder.id, ready_dispatch_ids)
        self.assertIn(remainder.id, upcoming_ids)
        self.assertIn(future_planned.id, assignment_ids)
        self.assertIn(future_planned.id, ready_dispatch_ids)
        self.assertIn(future_planned.id, upcoming_ids)

    def test_available_workers_api_includes_supervisors_with_worker_mode(self):
        supervisor = create_user_with_role('supervisor_operator_mode', 'supervisor', self.company)
        supervisor.profile.worker_mode_enabled = True
        supervisor.profile.save(update_fields=['worker_mode_enabled'])
        worker = create_user_with_role('real_assignment_worker', 'worker', self.company)

        response = self.client.get(reverse('get_available_workers'), {"machine_id": self.machine.id})
        self.assertEqual(response.status_code, 200)

        worker_names = {row["username"] for row in response.json()["workers"]}
        self.assertIn(worker.username, worker_names)
        self.assertIn(supervisor.username, worker_names)
