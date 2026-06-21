from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from manufacturing.models import BOMOperation, BillOfMaterial, Machine, Product, ProductionStage, ShiftAssignment, WorkOrder
from manufacturing.tests.utils import create_company, create_user_with_role
from manufacturing.work_order_visibility import get_current_shift_window_for_company


class MachineDisplayLabelTests(TestCase):
    def setUp(self):
        self.company = create_company("Machine Label Co")
        self.planner = create_user_with_role("machine_label_planner", "planner", self.company)
        self.supervisor = create_user_with_role("machine_label_supervisor", "supervisor", self.company)
        self.supervisor.profile.worker_mode_enabled = True
        self.supervisor.profile.save(update_fields=["worker_mode_enabled"])

        self.machine = Machine.objects.create(
            company=self.company,
            name="machine",
            code="M1",
            category="Mix",
            type="Mix",
            status="operational",
            is_active=True,
        )
        self.stage = ProductionStage.objects.create(
            name="Op 10: Mix",
            order=10,
            machine=self.machine,
            category="Mix",
        )
        self.product = Product.objects.create(name="Machine Label Product", company=self.company)
        self.bom = BillOfMaterial.objects.create(product=self.product, status="active", base_quantity=1)
        BOMOperation.objects.create(
            bom=self.bom,
            stage=self.stage,
            machine=self.machine,
            order=10,
            duration_minutes=30,
        )
        self.work_order = WorkOrder.objects.create(
            product_name="Machine Label Batch",
            bom=self.bom,
            quantity=25,
            status="pending",
            company=self.company,
            machine=self.machine,
            stage=self.stage,
            current_stage=self.stage,
            assigned_worker=self.supervisor,
            start_date=timezone.now(),
        )
        shift_window = get_current_shift_window_for_company(self.company)
        ShiftAssignment.objects.create(
            worker=self.supervisor,
            machine=self.machine,
            shift_type=shift_window["shift_type"],
            date=shift_window["assignment_date"],
            created_by=self.planner,
        )

    def test_machine_display_label_combines_code_and_name(self):
        self.assertEqual(self.machine.display_label, "M1 - machine")

    def test_work_order_detail_api_returns_machine_display_name(self):
        client = Client()
        client.force_login(self.planner)

        response = client.get(reverse("api_workorder_detail", args=[self.work_order.id]))

        self.assertEqual(response.status_code, 200, response.content.decode())
        payload = response.json()
        self.assertTrue(payload["success"])
        self.assertEqual(payload["all_machines"][0]["display_name"], "M1 - machine")

    def test_legacy_work_order_details_api_returns_machine_display_name(self):
        client = Client()
        client.force_login(self.planner)

        response = client.get(reverse("get_work_order", args=[self.work_order.id]))

        self.assertEqual(response.status_code, 200, response.content.decode())
        payload = response.json()
        self.assertTrue(payload["success"])
        self.assertEqual(payload["machines"][0]["display_name"], "M1 - machine")
        self.assertEqual(payload["machines"][0]["code"], "M1")

    def test_bom_json_api_returns_machine_display_name(self):
        client = Client()
        client.force_login(self.planner)

        response = client.get(reverse("get_bom_json", args=[self.bom.id]))

        self.assertEqual(response.status_code, 200, response.content.decode())
        payload = response.json()
        self.assertTrue(payload["success"])
        self.assertEqual(payload["bom"]["all_machines"][0]["display_name"], "M1 - machine")
        self.assertEqual(payload["bom"]["all_machines"][0]["code"], "M1")

    def test_shop_floor_and_factory_setup_render_machine_display_label(self):
        supervisor_client = Client()
        supervisor_client.force_login(self.supervisor)
        shop_floor_response = supervisor_client.get(reverse("shop_floor"))

        self.assertEqual(shop_floor_response.status_code, 200)
        self.assertContains(shop_floor_response, "M1 - machine")

        planner_client = Client()
        planner_client.force_login(self.planner)
        factory_setup_response = planner_client.get(reverse("factory_setup"))

        self.assertEqual(factory_setup_response.status_code, 200)
        self.assertContains(factory_setup_response, "M1 - machine")

    def test_planner_dashboard_payload_and_helpers_prefer_display_label(self):
        client = Client()
        client.force_login(self.planner)

        planner_response = client.get(reverse("planner_dashboard"))

        self.assertEqual(planner_response.status_code, 200)
        self.assertContains(planner_response, '"display_name": "M1 - machine"', html=False)
        self.assertContains(planner_response, "const explicitDisplay = String(machine?.display_name || '').trim();")

        drawer_response = client.get(reverse("planner_dashboard"))
        self.assertContains(drawer_response, "const explicitDisplay = String(machine?.display_name || '').trim();")
