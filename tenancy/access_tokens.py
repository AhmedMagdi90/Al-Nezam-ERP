from django.contrib.auth.tokens import PasswordResetTokenGenerator


class TenantAccessTokenGenerator(PasswordResetTokenGenerator):
    """
    Tenant setup/reset tokens should survive the first auto-login after signup.
    Password changes still invalidate old links.
    """

    def _make_hash_value(self, user, timestamp):
        email = getattr(user, "email", "") or ""
        return f"{user.pk}{user.password}{timestamp}{email}{user.is_active}"


tenant_access_token_generator = TenantAccessTokenGenerator()
