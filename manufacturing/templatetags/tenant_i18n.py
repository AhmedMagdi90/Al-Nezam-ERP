from django import template
from django.utils.translation import get_language

from accounts.models import Profile
from manufacturing.runtime_translations import resolve_runtime_translation

register = template.Library()


@register.simple_tag(takes_context=True)
def tenant_trans(context, msgid):
    request = context.get("request")
    user = getattr(request, "user", None)
    profile = None
    if getattr(user, "is_authenticated", False):
        try:
            profile = user.profile
        except Profile.DoesNotExist:
            profile = None
    company = getattr(profile, "company", None) if profile else None
    return resolve_runtime_translation(company, msgid, get_language())
