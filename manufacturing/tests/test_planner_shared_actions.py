import json

from django.test import RequestFactory, TestCase

from manufacturing.models import ProductionLog, WorkOrder
from manufacturing.tests.utils import create_company, create_user_with_role
from manufacturing.views.shop_floor import ApproveLogView, ProductionLogEditView


class PlannerSharedActionsTests(TestCase):
    def setUp(self):
        self.company = create_company()
        self.planner = create_user_with_role("planner_user", "planner", self.company)
        self.worker = create_user_with_role("worker_user", "worker", self.company)
        self.factory = RequestFactory()

        self.work_order = WorkOrder.objects.create(
            product_name="Shared UI WO",
            quantity=10,
            status="in_progress",
            company=self.company,
            assigned_worker=self.worker,
            assigned_to=self.planner,
        )

    def _create_pending_log(self):
        return ProductionLog.objects.create(
            work_order=self.work_order,
            worker=self.worker,
            quantity=4,
            status="pending",
            shift="morning",
        )

    def test_planner_can_approve_pending_log(self):
        log = self._create_pending_log()
        request = self.factory.post("/manufacturing/approve-log/", {"action": "approve"})
        request.user = self.planner
        response = ApproveLogView.as_view()(request, log_id=log.id)

        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.content.decode("utf-8"))
        self.assertTrue(payload.get("success"))
        log.refresh_from_db()
        self.assertEqual(log.status, "approved")
        self.assertEqual(log.reviewed_by, self.planner)

    def test_reject_log_requires_reason(self):
        log = self._create_pending_log()
        request = self.factory.post("/manufacturing/approve-log/", {"action": "reject"})
        request.user = self.planner
        response = ApproveLogView.as_view()(request, log_id=log.id)

        self.assertEqual(response.status_code, 400)
        payload = json.loads(response.content.decode("utf-8"))
        self.assertFalse(payload.get("success"))
        self.assertEqual(payload.get("error"), "Rejection reason is required.")
        log.refresh_from_db()
        self.assertEqual(log.status, "pending")

    def test_reject_log_stores_reason_on_note(self):
        log = self._create_pending_log()
        log.note = "Worker note"
        log.save(update_fields=["note"])
        request = self.factory.post(
            "/manufacturing/approve-log/",
            {"action": "reject", "reason": "Quantity does not match produced parts."},
        )
        request.user = self.planner
        response = ApproveLogView.as_view()(request, log_id=log.id)

        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.content.decode("utf-8"))
        self.assertTrue(payload.get("success"))
        log.refresh_from_db()
        self.assertEqual(log.status, "rejected")
        self.assertEqual(log.reviewed_by, self.planner)
        self.assertIn("Worker note", log.note)
        self.assertIn("Rejection reason: Quantity does not match produced parts.", log.note)

    def test_planner_can_edit_pending_log(self):
        log = self._create_pending_log()
        request = self.factory.post(
            "/manufacturing/api/production-log/update/",
            data='{"quantity": 5, "note": "planner edit"}',
            content_type="application/json",
        )
        request.user = self.planner
        response = ProductionLogEditView.as_view()(request, log_id=log.id)

        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.content.decode("utf-8"))
        self.assertTrue(payload.get("success"))
        log.refresh_from_db()
        self.assertEqual(log.quantity, 5)
        self.assertEqual(log.note, "planner edit")
