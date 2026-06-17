from django.contrib.auth import get_user_model
from django.contrib.auth.backends import BaseBackend
from django.conf import settings

from .context import get_current_tenant_db


class TenantModelBackend(BaseBackend):
    """
    Authenticate users against the active tenant database.
    """

    def _allow_default_db(self):
        """
        In local/test runs we may authenticate against default DB when tenant DB
        context is not set yet (e.g. force_login in Django tests).
        """
        return bool(getattr(settings, "TENANCY_ALLOW_TENANT_APPS_ON_DEFAULT", False))

    def authenticate(self, request, username=None, password=None, tenant_db_alias=None, **kwargs):
        user_model = get_user_model()
        if username is None:
            username = kwargs.get(user_model.USERNAME_FIELD)
        if username is None or password is None:
            return None

        alias = tenant_db_alias or getattr(request, "tenant_db_alias", None) or get_current_tenant_db()
        if not alias:
            return None
        if alias == "default" and not self._allow_default_db():
            return None

        try:
            user = user_model._default_manager.db_manager(alias).get_by_natural_key(username)
        except user_model.DoesNotExist:
            return None

        if user.check_password(password) and self.user_can_authenticate(user):
            user.backend = "tenancy.auth_backend.TenantModelBackend"
            return user
        return None

    def get_user(self, user_id):
        user_model = get_user_model()
        alias = get_current_tenant_db()
        if not alias:
            return None
        if alias == "default" and not self._allow_default_db():
            return None
        try:
            return user_model._default_manager.db_manager(alias).get(pk=user_id)
        except user_model.DoesNotExist:
            return None

    def user_can_authenticate(self, user):
        is_active = getattr(user, "is_active", None)
        return is_active or is_active is None
