from __future__ import annotations

import logging

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string

from .db import ensure_tenant_database_registered
from .demo_seed import DEMO_LOGIN_PASSWORD, get_demo_login_accounts
from .models import Organization, Tenant
from .portal_urls import (
    direct_login_url,
    environment_access_setup_url,
    portal_environment_url,
    portal_login_url,
    tenant_app_url,
)

logger = logging.getLogger(__name__)


ENVIRONMENT_ORDER = {
    Tenant.EnvironmentType.DEMO: 0,
    Tenant.EnvironmentType.TEST: 1,
    Tenant.EnvironmentType.LIVE: 2,
    Tenant.EnvironmentType.DEV: 3,
}

ENVIRONMENT_LABELS = {
    Tenant.EnvironmentType.DEMO: "Demo Server",
    Tenant.EnvironmentType.TEST: "Test Server",
    Tenant.EnvironmentType.LIVE: "Live Server",
    Tenant.EnvironmentType.DEV: "Dev Server",
}

ENVIRONMENT_DESCRIPTIONS = {
    Tenant.EnvironmentType.DEMO: "Seeded workspace for guided product evaluation with sample data and role-based users.",
    Tenant.EnvironmentType.TEST: "Customer validation space for onboarding, training, and sign-off before go-live.",
    Tenant.EnvironmentType.LIVE: "Production workspace for real planning, supervision, execution, and reporting.",
    Tenant.EnvironmentType.DEV: "Internal engineering and support environment for preview and troubleshooting work.",
}

def _email_delivery_configured() -> bool:
    backend = getattr(settings, "EMAIL_BACKEND", "")
    if backend != "django.core.mail.backends.smtp.EmailBackend":
        return True

    return bool(
        getattr(settings, "EMAIL_HOST", "")
        and getattr(settings, "EMAIL_HOST_USER", "")
        and getattr(settings, "EMAIL_HOST_PASSWORD", "")
    )


def _tenant_owner_user(tenant: Tenant, owner_email: str):
    owner_email = (owner_email or "").strip().lower()
    if not owner_email:
        return None

    tenant_db_alias = ensure_tenant_database_registered(tenant)
    User = get_user_model()
    user = User.objects.using(tenant_db_alias).filter(username__iexact=owner_email).first()
    if user is None:
        user = User.objects.using(tenant_db_alias).filter(email__iexact=owner_email).first()
    return user


def build_environment_access_context(organization: Organization) -> dict:
    tenants = list(
        Tenant.objects.using("default")
        .filter(organization=organization, is_active=True)
        .order_by("created_at")
    )
    tenants.sort(key=lambda tenant: (ENVIRONMENT_ORDER.get(tenant.environment_type, 99), tenant.created_at))

    environment_entries = []
    demo_accounts = {}
    for tenant in tenants:
        if tenant.environment_type == Tenant.EnvironmentType.DEMO and not demo_accounts:
            demo_accounts = get_demo_login_accounts(organization.name)
        owner_user = _tenant_owner_user(tenant, organization.owner_email)

        environment_entries.append(
            {
                "tenant_code": tenant.code,
                "label": ENVIRONMENT_LABELS.get(tenant.environment_type, tenant.get_environment_type_display()),
                "description": ENVIRONMENT_DESCRIPTIONS.get(tenant.environment_type, ""),
                "environment_type": tenant.environment_type,
                "hostname": tenant.hostname,
                "login_url": direct_login_url(tenant),
                "app_url": tenant_app_url(tenant),
                "setup_url": environment_access_setup_url(tenant, owner_user) if owner_user else "",
            }
        )

    return {
        "organization": organization,
        "recipient_email": organization.owner_email,
        "portal_login_url": portal_login_url(),
        "portal_environment_url": portal_environment_url(),
        "environment_entries": environment_entries,
        "demo_accounts": demo_accounts,
        "demo_password": DEMO_LOGIN_PASSWORD if demo_accounts else "",
    }


def send_environment_access_email(organization: Organization, recipient_email: str | None = None) -> bool:
    if not organization:
        return False

    recipient = (recipient_email or organization.owner_email or "").strip().lower()
    if not recipient:
        logger.warning("Skipping environment access email because no recipient email is available for org=%s", organization.slug)
        return False

    if not _email_delivery_configured():
        logger.info("Skipping environment access email because email delivery is not configured for org=%s", organization.slug)
        return False

    context = build_environment_access_context(organization)
    context["recipient_email"] = recipient

    subject = f"Nezam access details for {organization.name}"
    text_body = render_to_string("emails/tenant_environment_access.txt", context)
    html_body = render_to_string("emails/tenant_environment_access.html", context)

    message = EmailMultiAlternatives(
        subject=subject,
        body=text_body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[recipient],
    )
    if html_body.strip():
        message.attach_alternative(html_body, "text/html")
    message.send(fail_silently=False)
    return True
