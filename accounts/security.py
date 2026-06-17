import hashlib
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone


@dataclass
class LoginThrottleState:
    is_blocked: bool
    retry_after_seconds: int = 0


def _client_ip(request) -> str:
    forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "unknown")


def _attempt_key(scope: str, fingerprint: str) -> str:
    return f"auth:attempts:{scope}:{fingerprint}"


def _lock_key(scope: str, fingerprint: str) -> str:
    return f"auth:lock:{scope}:{fingerprint}"


def _fingerprint(*values: str) -> str:
    joined = "|".join((value or "").strip().lower() for value in values)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _read_lock_remaining_seconds(key: str) -> int:
    until = cache.get(key)
    if not until:
        return 0
    remaining = int(float(until) - timezone.now().timestamp())
    return max(0, remaining)


def get_login_throttle_state(request, tenant_code: str, login_identifier: str) -> LoginThrottleState:
    ip = _client_ip(request)
    credential_fp = _fingerprint(ip, tenant_code, login_identifier)
    ip_fp = _fingerprint(ip, tenant_code)

    credential_lock_remaining = _read_lock_remaining_seconds(_lock_key("credential", credential_fp))
    if credential_lock_remaining > 0:
        return LoginThrottleState(is_blocked=True, retry_after_seconds=credential_lock_remaining)

    ip_lock_remaining = _read_lock_remaining_seconds(_lock_key("ip", ip_fp))
    if ip_lock_remaining > 0:
        return LoginThrottleState(is_blocked=True, retry_after_seconds=ip_lock_remaining)

    return LoginThrottleState(is_blocked=False)


def register_login_failure(request, tenant_code: str, login_identifier: str):
    max_attempts = max(1, int(getattr(settings, "LOGIN_MAX_ATTEMPTS", 5)))
    max_attempts_per_ip = max(1, int(getattr(settings, "LOGIN_MAX_ATTEMPTS_PER_IP", 20)))
    attempt_window_seconds = max(60, int(getattr(settings, "LOGIN_ATTEMPT_WINDOW_SECONDS", 900)))
    lockout_seconds = max(60, int(getattr(settings, "LOGIN_LOCKOUT_SECONDS", 900)))

    ip = _client_ip(request)
    credential_fp = _fingerprint(ip, tenant_code, login_identifier)
    ip_fp = _fingerprint(ip, tenant_code)

    credential_attempts_key = _attempt_key("credential", credential_fp)
    ip_attempts_key = _attempt_key("ip", ip_fp)

    credential_attempts = int(cache.get(credential_attempts_key, 0)) + 1
    ip_attempts = int(cache.get(ip_attempts_key, 0)) + 1
    cache.set(credential_attempts_key, credential_attempts, attempt_window_seconds)
    cache.set(ip_attempts_key, ip_attempts, attempt_window_seconds)

    lock_until = (timezone.now() + timedelta(seconds=lockout_seconds)).timestamp()
    if credential_attempts >= max_attempts:
        cache.set(_lock_key("credential", credential_fp), lock_until, lockout_seconds)
    if ip_attempts >= max_attempts_per_ip:
        cache.set(_lock_key("ip", ip_fp), lock_until, lockout_seconds)


def clear_login_failures(request, tenant_code: str, login_identifier: str):
    ip = _client_ip(request)
    credential_fp = _fingerprint(ip, tenant_code, login_identifier)
    ip_fp = _fingerprint(ip, tenant_code)

    cache.delete_many(
        [
            _attempt_key("credential", credential_fp),
            _attempt_key("ip", ip_fp),
            _lock_key("credential", credential_fp),
            _lock_key("ip", ip_fp),
        ]
    )
