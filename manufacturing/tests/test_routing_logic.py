from django.test import TestCase, Client
from django.urls import reverse
from django.utils import timezone

from manufacturing.models import WorkOrder, Machine, Product, BillOfMaterial
from manufacturing.tests.utils import create_company, create_user_with_role

class RoutingLogicTests(TestCase):
    def setUp(self):
        self.company = create_company()
        self.planner = create_user_with_role('planner', 'planner', self.company)
        self.client = Client()
        self.client.force_login(self.planner)
        
        self.machine = Machine.objects.create(
            name="Cutting Machine",
            code="M1",
            status="operational",
            company=self.company
        )
        self.product = Product.objects.create(name="T-Shirt", company=self.company)
        self.bom = BillOfMaterial.objects.create(
            product=self.product,
            status='active',
            base_quantity=1
        )
        
        # Create Parent WO
        self.parent_wo = WorkOrder.objects.create(
            product_name="T-Shirt Batch",
            bom=self.bom,
            quantity=100,
            status='in_progress',
            start_date=timezone.now(),
            company=self.company,
            material_readiness_status='ready',
        )
        
        # Create Sub-Task (Backlog Item)
        self.sub_task = WorkOrder.objects.create(
            parent=self.parent_wo,
            product_name="T-Shirt - Cutting",
            bom=self.bom,
            quantity=100,
            machine=self.machine, # Linked to machine via routing
            status='pending',
            company=self.company,
            material_readiness_status='ready',
        )

    def test_dashboard_backlog_context(self):
        """Verify that pending sub-tasks appear in the backlog context."""
        url = reverse('supervisor_dashboard')
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        pending_tasks = response.context['pending_tasks']
        self.assertTrue(pending_tasks.filter(id=self.sub_task.id).exists())

    def test_start_task_api(self):
        """Verify 'Start' button API call."""
        url = reverse('assign_work_order')
        response = self.client.post(url, {
            'wo_id': self.sub_task.id,
            'machine_id': self.machine.id,
            'status': 'in_progress'
        })
        
        self.assertTrue(response.json()['success'])
        
        self.sub_task.refresh_from_db()
        
        self.assertEqual(self.sub_task.status, 'in_progress')
        self.assertEqual(self.sub_task.machine, self.machine)
