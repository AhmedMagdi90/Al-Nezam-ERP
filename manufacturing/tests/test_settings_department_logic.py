from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.test import override_settings
from django.urls import reverse
from django.contrib.messages import get_messages
import tempfile

from accounts.models import Profile, Role
from accounts.constants import RoleType
from manufacturing.models import SystemSettings
from manufacturing.tests.utils import create_company, create_user_with_role


class SettingsDepartmentLogicTests(TestCase):
    def setUp(self):
        self.company = create_company("Settings Co")
        self.admin = create_user_with_role("settings_admin", "admin", self.company)
        self.client.force_login(self.admin)
        self.url = reverse("settings_dashboard")
        self.settings, _ = SystemSettings.objects.get_or_create(company=self.company)

    def test_department_options_start_empty_without_hardcoded_defaults(self):
        response = self.client.get(f"{self.url}?tab=members&scope=planner")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["department_options"], [])

    def test_create_member_preview_uses_draft_empty_state_copy(self):
        response = self.client.get(f"{self.url}?tab=members&scope=planner")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Draft Preview")
        self.assertContains(response, "Enter first and last name")
        self.assertContains(response, "Add work email or phone")
        self.assertContains(response, "Contact details added")
        self.assertContains(response, "Generated after save")
        self.assertContains(response, 'autocomplete="new-password"')
        self.assertContains(response, "clearInviteDraft()")
        self.assertContains(response, "invitePasswordInput")
        self.assertContains(response, "inviteConfirmPasswordInput")
        self.assertNotContains(response, "New Team Member")
        self.assertNotContains(response, "email@company.com or +201234567890")

    def test_team_tab_uses_action_focused_member_command_center(self):
        response = self.client.get(f"{self.url}?tab=members&scope=planner")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-team-command-center')
        self.assertContains(response, 'x-model.debounce.150ms="teamSearch"')
        self.assertContains(response, 'x-model="teamRoleFilter"')
        self.assertContains(response, 'x-model="teamStatusFilter"')
        self.assertContains(response, 'data-team-stats-row')
        self.assertContains(response, 'data-team-member-row')
        self.assertContains(response, 'data-team-command-center class="rounded-[24px] border border-slate-200 bg-white shadow-sm"')
        self.assertContains(response, 'data-team-department-directory class="hidden grid-cols-1 gap-4"')
        page_html = response.content.decode()
        self.assertLess(page_html.index("data-team-command-center"), page_html.index("data-team-stats-row"))
        self.assertLess(page_html.index("data-team-command-center"), page_html.index("data-team-department-directory"))
        self.assertNotContains(response, "{% tenant_trans")
        self.assertContains(response, ">Team members</h2>")
        self.assertNotContains(response, ">Members &amp; departments</h2>")
        self.assertNotContains(response, "Members Location")
        self.assertContains(response, "Total Members")
        self.assertContains(response, "Add first member")
        self.assertContains(response, "Showing")
        self.assertContains(response, "Role &amp; Shift")
        self.assertContains(response, "Password")
        self.assertContains(response, "Delete member")
        self.assertContains(response, 'name="action" value="delete_user"')
        self.assertNotContains(response, "Actions (Visible on Hover)")

    def test_admin_can_invite_planner_without_selecting_departments(self):
        planner_role, _ = Role.objects.get_or_create(name=RoleType.PLANNER.value)

        response = self.client.post(
            self.url,
            data={
                "action": "invite_user",
                "scope": "planner",
                "first_name": "Main",
                "last_name": "Planner",
                "email": "main.planner@example.com",
                "role": str(planner_role.id),
                "password": "N3zam!InviteSeed942",
                "confirm_password": "N3zam!InviteSeed942",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        planner = User.objects.get(email="main.planner@example.com")
        planner_profile = Profile.objects.get(user=planner)
        self.assertEqual(planner_profile.role.name, RoleType.PLANNER.value)
        self.assertFalse(planner_profile.department)

    def test_admin_can_invite_store_without_selecting_departments(self):
        store_role, _ = Role.objects.get_or_create(name=RoleType.STORE.value)

        response = self.client.post(
            self.url,
            data={
                "action": "invite_user",
                "scope": "planner",
                "first_name": "Store",
                "last_name": "Keeper",
                "email": "store.keeper@example.com",
                "role": str(store_role.id),
                "password": "N3zam!InviteSeed942",
                "confirm_password": "N3zam!InviteSeed942",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        store_user = User.objects.get(email="store.keeper@example.com")
        store_profile = Profile.objects.get(user=store_user)
        self.assertEqual(store_profile.role.name, RoleType.STORE.value)
        self.assertFalse(store_profile.department)

    def test_planner_cannot_invite_store_keeper(self):
        planner = create_user_with_role("settings_inviter_planner", "planner", self.company)
        planner_client = self.client_class()
        planner_client.force_login(planner)
        store_role, _ = Role.objects.get_or_create(name=RoleType.STORE.value)

        response = planner_client.post(
            self.url,
            data={
                "action": "invite_user",
                "scope": "planner",
                "first_name": "Blocked",
                "last_name": "Store",
                "email": "blocked.store@example.com",
                "role": str(store_role.id),
                "password": "N3zam!InviteSeed942",
                "confirm_password": "N3zam!InviteSeed942",
            },
            follow=False,
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(User.objects.filter(email="blocked.store@example.com").exists())

    def test_supervisor_invite_requires_created_departments(self):
        supervisor_role, _ = Role.objects.get_or_create(name=RoleType.SUPERVISOR.value)

        response = self.client.post(
            self.url,
            data={
                "action": "invite_user",
                "scope": "planner",
                "first_name": "Line",
                "last_name": "Supervisor",
                "email": "line.supervisor@example.com",
                "role": str(supervisor_role.id),
                "password": "N3zam!InviteSeed942",
                "confirm_password": "N3zam!InviteSeed942",
            },
            follow=False,
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn("member_modal=invite", response.url)
        self.assertIn("member_step=2", response.url)
        self.assertIn("invite_error=", response.url)
        self.assertFalse(User.objects.filter(email="line.supervisor@example.com").exists())

    def test_planner_receives_all_created_departments_automatically(self):
        self.settings.department_catalog = {"planner": ["Cutting", "Assembly"]}
        self.settings.save(update_fields=["department_catalog"])
        planner_role, _ = Role.objects.get_or_create(name=RoleType.PLANNER.value)

        self.client.post(
            self.url,
            data={
                "action": "invite_user",
                "scope": "planner",
                "first_name": "Area",
                "last_name": "Planner",
                "email": "area.planner@example.com",
                "role": str(planner_role.id),
                "password": "N3zam!InviteSeed942",
                "confirm_password": "N3zam!InviteSeed942",
            },
            follow=True,
        )

        planner = User.objects.get(email="area.planner@example.com")
        planner_profile = Profile.objects.get(user=planner)
        self.assertEqual(planner_profile.department, "Cutting\nAssembly")

    def test_admin_can_invite_member_with_phone_only(self):
        planner_role, _ = Role.objects.get_or_create(name=RoleType.PLANNER.value)

        response = self.client.post(
            self.url,
            data={
                "action": "invite_user",
                "scope": "planner",
                "first_name": "Phone",
                "last_name": "Only",
                "phone": "+20 100 123 4567",
                "role": str(planner_role.id),
                "password": "N3zam!InviteSeed942",
                "confirm_password": "N3zam!InviteSeed942",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        invited = User.objects.get(username="+201001234567")
        invited_profile = Profile.objects.get(user=invited)
        self.assertEqual(invited.email, "")
        self.assertEqual(invited_profile.phone, "+201001234567")

    def test_company_logo_upload_persists_from_settings_profile_form(self):
        with tempfile.TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=media_root):
                upload = SimpleUploadedFile(
                    "logo.png",
                    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR",
                    content_type="image/png",
                )

                response = self.client.post(
                    self.url,
                    data={
                        "action": "update_profile",
                        "company_name": "Settings Co",
                        "company_logo": upload,
                    },
                    follow=True,
                )

                self.assertEqual(response.status_code, 200)
                self.company.refresh_from_db()
                self.assertTrue(self.company.logo.name.startswith("company_logos/"))
                self.assertIn("logo", self.company.logo.name)

    def test_update_member_accepts_phone_without_email(self):
        planner_role, _ = Role.objects.get_or_create(name=RoleType.PLANNER.value)
        member = User.objects.create_user(username="edit_target", email="old@example.com", password="Password1!")
        profile, _ = Profile.objects.get_or_create(user=member)
        profile.company = self.company
        profile.role = planner_role
        profile.app_scope = "planner"
        profile.save()

        response = self.client.post(
            self.url,
            data={
                "action": "update_member",
                "scope": "planner",
                "member_id": str(member.id),
                "first_name": "Edited",
                "last_name": "Member",
                "email": "",
                "phone": "01001234567",
                "role": str(planner_role.id),
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        member.refresh_from_db()
        profile.refresh_from_db()
        self.assertEqual(member.email, "")
        self.assertEqual(profile.phone, "01001234567")

    def test_admin_can_reset_another_members_password(self):
        planner_role, _ = Role.objects.get_or_create(name=RoleType.PLANNER.value)
        member = User.objects.create_user(username="reset_target", email="reset@example.com", password="OldPassword1!")
        profile, _ = Profile.objects.get_or_create(user=member)
        profile.company = self.company
        profile.role = planner_role
        profile.app_scope = "planner"
        profile.save()

        response = self.client.post(
            self.url,
            data={
                "action": "reset_member_password",
                "scope": "planner",
                "member_id": str(member.id),
                "new_password": "N3zam!ResetSeed942",
                "confirm_password": "N3zam!ResetSeed942",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        member.refresh_from_db()
        self.assertTrue(member.check_password("N3zam!ResetSeed942"))

    def test_admin_can_delete_team_member_from_settings(self):
        worker_role, _ = Role.objects.get_or_create(name=RoleType.WORKER.value)
        member = User.objects.create_user(username="delete_target", email="delete@example.com", password="OldPassword1!")
        profile, _ = Profile.objects.get_or_create(user=member)
        profile.company = self.company
        profile.role = worker_role
        profile.app_scope = "planner"
        profile.save()

        response = self.client.post(
            self.url,
            data={
                "action": "delete_user",
                "scope": "planner",
                "user_id": str(member.id),
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(pk=member.id).exists())
        messages = [message.message for message in get_messages(response.wsgi_request)]
        self.assertIn("User removed from company.", messages)

    def test_non_admin_cannot_reset_member_password(self):
        planner = create_user_with_role("settings_planner", "planner", self.company)
        planner_client = self.client_class()
        planner_client.force_login(planner)
        worker_role, _ = Role.objects.get_or_create(name=RoleType.WORKER.value)
        member = User.objects.create_user(username="worker_reset_target", email="workerreset@example.com", password="OldPassword1!")
        profile, _ = Profile.objects.get_or_create(user=member)
        profile.company = self.company
        profile.role = worker_role
        profile.app_scope = "planner"
        profile.save()

        response = planner_client.post(
            self.url,
            data={
                "action": "reset_member_password",
                "scope": "planner",
                "member_id": str(member.id),
                "new_password": "N3zam!ResetSeed942",
                "confirm_password": "N3zam!ResetSeed942",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        member.refresh_from_db()
        self.assertTrue(member.check_password("OldPassword1!"))
        messages = [message.message for message in get_messages(response.wsgi_request)]
        self.assertIn("Only admins can reset other users' passwords.", messages)
