from django.apps import apps
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
import csv
import io

from manufacturing.models import (
    BOMComponent,
    BOMOperation,
    BillOfMaterial,
    Machine,
    MaterialUsage,
    Product,
    ProductionLog,
    ProductionStage,
    WorkOrder,
)
from manufacturing.tests.utils import create_company, create_user_with_role


AuditLog = apps.get_model("manufacturing", "AuditLog")


class ReportsDashboardAuditTests(TestCase):
    def setUp(self):
        self.company = create_company("Reports Company")
        self.other_company = create_company("Other Reports Company")
        self.admin = create_user_with_role("admin_reports", "admin", self.company)
        self.planner = create_user_with_role("planner_reports", "planner", self.company)
        self.supervisor = create_user_with_role("supervisor_reports", "supervisor", self.company)
        self.worker = create_user_with_role("worker_reports", "worker", self.company)
        self.other_planner = create_user_with_role("planner_other_reports", "planner", self.other_company)

    def test_reports_are_limited_to_admin_and_planner(self):
        for allowed_user in (self.admin, self.planner):
            client = Client()
            client.force_login(allowed_user)
            response = client.get(reverse("reports_dashboard"))
            self.assertEqual(response.status_code, 200)

        for blocked_user in (self.supervisor, self.worker):
            client = Client()
            client.force_login(blocked_user)
            response = client.get(reverse("reports_dashboard"))
            self.assertEqual(response.status_code, 403)

    def test_audit_tab_shows_only_current_company_logs(self):
        AuditLog.objects.create(
            user=self.planner,
            company=self.company,
            action="create",
            model_name="WorkOrder",
            object_id=101,
            object_repr="Visible Planner WO",
            details={"event": "work_order_created"},
        )
        AuditLog.objects.create(
            user=self.other_planner,
            company=self.other_company,
            action="create",
            model_name="WorkOrder",
            object_id=202,
            object_repr="Hidden Other WO",
            details={"event": "work_order_created"},
        )

        client = Client()
        client.force_login(self.planner)
        response = client.get(reverse("reports_dashboard"), {"section": "audit"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Audit Trail")
        self.assertContains(response, "Visible Planner WO")
        self.assertNotContains(response, "Hidden Other WO")

    def test_audit_tab_filters_by_action(self):
        AuditLog.objects.create(
            user=self.planner,
            company=self.company,
            action="create",
            model_name="WorkOrder",
            object_id=301,
            object_repr="Create Action WO",
            details={"event": "work_order_created"},
        )
        AuditLog.objects.create(
            user=self.planner,
            company=self.company,
            action="update",
            model_name="WorkOrder",
            object_id=302,
            object_repr="Update Action WO",
            details={"event": "work_order_updated_from_timeline"},
        )

        client = Client()
        client.force_login(self.planner)
        response = client.get(
            reverse("reports_dashboard"),
            {"section": "audit", "audit_action": "create"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Create Action WO")
        self.assertNotContains(response, "Update Action WO")

    def test_audit_tab_user_filter_matches_related_worker_in_details(self):
        AuditLog.objects.create(
            user=self.planner,
            company=self.company,
            action="update",
            model_name="WorkOrder",
            object_id=401,
            object_repr="Assigned Worker WO",
            details={
                "event": "worker_assigned",
                "worker_id": self.worker.id,
                "worker_username": self.worker.username,
            },
        )

        client = Client()
        client.force_login(self.planner)
        response = client.get(
            reverse("reports_dashboard"),
            {"section": "audit", "audit_user": self.worker.id},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Assigned Worker WO")

    def test_audit_csv_export_returns_filtered_rows(self):
        AuditLog.objects.create(
            user=self.planner,
            company=self.company,
            action="update",
            model_name="WorkOrder",
            object_id=501,
            object_repr="Audit Export WO",
            details={"event": "work_order_scheduled", "work_order_id": 501},
        )

        client = Client()
        client.force_login(self.planner)
        response = client.get(reverse("export_audit_csv"), {"audit_search": "Audit Export"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv")
        self.assertIn("Audit Export WO", response.content.decode("utf-8"))

    def test_audit_tab_uses_operational_labels_and_context(self):
        AuditLog.objects.create(
            user=self.planner,
            company=self.company,
            action="update",
            model_name="WorkOrder",
            object_id=601,
            object_repr="Material Check WO",
            details={
                "event": "material_readiness_updated",
                "work_order_id": 601,
                "status": "partial",
                "available_qty": 42,
                "available_percent": "42.00",
                "expected_delivery_date": "2026-06-20",
                "shortage_note": "Missing fasteners",
            },
        )

        client = Client()
        client.force_login(self.planner)
        response = client.get(reverse("reports_dashboard"), {"section": "audit"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Material Readiness Updated")
        self.assertContains(response, "Status: Partially OK")
        self.assertContains(response, "Available Qty: 42")
        self.assertContains(response, "Available %: 42.00")
        self.assertContains(response, "Expected Delivery: 2026-06-20")
        self.assertContains(response, "Material Note: Missing fasteners")

    def test_audit_csv_export_uses_readable_event_labels(self):
        AuditLog.objects.create(
            user=self.planner,
            company=self.company,
            action="update",
            model_name="WorkOrder",
            object_id=602,
            object_repr="BOM Decision WO",
            details={
                "event": "bom_change_scrap_apply",
                "work_order_id": 602,
                "previous_bom_version": "v1.0",
                "new_bom_version": "v1.1",
                "scrapped_qty": 3,
            },
        )

        client = Client()
        client.force_login(self.planner)
        response = client.get(reverse("export_audit_csv"), {"audit_search": "BOM Decision"})

        self.assertEqual(response.status_code, 200)
        csv_rows = list(csv.DictReader(io.StringIO(response.content.decode("utf-8"))))
        self.assertEqual(csv_rows[0]["Event"], "BOM Change: Scrap and Apply")
        self.assertIn("BOM: v1.0 -> v1.1", csv_rows[0]["Context"])
        self.assertIn("Scrapped Qty: 3", csv_rows[0]["Context"])


class ReportsDashboardActualVsPlannedTests(TestCase):
    def setUp(self):
        self.company = create_company("Variance Company")
        self.planner = create_user_with_role("planner_variance", "planner", self.company)

        self.finished_product = Product.objects.create(
            company=self.company,
            name="Nut 15",
            unit="pcs",
            material_type="finished",
        )
        self.material_product = Product.objects.create(
            company=self.company,
            name="Steel Coil",
            unit="kg",
            material_type="raw",
        )
        self.machine = Machine.objects.create(
            company=self.company,
            name="CNC Machine 01",
            code="M-001",
            category="CNC",
        )
        self.stage = ProductionStage.objects.create(
            name="Op 01: CNC",
            category="CNC",
            order=1,
        )
        self.bom = BillOfMaterial.objects.create(
            product=self.finished_product,
            base_quantity=10,
            uom="pcs",
            created_by=self.planner,
        )
        BOMComponent.objects.create(
            bom=self.bom,
            product=self.material_product,
            material_name=self.material_product.name,
            quantity=10,
            unit="kg",
        )
        BOMOperation.objects.create(
            bom=self.bom,
            stage=self.stage,
            machine=self.machine,
            order=10,
            setup_time=30,
            run_time=3,
            duration_minutes=60,
        )
        self.root_wo = WorkOrder.objects.create(
            company=self.company,
            product_name=self.finished_product.name,
            bom=self.bom,
            quantity=10,
            status="pending",
            start_date=timezone.now(),
            operation_flow_mode="series",
        )
        self.child_wo = WorkOrder.objects.create(
            company=self.company,
            parent=self.root_wo,
            product_name=self.finished_product.name,
            bom=self.bom,
            quantity=10,
            stage=self.stage,
            current_stage=self.stage,
            machine=self.machine,
            status="pending",
            start_date=timezone.now(),
        )
        self.log = ProductionLog.objects.create(
            work_order=self.child_wo,
            worker=self.planner,
            quantity=8,
            status="approved",
        )
        MaterialUsage.objects.create(
            production_log=self.log,
            product=self.material_product,
            material_name=self.material_product.name,
            quantity_used="11.000",
            unit="kg",
        )
        self.client = Client()
        self.client.force_login(self.planner)

    def test_actual_vs_planned_json_rolls_child_logs_up_to_root_work_order(self):
        response = self.client.get(
            reverse("reports_dashboard"),
            {
                "format": "json",
                "work_order": self.root_wo.id,
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(len(payload["rows"]), 1)
        row = payload["rows"][0]
        self.assertEqual(row["wo_id"], self.root_wo.id)
        self.assertEqual(row["actual_output_qty"], 8.0)
        self.assertEqual(row["actual_material_qty"], 11.0)
        self.assertEqual(row["material_delta_qty"], 1.0)
        self.assertTrue(row["actual_output_available"])
        self.assertTrue(row["actual_material_available"])
        self.assertEqual(payload["summary"]["work_order_count"], 1)

    def test_work_order_sheet_renders_for_selected_work_order(self):
        response = self.client.get(
            reverse("export_work_order_sheet"),
            {"work_order": self.root_wo.id},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Work Order Efficiency Report")
        self.assertContains(response, f"WO-{self.root_wo.id}")
        self.assertContains(response, "Time Variance")
        self.assertContains(response, "Material Variance")
        self.assertContains(response, "Production Variance")
        self.assertContains(response, "Nut 15")
        self.assertContains(response, "Steel Coil")

    def test_work_order_sheet_without_matching_filters_redirects_back_to_reports(self):
        response = self.client.get(
            reverse("export_work_order_sheet"),
            {"date": "2099-01-01"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("reports_dashboard"), response["Location"])
        self.assertIn("sheet_error=no_work_order", response["Location"])

    def test_export_csv_includes_product_order_and_done_quantity_columns(self):
        response = self.client.get(
            reverse("export_production_csv"),
            {"work_order": self.root_wo.id},
        )

        self.assertEqual(response.status_code, 200)
        reader = csv.reader(io.StringIO(response.content.decode("utf-8")))
        rows = list(reader)
        self.assertGreaterEqual(len(rows), 2)

        header = rows[0]
        data_row = rows[1]
        self.assertIn("Product", header)
        self.assertIn("Order Qty", header)
        self.assertIn("Done Qty", header)

        product_idx = header.index("Product")
        order_qty_idx = header.index("Order Qty")
        done_qty_idx = header.index("Done Qty")
        self.assertEqual(data_row[product_idx], "Nut 15")
        self.assertEqual(data_row[order_qty_idx], "10.0")
        self.assertEqual(data_row[done_qty_idx], "8.0")

    def test_bi_dashboard_exposes_manufacturing_kpis(self):
        self.root_wo.status = "completed"
        self.root_wo.closed_by_planner = True
        self.root_wo.material_readiness_status = "ready"
        self.root_wo.due_date = timezone.now() + timedelta(days=1)
        self.root_wo.end_date = timezone.now()
        self.root_wo.save(update_fields=["status", "closed_by_planner", "material_readiness_status", "due_date", "end_date"])

        response = self.client.get(reverse("reports_dashboard"), {"section": "bi"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Manufacturing Status Mix")
        self.assertContains(response, "Schedule Adherence")
        self.assertContains(response, "Material Ready")
        self.assertContains(response, "Nut 15")

    def test_pdf_export_returns_pdf_document(self):
        response = self.client.get(reverse("export_report_pdf"), {"section": "bi"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertTrue(response.content.startswith(b"%PDF"))
