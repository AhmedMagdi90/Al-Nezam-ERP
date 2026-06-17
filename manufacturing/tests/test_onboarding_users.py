from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from accounts.models import Profile
from accounts.constants import RoleType
from manufacturing.tests.utils import create_company, create_user_with_role
from tenancy.models import Tenant


class OnboardingUsersTests(TestCase):
    def setUp(self):
        self.company = create_company("Launch Team Co")
        self.owner = create_user_with_role("owner@example.com", RoleType.ADMIN.value, self.company)
        self.client.force_login(self.owner)
        Tenant.objects.create(
            name="Launch Team",
            code="launch-team",
            db_alias="default",
            db_name="db.sqlite3",
        )
        session = self.client.session
        session["tenant_code"] = "launch-team"
        session.save()

    def test_post_creates_launch_team_members(self):
        session = self.client.session
        session["first_time_company_setup"] = True
        session.save()

        response = self.client.post(
            reverse("onboarding_users"),
            data={
                "name_planner": "Production Planner",
                "email_planner": "planner@launch.test",
                "name_supervisor": "Line Supervisor",
                "email_supervisor": "supervisor@launch.test",
                "name_worker": "Shop Worker",
                "email_worker": "worker@launch.test",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("planner_dashboard"))
        self.assertFalse(self.client.session["first_time_company_setup"])

        expected_roles = {
            "planner@launch.test": RoleType.PLANNER.value,
            "supervisor@launch.test": RoleType.SUPERVISOR.value,
            "worker@launch.test": RoleType.WORKER.value,
        }
        for email, role_name in expected_roles.items():
            user = User.objects.get(username=email)
            profile = Profile.objects.get(user=user)
            self.assertEqual(user.email, email)
            self.assertTrue(user.check_password("Password123!"))
            self.assertEqual(profile.company, self.company)
            self.assertEqual(profile.role.name, role_name)
            self.assertEqual(profile.app_scope, "manufacturing")

    def test_post_attaches_existing_user_to_launch_team_role(self):
        existing = User.objects.create_user(
            username="existing.supervisor@launch.test",
            email="existing.supervisor@launch.test",
            password="old-password",
        )

        response = self.client.post(
            reverse("onboarding_users"),
            data={
                "name_supervisor": "Existing Supervisor",
                "email_supervisor": "existing.supervisor@launch.test",
            },
        )

        self.assertEqual(response.status_code, 302)
        existing.refresh_from_db()
        profile = Profile.objects.get(user=existing)
        self.assertEqual(profile.company, self.company)
        self.assertEqual(profile.role.name, RoleType.SUPERVISOR.value)
        self.assertEqual(profile.app_scope, "manufacturing")

    def test_post_without_team_keeps_user_on_team_setup(self):
        session = self.client.session
        session["first_time_company_setup"] = True
        session.save()

        response = self.client.post(reverse("onboarding_users"), data={})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("onboarding_users"))
        self.assertTrue(self.client.session["first_time_company_setup"])

    def test_skip_team_setup_explicitly_opens_planner(self):
        session = self.client.session
        session["first_time_company_setup"] = True
        session.save()

        response = self.client.post(
            reverse("onboarding_users"),
            data={"skip_team_setup": "1"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("planner_dashboard"))
        self.assertFalse(self.client.session["first_time_company_setup"])
