import json
import tempfile
from decimal import Decimal

from django.core.files.base import ContentFile
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from manufacturing.models import BillOfMaterial, BOMComponent, Machine, Notification, ProductionLog, ProductionStage, Product, WorkOrder, WorkOrderStage
from manufacturing.services import DashboardService
from manufacturing.tests.utils import create_company, create_user_with_role


class BOMSaveAPITests(TestCase):
    def setUp(self):
        self.company = create_company("BOM Save Co")
        self.user = create_user_with_role("bom_planner", "planner", self.company)
        self.client = Client()
        self.client.force_login(self.user)

    def test_create_full_bom_tolerates_duplicate_stage_names(self):
        machine = Machine.objects.create(
            company=self.company,
            name="Assembly Machine 01",
            code="ASM-01",
            category="Assembly",
            type="Assembly",
            status="operational",
        )
        ProductionStage.objects.create(name="Assembly", category="Assembly")
        ProductionStage.objects.create(name="Assembly", category="Assembly", machine=machine)

        response = self.client.post(
            "/manufacturing/api/v1/boms/create_full_bom/",
            data=json.dumps(
                {
                    "product": "big_ben",
                    "batch": 10,
                    "materials": [],
                    "operations": [
                        {
                            "id": 1,
                            "machine_id": None,
                            "stage_id": None,
                            "stage_name": "Assembly",
                            "name": "Op 20: Assembly",
                            "type": "Assembly",
                            "setup_time": 15,
                            "run_time": 1,
                            "quality_check": False,
                        }
                    ],
                    "qualityChecks": [],
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200, response.content.decode())
        payload = response.json()
        self.assertTrue(payload["success"])

        bom = BillOfMaterial.objects.get(id=payload["bom_id"])
        self.assertEqual(bom.product.name, "big_ben")
        self.assertEqual(bom.operations.count(), 1)
        self.assertEqual(bom.operations.first().stage.name, "Assembly")

    def test_create_full_bom_keeps_stage_only_operation_machine_optional(self):
        machine = Machine.objects.create(
            company=self.company,
            name="Paint Booth 01",
            code="PAINT-01",
            category="Painting",
            type="Painting",
            status="operational",
        )
        stage = ProductionStage.objects.create(
            name="Painting",
            category="Painting",
            machine=machine,
            order=10,
        )

        response = self.client.post(
            "/manufacturing/api/v1/boms/create_full_bom/",
            data=json.dumps(
                {
                    "product": "stage_only_product",
                    "batch": 10,
                    "materials": [],
                    "operations": [
                        {
                            "stage_id": stage.id,
                            "stage_name": "Painting",
                            "machine_id": None,
                            "type": "Painting",
                            "setup_time": 15,
                            "run_time": 1,
                        }
                    ],
                    "qualityChecks": [],
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200, response.content.decode())
        bom = BillOfMaterial.objects.get(id=response.json()["bom_id"])
        operation = bom.operations.get()
        self.assertEqual(operation.stage, stage)
        self.assertIsNone(operation.machine_id)
        self.assertEqual(operation.machine_type, "Painting")

    def test_create_full_bom_normalizes_operation_time_units_to_minutes(self):
        machine = Machine.objects.create(
            company=self.company,
            name="Packing Machine",
            code="PACK-01",
            category="Packing",
            type="Packing",
            status="operational",
        )

        response = self.client.post(
            "/manufacturing/api/v1/boms/create_full_bom/",
            data=json.dumps(
                {
                    "product": "unit_timed_product",
                    "batch": 10,
                    "materials": [],
                    "operations": [
                        {
                            "machine_id": machine.id,
                            "stage_name": "Packing",
                            "type": "Packing",
                            "setup_time": "2",
                            "setup_time_unit": "hr",
                            "run_time": "30",
                            "run_time_unit": "sec",
                        }
                    ],
                    "qualityChecks": [],
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200, response.content.decode())
        bom = BillOfMaterial.objects.get(id=response.json()["bom_id"])
        operation = bom.operations.get()
        self.assertEqual(operation.setup_time, Decimal("120.0000"))
        self.assertEqual(operation.run_time, Decimal("0.5000"))

    def test_create_full_bom_does_not_reuse_foreign_stage_with_same_name(self):
        other_company = create_company("Other BOM Co")
        other_machine = Machine.objects.create(
            company=other_company,
            name="Other Cutting Machine",
            code="OTHER-CUT-01",
            category="Cutting",
            type="Cutting",
            status="operational",
        )
        other_product = Product.objects.create(company=other_company, name="other_product")
        other_bom = BillOfMaterial.objects.create(product=other_product, status="active", base_quantity=1)
        foreign_stage = ProductionStage.objects.create(name="Shared Cutting", category="Cutting")
        from manufacturing.models import BOMOperation

        BOMOperation.objects.create(
            bom=other_bom,
            stage=foreign_stage,
            machine=other_machine,
            order=1,
            duration_minutes=30,
        )

        local_machine = Machine.objects.create(
            company=self.company,
            name="Local Cutting Machine",
            code="LOCAL-CUT-01",
            category="Cutting",
            type="Cutting",
            status="operational",
        )
        response = self.client.post(
            "/manufacturing/api/v1/boms/create_full_bom/",
            data=json.dumps(
                {
                    "product": "local_cut_product",
                    "batch": 10,
                    "materials": [],
                    "operations": [
                        {
                            "id": 1,
                            "machine_id": local_machine.id,
                            "stage_name": "Shared Cutting",
                            "name": "Op 10: Shared Cutting",
                            "type": "Cutting",
                            "setup_time": 15,
                            "run_time": 1,
                        }
                    ],
                    "qualityChecks": [],
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200, response.content.decode())
        bom = BillOfMaterial.objects.get(id=response.json()["bom_id"])
        local_stage = bom.operations.get().stage
        self.assertNotEqual(local_stage.id, foreign_stage.id)
        self.assertEqual(local_stage.machine, local_machine)
        self.assertEqual(local_stage.order, 1)

    def test_create_full_bom_handles_legacy_non_numeric_versions(self):
        product = Product.objects.create(company=self.company, name="legacy_bom_product")
        BillOfMaterial.objects.create(
            product=product,
            version="legacy-copy",
            status="active",
            base_quantity=1,
            created_by=self.user,
        )

        response = self.client.post(
            "/manufacturing/api/v1/boms/create_full_bom/",
            data=json.dumps(
                {
                    "product": "legacy_bom_product",
                    "batch": 5,
                    "materials": [],
                    "operations": [],
                    "qualityChecks": [],
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200, response.content.decode())
        payload = response.json()
        self.assertTrue(payload["success"])

        bom = BillOfMaterial.objects.get(id=payload["bom_id"])
        self.assertEqual(bom.status, "draft")
        self.assertEqual(bom.version, "v2.0")

    def test_create_full_bom_persists_operation_material_mapping(self):
        cnc_machine = Machine.objects.create(
            company=self.company,
            name="CNC Machine 01",
            code="CNC-01",
            category="CNC",
            type="CNC",
            status="operational",
        )
        assembly_machine = Machine.objects.create(
            company=self.company,
            name="Assembly Machine 02",
            code="ASM-02",
            category="Assembly",
            type="Assembly",
            status="operational",
        )
        cnc_stage = ProductionStage.objects.create(
            name="Op 10: CNC",
            category="CNC",
            machine=cnc_machine,
            order=10,
        )
        assembly_stage = ProductionStage.objects.create(
            name="Op 20: Assembly",
            category="Assembly",
            machine=assembly_machine,
            order=20,
        )

        response = self.client.post(
            "/manufacturing/api/v1/boms/create_full_bom/",
            data=json.dumps(
                {
                    "product": "route_product",
                    "batch": 10,
                    "materials": [
                        {"client_id": "cmp-1", "name": "Steel Rod", "qty": 10, "unit": "pcs"},
                        {"client_id": "cmp-2", "name": "Assembly Glue", "qty": 2, "unit": "kg"},
                    ],
                    "operations": [
                        {
                            "id": 1,
                            "machine_id": cnc_machine.id,
                            "stage_id": cnc_stage.id,
                            "stage_name": "Op 10: CNC",
                            "name": "Op 10: CNC",
                            "type": "CNC",
                            "setup_time": 15,
                            "run_time": 1,
                            "quality_check": False,
                            "material_client_ids": ["cmp-1"],
                        },
                        {
                            "id": 2,
                            "machine_id": assembly_machine.id,
                            "stage_id": assembly_stage.id,
                            "stage_name": "Op 20: Assembly",
                            "name": "Op 20: Assembly",
                            "type": "Assembly",
                            "setup_time": 10,
                            "run_time": 1,
                            "quality_check": False,
                            "material_client_ids": ["cmp-2"],
                        },
                    ],
                    "qualityChecks": [],
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200, response.content.decode())
        payload = response.json()
        self.assertTrue(payload["success"])

        bom = BillOfMaterial.objects.get(id=payload["bom_id"])
        self.assertEqual(bom.operations.count(), 2)
        cnc_op = bom.operations.get(stage=cnc_stage)
        assembly_op = bom.operations.get(stage=assembly_stage)
        self.assertEqual(
            list(cnc_op.material_links.values_list("component__material_name", flat=True)),
            ["Steel Rod"],
        )
        self.assertEqual(
            list(assembly_op.material_links.values_list("component__material_name", flat=True)),
            ["Assembly Glue"],
        )

    def test_create_full_bom_persists_batch_type(self):
        response = self.client.post(
            "/manufacturing/api/v1/boms/create_full_bom/",
            data=json.dumps(
                {
                    "product": "liquid_mix",
                    "batch": 25,
                    "batch_type": "l",
                    "materials": [],
                    "operations": [],
                    "qualityChecks": [],
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200, response.content.decode())
        payload = response.json()
        self.assertTrue(payload["success"])

        bom = BillOfMaterial.objects.get(id=payload["bom_id"])
        self.assertEqual(float(bom.base_quantity), 25.0)
        self.assertEqual(bom.uom, "l")

    def test_bom_json_exposes_batch_type(self):
        product = Product.objects.create(company=self.company, name="json_ready_product")
        bom = BillOfMaterial.objects.create(
            product=product,
            version="v1.0",
            status="draft",
            base_quantity=12,
            uom="kg",
            created_by=self.user,
        )

        response = self.client.get(f"/manufacturing/api/bom/{bom.id}/json/")

        self.assertEqual(response.status_code, 200, response.content.decode())
        payload = response.json()
        self.assertTrue(payload["success"])
        self.assertEqual(payload["bom"]["uom"], "kg")
        self.assertEqual(payload["bom"]["base_quantity"], 12.0)

    def test_legacy_bom_save_api_persists_batch_type(self):
        response = self.client.post(
            "/manufacturing/api/bom/save/",
            data=json.dumps(
                {
                    "product_name": "legacy_batch_type_product",
                    "base_qty": 8,
                    "uom": "kg",
                    "status": "draft",
                    "components": [
                        {
                            "name": "Steel Powder",
                            "qty": 8,
                            "unit": "kg",
                            "cost": 0,
                            "scrap_qty": 0,
                            "scrap_price": 0,
                            "scrap_type": "sell_as_scrap",
                        }
                    ],
                    "criteria": [],
                    "operations": [],
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200, response.content.decode())
        payload = response.json()
        self.assertEqual(payload["status"], "success")

        bom = BillOfMaterial.objects.get(id=payload["bom_id"])
        self.assertEqual(float(bom.base_quantity), 8.0)
        self.assertEqual(bom.uom, "kg")

    def test_legacy_bom_save_api_accepts_attachment_and_base_quantity_alias(self):
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            response = self.client.post(
                "/manufacturing/api/bom/save/",
                data=json.dumps(
                    {
                        "product_name": "attached_bom_product",
                        "base_quantity": 12,
                        "uom": "pcs",
                        "status": "draft",
                        "components": [
                            {
                                "name": "Steel Plate",
                                "qty": 2,
                                "unit": "pcs",
                                "cost": 0,
                                "scrap_qty": 0,
                                "scrap_price": 0,
                            }
                        ],
                        "criteria": [],
                        "operations": [],
                        "attachment_name": "build-guide.pdf",
                        "attachment_data": "data:application/pdf;base64,JVBERi0xLjQK",
                    }
                ),
                content_type="application/json",
            )

            self.assertEqual(response.status_code, 200, response.content.decode())
            bom = BillOfMaterial.objects.get(id=response.json()["bom_id"])
            self.assertEqual(float(bom.base_quantity), 12.0)
            self.assertEqual(bom.attachment_name, "build-guide.pdf")
            self.assertTrue(bom.attachment.name.endswith(".pdf"))

            detail_response = self.client.get(f"/manufacturing/api/bom/{bom.id}/json/")
            attachment = detail_response.json()["bom"]["attachment"]
            self.assertEqual(attachment["name"], "build-guide.pdf")
            self.assertTrue(attachment["is_pdf"])

            html_response = self.client.get(f"/manufacturing/bom/{bom.id}/details/")
            self.assertEqual(html_response.status_code, 200, html_response.content.decode())
            self.assertIn("build-guide.pdf", html_response.json()["html"])

    def test_legacy_bom_save_api_rejects_invalid_attachment_type(self):
        response = self.client.post(
            "/manufacturing/api/bom/save/",
            data=json.dumps(
                {
                    "product_name": "bad_attachment_product",
                    "base_qty": 1,
                    "uom": "pcs",
                    "status": "draft",
                    "components": [
                        {
                            "name": "Steel Plate",
                            "qty": 1,
                            "unit": "pcs",
                            "cost": 0,
                            "scrap_qty": 0,
                            "scrap_price": 0,
                        }
                    ],
                    "criteria": [],
                    "operations": [],
                    "attachment_name": "macro.exe",
                    "attachment_data": "data:application/octet-stream;base64,AA==",
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400, response.content.decode())
        self.assertIn("JPG, PNG, or PDF", response.json()["message"])

    def test_full_bom_save_api_accepts_attachment(self):
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            response = self.client.post(
                "/manufacturing/api/v1/boms/create_full_bom/",
                data=json.dumps(
                    {
                        "product": "full_builder_attachment_product",
                        "batch": 10,
                        "batch_type": "pcs",
                        "materials": [
                            {"client_id": "cmp-1", "name": "Steel Rod", "qty": 10, "unit": "pcs"}
                        ],
                        "operations": [],
                        "qualityChecks": [],
                        "attachment_name": "photo.png",
                        "attachment_data": "data:image/png;base64,iVBORw0KGgo=",
                    }
                ),
                content_type="application/json",
            )

            self.assertEqual(response.status_code, 200, response.content.decode())
            bom = BillOfMaterial.objects.get(id=response.json()["bom_id"])
            self.assertEqual(bom.attachment_name, "photo.png")
            self.assertTrue(bom.attachment.name.endswith(".png"))

    def test_work_order_detail_exposes_bom_attachment(self):
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            product = Product.objects.create(company=self.company, name="wo_attachment_product")
            bom = BillOfMaterial.objects.create(
                product=product,
                version="v1.0",
                status="active",
                base_quantity=1,
                created_by=self.user,
            )
            bom.attachment.save("operator-guide.pdf", ContentFile(b"%PDF-1.4\n"), save=False)
            bom.attachment_name = "operator-guide.pdf"
            bom.save(update_fields=["attachment", "attachment_name"])
            work_order = WorkOrder.objects.create(
                company=self.company,
                product_name=product.name,
                bom=bom,
                quantity=10,
                status="pending",
            )

            schedule_response = self.client.get(f"/manufacturing/api/workorder/{work_order.id}/json/")
            self.assertEqual(schedule_response.status_code, 200, schedule_response.content.decode())
            schedule_attachment = schedule_response.json()["work_order"]["bom_attachment"]
            self.assertEqual(schedule_attachment["name"], "operator-guide.pdf")
            self.assertTrue(schedule_attachment["is_pdf"])

            drawer_response = self.client.get(f"/manufacturing/api/work-order/{work_order.id}/")
            self.assertEqual(drawer_response.status_code, 200, drawer_response.content.decode())
            drawer_attachment = drawer_response.json()["work_order"]["bom_attachment"]
            self.assertEqual(drawer_attachment["name"], "operator-guide.pdf")
            self.assertTrue(drawer_attachment["download_url"].endswith(".pdf"))

    def test_legacy_bom_save_api_versions_bom_used_by_work_order(self):
        product = Product.objects.create(company=self.company, name="versioned_product")
        bom = BillOfMaterial.objects.create(
            product=product,
            version="v1.0",
            status="draft",
            base_quantity=10,
            uom="pcs",
            created_by=self.user,
        )
        BOMComponent.objects.create(
            bom=bom,
            material_name="Old Material",
            quantity=1,
            unit="pcs",
        )
        bom.status = "active"
        bom.save(update_fields=["status"])
        work_order = WorkOrder.objects.create(
            company=self.company,
            product_name=product.name,
            bom=bom,
            quantity=20,
        )

        response = self.client.post(
            "/manufacturing/api/bom/save/",
            data=json.dumps(
                {
                    "bom_id": bom.id,
                    "product_name": product.name,
                    "base_qty": 12,
                    "uom": "pcs",
                    "status": "draft",
                    "components": [
                        {
                            "name": "Corrected Material",
                            "qty": 2,
                            "unit": "pcs",
                            "cost": 0,
                            "scrap_qty": 0,
                            "scrap_price": 0,
                            "scrap_type": "sell_as_scrap",
                        }
                    ],
                    "criteria": [],
                    "operations": [],
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200, response.content.decode())
        payload = response.json()
        self.assertEqual(payload["status"], "success")
        self.assertTrue(payload["version_created"])
        self.assertNotEqual(payload["bom_id"], bom.id)

        work_order.refresh_from_db()
        self.assertEqual(work_order.bom_id, bom.id)
        self.assertEqual(work_order.bom_snapshot["components"][0]["material_name"], "Old Material")

        corrected_bom = BillOfMaterial.objects.get(id=payload["bom_id"])
        self.assertEqual(corrected_bom.version, "v1.1")
        self.assertEqual(corrected_bom.components.get().material_name, "Corrected Material")
        self.assertTrue(
            Notification.objects.filter(
                recipient=self.user,
                title="BOM version created",
                message__contains="Existing work orders keep their original BOM snapshot",
            ).exists()
        )

    def test_apply_latest_bom_updates_eligible_pending_work_order(self):
        product = Product.objects.create(company=self.company, name="apply_latest_product")
        old_bom = BillOfMaterial.objects.create(
            product=product,
            version="v1.0",
            status="draft",
            base_quantity=10,
            uom="pcs",
            created_by=self.user,
        )
        BOMComponent.objects.create(bom=old_bom, material_name="Old Material", quantity=1, unit="pcs")
        old_bom.status = "active"
        old_bom.save(update_fields=["status"])

        work_order = WorkOrder.objects.create(
            company=self.company,
            product_name=product.name,
            bom=old_bom,
            quantity=10,
            status="pending",
        )

        new_bom = BillOfMaterial.objects.create(
            product=product,
            version="v1.1",
            status="draft",
            base_quantity=10,
            uom="pcs",
            created_by=self.user,
        )
        BOMComponent.objects.create(bom=new_bom, material_name="Corrected Material", quantity=2, unit="pcs")
        new_bom.status = "active"
        new_bom.save(update_fields=["status"])

        response = self.client.post(f"/manufacturing/api/work-order/{work_order.id}/apply-latest-bom/")

        self.assertEqual(response.status_code, 200, response.content.decode())
        payload = response.json()
        self.assertTrue(payload["success"])
        self.assertEqual(payload["bom_id"], new_bom.id)
        self.assertEqual(payload["bom_version"], "v1.1")

        work_order.refresh_from_db()
        self.assertEqual(work_order.bom_id, new_bom.id)
        self.assertEqual(work_order.bom_version, "v1.1")
        self.assertEqual(work_order.bom_snapshot["components"][0]["material_name"], "Corrected Material")
        self.assertEqual(work_order.material_readiness_status, "not_checked")

    def test_apply_latest_bom_clears_unstarted_route_plan(self):
        product = Product.objects.create(company=self.company, name="planned_apply_latest_product")
        old_bom = BillOfMaterial.objects.create(
            product=product,
            version="v1.0",
            status="active",
            base_quantity=10,
            uom="pcs",
            created_by=self.user,
        )
        machine = Machine.objects.create(
            company=self.company,
            name="CNC Milling Machine",
            code="CNC-01",
            category="CNC",
            type="CNC",
            status="operational",
        )
        stage = ProductionStage.objects.create(name="Cutting", category="CNC", machine=machine)
        work_order = WorkOrder.objects.create(
            company=self.company,
            product_name=product.name,
            bom=old_bom,
            quantity=10,
            status="pending",
            machine=machine,
            stage=stage,
            current_stage=stage,
        )
        WorkOrder.objects.create(
            company=self.company,
            product_name=f"{product.name} - Cutting",
            bom=old_bom,
            quantity=10,
            status="pending",
            parent=work_order,
            machine=machine,
            stage=stage,
            current_stage=stage,
        )
        WorkOrderStage.objects.create(
            work_order=work_order,
            stage=stage,
            machine=machine,
            sequence_order=1,
            quantity=10,
            status="scheduled",
        )
        new_bom = BillOfMaterial.objects.create(
            product=product,
            version="v1.1",
            status="active",
            base_quantity=10,
            uom="pcs",
            created_by=self.user,
        )

        response = self.client.post(f"/manufacturing/api/work-order/{work_order.id}/apply-latest-bom/")

        self.assertEqual(response.status_code, 200, response.content.decode())
        payload = response.json()
        self.assertTrue(payload["route_plan_cleared"])
        work_order.refresh_from_db()
        self.assertEqual(work_order.bom_id, new_bom.id)
        self.assertIsNone(work_order.machine_id)
        self.assertIsNone(work_order.stage_id)
        self.assertFalse(work_order.sub_tasks.exists())
        self.assertFalse(work_order.stages.exists())

    def test_apply_latest_bom_blocks_started_work_order(self):
        product = Product.objects.create(company=self.company, name="started_apply_latest_product")
        old_bom = BillOfMaterial.objects.create(
            product=product,
            version="v1.0",
            status="active",
            base_quantity=10,
            uom="pcs",
            created_by=self.user,
        )
        work_order = WorkOrder.objects.create(
            company=self.company,
            product_name=product.name,
            bom=old_bom,
            quantity=10,
            status="in_progress",
        )
        new_bom = BillOfMaterial.objects.create(
            product=product,
            version="v1.1",
            status="active",
            base_quantity=10,
            uom="pcs",
            created_by=self.user,
        )

        response = self.client.post(f"/manufacturing/api/work-order/{work_order.id}/apply-latest-bom/")

        self.assertEqual(response.status_code, 400, response.content.decode())
        payload = response.json()
        self.assertFalse(payload["success"])
        self.assertIn("Only pending", payload["error"])
        work_order.refresh_from_db()
        self.assertEqual(work_order.bom_id, old_bom.id)
        self.assertNotEqual(work_order.bom_id, new_bom.id)

    def test_activating_new_bom_flags_started_work_order_for_decision(self):
        product = Product.objects.create(company=self.company, name="impact_product")
        old_bom = BillOfMaterial.objects.create(
            product=product,
            version="v1.0",
            status="active",
            base_quantity=10,
            uom="pcs",
            created_by=self.user,
        )
        work_order = WorkOrder.objects.create(
            company=self.company,
            product_name=product.name,
            bom=old_bom,
            quantity=10,
            status="in_progress",
            worker_start_at=timezone.now(),
        )
        ProductionLog.objects.create(work_order=work_order, worker=self.user, quantity=3, status="approved")
        new_bom = BillOfMaterial.objects.create(
            product=product,
            version="v1.1",
            status="draft",
            base_quantity=10,
            uom="pcs",
            created_by=self.user,
        )

        response = self.client.post("/manufacturing/update-bom-status/", data={"bom_id": new_bom.id, "status": "active"})

        self.assertEqual(response.status_code, 200, response.content.decode())
        payload = response.json()
        self.assertTrue(payload["success"])
        self.assertEqual(payload["impacted_work_orders"], 1)
        work_order.refresh_from_db()
        self.assertEqual(work_order.bom_change_status, "action_required")
        self.assertEqual(work_order.bom_change_latest_bom_id, new_bom.id)

        details = self.client.get(f"/manufacturing/api/work-order/{work_order.id}/").json()["work_order"]
        self.assertTrue(details["bom_change_action_required"])
        self.assertTrue(details["bom_change"]["has_started"])
        self.assertEqual(details["bom_change"]["reported_qty"], 3)

    def test_bom_change_continue_old_acknowledges_warning(self):
        product = Product.objects.create(company=self.company, name="continue_old_product")
        old_bom = BillOfMaterial.objects.create(product=product, version="v1.0", status="active", created_by=self.user)
        work_order = WorkOrder.objects.create(
            company=self.company,
            product_name=product.name,
            bom=old_bom,
            quantity=10,
            status="in_progress",
        )
        new_bom = BillOfMaterial.objects.create(product=product, version="v1.1", status="draft", created_by=self.user)
        self.client.post("/manufacturing/update-bom-status/", data={"bom_id": new_bom.id, "status": "active"})

        response = self.client.post(
            f"/manufacturing/api/work-order/{work_order.id}/bom-change-decision/",
            data=json.dumps({"decision": "continue_old", "note": "Customer approved old version"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200, response.content.decode())
        work_order.refresh_from_db()
        self.assertEqual(work_order.bom_change_status, "ignored")
        self.assertEqual(work_order.bom_id, old_bom.id)
        self.assertIn("Customer approved", work_order.bom_change_decision_note)

    def test_bom_change_scrap_apply_adds_compensation_and_latest_bom(self):
        product = Product.objects.create(company=self.company, name="scrap_apply_product")
        old_bom = BillOfMaterial.objects.create(product=product, version="v1.0", status="active", created_by=self.user)
        machine = Machine.objects.create(
            company=self.company,
            name="Scrap CNC",
            code="SCR-CNC",
            category="CNC",
            type="CNC",
            status="operational",
        )
        stage = ProductionStage.objects.create(name="Scrap Cutting", category="CNC", machine=machine)
        work_order = WorkOrder.objects.create(
            company=self.company,
            product_name=product.name,
            bom=old_bom,
            quantity=10,
            status="in_progress",
            machine=machine,
            stage=stage,
            current_stage=stage,
        )
        route_child = WorkOrder.objects.create(
            company=self.company,
            product_name=f"{product.name} - {stage.name}",
            bom=old_bom,
            quantity=10,
            status="in_progress",
            parent=work_order,
            machine=machine,
            stage=stage,
            current_stage=stage,
        )
        WorkOrderStage.objects.create(
            work_order=work_order,
            stage=stage,
            machine=machine,
            sequence_order=1,
            quantity=10,
            status="in_progress",
        )
        ProductionLog.objects.create(work_order=route_child, worker=self.user, quantity=4, status="approved")
        new_bom = BillOfMaterial.objects.create(product=product, version="v1.1", status="draft", created_by=self.user)
        BOMComponent.objects.create(bom=new_bom, material_name="New Material", quantity=2, unit="pcs")
        self.client.post("/manufacturing/update-bom-status/", data={"bom_id": new_bom.id, "status": "active"})

        response = self.client.post(
            f"/manufacturing/api/work-order/{work_order.id}/bom-change-decision/",
            data=json.dumps({"decision": "scrap_apply"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200, response.content.decode())
        payload = response.json()
        self.assertEqual(payload["scrapped_qty"], 4)
        work_order.refresh_from_db()
        self.assertEqual(work_order.bom_id, new_bom.id)
        self.assertEqual(work_order.bom_change_status, "scrap_applied")
        self.assertEqual(work_order.bom_change_scrapped_qty, 4)
        self.assertEqual(work_order.quantity, 10)
        self.assertEqual(work_order.scrap_compensation_qty, 0)
        self.assertIsNone(work_order.machine_id)
        self.assertFalse(work_order.stages.exists())
        route_child.refresh_from_db()
        self.assertEqual(route_child.status, "archived")

        timeline_data = DashboardService.get_timeline_data(
            self.company,
            include_unscheduled=True,
            viewer_role="planner",
            viewer=self.user,
        )
        self.assertIn(work_order.id, [task["id"] for task in timeline_data["tasks"]])
        timeline_task = next(task for task in timeline_data["tasks"] if task["id"] == work_order.id)
        self.assertEqual(timeline_task["quantity"], 10)
        self.assertEqual(timeline_task["bom_change"]["scrapped_qty"], 4)

    def test_bom_change_archive_new_creates_replacement(self):
        product = Product.objects.create(company=self.company, name="archive_new_product")
        old_bom = BillOfMaterial.objects.create(product=product, version="v1.0", status="active", created_by=self.user)
        work_order = WorkOrder.objects.create(
            company=self.company,
            product_name=product.name,
            bom=old_bom,
            quantity=10,
            status="in_progress",
            priority="High",
        )
        new_bom = BillOfMaterial.objects.create(product=product, version="v1.1", status="draft", created_by=self.user)
        self.client.post("/manufacturing/update-bom-status/", data={"bom_id": new_bom.id, "status": "active"})

        response = self.client.post(
            f"/manufacturing/api/work-order/{work_order.id}/bom-change-decision/",
            data=json.dumps({"decision": "archive_new"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200, response.content.decode())
        replacement_id = response.json()["replacement_wo_id"]
        work_order.refresh_from_db()
        replacement = WorkOrder.objects.get(id=replacement_id)
        self.assertEqual(work_order.status, "archived")
        self.assertEqual(work_order.bom_change_status, "archived_replaced")
        self.assertEqual(work_order.bom_change_replacement_wo_id, replacement.id)
        self.assertEqual(replacement.bom_id, new_bom.id)
        self.assertEqual(replacement.quantity, 10)
        self.assertEqual(replacement.priority, "High")
