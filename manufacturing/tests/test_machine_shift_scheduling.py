import json
from datetime import datetime, timedelta
from decimal import Decimal

from django.http import QueryDict
from django.test import Client, TestCase
from django.test.client import BOUNDARY, MULTIPART_CONTENT, encode_multipart
from django.urls import reverse
from django.utils import timezone

from manufacturing.models import Machine
from manufacturing.models import SystemSettings
from manufacturing.serializers import MachineSerializer
from manufacturing.services import DashboardService, WorkOrderService
from manufacturing.tests.utils import create_company, create_user_with_role


class MachineShiftSchedulingTests(TestCase):
    def setUp(self):
        self.company = create_company("Machine Shift Co")
        self.planner = create_user_with_role("planner_machine_shift", "planner", self.company)
        self.client = Client()
        self.client.force_login(self.planner)

    def test_create_machine_can_save_custom_shift_configuration(self):
        response = self.client.post(
            reverse("create_machine"),
            data={
                "name": "Packing Machine",
                "code": "PACK-01",
                "category": "Packing",
                "status": "operational",
                "use_factory_shifts": "false",
                "shift_configuration": json.dumps({
                    "morning": {"enabled": True, "start": "08:00", "end": "16:00"},
                    "afternoon": {"enabled": False, "start": "14:00", "end": "22:00"},
                    "night": {"enabled": False, "start": "22:00", "end": "06:00"},
                }),
            },
        )

        self.assertEqual(response.status_code, 200, response.content.decode())
        machine = Machine.objects.get(code="PACK-01", company=self.company)
        self.assertFalse(machine.use_factory_shifts)
        self.assertEqual(machine.shift_configuration["morning"]["start"], "08:00")
        self.assertFalse(machine.shift_configuration["afternoon"]["enabled"])

    def test_create_machine_normalizes_code_and_name(self):
        response = self.client.post(
            reverse("create_machine"),
            data={
                "name": "  Packing Machine  ",
                "code": " pack 01 ",
                "category": "Packing",
                "status": "operational",
            },
        )

        self.assertEqual(response.status_code, 200, response.content.decode())
        machine = Machine.objects.get(company=self.company)
        self.assertEqual(machine.name, "Packing Machine")
        self.assertEqual(machine.code, "PACK-01")

    def test_machine_api_patch_rejects_case_insensitive_duplicate_code(self):
        first = Machine.objects.create(
            name="Packing Machine",
            code="PACK-01",
            category="Packing",
            status="operational",
            company=self.company,
        )
        second = Machine.objects.create(
            name="Packing Machine 2",
            code="PACK-02",
            category="Packing",
            status="operational",
            company=self.company,
        )

        response = self.client.patch(
            reverse("machine-detail", args=[second.id]),
            data=json.dumps({
                "code": "pack-01",
            }),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400, response.content.decode())
        self.assertIn("already exists", response.content.decode().lower())
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.code, "PACK-01")
        self.assertEqual(second.code, "PACK-02")

    def test_machine_serializer_accepts_querydict_shift_configuration_on_partial_update(self):
        payload = QueryDict("", mutable=True)
        payload.update({
            "category": "CNC",
            "use_factory_shifts": "true",
            "shift_configuration": json.dumps({
                "morning": {"enabled": True, "start": "06:00", "end": "14:00"},
                "afternoon": {"enabled": True, "start": "14:00", "end": "22:00"},
                "night": {"enabled": True, "start": "22:00", "end": "06:00"},
            }),
        })

        serializer = MachineSerializer(data=payload, partial=True)

        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertTrue(serializer.validated_data["use_factory_shifts"])
        self.assertEqual(serializer.validated_data["shift_configuration"], {})

    def test_machine_serializer_ignores_empty_image_on_partial_update(self):
        payload = QueryDict("", mutable=True)
        payload.update({
            "category": "CNC",
            "image": "",
        })

        serializer = MachineSerializer(data=payload, partial=True)

        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertNotIn("image", serializer.validated_data)

    def test_machine_api_patch_accepts_stringified_shift_configuration(self):
        machine = Machine.objects.create(
            name="Packing Machine",
            code="PACK-01",
            category="Packing",
            status="operational",
            company=self.company,
        )

        response = self.client.patch(
            reverse("machine-detail", args=[machine.id]),
            data=json.dumps({
                "use_factory_shifts": False,
                "shift_configuration": {
                    "morning": {"enabled": True, "start": "08:00", "end": "16:00"},
                    "afternoon": {"enabled": False, "start": "14:00", "end": "22:00"},
                    "night": {"enabled": False, "start": "22:00", "end": "06:00"},
                },
            }),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200, response.content.decode())
        machine.refresh_from_db()
        self.assertFalse(machine.use_factory_shifts)
        self.assertEqual(machine.shift_configuration["morning"]["end"], "16:00")

    def test_machine_api_patch_propagates_shift_configuration_to_same_category(self):
        source = Machine.objects.create(
            name="Packing Machine",
            code="PACK-01",
            category="Packing",
            type="Packing",
            status="operational",
            company=self.company,
        )
        peer = Machine.objects.create(
            name="Packing Machine 2",
            code="PACK-02",
            category="Packing",
            type="Packing",
            status="operational",
            company=self.company,
        )
        other = Machine.objects.create(
            name="CNC Machine",
            code="CNC-01",
            category="CNC",
            type="CNC",
            status="operational",
            company=self.company,
        )

        response = self.client.patch(
            reverse("machine-detail", args=[source.id]),
            data=json.dumps({
                "use_factory_shifts": False,
                "shift_configuration": {
                    "morning": {"enabled": True, "start": "07:00", "end": "15:00"},
                    "afternoon": {"enabled": False, "start": "15:00", "end": "23:00"},
                    "night": {"enabled": False, "start": "23:00", "end": "07:00"},
                },
            }),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200, response.content.decode())
        source.refresh_from_db()
        peer.refresh_from_db()
        other.refresh_from_db()
        self.assertFalse(source.use_factory_shifts)
        self.assertFalse(peer.use_factory_shifts)
        self.assertEqual(peer.shift_configuration["morning"]["start"], "07:00")
        self.assertTrue(other.use_factory_shifts)
        self.assertEqual(other.shift_configuration, {})

    def test_machine_api_form_patch_propagates_shift_configuration_to_matching_legacy_type(self):
        source = Machine.objects.create(
            name="Packing Machine",
            code="PACK-01",
            category="Packing",
            type="Shared Line",
            status="operational",
            company=self.company,
        )
        peer = Machine.objects.create(
            name="Legacy Line Machine",
            code="LEG-01",
            category="",
            type="Shared Line",
            status="operational",
            company=self.company,
        )
        other = Machine.objects.create(
            name="CNC Machine",
            code="CNC-01",
            category="CNC",
            type="CNC",
            status="operational",
            company=self.company,
        )

        response = self.client.patch(
            reverse("machine-detail", args=[source.id]),
            data=encode_multipart(BOUNDARY, {
                "name": "Packing Machine",
                "code": "PACK-01",
                "category": "Packing",
                "status": "operational",
                "use_factory_shifts": "false",
                "apply_shift_to_category": "true",
                "shift_configuration": json.dumps({
                    "morning": {"enabled": True, "start": "07:30", "end": "15:30"},
                    "afternoon": {"enabled": False, "start": "15:30", "end": "23:30"},
                    "night": {"enabled": False, "start": "23:30", "end": "07:30"},
                }),
            }),
            content_type=MULTIPART_CONTENT,
        )

        self.assertEqual(response.status_code, 200, response.content.decode())
        source.refresh_from_db()
        peer.refresh_from_db()
        other.refresh_from_db()
        self.assertFalse(source.use_factory_shifts)
        self.assertFalse(peer.use_factory_shifts)
        self.assertEqual(peer.shift_configuration["morning"]["start"], "07:30")
        self.assertTrue(other.use_factory_shifts)
        self.assertEqual(other.shift_configuration, {})
        self.assertEqual(response.json()["shift_propagated_count"], 1)

    def test_machine_api_form_patch_preserves_blank_legacy_category_and_hidden_fields(self):
        machine = Machine.objects.create(
            name="Legacy Cutter",
            code="CUT-01",
            category="",
            type="Legacy Cutting",
            status="maintenance",
            maintenance_note="Pending bearing check",
            hourly_rate=Decimal("88.25"),
            company=self.company,
            use_factory_shifts=False,
            shift_configuration={
                "morning": {"enabled": True, "start": "08:00", "end": "16:00"},
                "afternoon": {"enabled": False, "start": "16:00", "end": "00:00"},
                "night": {"enabled": False, "start": "00:00", "end": "08:00"},
            },
        )

        response = self.client.patch(
            reverse("machine-detail", args=[machine.id]),
            data=encode_multipart(BOUNDARY, {
                "name": "Legacy Cutter Updated",
                "code": "CUT-01",
                "category": "",
                "status": "operational",
                "use_factory_shifts": "false",
                "shift_configuration": json.dumps({
                    "morning": {"enabled": True, "start": "09:00", "end": "17:00"},
                    "afternoon": {"enabled": False, "start": "17:00", "end": "01:00"},
                    "night": {"enabled": False, "start": "01:00", "end": "09:00"},
                }),
            }),
            content_type=MULTIPART_CONTENT,
        )

        self.assertEqual(response.status_code, 200, response.content.decode())
        machine.refresh_from_db()
        self.assertEqual(machine.name, "Legacy Cutter Updated")
        self.assertEqual(machine.category, "")
        self.assertEqual(machine.type, "Legacy Cutting")
        self.assertEqual(machine.maintenance_note, "Pending bearing check")
        self.assertEqual(machine.hourly_rate, Decimal("88.25"))
        self.assertEqual(machine.shift_configuration["morning"]["start"], "09:00")

    def test_machine_api_patch_can_skip_shift_category_propagation(self):
        source = Machine.objects.create(
            name="Packing Machine",
            code="PACK-01",
            category="Packing",
            type="Packing",
            status="operational",
            company=self.company,
        )
        peer = Machine.objects.create(
            name="Packing Machine 2",
            code="PACK-02",
            category="Packing",
            type="Packing",
            status="operational",
            company=self.company,
        )

        response = self.client.patch(
            reverse("machine-detail", args=[source.id]),
            data=json.dumps({
                "use_factory_shifts": False,
                "apply_shift_to_category": False,
                "shift_configuration": {
                    "morning": {"enabled": True, "start": "07:00", "end": "15:00"},
                    "afternoon": {"enabled": False, "start": "15:00", "end": "23:00"},
                    "night": {"enabled": False, "start": "23:00", "end": "07:00"},
                },
            }),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200, response.content.decode())
        source.refresh_from_db()
        peer.refresh_from_db()
        self.assertFalse(source.use_factory_shifts)
        self.assertTrue(peer.use_factory_shifts)
        self.assertEqual(peer.shift_configuration, {})
        self.assertEqual(response.json()["shift_propagated_count"], 0)

    def test_machine_api_patch_preserves_unsubmitted_machine_fields(self):
        machine = Machine.objects.create(
            name="Legacy Lathe",
            code="LAT-01",
            category="Lathe",
            type="Legacy Type",
            status="maintenance",
            maintenance_note="Bearing inspection",
            hourly_rate=Decimal("125.50"),
            company=self.company,
            use_factory_shifts=False,
            shift_configuration={
                "morning": {"enabled": True, "start": "08:00", "end": "16:00"},
                "afternoon": {"enabled": False, "start": "14:00", "end": "22:00"},
                "night": {"enabled": False, "start": "22:00", "end": "06:00"},
            },
        )

        response = self.client.patch(
            reverse("machine-detail", args=[machine.id]),
            data=json.dumps({
                "name": "Updated Lathe",
                "code": "LAT-01",
                "category": "Lathe",
                "status": "operational",
            }),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200, response.content.decode())
        machine.refresh_from_db()
        self.assertEqual(machine.name, "Updated Lathe")
        self.assertEqual(machine.type, "Legacy Type")
        self.assertEqual(machine.maintenance_note, "Bearing inspection")
        self.assertEqual(machine.hourly_rate, Decimal("125.50"))
        self.assertFalse(machine.use_factory_shifts)
        self.assertEqual(machine.shift_configuration["morning"]["start"], "08:00")

    def test_find_next_available_slot_rolls_to_next_machine_shift_window(self):
        machine = Machine.objects.create(
            name="Packing Machine",
            code="PACK-02",
            category="Packing",
            status="operational",
            company=self.company,
            use_factory_shifts=False,
            shift_configuration={
                "morning": {"enabled": True, "start": "08:00", "end": "16:00"},
                "afternoon": {"enabled": False, "start": "14:00", "end": "22:00"},
                "night": {"enabled": False, "start": "22:00", "end": "06:00"},
            },
        )

        start_after = timezone.make_aware(datetime(2026, 4, 7, 17, 0))
        slot_start, slot_end = WorkOrderService.find_next_available_slot(
            machine,
            duration_minutes=120,
            start_after=start_after,
        )

        self.assertEqual(timezone.localtime(slot_start).hour, 8)
        self.assertEqual(timezone.localtime(slot_start).day, 8)
        self.assertEqual(slot_end, slot_start + timedelta(minutes=120))

    def test_find_next_available_slot_spans_multiple_machine_shift_windows(self):
        machine = Machine.objects.create(
            name="Packing Machine",
            code="PACK-03",
            category="Packing",
            status="operational",
            company=self.company,
            use_factory_shifts=False,
            shift_configuration={
                "morning": {"enabled": True, "start": "08:00", "end": "16:00"},
                "afternoon": {"enabled": False, "start": "14:00", "end": "22:00"},
                "night": {"enabled": False, "start": "22:00", "end": "06:00"},
            },
        )

        start_after = timezone.make_aware(datetime(2026, 4, 7, 15, 0))
        slot_start, slot_end = WorkOrderService.find_next_available_slot(
            machine,
            duration_minutes=120,
            start_after=start_after,
        )

        self.assertEqual(timezone.localtime(slot_start), timezone.make_aware(datetime(2026, 4, 7, 15, 0)))
        self.assertEqual(timezone.localtime(slot_end), timezone.make_aware(datetime(2026, 4, 8, 9, 0)))

    def test_machine_inheriting_factory_evening_shift_uses_factory_hours(self):
        settings = SystemSettings.objects.create(
            company=self.company,
            shift_mode="2",
            shift_configuration={
                "morning": {"start": "06:00", "end": "10:00"},
                "evening": {"start": "10:00", "end": "14:00"},
                "night": {"start": "22:00", "end": "06:00"},
            },
        )
        self.company.system_settings = settings
        machine = Machine.objects.create(
            name="Inherited Machine",
            code="INH-01",
            category="Packing",
            status="operational",
            company=self.company,
            use_factory_shifts=True,
        )

        start_after = timezone.make_aware(datetime(2026, 4, 7, 10, 30))
        slot_start, slot_end = WorkOrderService.find_next_available_slot(
            machine,
            duration_minutes=120,
            start_after=start_after,
        )

        self.assertEqual(timezone.localtime(slot_start), timezone.make_aware(datetime(2026, 4, 7, 10, 30)))
        self.assertEqual(timezone.localtime(slot_end), timezone.make_aware(datetime(2026, 4, 7, 12, 30)))

    def test_machine_inheriting_factory_one_shift_rolls_to_next_day(self):
        settings = SystemSettings.objects.create(
            company=self.company,
            shift_mode="1",
            shift_configuration={
                "morning": {"start": "08:00", "end": "16:00", "enabled": True},
                "evening": {"start": "16:00", "end": "00:00", "enabled": False},
                "night": {"start": "00:00", "end": "08:00", "enabled": False},
            },
        )
        self.company.system_settings = settings
        machine = Machine.objects.create(
            name="Inherited Machine",
            code="INH-02",
            category="Packing",
            status="operational",
            company=self.company,
            use_factory_shifts=True,
        )

        start_after = timezone.make_aware(datetime(2026, 4, 7, 17, 0))
        slot_start, slot_end = WorkOrderService.find_next_available_slot(
            machine,
            duration_minutes=60,
            start_after=start_after,
        )

        self.assertEqual(timezone.localtime(slot_start), timezone.make_aware(datetime(2026, 4, 8, 8, 0)))
        self.assertEqual(timezone.localtime(slot_end), timezone.make_aware(datetime(2026, 4, 8, 9, 0)))

    def test_planner_dashboard_exposes_machine_shift_summary_and_effective_shift_config(self):
        machine = Machine.objects.create(
            name="Packing Machine",
            code="PACK-04",
            category="Packing",
            status="operational",
            company=self.company,
            use_factory_shifts=False,
            shift_configuration={
                "morning": {"enabled": True, "start": "08:00", "end": "16:00"},
                "afternoon": {"enabled": False, "start": "14:00", "end": "22:00"},
                "night": {"enabled": False, "start": "22:00", "end": "06:00"},
            },
        )

        context = DashboardService.get_dashboard_context(
            self.company,
            viewer_role="planner",
            viewer=self.planner,
        )
        machine_payload = next((item for item in context["machines_data"] if item["id"] == machine.id), None)

        self.assertIsNotNone(machine_payload)
        self.assertEqual(machine_payload["working_hours_summary"], "Morning 08:00-16:00")
        self.assertFalse(machine_payload["use_factory_shifts"])
        self.assertFalse(machine_payload["shift_configuration"]["afternoon"]["enabled"])

    def test_settings_shift_mode_update_saves_enabled_factory_shifts(self):
        response = self.client.post(
            reverse("settings_dashboard"),
            data={
                "action": "update_shifts",
                "shift_mode": "2",
                "morning_start": "08:00",
                "morning_end": "16:00",
                "evening_start": "16:00",
                "evening_end": "00:00",
                "night_start": "00:00",
                "night_end": "08:00",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        settings = SystemSettings.objects.get(company=self.company)
        self.assertEqual(settings.shift_mode, "2")
        self.assertTrue(settings.shift_configuration["morning"]["enabled"])
        self.assertTrue(settings.shift_configuration["evening"]["enabled"])
        self.assertFalse(settings.shift_configuration["night"]["enabled"])
        self.assertContains(response, 'value="2" selected')

    def test_factory_setup_edit_button_keeps_category_and_type_separate(self):
        Machine.objects.create(
            name="Legacy Cutter",
            code="CUT-01",
            category="",
            type="Legacy Cutting",
            status="operational",
            company=self.company,
        )

        response = self.client.get(reverse("factory_setup"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-machine-category=""')
        self.assertContains(response, 'data-machine-type="Legacy Cutting"')
