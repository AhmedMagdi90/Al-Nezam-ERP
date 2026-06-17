import logging
from urllib.parse import urlencode

from django.contrib import messages
from django.shortcuts import redirect
from django.urls import reverse
from django.views.csrf import csrf_failure as django_csrf_failure

logger = logging.getLogger(__name__)


def csrf_failure(request, reason="", template_name="403_csrf.html"):
    """
    Keep CSRF protection active, but recover gracefully for login form submissions
    by redirecting to a fresh login page instead of showing a hard 403 page.
    """
    logger.warning("CSRF failure on %s: %s", request.path, reason)

    login_path = reverse("login")
    if request.method == "POST" and request.path == login_path:
        next_url = (request.GET.get("next") or request.POST.get("next") or "").strip()
        messages.error(
            request,
            "Security token expired or missing. Please try again. If this repeats, enable cookies for this site.",
        )
        params = {}
        if "csrf cookie not set" in (reason or "").lower():
            params["cookie_required"] = "1"
        if next_url:
            params["next"] = next_url
        if params:
            return redirect(f"{login_path}?{urlencode(params)}")
        return redirect(login_path)

    return django_csrf_failure(request, reason=reason, template_name=template_name)
