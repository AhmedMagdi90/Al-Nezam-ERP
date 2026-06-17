from django.test import TestCase
from django.utils import timezone
from datetime import timedelta
from django.contrib.auth.models import User

from manufacturing.models import Product, Machine, WorkOrder, ProductionLog
from manufacturing.tests.utils import create_company


class ComplexScenarioTests(TestCase):

    def setUp(self):
        self.company = create_company()
        # Create Basic Data
        self.cnc_machine = Machine.objects.create(
            name="CNC Main",
            code="CNC-01",
            company=self.company,
            status='operational'
        )
        self.assembly_machine = Machine.objects.create(
            name="Assembly Station",
            code="ASM-01",
            company=self.company,
            status='operational'
        )

        self.product_long = Product.objects.create(name="Long-Run Component X", description="Long run item", company=self.company)
        self.product_complex = Product.objects.create(name="Complex Widget Y", description="Multi-stage item", company=self.company)
        self.product_split = Product.objects.create(name="Split Candidate Z", description="Split item", company=self.company)

        # Create User for logging
        self.user = User.objects.create_user(username='testworker', password='password')

    def test_case_1_long_duration_work_order(self):
        """
        Verify that a work order can be scheduled for a duration > 24 hours (e.g. 3 days)
        and persists correctly in the database.
        """
        start_time = timezone.now()
        end_time = start_time + timedelta(days=3)

        wo = WorkOrder.objects.create(
            product_name=self.product_long.name,
            machine=self.cnc_machine,
            quantity=1000,
            status='pending',
            scheduled_start_date=start_time,
            due_date=end_time,
            instructions="3-Day Run",
            company=self.company
        )

        # assertions
        self.assertEqual(wo.status, 'pending')
        self.assertGreater(wo.due_date - wo.scheduled_start_date, timedelta(days=2))
        self.assertEqual(wo.machine.name, "CNC Main")
        self.assertIsNotNone(wo.id)

    def test_case_2_multi_stage_waterfall(self):
        """
        Verify creation of dependent work orders (Waterfall).
        Since we don't have explicit foreign key hard-linking them in this simple model,
        we verify they can exist with sequential timing.
        """
        # Stage 1
        s1_start = timezone.now()
        s1_end = s1_start + timedelta(hours=4)
        wo_1 = WorkOrder.objects.create(
            product_name=self.product_complex.name,
            machine=self.cnc_machine,
            quantity=500,
            status='pending',
            scheduled_start_date=s1_start,
            due_date=s1_end,
            instructions="Stage 1",
            company=self.company
        )

        # Stage 2 (Starts after Stage 1)
        s2_start = s1_end + timedelta(hours=1)
        s2_end = s2_start + timedelta(hours=4)
        wo_2 = WorkOrder.objects.create(
            product_name=self.product_complex.name,
            machine=self.assembly_machine,
            quantity=500,
            status='pending',
            scheduled_start_date=s2_start,
            due_date=s2_end,
            instructions="Stage 2",
            company=self.company
        )

        self.assertTrue(wo_2.scheduled_start_date > wo_1.due_date)
        self.assertIsNotNone(wo_2.id)

    def test_case_3_split_logic_simulation(self):
        """
        Simulate the logic of splitting a work order:
        1. Existing WO has production logged.
        2. 'Split' creates a NEW WO with the remaining/moved quantity.
        3. Original WO quantity might be adjusted (depending on business logic).

        *Note: This tests the DATA logic that the View/Modal would perform.*
        """
        # Original Order: 1000 units
        original_qty = 1000
        wo_original = WorkOrder.objects.create(
            product_name=self.product_split.name,
            machine=self.cnc_machine,
            quantity=original_qty,
            status='in_progress',
            scheduled_start_date=timezone.now(),
            due_date=timezone.now() + timedelta(hours=8),
            instructions="Original Order",
            company=self.company
        )

        # Simulate producing 100 units
        ProductionLog.objects.create(
            work_order=wo_original,
            worker=self.user,
            quantity=100,
            note="Partial complete"
        )

        # --- EXECUTE SPLIT LOGIC ---
        # Move 100 units to a new machine (Assembly)
        move_qty = 100
        new_start = timezone.now() + timedelta(hours=1)

        wo_split_child = WorkOrder.objects.create(
            product_name=wo_original.product_name, # Same product
            machine=self.assembly_machine,    # New machine
            quantity=move_qty,                # The moved quantity
            status='pending',
            scheduled_start_date=new_start,
            due_date=new_start + timedelta(hours=2),
            instructions=f"Split from WO #{wo_original.id}",
            company=self.company
        )

        # Verification
        self.assertEqual(wo_split_child.quantity, 100)
        self.assertEqual(wo_split_child.machine.code, "ASM-01")
        self.assertIn(f"Split from WO #{wo_original.id}", wo_split_child.instructions)
        self.assertIsNotNone(wo_split_child.id)
