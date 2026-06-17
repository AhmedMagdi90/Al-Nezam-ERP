import math
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from tenancy.models import Tenant


def demo_tenant_lifetime_days() -> int:
    try:
        return max(1, int(getattr(settings, "DEMO_TENANT_LIFETIME_DAYS", 14)))
    except (TypeError, ValueError):
        return 14


def describe_tenant_access(tenant: Tenant, *, now=None) -> dict:
    now = now or timezone.now()
    is_demo = tenant.environment_type == Tenant.EnvironmentType.DEMO
    summary = {
        "is_demo": is_demo,
        "is_expired": False,
        "is_accessible": bool(tenant.is_active),
        "status_label": "Active" if tenant.is_active else "Inactive",
        "status_tone": "slate",
        "badge_label": None,
        "badge_tone": "slate",
        "summary": "",
        "expires_at": None,
        "days_remaining": None,
        "blocked_message": "Selected environment is not available.",
    }
    if not is_demo:
        return summary

    created_at = tenant.created_at or now
    expires_at = created_at + timedelta(days=demo_tenant_lifetime_days())
    remaining_seconds = (expires_at - now).total_seconds()
    is_expired = remaining_seconds <= 0
    days_remaining = 0 if is_expired else max(1, math.ceil(remaining_seconds / 86400))
    expires_on = timezone.localtime(expires_at)

    summary.update(
        {
            "is_expired": is_expired,
            "is_accessible": bool(tenant.is_active) and not is_expired,
            "status_label": "Expired" if is_expired else "Active",
            "status_tone": "rose" if is_expired else "cyan",
            "badge_label": "Expired" if is_expired else f"{days_remaining} Day{'s' if days_remaining != 1 else ''} Left",
            "badge_tone": "rose" if is_expired else "cyan",
            "summary": (
                f"Expired on {expires_on.strftime('%b %d, %Y')}."
                if is_expired
                else f"Available for {days_remaining} more day{'s' if days_remaining != 1 else ''}. Expires {expires_on.strftime('%b %d, %Y')}."
            ),
            "expires_at": expires_at,
            "days_remaining": days_remaining,
            "blocked_message": (
                f"Demo Server expired on {expires_on.strftime('%b %d, %Y')}. Ask the organization owner to create a Test Server or renew the demo."
            ),
        }
    )
    return summary


def tenant_allows_workspace_access(tenant: Tenant, *, now=None) -> bool:
    return describe_tenant_access(tenant, now=now)["is_accessible"]
