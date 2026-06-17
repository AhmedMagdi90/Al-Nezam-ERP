from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse


class PublicSecuritySurfaceTests(TestCase):
    def test_api_schema_requires_staff_login(self):
        response = self.client.get(reverse("schema"))

        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login/", response["Location"])

    def test_api_docs_requires_staff_login(self):
        response = self.client.get(reverse("swagger-ui"))

        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login/", response["Location"])

    def test_staff_can_access_api_schema(self):
        User.objects.create_user(
            username="staff",
            password="StrongPass123!",
            is_staff=True,
        )
        self.client.login(username="staff", password="StrongPass123!")

        response = self.client.get(reverse("schema"))

        self.assertEqual(response.status_code, 200)


class CsrfEnforcementTests(TestCase):
    def test_authenticated_mutation_endpoint_rejects_missing_csrf_token(self):
        client = Client(enforce_csrf_checks=True)
        user = User.objects.create_user(username="planner", password="StrongPass123!")
        client.force_login(user)

        response = client.post(
            reverse("api_planner_undo"),
            data="{}",
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 403)
