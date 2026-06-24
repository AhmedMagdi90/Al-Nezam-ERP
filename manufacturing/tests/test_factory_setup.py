import json

from django.test import Client, TestCase
from django.urls import reverse

from manufacturing.models import BOMOperation, BillOfMaterial, Machine, Product, ProductionStage, WorkOrder
from manufacturing.tests.utils import create_company, create_user_with_role


class FactorySetupWorkOrdersTests(TestCase):
    def setUp(self):
        self.company = create_company("Factory Setup Co")
        self.user = create_user_with_role("factory_setup_planner", "planner", self.company)
        self.client = Client()
        self.client.force_login(self.user)

        self.product = Product.objects.create(name="Factory Product", company=self.company)
        self.bom = BillOfMaterial.objects.create(product=self.product, status="active", base_quantity=1)

    def test_factory_setup_hides_archived_work_orders(self):
        visible_wo = WorkOrder.objects.create(
            product_name="Visible Batch",
            bom=self.bom,
            quantity=10,
            status="pending",
            company=self.company,
        )
        archived_wo = WorkOrder.objects.create(
            product_name="Archived Batch",
            bom=self.bom,
            quantity=5,
            status="archived",
            company=self.company,
        )

        response = self.client.get(reverse("factory_setup"))

        self.assertEqual(response.status_code, 200)
        work_order_ids = [wo["id"] for wo in response.context["work_orders"]]
        self.assertIn(visible_wo.id, work_order_ids)
        self.assertNotIn(archived_wo.id, work_order_ids)
        self.assertNotContains(response, "Archived Batch")

    def test_bulk_archive_removes_work_order_from_factory_setup_list(self):
        work_order = WorkOrder.objects.create(
            product_name="Archive Me",
            bom=self.bom,
            quantity=12,
            status="pending",
            company=self.company,
        )

        archive_response = self.client.post(
            reverse("api_bulk_wo_action"),
            data=json.dumps({"action": "set_archived", "ids": [work_order.id]}),
            content_type="application/json",
        )

        self.assertEqual(archive_response.status_code, 200)
        work_order.refresh_from_db()
        self.assertEqual(work_order.status, "archived")

        response = self.client.get(reverse("factory_setup"))

        self.assertEqual(response.status_code, 200)
        work_order_ids = [wo["id"] for wo in response.context["work_orders"]]
        self.assertNotIn(work_order.id, work_order_ids)
        self.assertNotContains(response, "Archive Me")

    def test_factory_setup_persists_active_tab_state_in_template(self):
        response = self.client.get(reverse("factory_setup"))

        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn("factorySetup.activeSection", content)
        self.assertIn("allowedSections: ['machines', 'stages', 'boms', 'work_orders', 'bulk_upload']", content)
        self.assertIn("setActiveSection('work_orders')", content)
        self.assertIn("url.searchParams.set('tab', normalized)", content)
        self.assertIn('data-factory-setup="true"', content)
        self.assertIn('role="tablist"', content)
        self.assertIn('role="tabpanel"', content)
        self.assertIn('factory-setup-tabs flex gap-6 overflow-x-auto', content)
        self.assertIn('factory-setup-table-wrap overflow-x-auto', content)
        self.assertIn('aria-live="polite"', content)
        self.assertIn('aria-label="Edit BOM', content)

    def test_factory_setup_machine_modal_explains_shift_propagation_impact(self):
        with open("templates/manufacturing/modals.html", encoding="utf-8") as handle:
            modal = handle.read()
        with open("templates/manufacturing/factory_setup.html", encoding="utf-8") as handle:
            setup = handle.read()

        self.assertIn("machineShiftAffectedCount", modal)
        self.assertIn("machineSaveImpactSummary", modal)
        self.assertIn("updateMachineSaveImpactSummary()", modal)
        self.assertIn("Apply working hours to matching department/category machines", modal)
        self.assertIn("This will apply the same working hours to ${matchingCount} matching machine", modal)
        self.assertIn("factorySetup.machineSaveNotice", modal)
        self.assertIn('aria-label="Close machine form"', modal)
        self.assertIn('aria-label="Close stage form"', modal)
        self.assertIn("factoryMachineSaveNotice", setup)
        self.assertIn("localStorage.getItem('factorySetup.machineSaveNotice')", setup)
        self.assertIn("deleteFactoryResource('machine'", setup)
        self.assertIn("deleteFactoryResource('stage'", setup)

    def test_delete_unused_machine(self):
        machine = Machine.objects.create(
            company=self.company,
            name="Unused Machine",
            code="UNUSED-01",
            category="Setup",
            type="Setup",
            status="operational",
        )

        response = self.client.post(reverse("delete_machine", args=[machine.id]))

        self.assertEqual(response.status_code, 200, response.content.decode())
        self.assertTrue(response.json()["success"])
        self.assertFalse(Machine.objects.filter(id=machine.id).exists())

    def test_delete_machine_blocks_when_in_use(self):
        machine = Machine.objects.create(
            company=self.company,
            name="Used Machine",
            code="USED-01",
            category="Setup",
            type="Setup",
            status="operational",
        )
        WorkOrder.objects.create(
            product_name="Machine Job",
            bom=self.bom,
            quantity=1,
            status="pending",
            company=self.company,
            machine=machine,
        )

        response = self.client.post(reverse("delete_machine", args=[machine.id]))

        self.assertEqual(response.status_code, 409, response.content.decode())
        self.assertFalse(response.json()["success"])
        self.assertIn("work orders", response.json()["error"])
        self.assertTrue(Machine.objects.filter(id=machine.id).exists())

    def test_delete_unused_stage(self):
        machine = Machine.objects.create(
            company=self.company,
            name="Stage Machine",
            code="STAGE-01",
            category="Setup",
            type="Setup",
            status="operational",
        )
        stage = ProductionStage.objects.create(
            name="Temporary Stage",
            category="Setup",
            machine=machine,
            order=99,
        )

        response = self.client.post(reverse("delete_stage", args=[stage.id]))

        self.assertEqual(response.status_code, 200, response.content.decode())
        self.assertTrue(response.json()["success"])
        self.assertFalse(ProductionStage.objects.filter(id=stage.id).exists())

    def test_delete_stage_blocks_when_in_use(self):
        machine = Machine.objects.create(
            company=self.company,
            name="Route Machine",
            code="ROUTE-01",
            category="Route",
            type="Route",
            status="operational",
        )
        stage = ProductionStage.objects.create(
            name="Route Stage",
            category="Route",
            machine=machine,
            order=1,
        )
        BOMOperation.objects.create(bom=self.bom, stage=stage, order=1, duration_minutes=30)

        response = self.client.post(reverse("delete_stage", args=[stage.id]))

        self.assertEqual(response.status_code, 409, response.content.decode())
        self.assertFalse(response.json()["success"])
        self.assertIn("BOM operations", response.json()["error"])
        self.assertTrue(ProductionStage.objects.filter(id=stage.id).exists())

    def test_delete_stage_rejects_cross_company_stage(self):
        other_company = create_company("Other Factory Setup Co")
        other_machine = Machine.objects.create(
            company=other_company,
            name="Other Machine",
            code="OTHER-01",
            category="Other",
            type="Other",
            status="operational",
        )
        other_stage = ProductionStage.objects.create(
            name="Other Stage",
            category="Other",
            machine=other_machine,
            order=1,
        )

        response = self.client.post(reverse("delete_stage", args=[other_stage.id]))

        self.assertEqual(response.status_code, 404, response.content.decode())
        self.assertTrue(ProductionStage.objects.filter(id=other_stage.id).exists())

    def test_create_stage_order_considers_stages_owned_through_bom_operations(self):
        machine = Machine.objects.create(
            company=self.company,
            name="CNC Machine",
            code="CNC-01",
            category="CNC",
            type="CNC",
            status="operational",
        )
        first_stage = ProductionStage.objects.create(name="Cutting", category="CNC", order=1)
        second_stage = ProductionStage.objects.create(name="Assembly", category="Assembly", order=2)
        BOMOperation.objects.create(bom=self.bom, stage=first_stage, order=1, duration_minutes=30)
        BOMOperation.objects.create(bom=self.bom, stage=second_stage, order=2, duration_minutes=20)

        response = self.client.post(
            reverse("create_stage"),
            data={"name": "Painting", "category": "CNC"},
        )

        self.assertEqual(response.status_code, 200, response.content.decode())
        created_stage = ProductionStage.objects.get(name="Painting")
        self.assertEqual(created_stage.machine, machine)
        self.assertEqual(created_stage.order, 3)

    def test_create_stage_after_bom_created_stages_uses_next_route_order(self):
        machine = Machine.objects.create(
            company=self.company,
            name="Assembly Machine",
            code="ASM-01",
            category="Assembly",
            type="Assembly",
            status="operational",
        )

        save_response = self.client.post(
            "/manufacturing/api/v1/boms/create_full_bom/",
            data=json.dumps(
                {
                    "product": "Factory Routed Product",
                    "batch": 10,
                    "materials": [],
                    "operations": [
                        {
                            "id": 1,
                            "machine_id": machine.id,
                            "stage_name": "Cutting",
                            "name": "Op 10: Cutting",
                            "type": "Assembly",
                            "setup_time": 15,
                            "run_time": 1,
                        },
                        {
                            "id": 2,
                            "machine_id": machine.id,
                            "stage_name": "Assembly",
                            "name": "Op 20: Assembly",
                            "type": "Assembly",
                            "setup_time": 15,
                            "run_time": 1,
                        },
                    ],
                    "qualityChecks": [],
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(save_response.status_code, 200, save_response.content.decode())

        response = self.client.post(
            reverse("create_stage"),
            data={"name": "Packing", "category": "Assembly"},
        )

        self.assertEqual(response.status_code, 200, response.content.decode())
        self.assertEqual(ProductionStage.objects.get(name="Cutting").order, 1)
        self.assertEqual(ProductionStage.objects.get(name="Assembly").order, 2)
        self.assertEqual(ProductionStage.objects.get(name="Packing").order, 3)
