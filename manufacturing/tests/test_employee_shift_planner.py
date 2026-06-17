from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import Profile
from manufacturing.models import EmployeeShiftChangeLog, SystemSettings
from manufacturing.tests.utils import create_company, create_user_with_role


class EmployeeShiftPlannerTests(TestCase):
    def setUp(self):
        self.company = create_company("Shift Co")
        self.admin = create_user_with_role("shift_admin", "admin", self.company)
        self.worker = create_user_with_role("shift_worker", "worker", self.company)
        self.supervisor = create_user_with_role("shift_supervisor", "supervisor", self.company)

        self.worker.profile.department = "Assembly"
        self.worker.profile.shift = "morning"
        self.worker.profile.save(update_fields=["department", "shift"])

        self.supervisor.profile.department = "Assembly"
        self.supervisor.profile.shift = "night"
        self.supervisor.profile.save(update_fields=["department", "shift"])

        self.settings, _ = SystemSettings.objects.get_or_create(company=self.company)
        self.settings.department_catalog = {"planner": ["Assembly"]}
        self.settings.save(update_fields=["department_catalog"])

        self.url = reverse("employee_shift_planner")
        self.client.force_login(self.admin)

    def test_shift_planner_lists_workers_and_supervisors(self):
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        listed_ids = {row["user"].id for row in response.context["employee_rows"]}
        self.assertIn(self.worker.id, listed_ids)
        self.assertIn(self.supervisor.id, listed_ids)

    def test_bulk_assign_sets_planned_shift_start_date_and_audit_log(self):
        start_date = timezone.localdate() + timedelta(days=3)

        response = self.client.post(
            self.url,
            data={
                "bulk_action": "assign",
                "employee_ids": [str(self.worker.id), str(self.supervisor.id)],
                "new_shift": "evening",
                "start_date": start_date.isoformat(),
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.worker.profile.refresh_from_db()
        self.supervisor.profile.refresh_from_db()
        self.assertEqual(self.worker.profile.planned_shift, "evening")
        self.assertEqual(self.worker.profile.planned_shift_start_date, start_date)
        self.assertEqual(self.supervisor.profile.planned_shift, "evening")
        self.assertEqual(
            EmployeeShiftChangeLog.objects.filter(company=self.company, action="assign").count(),
            2,
        )

    def test_drag_drop_assign_accepts_csv_employee_ids(self):
        start_date = timezone.localdate() + timedelta(days=2)

        response = self.client.post(
            self.url,
            data={
                "bulk_action": "assign",
                "employee_ids_csv": f"{self.worker.id},{self.supervisor.id}",
                "new_shift": "evening",
                "start_date": start_date.isoformat(),
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.worker.profile.refresh_from_db()
        self.supervisor.profile.refresh_from_db()
        self.assertEqual(self.worker.profile.planned_shift, "evening")
        self.assertEqual(self.supervisor.profile.planned_shift, "evening")
        self.assertEqual(
            EmployeeShiftChangeLog.objects.filter(company=self.company, action="assign").count(),
            2,
        )

    def test_bulk_swap_uses_current_active_shift_pair(self):
        start_date = timezone.localdate() + timedelta(days=5)

        response = self.client.post(
            self.url,
            data={
                "bulk_action": "swap",
                "employee_ids": [str(self.worker.id), str(self.supervisor.id)],
                "from_shift": "morning",
                "to_shift": "night",
                "start_date": start_date.isoformat(),
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        worker_profile = Profile.objects.get(user=self.worker)
        supervisor_profile = Profile.objects.get(user=self.supervisor)
        self.assertEqual(worker_profile.planned_shift, "night")
        self.assertEqual(supervisor_profile.planned_shift, "morning")
        self.assertEqual(
            EmployeeShiftChangeLog.objects.filter(company=self.company, action="swap").count(),
            2,
        )

    def test_start_date_cannot_be_in_past(self):
        response = self.client.post(
            self.url,
            data={
                "bulk_action": "assign",
                "employee_ids": [str(self.worker.id)],
                "new_shift": "evening",
                "start_date": (timezone.localdate() - timedelta(days=1)).isoformat(),
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.worker.profile.refresh_from_db()
        self.assertIsNone(self.worker.profile.planned_shift)
        self.assertFalse(EmployeeShiftChangeLog.objects.filter(company=self.company).exists())
