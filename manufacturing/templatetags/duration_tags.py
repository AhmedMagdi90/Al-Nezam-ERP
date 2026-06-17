from django import template

from manufacturing.utils import humanize_duration_minutes

register = template.Library()


@register.filter(name="humanize_duration_minutes")
def humanize_duration_minutes_filter(value):
    return humanize_duration_minutes(value)
