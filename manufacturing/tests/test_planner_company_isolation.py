import json
from datetime import timedelta

from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from manufacturing.models import (
    BOMOperation,
    BillOfMaterial,
    Machine,
    Product,
    ProductionStage,
    WorkOrder,
)
from manufacturing.tests.utils import create_company, create_user_with_role


class PlannerCompanyIsolationTests(TestCase):
    def setUp(self):
        self.company = create_company("Planner Co")
        self.other_company = create_company("Other Co")
        self.planner = create_user_with_role("planner_company_scope", "planner", self.company)
        self.client = Client()
        self.client.force_login(self.planner)

        self.machine = Machine.objects.create(
            name="Planner Machine",
            code="PLAN-01",
            type="Assembly",
            category="Assembly",
            status="operational",
            company=self.company,
        )
        self.local_stage = ProductionStage.objects.create(
            name="Local Assembly",
            category="Assembly",
            machine=self.machine,
        )
        self.product = Product.objects.create(name="Planner Product", company=self.company)
        self.bom = BillOfMaterial.objects.create(
            product=self.product,
            status="active",
            base_quantity=1,
        )
        BOMOperation.objects.create(
            bom=self.bom,
            stage=self.local_stage,
            machine=self.machine,
            order=1,
            duration_minutes=60,
        )
        self.work_order = WorkOrder.objects.create(
            product_name="Planner Product",
            bom=self.bom,
            quantity=10,
            status="pending",
            company=self.company,
            material_readiness_status="ready",
        )

        self.other_machine = Machine.objects.create(
            name="Other Machine",
            code="OTHER-01",
            type="Cutting",
            category="Cutting",
            status="operational",
            company=self.other_company,
        )
        self.foreign_stage = ProductionStage.objects.create(
            name="Foreign Machine-Less Stage",
            category="Cutting",
        )
        self.other_product = Product.objects.create(name="Other Product", company=self.other_company)
        self.other_bom = BillOfMaterial.objects.create(
            product=self.other_product,
            status="active",
            base_quantity=1,
        )
        BOMOperation.objects.create(
            bom=self.other_bom,
            stage=self.foreign_stage,
            machine=self.other_machine,
            order=1,
            duration_minutes=45,
        )

    def test_schedule_api_rejects_foreign_stage_without_company_machine(self):
        response = self.client.post(
            reverse("api_schedule_work_order", args=[self.work_order.id]),
            data=json.dumps(
                {
                    "stage_id": self.foreign_stage.id,
                    "machine_id": self.machine.id,
                    "start_date": (timezone.now() + timedelta(hours=1)).isoformat(),
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400, response.content.decode())
        payload = response.json()
        self.assertFalse(payload["success"])
        self.assertIn("Invalid stage", payload["error"])

    def test_assign_work_order_rejects_foreign_stage_without_company_machine(self):
        response = self.client.post(
            reverse("assign_work_order"),
            {
                "wo_id": self.work_order.id,
                "machine_id": self.machine.id,
                "stage_id": self.foreign_stage.id,
            },
        )

        self.assertEqual(response.status_code, 200, response.content.decode())
        payload = response.json()
        self.assertFalse(payload["success"])
        self.assertIn("Invalid stage", payload["error"])

    def test_update_work_order_hides_foreign_work_order_existence(self):
        foreign_work_order = WorkOrder.objects.create(
            product_name="Foreign WO",
            bom=self.other_bom,
            quantity=5,
            status="draft",
            company=self.other_company,
        )

        response = self.client.post(
            reverse("update_work_order", args=[foreign_work_order.id]),
            {"status": "pending"},
        )

        self.assertEqual(response.status_code, 200, response.content.decode())
        payload = response.json()
        self.assertFalse(payload["success"])
        self.assertIn("not found", payload["error"].lower())
        self.assertNotIn("different company", payload["error"].lower())

    def test_create_work_order_split_rejects_foreign_stage_without_company_machine(self):
        response = self.client.post(
            reverse("api_create_work_order"),
            data=json.dumps(
                {
                    "bom_id": self.bom.id,
                    "quantity": 10,
                    "split_config": {
                        "is_split": True,
                        "stage_id": self.foreign_stage.id,
                        "splits": [
                            {
                                "machine_id": self.machine.id,
                                "qty": 10,
                            }
                        ],
                    },
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400, response.content.decode())
        payload = response.json()
        self.assertFalse(payload["success"])
        self.assertIn("Invalid split stage", payload["error"])
