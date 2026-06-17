from django.contrib import admin

from .models import Organization, PlatformSettings, SupportActionLog, Tenant


@admin.register(PlatformSettings)
class PlatformSettingsAdmin(admin.ModelAdmin):
    list_display = (
        "subscription_currency",
        "base_monthly_price",
        "included_users",
        "extra_user_monthly_price",
        "test_environment_monthly_price",
        "guided_onboarding_one_time_fee",
        "annual_discount_rate",
        "manual_quote_email",
        "updated_at",
    )

    def has_add_permission(self, request):
        if PlatformSettings.objects.exists():
            return False
        return super().has_add_permission(request)

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Organization)
class OrganizationAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "owner_email", "status", "seat_limit", "wants_test_environment", "created_at")
    search_fields = ("name", "slug", "owner_email")
    list_filter = ("status", "wants_test_environment")


@admin.register(Tenant)
class TenantAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "organization",
        "code",
        "environment_type",
        "hostname",
        "owner_email",
        "db_alias",
        "is_active",
        "created_at",
    )
    search_fields = ("name", "organization__name", "organization__slug", "code", "owner_email", "db_alias", "hostname")
    list_filter = ("environment_type", "is_active", "is_primary")


@admin.register(SupportActionLog)
class SupportActionLogAdmin(admin.ModelAdmin):
    list_display = ("organization", "action_type", "target_label", "actor_email", "created_at")
    search_fields = ("organization__name", "organization__slug", "action_type", "target_label", "actor_email", "notes")
    list_filter = ("action_type", "created_at")
