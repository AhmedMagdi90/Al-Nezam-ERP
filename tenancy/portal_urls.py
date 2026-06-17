from __future__ import annotations

import os

from django.urls import reverse
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from tenancy.access_tokens import tenant_access_token_generator


def _clean_base_url(value: str) -> str:
    return (value or "").strip().rstrip("/")


def portal_base_url() -> str:
    explicit = _clean_base_url(os.getenv("TENANT_PORTAL_URL", ""))
    if explicit:
        return explicit

    public_base = _clean_base_url(os.getenv("PUBLIC_WEB_URL", ""))
    if public_base:
        return public_base

    base_domain = (os.getenv("TENANT_BASE_DOMAIN", "") or "").strip().lower().lstrip(".")
    if base_domain:
        return f"https://portal.{base_domain}"

    return "http://localhost:8000"


def portal_login_url(tenant_code: str = "") -> str:
    base = portal_base_url()
    if tenant_code:
        return f"{base}/accounts/login/?tenant_code={tenant_code}"
    return f"{base}/accounts/login/"


def portal_environment_url() -> str:
    return f"{portal_base_url()}/accounts/portal/"


def direct_login_url(tenant) -> str:
    if tenant.hostname:
        return f"https://{tenant.hostname}/accounts/login/"
    return portal_login_url(tenant.code)


def tenant_app_url(tenant) -> str:
    if tenant.hostname:
        return f"https://{tenant.hostname}"
    return portal_environment_url()


def environment_access_setup_url(tenant, user) -> str:
    path = reverse(
        "environment_access_setup",
        kwargs={
            "tenant_code": tenant.code,
            "uidb64": urlsafe_base64_encode(force_bytes(user.pk)),
            "token": tenant_access_token_generator.make_token(user),
        },
    )
    return f"{portal_base_url()}{path}"
