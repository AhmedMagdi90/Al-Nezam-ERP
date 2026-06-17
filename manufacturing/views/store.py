import json

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Q, Sum
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views import View

from accounts.constants import RoleType
from manufacturing.access_control import resolve_user_role
from manufacturing.models import WorkOrder
from manufacturing.security import audit_request_action
from manufacturing.services import (
    NotificationService,
    get_workorder_material_readiness_payload,
    get_workorder_quantity_breakdown,
    request_store_receipt_for_work_order,
)
from .dashboard import redirect_to_role_home, require_company, user_has_role


class StoreDashboardView(LoginRequiredMixin, View):
    def get(self, request):
        if not user_has_role(request.user, [RoleType.STORE, RoleType.ADMIN]):
            messages.warning(request, "Your account is not assigned to Store.")
            return redirect_to_role_home(request.user)

        company = require_company(request.user)
        material_requests = list(
            WorkOrder.objects.filter(
                company=company,
                parent__isnull=True,
                material_readiness_status__in=["not_checked", "partial", "shortage"],
            )
            .exclude(status__in=["completed", "canceled", "archived"])
            .select_related("bom", "bom__product", "customer", "assigned_to")
            .order_by("material_readiness_updated_at", "-id")
        )
        for wo in material_requests:
            wo.material_payload = get_workorder_material_readiness_payload(wo, company)
            _base_qty, _comp_qty, adjusted_qty = get_workorder_quantity_breakdown(wo)
            wo.display_quantity = int(adjusted_qty or wo.quantity or 0)

        receipt_requests = list(
            WorkOrder.objects.filter(
                company=company,
                parent__isnull=True,
            )
            .filter(
                Q(store_receipt_status="pending")
                | Q(store_receipt_status="not_requested", status="completed", planner_action_required=True)
            )
            .select_related("customer", "bom", "bom__product")
            .prefetch_related("sub_tasks")
            .order_by("store_receipt_requested_at", "-end_date", "-id")
        )
        for wo in receipt_requests:
            if wo.store_receipt_status == "not_requested":
                request_store_receipt_for_work_order(wo, actor=request.user, notify=False)
                wo.refresh_from_db(fields=["store_receipt_status", "store_receipt_requested_at"])
            child_ids = list(wo.sub_tasks.values_list("id", flat=True))
            wo.receipt_reported_qty = (
                WorkOrder.objects.filter(id__in=child_ids)
                .aggregate(total=Sum("production_logs__quantity", filter=~Q(production_logs__status="rejected")))
                .get("total")
                or 0
            )
            wo.receipt_approved_qty = (
                WorkOrder.objects.filter(id__in=child_ids)
                .aggregate(total=Sum("production_logs__quantity", filter=Q(production_logs__status="approved")))
                .get("total")
                or 0
            )

        context = {
            "current_role_name": "store",
            "store_shell": True,
            "material_requests": material_requests,
            "material_requests_count": len(material_requests),
            "receipt_requests": receipt_requests,
            "receipt_requests_count": len(receipt_requests),
        }
        return render(request, "manufacturing/store_dashboard.html", context)


class StoreReceiptConfirmAPI(LoginRequiredMixin, View):
    def post(self, request, wo_id):
        if not user_has_role(request.user, [RoleType.STORE, RoleType.ADMIN]):
            return JsonResponse({"success": False, "error": "Unauthorized"}, status=403)

        company = require_company(request.user)
        wo = get_object_or_404(WorkOrder, id=wo_id, company=company, parent__isnull=True)
        try:
            data = json.loads(request.body or "{}")
        except Exception:
            data = {}

        try:
            received_qty = int(data.get("received_qty"))
            scrap_qty = int(data.get("scrap_qty") or 0)
        except (TypeError, ValueError):
            return JsonResponse({"success": False, "error": "Received and scrap quantities must be valid numbers."}, status=400)

        if received_qty < 0 or scrap_qty < 0:
            return JsonResponse({"success": False, "error": "Quantities cannot be negative."}, status=400)
        if received_qty + scrap_qty <= 0:
            return JsonResponse({"success": False, "error": "Confirm received or scrap quantity before approval."}, status=400)

        note = str(data.get("note") or "").strip()
        wo.store_receipt_status = "received"
        wo.store_received_qty = received_qty
        wo.store_scrap_qty = scrap_qty
        wo.store_receipt_note = note
        wo.store_receipt_confirmed_at = timezone.now()
        wo.store_receipt_confirmed_by = request.user
        wo.save(
            update_fields=[
                "store_receipt_status",
                "store_received_qty",
                "store_scrap_qty",
                "store_receipt_note",
                "store_receipt_confirmed_at",
                "store_receipt_confirmed_by",
            ]
        )

        audit_request_action(
            request,
            "update",
            target=wo,
            details={
                "event": "store_receipt_confirmed",
                "work_order_id": wo.id,
                "received_qty": received_qty,
                "scrap_qty": scrap_qty,
                "note": note,
            },
        )
        NotificationService.notify_role(
            company,
            roles=["planner", "admin"],
            title="Store receipt confirmed",
            message=f"WO #{wo.id} received {received_qty}; scrap {scrap_qty}. Planner can close the WO.",
            link=f"/manufacturing/dashboard/?wo={wo.id}",
            exclude_user=request.user,
        )
        return JsonResponse(
            {
                "success": True,
                "work_order_id": wo.id,
                "store_receipt": {
                    "status": wo.store_receipt_status,
                    "received_qty": received_qty,
                    "scrap_qty": scrap_qty,
                    "note": note,
                },
            }
        )
