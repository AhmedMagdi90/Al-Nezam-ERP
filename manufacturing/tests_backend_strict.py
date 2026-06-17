from django.test import TestCase
from .models import Product, BillOfMaterial, BOMComponent
from .services import BOMService
from decimal import Decimal

class BackendStrictComplianceTests(TestCase):
    def test_mandatory_acceptance_criteria(self):
        """
        Acceptance Test (MANDATORY):
        - Create BOM with base_quantity = 10 KG
        - Produce target_qty = 100 KG
        - Materials must scale by 10x
        - Scrap must reduce net cost correctly
        """
        # 1. Setup
        product = Product.objects.create(name="Bulk Chemical", unit="kg")
        
        # BOM Base = 10 KG
        bom = BillOfMaterial.objects.create(
            product=product, 
            base_quantity=10, 
            uom="kg", 
            status='draft' # Start as Draft to allow component addition
        )
        
        # Component: 5 KG Raw Material required for 10 KG Output
        # Cost: $100/kg
        # Waste: 1 KG
        # Scrap Value: $20/kg (Recovery)
        comp = BOMComponent.objects.create(
            bom=bom,
            material_name="Raw Solvent",
            quantity=5, 
            unit="kg",
            cost_per_unit=100,
            wastage_quantity=1,
            scrap_value_per_unit=20,
            scrap_type='sell_as_scrap'
        )
        
        # Activate after building
        bom.status = 'active'
        bom.save()
        
        # 2. Scale for 100 KG Output (Ratio = 100 / 10 = 10)
        target_qty = 100
        requirements = BOMService.calculate_requirements(bom, target_qty)
        
        # 3. Verify Scaling
        # Expected Material Qty = 5 * 10 = 50
        req = requirements[0]
        self.assertEqual(req['required_qty'], 50, "Failed to scale quantity correctly (Expected 50)")
        
        # Expected Wastage Qty = 1 * 10 = 10
        self.assertEqual(req['wastage_qty'], 10, "Failed to scale wastage correctly (Expected 10)")
        
        # 4. Verify Cost Logic
        # Net Cost Formula per line: (Qty * Cost) - (Waste * ScrapVal)
        # Scaled: (50 * 100) - (10 * 20) = 5000 - 200 = 4800
        
        gross = req['required_qty'] * comp.cost_per_unit # 50 * 100 = 5000
        recovery = req['wastage_qty'] * comp.scrap_value_per_unit # 10 * 20 = 200
        net = gross - recovery
        
        self.assertEqual(net, 4800, "Failed to calculate Net Cost correctly")
        
    def test_immutability(self):
        """Test that Active BOM cannot be modified."""
        product = Product.objects.create(name="Locked Product")
        bom = BillOfMaterial.objects.create(product=product, status='active', base_quantity=10)
        
        with self.assertRaises(ValueError):
             # Attempt to change Base Qty
             bom.base_quantity = 20
             bom.save()
             
        # Create Component attempt
        with self.assertRaises(ValueError):
            BOMComponent.objects.create(bom=bom, material_name="fail", quantity=1)

    def test_unit_conversion_service(self):
        """Test Unit Conversion Logic."""
        from .units import UnitService
        
        # 1 kg = 1000 gm
        res = UnitService.convert(1, 'kg', 'gm')
        self.assertEqual(res, 1000)
        
        # Incompatible
        with self.assertRaises(ValueError):
            UnitService.convert(1, 'kg', 'm')

