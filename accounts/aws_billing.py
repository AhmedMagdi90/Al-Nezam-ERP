from __future__ import annotations

import os
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation


def _integration_enabled() -> bool:
    return (os.getenv("AWS_COST_EXPLORER_ENABLED", "0") or "").strip() == "1"


def _aws_cost_tag_key() -> str:
    return (os.getenv("AWS_COST_EXPLORER_TAG_KEY", "") or "").strip()


def _billing_region() -> str:
    return (os.getenv("AWS_COST_EXPLORER_REGION", "") or "").strip() or "us-east-1"


def _currency_symbol(currency: str) -> str:
    return {
        "USD": "$",
        "EUR": "EUR ",
        "AED": "AED ",
        "SAR": "SAR ",
        "EGP": "EGP ",
    }.get((currency or "USD").upper(), f"{currency or 'USD'} ")


def _boto3_available() -> bool:
    try:
        import boto3  # noqa: F401
    except ImportError:
        return False
    return True


def _aws_credentials_available() -> bool:
    if not _boto3_available():
        return False
    try:
        import boto3

        return bool(boto3.Session().get_credentials())
    except Exception:
        return False


def _quantize_amount(raw_amount) -> Decimal:
    try:
        return Decimal(str(raw_amount or "0")).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return Decimal("0.00")


def get_organization_billing_snapshot(organization, tenant_cards, *, currency="USD") -> dict:
    integration_enabled = _integration_enabled()
    tag_key = _aws_cost_tag_key()
    should_check_aws_runtime = integration_enabled and bool(tag_key)
    boto3_available = _boto3_available() if should_check_aws_runtime else False
    credentials_available = _aws_credentials_available() if should_check_aws_runtime and boto3_available else False
    boto3_detail = (
        "Required to call AWS Cost Explorer from Django."
        if should_check_aws_runtime
        else "Skipped until billing integration and tag key are configured."
    )
    credentials_detail = (
        "Attach an instance role or export Cost Explorer credentials on the server."
        if should_check_aws_runtime and boto3_available
        else "Skipped until billing integration, tag key, and boto3 are ready."
    )
    fallback = {
        "available": False,
        "configured": integration_enabled and bool(tag_key),
        "currency": currency or "USD",
        "currency_symbol": _currency_symbol(currency or "USD"),
        "period_label": "Month to date",
        "total_amount": Decimal("0.00"),
        "estimated": False,
        "tenant_amounts": {},
        "reason": "AWS billing integration is not configured.",
        "tag_key": tag_key,
        "readiness_checks": [
            {
                "label": "Billing integration enabled",
                "ready": integration_enabled,
                "detail": "Set AWS_COST_EXPLORER_ENABLED=1 on the server.",
            },
            {
                "label": "Cost allocation tag configured",
                "ready": bool(tag_key),
                "detail": f"Current tag key: {tag_key}" if tag_key else "Set AWS_COST_EXPLORER_TAG_KEY to a customer tag such as TenantCode.",
            },
            {
                "label": "boto3 installed",
                "ready": boto3_available,
                "detail": boto3_detail,
            },
            {
                "label": "AWS credentials available",
                "ready": credentials_available,
                "detail": credentials_detail,
            },
        ],
    }
    if not integration_enabled:
        return fallback

    if not tag_key:
        fallback["reason"] = "Set AWS_COST_EXPLORER_TAG_KEY to a cost allocation tag such as TenantCode."
        return fallback

    if not boto3_available:
        fallback["reason"] = "boto3 is not installed in this environment."
        return fallback

    if not credentials_available:
        fallback["reason"] = "AWS credentials are not available on this server."
        return fallback

    import boto3

    tenant_values = [card["tenant"].code for card in tenant_cards if getattr(card["tenant"], "code", None)]
    if not tenant_values:
        fallback["reason"] = "No tenant codes are available for billing lookup."
        return fallback

    start = date.today().replace(day=1)
    end = date.today() + timedelta(days=1)
    try:
        client = boto3.client("ce", region_name=_billing_region())
        response = client.get_cost_and_usage(
            TimePeriod={"Start": start.isoformat(), "End": end.isoformat()},
            Granularity="MONTHLY",
            Metrics=["UnblendedCost"],
            Filter={"Tags": {"Key": tag_key, "Values": tenant_values}},
            GroupBy=[{"Type": "TAG", "Key": tag_key}],
        )
    except Exception as exc:
        fallback["reason"] = f"AWS Cost Explorer query failed: {exc}"
        return fallback

    results_by_time = response.get("ResultsByTime") or []
    groups = results_by_time[0].get("Groups", []) if results_by_time else []
    tenant_amounts = {}
    total_amount = Decimal("0.00")
    estimated = bool(results_by_time[0].get("Estimated")) if results_by_time else False

    for group in groups:
        keys = group.get("Keys") or []
        key_value = (keys[0] if keys else "").strip()
        tenant_code = key_value.split("$", 1)[-1] if "$" in key_value else key_value
        amount = _quantize_amount((((group.get("Metrics") or {}).get("UnblendedCost") or {}).get("Amount")))
        tenant_amounts[tenant_code] = amount
        total_amount += amount

    return {
        "available": True,
        "configured": True,
        "currency": currency or "USD",
        "currency_symbol": _currency_symbol(currency or "USD"),
        "period_label": "Month to date",
        "total_amount": total_amount,
        "estimated": estimated,
        "tenant_amounts": tenant_amounts,
        "reason": "",
        "tag_key": tag_key,
        "readiness_checks": fallback["readiness_checks"],
    }
