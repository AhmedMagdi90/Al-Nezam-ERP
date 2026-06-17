from django.core.management.base import BaseCommand
from django.utils import timezone
from datetime import timedelta
from manufacturing.models import (
    Product, Machine, WorkOrder, ProductionLog, User
)

class Command(BaseCommand):
    help = 'Seeds database with complex manufacturing test scenarios'

    def handle(self, *args, **kwargs):
        self.stdout.write("🚀 Starting Complex Scenario Simulation...")

        user = User.objects.filter(is_superuser=True).first()
        if not user:
            self.stdout.write(self.style.ERROR("❌ No superuser found! Please create one."))
            return

        # --- SCENARIO 1: Long Duration / Multi-Shift (3 Days) ---
        self.stdout.write("\n🔹 Setting up Scenario 1: Long Run Order (3 Days)")
        
        product_long, _ = Product.objects.get_or_create(
            name="Long-Run Component X",
            defaults={'description': 'Requires 72 hours of continuous machining'}
        )
        
        machine_cnc = Machine.objects.first()
        if not machine_cnc:
             machine_cnc = Machine.objects.create(name="CNC Main", code="CNC-01")

        start_time = timezone.now().replace(hour=8, minute=0, second=0, microsecond=0)
        end_time = start_time + timedelta(days=3)
        
        wo_long = WorkOrder.objects.create(
            product_name=product_long.name,
            machine=machine_cnc,
            quantity=1000,
            status='pending',
            priority='high',
            scheduled_start_date=start_time,
            due_date=end_time,
            instructions="Test Case 1: 3-Day Continuous Run"
        )
        self.stdout.write(self.style.SUCCESS(f"   ✅ Created Work Order: {wo_long}"))


        # --- SCENARIO 2: Multi-Machine / Multi-Stage (Waterfall) ---
        self.stdout.write("\n🔹 Setting up Scenario 2: Multi-Stage Waterfall")
        
        product_complex, _ = Product.objects.get_or_create(
            name="Complex Widget Y",
            defaults={'description': 'Multi-Machine Routing'}
        )
        
        machines = list(Machine.objects.all())
        if len(machines) < 2:
            m2 = Machine.objects.create(name="Assembly Station 1", code="ASM-01")
            machines.append(m2)
            
        m_cut = machines[0]
        m_asm = machines[1] if len(machines) > 1 else machines[0]
        
        # Stage 1: Cutting (Day 1)
        s1_start = timezone.now() + timedelta(days=1)
        s1_end = s1_start + timedelta(hours=4)
        
        wo_stage_1 = WorkOrder.objects.create(
            product_name=product_complex.name,
            machine=m_cut,
            quantity=500,
            status='pending',
            priority='medium',
            scheduled_start_date=s1_start,
            due_date=s1_end,
            instructions="Test Case 2: Stage 1 (Cutting)"
        )
        
        # Stage 2: Assembly (Day 1 Afternoon - Day 2)
        s2_start = s1_end + timedelta(hours=1) 
        s2_end = s2_start + timedelta(days=1)
        
        wo_stage_2 = WorkOrder.objects.create(
            product_name=product_complex.name,
            machine=m_asm,
            quantity=500,
            status='pending',
            priority='medium',
            scheduled_start_date=s2_start,
            due_date=s2_end,
            instructions="Test Case 2: Stage 2 (Assembly)"
        )
        self.stdout.write(self.style.SUCCESS(f"   ✅ Created Multi-Stage WOs: {wo_stage_1.id} -> {wo_stage_2.id}"))


        # --- SCENARIO 3: Split Operation Setup ---
        self.stdout.write("\n🔹 Setting up Scenario 3: Split Candidate")
        
        product_split, _ = Product.objects.get_or_create(
            name="Split Candidate Z",
            defaults={'description': 'Partial production ready for split'} 
        )
        
        wo_split = WorkOrder.objects.create(
            product_name=product_split.name,
            machine=machine_cnc,
            quantity=1000,
            status='in_progress',
            priority='urgent',
            scheduled_start_date=timezone.now(),
            due_date=timezone.now() + timedelta(hours=8),
            instructions="Test Case 3: Ready for Split"
        )
        
        ProductionLog.objects.create(
            work_order=wo_split,
            worker=user,
            quantity=100,
            note="Initial batch of 100 complete"
        )
        self.stdout.write(self.style.SUCCESS(f"   ✅ Created Split Candidate: {wo_split} (100/1000 produced)"))
        
        self.stdout.write(self.style.SUCCESS("\n✨ Simulation Data Seeding Complete!"))
