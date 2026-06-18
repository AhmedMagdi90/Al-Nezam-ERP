from django.test import TestCase
from django.contrib.auth.models import User
from .models import Product, BillOfMaterial, BOMComponent
from .services import BOMService
from decimal import Decimal

class EnterpriseBOMTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='bom_architect')
        
        # Products
        self.p_finished = Product.objects.create(name="Finished Engine", material_type="finished")
        self.p_sub = Product.objects.create(name="Piston Sub-Assembly", material_type="semi")
        self.p_raw = Product.objects.create(name="Steel Rod", material_type="raw")

    def test_recursive_costing(self):
        """test that cost rolls up from sub-assembly."""
        # 1. Create Sub-Assembly BOM
        bom_sub = BillOfMaterial.objects.create(product=self.p_sub, status='draft', base_quantity=1)
        # Sub BOM: 2 Rods @ $10 each = $20
        BOMComponent.objects.create(
            bom=bom_sub, material_name="Steel Rod", quantity=2, cost_per_unit=10, unit='pcs'
        )
        bom_sub.status = 'active'
        bom_sub.save()
        
        # 2. Create Finished BOM
        bom_main = BillOfMaterial.objects.create(product=self.p_finished, status='draft', base_quantity=1)
        # Main BOM: 4 Pistons (Sub-Assembly) + $50 Labor (dummy material)
        # We link the component to the sub_bom
        comp_sub = BOMComponent.objects.create(
            bom=bom_main, material_name="Piston", quantity=4, 
            unit='pcs', cost_per_unit=0, # Cost should come from sub-bom
            sub_bom=bom_sub # Linking to BOM directly
        )
        
        # Calculate Cost
        # Sub Cost = 2 * 10 = 20
        # Main Cost = (4 * 20) = 80
        total_cost = BOMService.calculate_cost(bom_main)
        self.assertEqual(total_cost, 80)

    def test_infinite_loop_detection(self):
        """Test that self-referencing BOMs raise RecursionError."""
        # BOM A -> Component B (linked to BOM B)
        # BOM B -> Component A (linked to BOM A)
        
        bom_a = BillOfMaterial.objects.create(version="A")
        bom_b = BillOfMaterial.objects.create(version="B")
        
        # A depends on B
        BOMComponent.objects.create(bom=bom_a, material_name="Comp B", quantity=1, sub_bom=bom_b)
        
        # B depends on A
        BOMComponent.objects.create(bom=bom_b, material_name="Comp A", quantity=1, sub_bom=bom_a)
        
        with self.assertRaises(RecursionError):
            BOMService.calculate_cost(bom_a)

    def test_scrap_strategy_sell(self):
        """Test 'sell_as_scrap' logic."""
        bom = BillOfMaterial.objects.create(status='draft')
        # Cost: 10 * 10 = 100
        # Waste: 2 * 5 = 10 (Recovered)
        # Net: 90
        BOMComponent.objects.create(
            bom=bom, material_name="Metal", quantity=10, cost_per_unit=10,
            wastage_quantity=2, scrap_value_per_unit=5, scrap_type='sell_as_scrap'
        )
        # create a dummy wrapper to use BOMService or just call total_cost direct
        # BOMService uses total_cost() which we updated in models
        # Let's verify the model method first
        comp = bom.components.first()
        self.assertEqual(comp.total_cost(), 90)

    def test_scrap_strategy_irretrievable(self):
        """Test 'irretrievable' logic (Cost absorbed)."""
        bom = BillOfMaterial.objects.create(status='draft')
        # Cost: 10 * 10 = 100
        # Waste: 2 * 5 = 10 (Ignored/Absorbed)
        # Net: 100
        BOMComponent.objects.create(
            bom=bom, material_name="Chemical", quantity=10, cost_per_unit=10,
            wastage_quantity=2, scrap_value_per_unit=5, scrap_type='irretrievable'
        )
        comp = bom.components.first()
        self.assertEqual(comp.total_cost(), 100)
