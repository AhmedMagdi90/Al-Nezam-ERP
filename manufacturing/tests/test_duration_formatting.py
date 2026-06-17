from decimal import Decimal

from django.template import Context, Template
from django.test import SimpleTestCase

from manufacturing.utils import humanize_duration_minutes, humanize_duration_seconds


class DurationFormattingTests(SimpleTestCase):
    def test_humanize_duration_minutes_formats_hours_and_minutes(self):
        self.assertEqual(humanize_duration_minutes(500), "8 hours 20 minutes")

    def test_humanize_duration_minutes_supports_sub_minute_values(self):
        self.assertEqual(humanize_duration_minutes(Decimal("0.5")), "30 seconds")

    def test_humanize_duration_seconds_supports_month_to_second_range(self):
        total_seconds = (
            (30 * 24 * 60 * 60)
            + (2 * 7 * 24 * 60 * 60)
            + (1 * 24 * 60 * 60)
            + (1 * 60 * 60)
            + (5 * 60)
            + 1
        )
        self.assertEqual(
            humanize_duration_seconds(total_seconds),
            "1 month 2 weeks 1 day 1 hour 5 minutes 1 second",
        )

    def test_duration_template_filter_renders_verbose_minutes(self):
        rendered = Template(
            "{% load duration_tags %}{{ value|humanize_duration_minutes }}"
        ).render(Context({"value": Decimal("0.5")}))
        self.assertEqual(rendered, "30 seconds")
