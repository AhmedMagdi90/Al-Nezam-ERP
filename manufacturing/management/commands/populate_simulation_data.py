from django.core.management.base import BaseCommand
from django.contrib.auth.models import User
from django.utils import timezone
from datetime import timedelta
from accounts.models import Profile, Role
from manufacturing.models import (
    Machine, Product, BillOfMaterial, BOMComponent, BOMOperation, 
    ProductionStage, WorkOrder, MachineFault
)
import random

class Command(BaseCommand):
    help = 'Populates the database with realistic verification data for the Manufacturing ERP.'

    def handle(self, *args, **kwargs):
        self.stdout.write(self.style.WARNING("Starting Data Simulation..."))

        # ---------------------------------------------------------
        # 1. Users & Roles
        # ---------------------------------------------------------
        roles_map = {
            'planner': 'Planner',
            'supervisor': 'Supervisor',
            'worker': 'Worker',
            'quality': 'Quality Officer',
            'maintenance': 'Maintenance'
        }
        
        # Ensure Roles Exist
        db_roles = {}
        for code, name in roles_map.items():
            role, _ = Role.objects.get_or_create(name=code)
            db_roles[code] = role

        # Create Users
        users = {}
        for username, role_name in roles_map.items(): # username=role_code (lowercase)
            user, created = User.objects.get_or_create(username=username)
            if created:
                user.set_password("kemet123")
                user.save()
            
            # Link Profile
            profile, _ = Profile.objects.get_or_create(user=user)
            profile.role = db_roles[username] # Match the key used in db_roles (e.g., 'planner')
            profile.save()
            users[username] = user
            
        self.stdout.write(f"[OK] Verified {len(users)} Users.")

        # ---------------------------------------------------------
        # 2. Machines & Stages
        # ---------------------------------------------------------
        machines_data = [
            ("Cutting Machine A", "CUT-01", "operational"),
            ("Sewing Station 1", "SEW-01", "operational"),
            ("Sewing Station 2", "SEW-02", "operational"), # Operational but maybe idle
            ("Packaging Unit", "PAK-01", "operational"),
        ]
        
        db_machines = {}
        for name, code, status in machines_data:
            m, _ = Machine.objects.get_or_create(code=code, defaults={'name': name})
            m.status = status
            m.save()
            db_machines[code] = m
            
            # Create a default Stage for each machine
            ProductionStage.objects.get_or_create(
                name=f"{name.split()[0]} Stage",
                machine=m,
                defaults={'order': 1}
            )

        self.stdout.write(f"[OK] Verified {len(db_machines)} Machines.")

        # ---------------------------------------------------------
        # 3. Product & BOM (The "T-Shirt" Lifecycle)
        # ---------------------------------------------------------
        product, _ = Product.objects.get_or_create(name="Cotton T-Shirt (L)")
        
        # Create BOM
        bom, created = BillOfMaterial.objects.get_or_create(
            product=product,
            version="v1.0",
            defaults={
                'status': 'active',
                'base_quantity': 1,
                'created_by': users['planner']
            }
        )
        
        if created or not bom.operations.exists():
            # Temporarily set to draft to allow edits
            bom.status = 'draft'
            bom.save()
            
            # Add Components
            BOMComponent.objects.create(bom=bom, material_name="Cotton Fabric", quantity=1.5, unit="meters", cost_per_unit=5.00)
            BOMComponent.objects.create(bom=bom, material_name="Thread", quantity=10, unit="meters", cost_per_unit=0.10)
            
            # Add Operations (Routing)
            # Op 1: Cutting
            BOMOperation.objects.create(
                bom=bom, 
                order=1, 
                machine=db_machines['CUT-01'], 
                duration_minutes=15,
                stage=ProductionStage.objects.get(machine=db_machines['CUT-01'])
            )
            # Op 2: Sewing
            BOMOperation.objects.create(
                bom=bom, 
                order=2, 
                machine=db_machines['SEW-01'], 
                duration_minutes=30,
                stage=ProductionStage.objects.get(machine=db_machines['SEW-01'])
            )
            # Op 3: Packing
            BOMOperation.objects.create(
                bom=bom, 
                order=3, 
                machine=db_machines['PAK-01'], 
                duration_minutes=10,
                stage=ProductionStage.objects.get(machine=db_machines['PAK-01'])
            )
            
            # Restore to Active
            bom.status = 'active'
            bom.save()
            
        self.stdout.write(f"[OK] Verified BOM for {product.name}.")

        # ---------------------------------------------------------
        # 4. Work Orders (The Flow)
        # ---------------------------------------------------------
        # Clear existing pending/in_progress WOs to avoid clutter? 
        # No, let's just add new ones if count is low.
        
        if WorkOrder.objects.filter(status='pending').count() < 3:
            # Create a Batch that explodes into sub-tasks
            # 1. Parent Order
            today = timezone.now()
            parent = WorkOrder.objects.create(
                product_name=f"Order #{random.randint(1000, 9999)} - T-Shirts",
                quantity=100,
                bom=bom,
                status='in_progress',
                start_date=today,
                assigned_to=users['planner']
            )
            
            # 2. Sub-Tasks (Backlog for Machines)
            # Task 1: Cutting (Pending on CUT-01)
            WorkOrder.objects.create(
                parent=parent,
                product_name="T-Shirt - Cutting Phase",
                quantity=100,
                machine=db_machines['CUT-01'],
                status='pending',
                priority='Urgent',
                start_date=today,
                end_date=today + timedelta(minutes=60)
            )
            
            # Task 2: Sewing (Pending on SEW-01 - wait for cutting)
            WorkOrder.objects.create(
                parent=parent,
                product_name="T-Shirt - Sewing Phase",
                quantity=100,
                machine=db_machines['SEW-01'],
                status='pending',
                priority='High',
                start_date=today + timedelta(minutes=60),
                end_date=today + timedelta(minutes=180)
            )
            
            self.stdout.write("[OK] Created Work Order Batch with Sub-Tasks.")

        # Ensure at least one Running Order (Simulation)
        running_wo = WorkOrder.objects.filter(status='in_progress', machine=db_machines['PAK-01']).first()
        if not running_wo:
            WorkOrder.objects.create(
                product_name="T-Shirt - Packaging Phase",
                quantity=500,
                machine=db_machines['PAK-01'],
                status='in_progress',
                progress=45, # 45% done
                start_date=timezone.now(),
                assigned_to=users['supervisor']
            )
            db_machines['PAK-01'].status = 'operational' # Should be running but model choices are limited?
            # actually logic says 'running' is valid for UI even if model choices differ?
            # Model choices: operational, maintenance, offline.
            # Views sets it to 'running' string? Let's check model. 
            # Model says: operational, maintenance, offline. 
            # View Logic: `if machine.status == 'idle': machine.status = 'running'` 
            # Wait, 'running' is NOT in choices? 
            # Let's trust valid choices: 'operational' implies running.
            db_machines['PAK-01'].save() 
            self.stdout.write("[OK] Ensured Active Packaging Order.")

        # ---------------------------------------------------------
        # 5. Alerts
        # ---------------------------------------------------------
        if MachineFault.objects.filter(status='open').count() == 0:
            MachineFault.objects.create(
                machine=db_machines['SEW-02'],
                reported_by=users['worker'],
                description="Needle alignment off",
                status='open'
            )
            db_machines['SEW-02'].status = 'maintenance'
            db_machines['SEW-02'].maintenance_note = "Needle alignment off"
            db_machines['SEW-02'].save()
            self.stdout.write("[OK] Created Machine Fault (Alert).")

        self.stdout.write(self.style.SUCCESS("[SUCCESS] Simulation Data Ready! Log in as 'supervisor' / 'kemet123'"))
