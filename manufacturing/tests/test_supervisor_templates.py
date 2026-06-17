from django.test import SimpleTestCase


class SupervisorTemplateWiringTests(SimpleTestCase):
    def test_supervisor_guide_is_button_driven_drawer(self):
        with open('templates/manufacturing/supervisor_dashboard.html', encoding='utf-8') as handle:
            content = handle.read()

        self.assertIn('showFlowGuide: false', content)
        self.assertIn('@click="showFlowGuide = true; showMoreMenu = false"', content)
        self.assertIn('x-show="showFlowGuide"', content)
        self.assertIn('{% tenant_trans "Supervisor Guide" %}', content)
        self.assertIn("@click=\"switchTab('assignments'); showFlowGuide = false\"", content)
        self.assertNotIn('class="supervisor-flow-cards grid grid-cols-1 lg:grid-cols-4 gap-3"', content)

    def test_supervisor_primary_tabs_are_compact_and_fixed(self):
        with open('templates/manufacturing/supervisor_dashboard.html', encoding='utf-8') as handle:
            content = handle.read()

        self.assertIn('{% tenant_trans "Dispatch" %}', content)
        self.assertIn('{% tenant_trans "Team" %}', content)
        self.assertIn('{% tenant_trans "More" %}', content)
        self.assertIn("switchTab('upcoming')", content)
        self.assertIn("switchTab('faults')", content)
        self.assertNotIn('data-reorderable-tabs="supervisor-tab-order"', content)

    def test_worker_assign_modal_uses_supervisor_tab_preserving_reload(self):
        with open('templates/manufacturing/modals/worker_assign_modal.html', encoding='utf-8') as handle:
            content = handle.read()

        self.assertIn('reloadSupervisorDashboardPreservingTab', content)
        self.assertIn("if (typeof reloadSupervisorDashboardPreservingTab === 'function')", content)

    def test_supervisor_ready_dispatch_is_compact_and_modal_driven(self):
        with open('templates/manufacturing/supervisor_dashboard.html', encoding='utf-8') as handle:
            content = handle.read()

        self.assertIn('{% for wo in ready_dispatch_tasks %}', content)
        self.assertIn('supervisor-execution-summary rounded-2xl', content)
        self.assertIn('supervisor-execution-card', content)
        self.assertIn('supervisor-meta-pill', content)
        self.assertIn('openWorkerAssignModal', content)
        self.assertIn('{% tenant_trans "Assign" %}', content)
        self.assertNotIn('Due now and waiting for a worker', content)

    def test_supervisor_approval_review_shows_decision_context(self):
        with open('templates/manufacturing/supervisor_dashboard.html', encoding='utf-8') as handle:
            content = handle.read()

        self.assertIn('supervisor-approval-card', content)
        self.assertIn('Review Output', content)
        self.assertIn('logReviewSubmittedAt', content)
        self.assertIn('logReviewWoQty', content)
        self.assertIn('logReviewApprovedQty', content)
        self.assertIn('logReviewRemainingQty', content)
        self.assertIn('logReviewCompletionFlag', content)
        self.assertIn('logReviewRejectReason', content)
        self.assertIn("Rejection reason is required.", content)
