from django.test import SimpleTestCase


class PublicTemplateSubscriptionTests(SimpleTestCase):
    def test_landing_page_exposes_manual_subscription_estimator(self):
        with open("templates/landing.html", encoding="utf-8") as handle:
            content = handle.read()

        self.assertIn('id="subscription-estimator"', content)
        self.assertIn('data-subscription-estimator="true"', content)
        self.assertIn('{{ subscription_estimator_config|json_script:"subscription-estimator-config" }}', content)
        self.assertIn("const rawConfigNode = document.getElementById('subscription-estimator-config');", content)
        self.assertIn("const subscriptionCalculatorConfig = Object.freeze(JSON.parse(rawConfigNode.textContent));", content)
        self.assertIn("window.subscriptionCalculatorConfig = subscriptionCalculatorConfig;", content)
        self.assertIn("Request Manual Quote", content)

    def test_login_register_flow_mentions_setup_first_and_estimator(self):
        with open("templates/accounts/login.html", encoding="utf-8") as handle:
            content = handle.read()

        self.assertIn('{% trans "Setup first" %}', content)
        self.assertIn('{% trans "Open subscription estimator" %}', content)
        self.assertIn('{% trans "Create Workspace" %}', content)
        self.assertIn('data-register-guide-step="company-name"', content)
        self.assertIn('{% trans "Step 1 of 8" %}', content)
        self.assertIn('{% trans "Start with your company name" %}', content)
        self.assertIn('{% trans "Step 2 of 8" %}', content)
        self.assertIn('{% trans "Step 3 of 8" %}', content)
        self.assertIn('{% trans "Step 4 of 8" %}', content)
        self.assertIn('data-register-guide-next', content)

    def test_register_company_page_mentions_clean_workspace_setup_flow(self):
        with open("templates/registration/register_company.html", encoding="utf-8") as handle:
            content = handle.read()

        self.assertIn("Registration opens a guided setup wizard first", content)
        self.assertIn("Create Workspace", content)
        self.assertIn('data-register-guide-step="company-name"', content)
        self.assertIn("Step 1 of 8", content)
        self.assertIn("Start with your company name", content)
        self.assertIn("Step 4 of 8", content)
        self.assertNotIn("Start 30-Day Free Trial", content)

    def test_onboarding_data_page_has_guided_upload_steps(self):
        with open("templates/registration/onboarding_data.html", encoding="utf-8") as handle:
            content = handle.read()

        self.assertIn('data-setup-guide-step="required-data"', content)
        self.assertIn("Step 5 of 8", content)
        self.assertIn("Load the production foundation", content)
        self.assertIn('data-setup-guide-step="work-orders"', content)
        self.assertIn("Step 6 of 8", content)
        self.assertIn('data-setup-guide-finish', content)

    def test_onboarding_users_page_has_guided_team_steps(self):
        with open("templates/registration/onboarding_users.html", encoding="utf-8") as handle:
            content = handle.read()

        self.assertIn('data-setup-guide-step="launch-team"', content)
        self.assertIn("Step 7 of 8", content)
        self.assertIn("Add the launch team", content)
        self.assertIn('data-setup-guide-step="open-planner"', content)
        self.assertIn("Step 8 of 8", content)
        self.assertIn('data-setup-guide-finish', content)
