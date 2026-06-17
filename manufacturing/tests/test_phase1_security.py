from django.conf import settings
from django.test import SimpleTestCase, override_settings
from django.urls import Resolver404, resolve


class Phase1SecurityTests(SimpleTestCase):
    def assert_url_does_not_resolve(self, path):
        with self.assertRaises(Resolver404):
            resolve(path)

    def test_removed_debug_routes_are_not_registered(self):
        self.assert_url_does_not_resolve("/manufacturing/debug-wos/")
        self.assert_url_does_not_resolve("/manufacturing/debug-delete-wos/")
        self.assert_url_does_not_resolve("/debug/shift-config/")

    @override_settings(DEBUG=True)
    def test_removed_debug_endpoints_return_404(self):
        for path in (
            "/manufacturing/debug-wos/",
            "/manufacturing/debug-delete-wos/",
            "/debug/shift-config/",
        ):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 404)

    def test_csrf_cookie_is_httponly(self):
        self.assertTrue(settings.CSRF_COOKIE_HTTPONLY)

    def test_session_cookie_is_httponly(self):
        self.assertTrue(settings.SESSION_COOKIE_HTTPONLY)
