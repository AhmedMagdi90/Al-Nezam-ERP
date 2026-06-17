from pathlib import Path
from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils.text import slugify


class PlatformSettings(models.Model):
    singleton_key = models.CharField(max_length=32, unique=True, default="default", editable=False)
    subscription_currency = models.CharField(max_length=8, default="USD")
    base_monthly_price = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("149.00"))
    included_users = models.PositiveIntegerField(default=5)
    extra_user_monthly_price = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("11.00"))
    test_environment_monthly_price = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("49.00"))
    guided_onboarding_one_time_fee = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("240.00"))
    annual_discount_rate = models.DecimalField(
        max_digits=5,
        decimal_places=4,
        default=Decimal("0.1200"),
        help_text="Fractional discount rate. Example: 0.1200 = 12% annual discount.",
    )
    manual_quote_email = models.EmailField(default="sales@kemet-erp.com")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Platform Settings"
        verbose_name_plural = "Platform Settings"

    def __str__(self):
        return "Platform Settings"

    @classmethod
    def get_solo(cls):
        obj, _ = cls.objects.get_or_create(singleton_key="default")
        return obj

    @property
    def estimator_config(self) -> dict:
        return {
            "currency": self.subscription_currency,
            "baseMonthly": float(self.base_monthly_price),
            "includedUsers": int(self.included_users),
            "extraUserMonthly": float(self.extra_user_monthly_price),
            "testEnvironmentMonthly": float(self.test_environment_monthly_price),
            "guidedOnboardingOneTime": float(self.guided_onboarding_one_time_fee),
            "annualDiscountRate": float(self.annual_discount_rate),
            "quoteEmail": self.manual_quote_email,
        }


class Organization(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        ACTIVE = "active", "Active"
        SUSPENDED = "suspended", "Suspended"

    name = models.CharField(max_length=200)
    slug = models.SlugField(max_length=64, unique=True, help_text="Stable organization key, e.g. al-nour")
    owner_email = models.EmailField(
        max_length=254,
        unique=True,
        null=True,
        blank=True,
        help_text="Globally unique organization owner email used at registration/login onboarding.",
    )
    support_notes = models.TextField(blank=True, default="")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.ACTIVE)
    seat_limit = models.PositiveIntegerField(default=1)
    wants_test_environment = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.slug})"

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)
        elif self.slug:
            self.slug = slugify(self.slug)
        if self.owner_email:
            self.owner_email = self.owner_email.strip().lower()
        super().save(*args, **kwargs)


class SupportActionLog(models.Model):
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="support_action_logs",
    )
    actor_email = models.EmailField(blank=True, default="")
    action_type = models.CharField(max_length=64)
    target_label = models.CharField(max_length=255, blank=True, default="")
    notes = models.TextField(blank=True, default="")
    metadata = models.JSONField(blank=True, default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"{self.organization.slug}:{self.action_type}@{self.created_at:%Y-%m-%d %H:%M:%S}"


class Tenant(models.Model):
    """
    Control-plane tenant registry.
    Each tenant maps to a dedicated database alias and database file path.
    """

    class EnvironmentType(models.TextChoices):
        DEMO = "demo", "Demo"
        LIVE = "live", "Live"
        TEST = "test", "Test"
        DEV = "dev", "Dev"

    name = models.CharField(max_length=200)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="tenants",
        null=True,
        blank=True,
    )
    owner_email = models.EmailField(
        max_length=254,
        null=True,
        blank=True,
        help_text="Legacy owner email retained for backward compatibility.",
    )
    code = models.SlugField(max_length=64, unique=True, help_text="Stable tenant key, e.g. al-nour")
    environment_type = models.CharField(max_length=16, choices=EnvironmentType.choices, default=EnvironmentType.LIVE)
    hostname = models.CharField(
        max_length=255,
        null=True,
        blank=True,
        unique=True,
        help_text="Optional hostname used for host-based tenant resolution, e.g. al-nour.nezam.com",
    )
    is_primary = models.BooleanField(default=False)
    db_alias = models.CharField(max_length=64, unique=True, help_text="Django DB alias, e.g. tenant_al_nour")
    db_name = models.CharField(
        max_length=255,
        help_text="Tenant database entry: PostgreSQL URL or legacy SQLite path.",
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.code})"

    def save(self, *args, **kwargs):
        if not self.code:
            self.code = slugify(self.name)
        if not self.db_alias:
            self.db_alias = f"tenant_{self.code.replace('-', '_')}"
        if not self.environment_type:
            self.environment_type = self.EnvironmentType.LIVE
        if self.owner_email:
            self.owner_email = self.owner_email.strip().lower()
        if not self.hostname:
            self.hostname = None
        if self.organization_id and self.environment_type == self.EnvironmentType.LIVE and not self.is_primary:
            self.is_primary = True
        if not self.db_name:
            self.db_name = f"tenant_dbs/{self.code}.sqlite3"
        super().save(*args, **kwargs)

    @property
    def resolved_db_path(self) -> Path:
        path = Path(self.db_name)
        if path.is_absolute():
            return path
        return Path(settings.BASE_DIR) / path

    @property
    def organization_slug(self) -> str:
        if self.organization_id and self.organization:
            return self.organization.slug
        return self.code
