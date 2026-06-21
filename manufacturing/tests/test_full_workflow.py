import json
from datetime import timedelta

from django.test import TestCase, Client
from django.urls import reverse
from django.utils import timezone

from manufacturing.models import (
    BOMComponent,
    BillOfMaterial,
    Machine,
    MaterialUsage,
    Product,
    ProductionLog,
    QualityCheck,
    ShiftAssignment,
    SystemSettings,
    WorkOrder,
)
from manufacturing.services import ProductionLogService
from manufacturing.tests.utils import create_company, create_user_with_role
from manufacturing.work_order_visibility import get_current_shift_window_for_company

class ManufacturingWorkflowTests(TestCase):
    def setUp(self):
        self.company = create_company()
        self.supervisor = create_user_with_role('supervisor', 'supervisor', self.company)
        self.worker = create_user_with_role('worker', 'worker', self.company)
        self.quality = create_user_with_role('quality', 'quality', self.company)
        self.maintenance = create_user_with_role('maintenance', 'maintenance', self.company)

        # Data
        self.product = Product.objects.create(name="Test Product", company=self.company)
        self.machine = Machine.objects.create(
            name="Test Machine",
            code="M001",
            status="operational",
            company=self.company
        )
        settings, _ = SystemSettings.objects.get_or_create(company=self.company)
        now_local = timezone.localtime()
        settings.shift_mode = "1"
        settings.shift_configuration = {
            "morning": {
                "enabled": True,
                "start": (now_local - timedelta(hours=1)).strftime("%H:%M"),
                "end": (now_local + timedelta(hours=6)).strftime("%H:%M"),
            },
            "afternoon": {"enabled": False, "start": "14:00", "end": "22:00"},
            "night": {"enabled": False, "start": "22:00", "end": "06:00"},
        }
        settings.save(update_fields=["shift_mode", "shift_configuration"])
        shift_window = get_current_shift_window_for_company(self.company)
        for user in (self.supervisor, self.worker):
            ShiftAssignment.objects.create(
                worker=user,
                machine=self.machine,
                shift_type=shift_window["shift_type"],
                date=shift_window["assignment_date"],
            )
        self.wo = WorkOrder.objects.create(
            product_name="Test Product",
            quantity=100,
            status='in_progress', # Already started for Worker test
            machine=self.machine,
            start_date=timezone.now(),
            company=self.company,
            assigned_worker=self.worker,
            assignment_type='manual'
        )
        
        self.client = Client()

    def test_worker_log_production(self):
        """Test Worker logging output."""
        self.client.force_login(self.worker)
        url = reverse('log_production') 
        payload = json.dumps({
            'work_order_id': self.wo.id,
            'quantity': 10,
            'shift': 'morning',
            'note': 'Test output'
        })
        response = self.client.post(url, data=payload, content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['success'])
        
        # Verify Log Created
        log = ProductionLog.objects.last()
        self.assertEqual(log.quantity, 10)
        self.assertEqual(log.work_order, self.wo)
        self.assertEqual(log.status, 'pending')
        
        self.wo.refresh_from_db()
        self.assertEqual(float(self.wo.progress), 0.0)

    def test_worker_log_production_accepts_bom_and_non_bom_materials(self):
        """Workers can log a subset of BOM materials plus ad hoc extra materials."""
        self.client.force_login(self.worker)

        raw_material = Product.objects.create(
            name="Steel Rod",
            company=self.company,
            material_type='raw',
            unit='kg',
        )
        bom = BillOfMaterial.objects.create(
            product=self.product,
            created_by=self.supervisor,
            status='draft',
            base_quantity=100,
            uom='pcs',
        )
        component = BOMComponent.objects.create(
            bom=bom,
            product=raw_material,
            material_name=raw_material.name,
            quantity=25,
            unit='kg',
            cost_per_unit=3,
        )
        self.wo.bom = bom
        self.wo.save(update_fields=['bom'])

        response = self.client.post(
            reverse('log_production'),
            data=json.dumps({
                'work_order_id': self.wo.id,
                'quantity': 10,
                'shift': 'morning',
                'note': 'Stage materials captured',
                'materials': [
                    {
                        'component_id': component.id,
                        'product_id': raw_material.id,
                        'material_name': raw_material.name,
                        'unit': 'kg',
                        'planned_quantity': '2.5',
                        'quantity': '2.25',
                    },
                    {
                        'material_name': 'Coolant',
                        'unit': 'l',
                        'quantity': '0.75',
                    },
                ],
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['success'])

        log = ProductionLog.objects.latest('id')
        logged_materials = list(log.material_usage.order_by('id'))
        self.assertEqual(len(logged_materials), 2)

        bom_usage = logged_materials[0]
        self.assertEqual(bom_usage.product, raw_material)
        self.assertEqual(bom_usage.material_name, 'Steel Rod')
        self.assertEqual(float(bom_usage.planned_quantity), 2.5)
        self.assertEqual(float(bom_usage.quantity_used), 2.25)

        extra_usage = logged_materials[1]
        self.assertIsNone(extra_usage.product)
        self.assertEqual(extra_usage.material_name, 'Coolant')
        self.assertEqual(extra_usage.unit, 'l')
        self.assertEqual(float(extra_usage.quantity_used), 0.75)

        self.assertEqual(MaterialUsage.objects.filter(production_log=log).count(), 2)

    def test_quality_check_submission(self):
        """Test Quality User submitting a check."""
        self.client.force_login(self.quality)
        url = reverse('quality_check')

        ProductionLog.objects.create(
            work_order=self.wo,
            worker=self.worker,
            quantity=90,
            status='approved'
        )
        qc = QualityCheck.objects.create(work_order=self.wo, status='new')
        
        data = {
            'action': 'override_qc',
            'qc_id': qc.id,
            'good_quantity': 90,
            'repair_quantity': 0,
            'faulty_quantity': 0,
            'notes': 'QC Passed'
        }
        
        response = self.client.post(url, data)
        # View redirects on success
        self.assertEqual(response.status_code, 302) 
        
        # Verify QC Record
        qc.refresh_from_db()
        self.assertEqual(qc.good_quantity, 90)
        self.assertEqual(qc.checked_by, self.quality)

    def test_maintenance_status_update(self):
        """Test reporting a fault flips machine status to broken."""
        self.client.force_login(self.supervisor)
        url = reverse('report_fault_api')
        
        payload = json.dumps({
            'work_order_id': self.wo.id,
            'description': 'Sensor Failure',
            'priority': 'urgent'
        })
        response = self.client.post(url, data=payload, content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['success'])
        
        self.machine.refresh_from_db()
        self.assertEqual(self.machine.status, 'broken')
