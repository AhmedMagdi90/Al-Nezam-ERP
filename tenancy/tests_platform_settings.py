from django.test import TestCase

from tenancy.models import PlatformSettings


class PlatformSettingsTests(TestCase):
    def test_get_solo_creates_and_reuses_singleton(self):
        first = PlatformSettings.get_solo()
        second = PlatformSettings.get_solo()

        self.assertEqual(first.id, second.id)
        self.assertEqual(PlatformSettings.objects.count(), 1)

    def test_estimator_config_reflects_admin_values(self):
        settings_obj = PlatformSettings.get_solo()
        settings_obj.subscription_currency = "AED"
        settings_obj.base_monthly_price = "299.00"
        settings_obj.included_users = 8
        settings_obj.extra_user_monthly_price = "15.00"
        settings_obj.test_environment_monthly_price = "79.00"
        settings_obj.guided_onboarding_one_time_fee = "500.00"
        settings_obj.annual_discount_rate = "0.1500"
        settings_obj.manual_quote_email = "pricing@nezam.com"
        settings_obj.save()

        config = settings_obj.estimator_config

        self.assertEqual(config["currency"], "AED")
        self.assertEqual(config["baseMonthly"], 299.0)
        self.assertEqual(config["includedUsers"], 8)
        self.assertEqual(config["extraUserMonthly"], 15.0)
        self.assertEqual(config["testEnvironmentMonthly"], 79.0)
        self.assertEqual(config["guidedOnboardingOneTime"], 500.0)
        self.assertEqual(config["annualDiscountRate"], 0.15)
        self.assertEqual(config["quoteEmail"], "pricing@nezam.com")

    def test_landing_page_renders_singleton_config(self):
        settings_obj = PlatformSettings.get_solo()
        settings_obj.manual_quote_email = "pricing@nezam.com"
        settings_obj.base_monthly_price = "299.00"
        settings_obj.subscription_currency = "USD"
        settings_obj.save()

        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="subscription-estimator-config"', html=False)
        self.assertContains(response, 'pricing@nezam.com', html=False)
        self.assertContains(response, '"baseMonthly": 299.0', html=False)
