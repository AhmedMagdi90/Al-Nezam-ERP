from django.test import Client


_original_force_login = Client.force_login


def _force_login_with_model_backend(self, user, backend=None):
    """
    Keep test authentication deterministic across environments.

    Some CI/server environments still resolve ``force_login()`` through the
    tenant backend first, which makes request.user anonymous unless a tenant
    context is pre-seeded on the session. These suites exercise planner and
    supervisor APIs against the default test database, so ModelBackend is the
    correct default for tests.
    """

    return _original_force_login(
        self,
        user,
        backend=backend or "django.contrib.auth.backends.ModelBackend",
    )


Client.force_login = _force_login_with_model_backend
