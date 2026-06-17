from io import BytesIO

import openpyxl
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from accounts.models import Profile
from accounts.constants import RoleType
from manufacturing.models import BillOfMaterial, Machine, Product
from manufacturing.tests.utils import create_company, create_user_with_role
from tenancy.models import Tenant


class BulkImportValidationTests(TestCase):
    def setUp(self):
        self.company = create_company("Bulk Validation Co")
        self.admin = create_user_with_role("bulk-admin", RoleType.ADMIN.value, self.company)
        self.client.force_login(self.admin)
        Tenant.objects.create(
            name="Bulk Validation",
            code="bulk-validation",
            db_alias="default",
            db_name="db.sqlite3",
        )
        session = self.client.session
        session["tenant_code"] = "bulk-validation"
        session.save()

    def _workbook_upload(self, name, headers, rows):
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.append(headers)
        for row in rows:
            sheet.append(row)
        buffer = BytesIO()
        workbook.save(buffer)
        return SimpleUploadedFile(
            name,
            buffer.getvalue(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    def test_products_import_rejects_employee_template_headers(self):
        upload = self._workbook_upload(
            "employees_template.xlsx",
            ["Employee Name", "Email", "Phone", "Role"],
            [["Line Worker", "worker@example.com", "", "worker"]],
        )

        response = self.client.post(
            reverse("handle_bulk_import"),
            data={"import_type": "products", "file": upload},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(Product.objects.filter(company=self.company).exists())

    def test_products_import_rejects_generic_name_team_file(self):
        upload = self._workbook_upload(
            "legacy_team.xlsx",
            ["Name", "Email", "Phone", "Role"],
            [["Line Worker", "worker@example.com", "", "worker"]],
        )

        response = self.client.post(
            reverse("handle_bulk_import"),
            data={"import_type": "products", "file": upload},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(Product.objects.filter(company=self.company).exists())

    def test_employees_import_accepts_manufacturing_scope_alias(self):
        upload = self._workbook_upload(
            "employees_template.xlsx",
            ["Employee Name", "Email", "Phone", "Role", "Department(s)", "Shift", "App Scope"],
            [["Area Planner", "area.planner@example.com", "", "planner", "", "", "manufacturing"]],
        )

        response = self.client.post(
            reverse("handle_bulk_import"),
            data={"import_type": "employees", "file": upload},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        imported = User.objects.get(username="area.planner@example.com")
        profile = Profile.objects.get(user=imported)
        self.assertEqual(profile.company, self.company)
        self.assertEqual(profile.role.name, RoleType.PLANNER.value)
        self.assertEqual(profile.app_scope, "manufacturing")

    def test_machines_import_rejects_product_template_headers(self):
        upload = self._workbook_upload(
            "products_template.xlsx",
            ["Name", "Material Type (raw/finished)", "Unit (pcs/kg/m)", "Description", "Cost"],
            [["Control Box", "finished", "pcs", "", 10]],
        )

        response = self.client.post(
            reverse("handle_bulk_import"),
            data={"import_type": "machines", "file": upload},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(Machine.objects.filter(company=self.company).exists())

    def test_employees_import_rejects_product_template_headers(self):
        upload = self._workbook_upload(
            "products_template.xlsx",
            ["Name", "Material Type (raw/finished)", "Unit (pcs/kg/m)", "Description", "Cost"],
            [["Control Box", "finished", "pcs", "", 10]],
        )

        response = self.client.post(
            reverse("handle_bulk_import"),
            data={"import_type": "employees", "file": upload},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(username="Control Box").exists())

    def test_bom_import_rejects_unknown_machine_without_partial_bom(self):
        upload = self._workbook_upload(
            "bom_template.xlsx",
            [
                "Product Name",
                "BOM Version",
                "BOM Status",
                "Base Quantity",
                "BOM UOM",
                "Component Name",
                "Component Quantity",
                "Component Unit",
                "Operation Name",
                "Duration (mins)",
                "Machine Code",
            ],
            [["Control Box", "v1.0", "active", 1, "pcs", "Steel Sheet", 2, "pcs", "Cutting", 30, "MISSING-01"]],
        )

        response = self.client.post(
            reverse("handle_bulk_import"),
            data={"import_type": "bom", "file": upload},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(BillOfMaterial.objects.filter(product__company=self.company).exists())

    def test_bom_import_rejects_missing_component_quantity_without_partial_bom(self):
        upload = self._workbook_upload(
            "bom_template.xlsx",
            [
                "Product Name",
                "BOM Version",
                "BOM Status",
                "Base Quantity",
                "BOM UOM",
                "Component Name",
                "Component Quantity",
                "Component Unit",
                "Operation Name",
                "Duration (mins)",
            ],
            [["Control Box", "v1.0", "active", 1, "pcs", "Steel Sheet", "", "pcs", "Cutting", 30]],
        )

        response = self.client.post(
            reverse("handle_bulk_import"),
            data={"import_type": "bom", "file": upload},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(BillOfMaterial.objects.filter(product__company=self.company).exists())

    def test_bom_import_rejects_operation_without_duration_or_run_time(self):
        upload = self._workbook_upload(
            "bom_template.xlsx",
            [
                "Product Name",
                "BOM Version",
                "BOM Status",
                "Base Quantity",
                "BOM UOM",
                "Operation Name",
                "Duration (mins)",
                "Component Name",
                "Component Quantity",
            ],
            [["Control Box", "v1.0", "active", 1, "pcs", "Cutting", "", "", ""]],
        )

        response = self.client.post(
            reverse("handle_bulk_import"),
            data={"import_type": "bom", "file": upload},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(BillOfMaterial.objects.filter(product__company=self.company).exists())

    def test_bom_import_carries_forward_common_bom_fields_and_preserves_details(self):
        machine = Machine.objects.create(
            company=self.company,
            name="Mixing Line",
            code="MIX-01",
            category="Mixing",
            type="Mixer",
        )
        upload = self._workbook_upload(
            "usual_bom.xlsx",
            [
                "Finished Good",
                "Revision",
                "State",
                "Batch Size",
                "Output Unit",
                "Process Name",
                "Resource Code",
                "Sequence",
                "Setup Minutes",
                "Minutes Per Unit",
                "Material Name",
                "Required Qty",
                "Consumption Unit",
                "Unit Cost",
                "Waste Qty",
                "Scrap Value",
                "Waste Type",
                "Instructions",
            ],
            [
                ["Smart Paint", "R2", "active", 100, "kg", "Mixing", "MIX-01", 10, 15, 2.5, "", "", "", "", "", "", "", "Keep under 40C"],
                ["", "", "", "", "", "", "", "", "", "", "Resin", 12.5, "kg", 7.25, 0.5, 1.2, "return to stock", ""],
                ["", "", "", "", "", "", "", "", "", "", "Pigment", 1.25, "kg", 3, 0, 0, "sell as scrap", ""],
            ],
        )

        response = self.client.post(
            reverse("handle_bulk_import"),
            data={"import_type": "bom", "file": upload},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        bom = BillOfMaterial.objects.get(product__company=self.company, product__name="Smart Paint")
        self.assertEqual(bom.version, "R2")
        self.assertEqual(bom.status, "active")
        self.assertEqual(bom.uom, "kg")
        self.assertEqual(float(bom.base_quantity), 100.0)
        self.assertEqual(bom.components.count(), 2)

        resin = bom.components.get(material_name="Resin")
        self.assertEqual(float(resin.quantity), 12.5)
        self.assertEqual(resin.unit, "kg")
        self.assertEqual(float(resin.cost_per_unit), 7.25)
        self.assertEqual(float(resin.wastage_quantity), 0.5)
        self.assertEqual(float(resin.scrap_value_per_unit), 1.2)
        self.assertEqual(resin.scrap_type, "return_to_stock")

        operation = bom.operations.get()
        self.assertEqual(operation.machine, machine)
        self.assertEqual(operation.order, 10)
        self.assertEqual(operation.setup_time, 15)
        self.assertEqual(float(operation.run_time), 2.5)
        self.assertEqual(operation.duration_minutes, 265)
        self.assertEqual(operation.description, "Keep under 40C")
        self.assertEqual(operation.material_links.count(), 2)
