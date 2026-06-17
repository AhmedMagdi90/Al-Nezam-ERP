from django.conf import settings

from .context import get_current_tenant_db


class TenantDatabaseRouter:
    """
    Routes tenant app models to the current tenant DB alias from context.
    Control-plane tenancy models always stay on default.
    """

    def __init__(self):
        self.default_alias = "default"

    @property
    def tenant_apps(self):
        return set(getattr(settings, "TENANCY_TENANT_APPS", []))

    @property
    def shared_apps(self):
        return set(getattr(settings, "TENANCY_SHARED_APPS", ["tenancy"]))

    def _is_shared_model(self, model):
        return model._meta.app_label in self.shared_apps

    def _is_tenant_model(self, model):
        return model._meta.app_label in self.tenant_apps

    def db_for_read(self, model, **hints):
        if self._is_shared_model(model):
            return self.default_alias
        if self._is_tenant_model(model):
            return get_current_tenant_db()
        return None

    def db_for_write(self, model, **hints):
        if self._is_shared_model(model):
            return self.default_alias
        if self._is_tenant_model(model):
            return get_current_tenant_db()
        return None

    def allow_relation(self, obj1, obj2, **hints):
        db1 = obj1._state.db
        db2 = obj2._state.db
        if db1 and db2 and db1 == db2:
            return True

        # Reject cross-DB relations involving tenant data.
        if obj1._meta.app_label in self.tenant_apps or obj2._meta.app_label in self.tenant_apps:
            return False
        return None

    def allow_migrate(self, db, app_label, model_name=None, **hints):
        if app_label in self.shared_apps:
            return db == self.default_alias

        if app_label in self.tenant_apps:
            if db == self.default_alias:
                return bool(getattr(settings, "TENANCY_ALLOW_TENANT_APPS_ON_DEFAULT", True))
            return True

        # Keep non-tenant apps in default DB only.
        return db == self.default_alias
