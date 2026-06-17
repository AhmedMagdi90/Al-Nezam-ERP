from types import SimpleNamespace
import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone
from django.core.exceptions import ObjectDoesNotExist

from manufacturing.models import BillOfMaterial, BOMComponent, Machine, Product, WorkOrder
from manufacturing.services import DashboardService, ProductionLogService
from manufacturing.tests.utils import create_company, create_user_with_role
from manufacturing.views.shop_floor import _assigned_leaf_work_orders, _prepare_shop_floor_work_order


class ShopFloorTests(TestCase):
    def setUp(self):
        self.company = create_company("Shop Floor Co")
        self.supervisor = create_user_with_role("supervisor_sf", "supervisor", self.company)
        self.supervisor.profile.worker_mode_enabled = True
        self.supervisor.profile.save(update_fields=["worker_mode_enabled"])

        self.machine = Machine.objects.create(
            name="Shop Machine",
            code="SF-01",
            status="operational",
            company=self.company,
        )
        self.product = Product.objects.create(name="Shop Product", company=self.company)
        self.bom = BillOfMaterial.objects.create(product=self.product, status="active", base_quantity=1)
        self.work_order = WorkOrder.objects.create(
            product_name="Shop Batch",
            bom=self.bom,
            quantity=10,
            status="pending",
            company=self.company,
            machine=self.machine,
            assigned_worker=self.supervisor,
            start_date=timezone.now(),
            material_readiness_status="ready",
            material_available_qty=10,
        )
        self.client = Client()
        self.client.force_login(self.supervisor)

    def test_shop_floor_renders_for_supervisor_with_worker_mode(self):
        response = self.client.get(reverse("shop_floor"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Assigned Jobs")
        self.assertContains(response, "Ready To Start")
        self.assertContains(response, "WO-")

    def test_shop_floor_hides_operator_guide_behind_button(self):
        response = self.client.get(reverse("shop_floor"))

        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn('showWorkGuide: false', content)
        self.assertIn('x-show="showWorkGuide"', content)
        self.assertIn('@click="showWorkGuide = true"', content)
        self.assertIn("Operator Guide", content)

    def test_shop_floor_renders_compact_status_legend(self):
        response = self.client.get(reverse("shop_floor"))

        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn("grid-cols-2 xl:grid-cols-3 2xl:grid-cols-6", content)
        self.assertIn("Supervisor review", content)
        self.assertIn("Future slot", content)
        self.assertIn("Section plan", content)
        self.assertIn("Show every job", content)
        self.assertIn(">Active<", content)

    def test_shop_floor_renders_queue_filter_controls(self):
        response = self.client.get(reverse("shop_floor"))

        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn("shopFloorQueueFilter", content)
        self.assertIn("worker-queue-filter-grid", content)
        self.assertIn("setQueueFilter('all')", content)
        self.assertIn("setQueueFilter('approval')", content)
        self.assertIn("setQueueFilter('active')", content)
        self.assertIn("setQueueFilter('ready')", content)
        self.assertIn("setQueueFilter('later')", content)
        self.assertIn("setQueueFilter('upcoming')", content)
        self.assertIn("shouldShowQueue('ready')", content)
        self.assertIn("No jobs in this category.", content)

    def test_shop_floor_upcoming_tab_lists_section_future_work_orders(self):
        self.supervisor.profile.worker_mode_enabled = False
        self.supervisor.profile.department = "Cutting"
        self.supervisor.profile.save(update_fields=["worker_mode_enabled", "department"])
        cutting_worker = create_user_with_role("cutting_worker_upcoming", "worker", self.company)
        packing_worker = create_user_with_role("packing_worker_upcoming", "worker", self.company)
        cutting_machine = Machine.objects.create(
            name="Cutting Line",
            code="CUT-UP",
            category="Cutting",
            status="operational",
            company=self.company,
        )
        packing_machine = Machine.objects.create(
            name="Packing Line",
            code="PACK-UP",
            category="Packing",
            status="operational",
            company=self.company,
        )
        future_start = timezone.now() + timedelta(hours=3)
        cutting_future = WorkOrder.objects.create(
            product_name="Cutting Future",
            bom=self.bom,
            quantity=4,
            status="pending",
            company=self.company,
            machine=cutting_machine,
            assigned_worker=cutting_worker,
            start_date=future_start,
            scheduled_start_date=future_start,
        )
        WorkOrder.objects.create(
            product_name="Packing Future",
            bom=self.bom,
            quantity=4,
            status="pending",
            company=self.company,
            machine=packing_machine,
            assigned_worker=packing_worker,
            start_date=future_start,
            scheduled_start_date=future_start,
        )

        response = self.client.get(reverse("shop_floor"), {"tab": "upcoming"})

        self.assertEqual(response.status_code, 200)
        upcoming_ids = [wo.id for wo in response.context["section_upcoming_tasks"]]
        self.assertEqual(upcoming_ids, [cutting_future.id])
        self.assertContains(response, "Upcoming Work Orders")
        self.assertContains(response, "Cutting Future")
        self.assertContains(response, "Cutting")
        self.assertContains(response, cutting_worker.username)
        self.assertNotContains(response, "Packing Future")

    def test_shop_floor_prefills_bom_material_actuals_from_planned_quantity(self):
        with open("templates/manufacturing/shop_floor.html", encoding="utf-8") as handle:
            content = handle.read()

        self.assertIn("actual_qty: existingWasEdited ? existing.actual_qty : plannedQty", content)
        self.assertIn("planned_quantity: m.expected_qty || null", content)
        self.assertIn("BOM quantities are prefilled", content)

    def test_bom_requirements_scales_material_qty_for_output_quantity(self):
        self.bom.status = "draft"
        self.bom.base_quantity = 100
        self.bom.save(update_fields=["status", "base_quantity"])
        material = Product.objects.create(name="wood", company=self.company, material_type="raw", unit="pcs")
        component = BOMComponent.objects.create(
            bom=self.bom,
            product=material,
            material_name="wood",
            quantity=100,
            unit="pcs",
            cost_per_unit=1,
        )

        response = self.client.get(
            f"/manufacturing/api/v1/workorders/{self.work_order.id}/bom-requirements/",
            {"qty": "50"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(len(payload["components"]), 1)
        row = payload["components"][0]
        self.assertEqual(row["component_id"], component.id)
        self.assertEqual(row["name"], "wood")
        self.assertEqual(row["base_quantity"], 100.0)
        self.assertEqual(row["component_quantity"], 100.0)
        self.assertEqual(row["expected_qty"], 50)

    def test_shop_floor_uses_selected_work_order_identity_in_header(self):
        response = self.client.get(reverse("shop_floor"))

        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn("worker-job-title", content)
        self.assertIn("worker-job-summary", content)
        self.assertContains(response, "SF-01 - Shop Machine")
        self.assertNotIn("Station 04", content)
        self.assertNotIn("CNC Machining - Active", content)

    def test_shop_floor_queue_cards_are_touch_safe(self):
        with open("templates/manufacturing/partials/shop_floor_queue_card.html", encoding="utf-8") as handle:
            content = handle.read()

        self.assertIn("worker-queue-action-row", content)
        self.assertIn("Tap to open", content)
        self.assertIn("Opened", content)
        self.assertNotIn("opacity-0 group-hover:opacity-100", content)

    def test_shop_floor_allows_future_pending_assignment_to_start_now(self):
        self.work_order.start_date = timezone.now() + timedelta(hours=1)
        self.work_order.scheduled_start_date = self.work_order.start_date
        self.work_order.save(update_fields=["start_date", "scheduled_start_date"])

        response = self.client.get(reverse("shop_floor"))

        self.assertEqual(response.status_code, 200)
        assigned_tasks = response.context["assigned_tasks"]
        self.assertEqual(list(assigned_tasks), [self.work_order])
        self.assertEqual(response.context["ready_task_ids"], [self.work_order.id])
        self.assertEqual(response.context["ready_tasks"], [self.work_order])
        self.assertEqual(response.context["future_pending_tasks"], [])
        self.assertEqual(response.context["selected_wo"].id, self.work_order.id)

    def test_shop_floor_start_preserves_planned_time_and_sets_actual_start(self):
        planned_start = timezone.now() + timedelta(hours=1)
        self.work_order.start_date = planned_start
        self.work_order.scheduled_start_date = planned_start
        self.work_order.save(update_fields=["start_date", "scheduled_start_date"])

        before_start = timezone.now()
        response = self.client.post(
            reverse("api_update_work_order_status", kwargs={"wo_id": self.work_order.id}),
            data=json.dumps({"status": "in_progress"}),
            content_type="application/json",
        )
        after_start = timezone.now()

        self.assertEqual(response.status_code, 200)
        self.work_order.refresh_from_db()
        self.assertEqual(self.work_order.status, "in_progress")
        self.assertEqual(self.work_order.scheduled_start_date, planned_start)
        self.assertGreaterEqual(self.work_order.start_date, before_start)
        self.assertLessEqual(self.work_order.start_date, after_start)
        self.assertGreaterEqual(self.work_order.worker_start_at, before_start)
        self.assertLessEqual(self.work_order.worker_start_at, after_start)

    def test_shop_floor_marks_all_visible_due_pending_jobs_as_ready_to_start(self):
        self.work_order.start_date = timezone.now() - timedelta(minutes=5)
        self.work_order.save(update_fields=["start_date"])
        later_work_order = WorkOrder.objects.create(
            product_name="Later Batch",
            bom=self.bom,
            quantity=6,
            status="pending",
            company=self.company,
            machine=self.machine,
            assigned_worker=self.supervisor,
            start_date=timezone.now() - timedelta(minutes=1),
            material_readiness_status="ready",
            material_available_qty=6,
        )
        future_work_order = WorkOrder.objects.create(
            product_name="Future Batch",
            bom=self.bom,
            quantity=8,
            status="pending",
            company=self.company,
            machine=self.machine,
            assigned_worker=self.supervisor,
            start_date=timezone.now() + timedelta(hours=1),
            scheduled_start_date=timezone.now() + timedelta(hours=1),
            material_readiness_status="ready",
            material_available_qty=8,
        )

        response = self.client.get(reverse("shop_floor"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            set(response.context["ready_task_ids"]),
            {self.work_order.id, later_work_order.id, future_work_order.id},
        )
        self.assertEqual(
            {wo.id for wo in response.context["ready_tasks"]},
            {self.work_order.id, later_work_order.id, future_work_order.id},
        )
        self.assertEqual(
            [wo.id for wo in response.context["future_pending_tasks"]],
            [],
        )
        self.assertEqual(response.context["selected_wo"].id, self.work_order.id)
        self.assertContains(response, "Ready to Start")
        self.assertContains(response, f"WO-{future_work_order.id}")
        self.assertContains(response, f"WO-{later_work_order.id}")

    def test_shop_floor_start_api_activates_assigned_ready_job(self):
        self.work_order.start_date = timezone.now() - timedelta(minutes=5)
        self.work_order.save(update_fields=["start_date"])

        response = self.client.post(
            reverse("api_update_work_order_status", args=[self.work_order.id]),
            data=json.dumps({"status": "in_progress"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "success")
        self.work_order.refresh_from_db()
        self.assertEqual(self.work_order.status, "in_progress")
        self.assertEqual(self.work_order.assigned_worker_id, self.supervisor.id)
        self.assertIsNotNone(self.work_order.worker_start_at)

    def test_worker_queue_classifier_keeps_future_assignments_visible(self):
        self.work_order.start_date = timezone.now() - timedelta(minutes=5)
        self.work_order.save(update_fields=["start_date"])
        future_work_order = WorkOrder.objects.create(
            product_name="Future Batch",
            bom=self.bom,
            quantity=8,
            status="pending",
            company=self.company,
            machine=self.machine,
            assigned_worker=self.supervisor,
            start_date=timezone.now() + timedelta(hours=1),
            scheduled_start_date=timezone.now() + timedelta(hours=1),
            material_readiness_status="ready",
            material_available_qty=8,
        )

        groups = DashboardService.classify_worker_queue_work_orders(
            [self.work_order, future_work_order],
            now=timezone.now(),
        )

        self.assertEqual([wo.id for wo in groups["ready"]], [self.work_order.id, future_work_order.id])
        self.assertEqual([wo.id for wo in groups["future_pending"]], [])
        self.assertEqual(groups["ready_ids"], [self.work_order.id, future_work_order.id])

    def test_assigned_leaf_work_orders_accept_user_like_reference(self):
        user_ref = SimpleNamespace(id=self.supervisor.id)

        ids = list(_assigned_leaf_work_orders(self.company, user_ref).values_list("id", flat=True))

        self.assertEqual(ids, [self.work_order.id])

    def test_create_log_accepts_user_reference_by_id(self):
        user_ref = SimpleNamespace(id=self.supervisor.id, is_superuser=False)

        log = ProductionLogService.create_log(
            work_order=self.work_order,
            worker=user_ref,
            quantity=2,
            shift="morning",
            note="tenant-safe log",
            materials=[],
            completion_requested=False,
        )

        self.assertEqual(log.worker_id, self.supervisor.id)
        self.assertEqual(log.work_order_id, self.work_order.id)

    def test_prepare_shop_floor_work_order_tolerates_missing_related_records(self):
        class BrokenWorkOrder:
            id = 91
            product_name = "Broken Batch"
            quantity = 7
            priority = "Normal"
            parent_id = 11
            machine_id = 22
            bom_id = 33
            stage_id = 44
            current_stage_id = 55
            assigned_worker_id = 66

            @property
            def parent(self):
                raise WorkOrder.DoesNotExist("missing parent")

            @property
            def machine(self):
                raise Machine.DoesNotExist("missing machine")

            @property
            def bom(self):
                raise BillOfMaterial.DoesNotExist("missing bom")

            @property
            def stage(self):
                raise ObjectDoesNotExist("missing stage")

            @property
            def current_stage(self):
                raise ObjectDoesNotExist("missing current stage")

            @property
            def assigned_worker(self):
                raise ObjectDoesNotExist("missing worker")

        wo = _prepare_shop_floor_work_order(BrokenWorkOrder())

        self.assertIsNone(wo.safe_parent)
        self.assertIsNone(wo.safe_machine)
        self.assertIsNone(wo.safe_bom)
        self.assertIsNone(wo.display_stage)
        self.assertEqual(wo.display_product_name, "Broken Batch")
        self.assertEqual(wo.display_machine_label, "Manual")

    def test_shop_floor_falls_back_to_idle_state_when_unexpected_error_occurs(self):
        with patch(
            "manufacturing.views.shop_floor.DashboardService.classify_worker_queue_work_orders",
            side_effect=RuntimeError("boom"),
        ):
            response = self.client.get(reverse("shop_floor"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Shop floor data is temporarily unavailable.")
        self.assertContains(response, "Station Ready")
