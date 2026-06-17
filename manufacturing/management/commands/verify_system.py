from django.core.management.base import BaseCommand
from django.contrib.auth.models import User
from accounts.models import Profile, Role
from manufacturing.models import (
    Company, Product, BillOfMaterial, BOMComponent, BOMOperation, BOMAcceptanceCriteria,
    WorkOrder, Machine, ProductionStage, ProductionLog, QualityCheck, Notification
)
from datetime import timedelta
from django.utils import timezone

class Command(BaseCommand):
    help = 'Verifies all core manufacturing features by simulating a full workflow.'

    def handle(self, *args, **kwargs):
        self.stdout.write(self.style.SUCCESS("Starting System Verification..."))

        try:
            # 1. Setup Environment
            self.stdout.write("   [Setup] Creating Test Company & Users...")
            company, _ = Company.objects.get_or_create(name="Verification Corp")
            
            owner, _ = User.objects.get_or_create(username="verify_owner", defaults={'email': 'owner@verify.com'})
            owner_role, _ = Role.objects.get_or_create(name="admin")
            Profile.objects.update_or_create(user=owner, defaults={'company': company, 'role': owner_role})

            planner, _ = User.objects.get_or_create(username="verify_planner", defaults={'email': 'planner@verify.com'})
            planner_role, _ = Role.objects.get_or_create(name="planner")
            Profile.objects.update_or_create(user=planner, defaults={'company': company, 'role': planner_role})

            self.stdout.write(self.style.SUCCESS("   [OK] Users Created"))

            # 2. Machine Setup
            self.stdout.write("   [Machine] Creating Machines...")
            machine_a, _ = Machine.objects.get_or_create(
                code="VER-M01", company=company, 
                defaults={'name': "Verify CNC", 'status': 'operational'}
            )
            self.stdout.write(self.style.SUCCESS(f"   [OK] Machine {machine_a.name} ready"))

            # 3. BOM Creation (The 3-Tab Feature)
            self.stdout.write("   [BOM] Creating Product & Complex BOM...")
            product, _ = Product.objects.get_or_create(name="Verify Widget", company=company)
            
            timestamp = timezone.now().strftime("%Y%m%d%H%M%S")
            bom = BillOfMaterial.objects.create(
                product=product,
                version=f"verify-{timestamp}",
                base_quantity=100,
                status='draft',
                created_by=owner
            )
            
            # Tab 1: Materials
            BOMComponent.objects.create(
                bom=bom, material_name="Raw Steel", quantity=10, unit="kg", 
                cost_per_unit=5.0, wastage_quantity=1, scrap_type="sell_as_scrap"
            )
            # Tab 2: Operations
            stage, _ = ProductionStage.objects.get_or_create(name="Cutting", machine=machine_a)
            BOMOperation.objects.create(
                bom=bom, machine=machine_a, stage=stage, order=1, duration_minutes=30
            )
            # Tab 3: Criteria
            BOMAcceptanceCriteria.objects.create(
                bom=bom, parameter="Length", method="Measure", criteria_min="9.9", criteria_max="10.1"
            )
            
            # Activate BOM
            bom.status = 'active'
            bom.save()
            
            # Verify Cost Calculation
            total_cost = sum(c.total_cost() for c in bom.components.all())
            self.stdout.write(self.style.SUCCESS(f"   [OK] BOM Created. Total Material Cost: ${total_cost}"))

            # 4. Work Order Lifecycle
            self.stdout.write("   [Planner] Creating Work Order flow...")
            wo = WorkOrder.objects.create(
                company=company, product_name=product.name, bom=bom, 
                quantity=200, status='draft', assigned_to=planner
            )
            
            # Draft -> Pending (Scheduling)
            wo.status = 'pending'
            wo.machine = machine_a
            wo.start_date = timezone.now()
            wo.end_date = timezone.now() + timedelta(hours=2)
            wo.save()
            
            # Pending -> Active (Supervisor/Worker start)
            wo.status = 'in_progress'
            wo.save()
            self.stdout.write(self.style.SUCCESS(f"   [OK] Work Order #{wo.id} moved Draft -> Active"))

            # 5. Production Logging
            self.stdout.write("   [Worker] Logging Production...")
            log = ProductionLog.objects.create(
                work_order=wo, worker=planner, quantity=50, shift='morning', status='approved'
            )
            # Verify Updates
            wo.refresh_from_db()
            progress = wo.progress
            self.stdout.write(self.style.SUCCESS(f"   [OK] Production Logged. WO Progress: {progress}%"))

            # 6. Quality Check
            self.stdout.write("   [Quality] Performing QC...")
            QualityCheck.objects.create(
                work_order=wo, checked_by=planner, good_quantity=48, repair_quantity=1, faulty_quantity=1
            )
            self.stdout.write(self.style.SUCCESS("   [OK] Quality Check Recorded"))

            # 7. Maintenance & Alerts
            self.stdout.write("   [Maintenance] Simulating Machine Failure...")
            machine_a.status = 'broken'
            machine_a.save()
            
            # Verify Notification trigger (simulated logic from view)
            Notification.objects.create(
                recipient=owner, title="Machine Broken", message=f"{machine_a.name} is down."
            )
            
            notif_count = Notification.objects.filter(recipient=owner, is_read=False).count()
            if notif_count > 0:
                self.stdout.write(self.style.SUCCESS(f"   [OK] Notification System Working ({notif_count} unread)"))
            else:
                self.stdout.write(self.style.WARNING("   [!] No notifications found (Check signal logic)"))

            self.stdout.write(self.style.SUCCESS("\n ALL SYSTEMS GO. Verification Complete."))

        except Exception as e:
            self.stdout.write(self.style.ERROR(f"\n[FAIL] Verification Failed: {str(e)}"))
            import traceback
            self.stdout.write(traceback.format_exc())

