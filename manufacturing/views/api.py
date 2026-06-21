import json
from django.apps import apps
from django.contrib.auth.mixins import LoginRequiredMixin
from django.views import View
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.core.serializers.json import DjangoJSONEncoder
from django.db.models import Q, Sum
from django.db import transaction
import json
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_FLOOR

from manufacturing.bom_attachments import serialize_bom_attachment
from manufacturing.models import WorkOrder, Machine, BillOfMaterial, Notification, WorkOrderChangeLog
from manufacturing.audit_formatting import audit_summary_text, readable_audit_event
from manufacturing.security import audit_request_action
from manufacturing.services import (
    WorkOrderService,
    WorkOrderCycleService,
    WorkOrderLifecycle,
    WorkOrderLifecycleError,
    BOMService,
    DashboardService,
    NotificationService,
    QualityService,
    resolve_bom_for_work_order,
    get_workorder_quantity_breakdown,
    get_workorder_material_readiness_payload,
    get_workorder_execution_readiness,
    get_workorder_reported_quantity_floor,
    get_workorder_bom_change_payload,
    get_apply_latest_bom_eligibility,
    get_latest_active_bom_for_work_order,
    get_company_default_operation_flow_mode,
    get_work_order_operation_flow_mode,
    get_material_readiness_planning_blocker,
    workorder_has_material_shortage,
)
from .dashboard import get_company_stage, require_company, user_has_role
from manufacturing.access_control import acts_as_worker
from manufacturing.work_order_visibility import can_user_see_work_order
from django.utils.dateparse import parse_date


def parse_client_datetime(value):
    """
    Parse datetime values coming from browser clients safely.
    Accepts:
    - YYYY-MM-DDTHH:MM
    - YYYY-MM-DDTHH:MM:SS
    - YYYY-MM-DD HH:MM:SS(.sss)Z
    - YYYY-MM-DD HH:MM:SS(.sss)+00:00
    """
    raw = str(value or "").strip()
    if not raw:
        return None

    normalized = raw
    if normalized.endswith('Z'):
        normalized = normalized[:-1] + '+00:00'
    normalized = normalized.replace('T', ' ')

    dt = datetime.fromisoformat(normalized)
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, timezone.get_current_timezone())
    return dt

# ---------------------------------------------------------------------
# 🔧 Work Order API
# ---------------------------------------------------------------------

class WorkOrderMaterialReadinessAPI(LoginRequiredMixin, View):
    def post(self, request, wo_id):
        if not user_has_role(request.user, ['planner', 'store', 'admin']):
            return JsonResponse({"success": False, "error": "Unauthorized"}, status=403)

        company = require_company(request.user)
        wo = get_object_or_404(WorkOrder, id=wo_id, company=company)
        target_wo = wo.parent if wo.parent_id and wo.parent else wo

        try:
            data = json.loads(request.body or "{}")
        except Exception:
            data = {}

        status = str(data.get("status") or "").strip().lower()
        valid_statuses = {choice[0] for choice in WorkOrder.MATERIAL_READINESS_CHOICES}
        if status not in valid_statuses:
            return JsonResponse({"success": False, "error": "Invalid material readiness status."}, status=400)

        shortage_note = str(data.get("shortage_note") or data.get("note") or "").strip()
        delivery_date_raw = str(data.get("expected_delivery_date") or data.get("delivery_date") or "").strip()
        expected_delivery_date = parse_date(delivery_date_raw) if delivery_date_raw else None
        if delivery_date_raw and expected_delivery_date is None:
            return JsonResponse({"success": False, "error": "Expected delivery date must be a valid date."}, status=400)

        order_qty = int(target_wo.quantity or 0)
        available_qty = None
        available_percent = None
        if status == "partial":
            percent_raw = data.get("available_percent")
            if percent_raw not in (None, ""):
                try:
                    available_percent = Decimal(str(percent_raw))
                except (InvalidOperation, ValueError):
                    return JsonResponse({"success": False, "error": "Available percent must be a number."}, status=400)
                if available_percent <= 0 or available_percent >= 100:
                    return JsonResponse({"success": False, "error": "Partial availability percent must be between 1 and 99."}, status=400)
                available_qty = int((Decimal(order_qty) * available_percent / Decimal("100")).to_integral_value(rounding=ROUND_FLOOR))
            else:
                try:
                    available_qty = int(data.get("available_qty"))
                except (TypeError, ValueError):
                    return JsonResponse({"success": False, "error": "Available percent is required for partial BOM readiness."}, status=400)
                if order_qty > 0:
                    available_percent = (Decimal(available_qty) * Decimal("100") / Decimal(order_qty)).quantize(Decimal("0.01"))
            if available_qty <= 0:
                return JsonResponse({"success": False, "error": "Partial availability must cover at least one production item."}, status=400)
            if available_qty >= order_qty:
                return JsonResponse({"success": False, "error": "Partial availability must be less than the WO quantity."}, status=400)
        if status == "shortage":
            available_qty = 0
            available_percent = Decimal("0")
        if status == "ready":
            available_qty = order_qty
            available_percent = Decimal("100")
            expected_delivery_date = None
            shortage_note = ""
        if status == "not_checked":
            shortage_note = ""
            expected_delivery_date = None

        target_wo.material_readiness_status = status
        target_wo.material_shortage_note = shortage_note
        target_wo.material_available_qty = available_qty
        target_wo.material_available_percent = available_percent
        target_wo.material_expected_delivery_date = expected_delivery_date
        target_wo.material_readiness_updated_at = timezone.now()
        target_wo.material_readiness_updated_by = request.user
        target_wo.save(
            update_fields=[
                "material_readiness_status",
                "material_shortage_note",
                "material_available_qty",
                "material_available_percent",
                "material_expected_delivery_date",
                "material_readiness_updated_at",
                "material_readiness_updated_by",
            ]
        )

        audit_request_action(
            request,
            "update",
            target=target_wo,
            details={
                "event": "material_readiness_updated",
                "work_order_id": target_wo.id,
                "status": status,
                "shortage_note": shortage_note,
                "available_qty": available_qty,
                "available_percent": str(available_percent) if available_percent is not None else None,
                "expected_delivery_date": expected_delivery_date.isoformat() if expected_delivery_date else "",
            },
        )

        if user_has_role(request.user, ['store']):
            NotificationService.notify_role(
                company,
                roles=['planner', 'admin'],
                title="Store material response",
                message=f"WO #{target_wo.id}: {target_wo.get_material_readiness_status_display()}. {shortage_note}".strip(),
                link=f"/manufacturing/dashboard/?wo={target_wo.id}",
                exclude_user=request.user,
            )

        return JsonResponse(
            {
                "success": True,
                "work_order_id": target_wo.id,
                "material_readiness": get_workorder_material_readiness_payload(target_wo, company),
            }
        )


class WorkOrderUpdateView(LoginRequiredMixin, View):
    """API to update status or assigned operator of a work order via AJAX."""
    def post(self, request, wo_id=None): # wo_id optional if passed in body, but URL usually has it
        try:
            if not user_has_role(request.user, ['planner', 'admin']):
                return JsonResponse({"success": False, "error": "Unauthorized"}, status=403)

            company = require_company(request.user)
            # URL usage: path('api/work-order/<int:wo_id>/update/', ...)
            # If wo_id is in URL, use it. Else check body/POST.
            
            # The original code handled two types of updates:
            # 1. update_work_order_status (drag drop on timeline) -> POST list parameters
            # 2. api_update_work_order_status (Shop console) -> JSON body
            
            # Let's handle both or separate them. 
            # Original 'update_work_order_status' (Line 471) used request.POST.get("id")
            # Original 'api_update_work_order_status' (Line 1612) used JSON body and URL param.
            
            # We will split this. This class is for the Timeline Drag/Drop (legacy name update_work_order_status)
            # We'll create another for the Shop Floor JSON update.
            return self.handle_timeline_update(request, company, wo_id)

        except Exception as e:
            return JsonResponse({"success": False, "error": str(e)})

    def handle_timeline_update(self, request, company, wo_id_url=None):
        payload = {}
        if request.content_type and 'application/json' in request.content_type:
            try:
                payload = json.loads(request.body or "{}")
            except Exception:
                payload = {}

        def get_param(key, default=None):
            val = request.POST.get(key, None)
            if val is None and key in payload:
                val = payload.get(key)
            return default if val is None else val

        wo_id = get_param("id") or wo_id_url
        status = get_param("status")
        assigned_to_id = get_param("assigned_to")

        try:
             wo = WorkOrder.objects.filter(company=company).get(id=wo_id)
        except WorkOrder.DoesNotExist:
             return JsonResponse({'success': False, 'error': f"Work Order #{wo_id} not found."})
        machine_changed = False
        original_start_date = wo.start_date
        original_end_date = wo.end_date
        original_machine_id = wo.machine_id
        original_quantity = wo.quantity
        
        if status:
            try:
                WorkOrderLifecycle.apply_transition(
                    wo,
                    status,
                    actor=request.user,
                    save=False,
                )
            except WorkOrderLifecycleError as exc:
                return JsonResponse({'success': False, 'error': str(exc)})
        if assigned_to_id:
            from django.contrib.auth.models import User
            wo.assigned_to = User.objects.filter(profile__company=company).get(id=assigned_to_id)
            
        stage_id = get_param("stage_id")
        if stage_id:
            stage = get_company_stage(company, stage_id)
            if not stage:
                return JsonResponse({'success': False, 'error': "Invalid stage for this company."})
            wo.current_stage_id = stage.id

        if get_param("start_date"):
            wo.start_date = parse_client_datetime(get_param("start_date"))

        if get_param("end_date"):
            wo.end_date = parse_client_datetime(get_param("end_date"))

        if wo.start_date and wo.end_date and wo.end_date <= wo.start_date:
            return JsonResponse(
                {'success': False, 'error': "End time must be after start time."},
                status=400,
            )

        quantity = get_param("quantity")
        if quantity not in [None, ""]:
            try:
                requested_quantity = int(quantity)
            except Exception:
                return JsonResponse({'success': False, 'error': "Invalid quantity"}, status=400)
            if requested_quantity <= 0:
                return JsonResponse({'success': False, 'error': "Quantity must be greater than zero."}, status=400)
            quantity_floor = get_workorder_reported_quantity_floor(wo)
            if requested_quantity < int(quantity_floor['reported']):
                return JsonResponse({
                    'success': False,
                    'error': f"Quantity cannot be below already reported output ({int(quantity_floor['reported'])}).",
                }, status=400)
            wo.quantity = requested_quantity
            if wo.base_quantity is not None:
                wo.base_quantity = max(
                    requested_quantity - int(getattr(wo, 'scrap_compensation_qty', 0) or 0),
                    0,
                )

        priority = get_param("priority")
        if priority:
            wo.priority = priority

        machine_id = get_param("machine_id")
        if machine_id and str(wo.machine_id) != str(machine_id):
            if wo.status in ['completed', 'canceled', 'archived']:
                return JsonResponse({'success': False, 'error': "Cannot change machine for a completed or inactive work order."})

            from django.db.models import Sum
            logged_qty = wo.production_logs.exclude(status='rejected').aggregate(
                total=Sum('quantity')
            )['total'] or 0

            if wo.status == 'in_progress' and logged_qty > 0:
                return JsonResponse({
                    'success': False,
                    'error': "Cannot change machine after production has started. Split the remaining quantity instead."
                })

            if wo.status == 'in_progress' and logged_qty == 0:
                return JsonResponse({
                    'success': False,
                    'error': "Cannot move an in-progress order back to pending. Create a new pending WO if you need re-planning."
                })

            machine_changed = True
            Machine.objects.filter(company=company, id=machine_id).get()  # Validate ownership
            wo.machine_id = machine_id
            
        if (
            wo.parent
            and wo.start_date
            and not WorkOrderService.has_parallel_stage_tasks(wo.parent)
        ):
            try:
                WorkOrderService.validate_series_stage_move(
                    wo.parent,
                    wo,
                    wo.start_date,
                )
            except ValueError as exc:
                return JsonResponse({'success': False, 'error': str(exc)}, status=400)

        wo.save()

        quantity_changed = original_quantity != wo.quantity
        quantity_replanned = False
        if quantity_changed:
            quantity_replanned = WorkOrderService.replan_after_quantity_change(wo)
            wo.refresh_from_db()

        timing_changed = (
            original_start_date != wo.start_date
            or original_end_date != wo.end_date
        )
        structure_changed = (
            machine_changed
            or timing_changed
            or quantity_replanned
            or (original_machine_id != wo.machine_id)
        )

        if structure_changed and user_has_role(request.user, ['planner', 'admin']):
            if wo.parent:
                if not WorkOrderService.has_parallel_stage_tasks(wo.parent):
                    WorkOrderService.reschedule_subtasks_from_anchor(wo.parent, anchor_task=wo)
            elif wo.sub_tasks.exists():
                if not WorkOrderService.has_parallel_stage_tasks(wo):
                    WorkOrderService.reschedule_subtasks(wo)

        if any([
            status,
            assigned_to_id,
            stage_id,
            get_param("start_date"),
            get_param("end_date"),
            quantity not in [None, ""],
            priority,
            machine_changed,
        ]):
            audit_request_action(
                request,
                'update',
                target=wo,
                details={
                    'event': 'work_order_updated_from_timeline',
                    'status': wo.status,
                    'machine_id': wo.machine_id,
                    'stage_id': wo.current_stage_id,
                    'start_date': wo.start_date.isoformat() if wo.start_date else None,
                    'end_date': wo.end_date.isoformat() if wo.end_date else None,
                    'quantity': wo.quantity,
                    'priority': wo.priority,
                    'rescheduled': bool(structure_changed),
                    'quantity_changed': bool(quantity_changed),
                },
            )

        return JsonResponse({"success": True, "rescheduled": structure_changed})


class PlannerUndoRestoreAPI(LoginRequiredMixin, View):
    """Restore the last accepted planner scheduling snapshot."""

    RESTOREABLE_STATUSES = {'pending', 'in_progress'}

    def post(self, request):
        try:
            if not user_has_role(request.user, ['planner', 'admin']):
                return JsonResponse({"success": False, "error": "Unauthorized"}, status=403)

            company = require_company(request.user)
            data = json.loads(request.body or "{}")
            raw_items = data.get("items") or []
            if not isinstance(raw_items, list) or not raw_items:
                return JsonResponse({"success": False, "error": "No undo snapshot provided."}, status=400)

            restore_items = []
            delete_items = []

            for entry in raw_items:
                if not isinstance(entry, dict):
                    continue
                try:
                    work_order_id = int(entry.get("id"))
                except (TypeError, ValueError):
                    return JsonResponse({"success": False, "error": "Invalid undo snapshot item."}, status=400)

                if entry.get("delete"):
                    delete_items.append(work_order_id)
                    continue

                restore_items.append({
                    "id": work_order_id,
                    "fields": entry.get("fields") or {},
                })

            if not restore_items and not delete_items:
                return JsonResponse({"success": False, "error": "Undo snapshot is empty."}, status=400)

            with transaction.atomic():
                for item in restore_items:
                    wo = get_object_or_404(WorkOrder, id=item["id"], company=company)
                    self._validate_restoreable(wo)
                    self._apply_restore_fields(wo, item["fields"], company)

                for work_order_id in delete_items:
                    wo = get_object_or_404(WorkOrder, id=work_order_id, company=company)
                    self._validate_restoreable(wo)
                    if wo.sub_tasks.exists():
                        return JsonResponse(
                            {"success": False, "error": f"WO #{wo.id} cannot be removed because it has child tasks."},
                            status=400,
                        )
                    wo.delete()

            return JsonResponse({"success": True, "message": "Last planning action was undone."})
        except ValueError as exc:
            return JsonResponse({"success": False, "error": str(exc)}, status=400)
        except Exception as exc:
            return JsonResponse({"success": False, "error": str(exc)}, status=500)

    def _validate_restoreable(self, work_order):
        if work_order.production_logs.exclude(status='rejected').exists():
            raise ValueError(f"WO #{work_order.id} cannot be undone because production has started.")
        if str(work_order.status or '').strip().lower() not in self.RESTOREABLE_STATUSES:
            raise ValueError(
                f"WO #{work_order.id} cannot be undone from status '{work_order.status}'."
            )

    def _apply_restore_fields(self, work_order, fields, company):
        machine_id = fields.get("machine_id")
        stage_id = fields.get("stage_id")
        current_stage_id = fields.get("current_stage_id")
        assigned_worker_id = fields.get("assigned_worker_id")

        update_fields = []

        def set_field(name, value):
            setattr(work_order, name, value)
            update_fields.append(name)

        if "status" in fields:
            status_value = str(fields.get("status") or "").strip().lower() or "pending"
            if status_value == "draft":
                status_value = "pending"
            if status_value not in self.RESTOREABLE_STATUSES:
                raise ValueError(f"Undo cannot restore WO #{work_order.id} to status '{status_value}'.")
            set_field("status", status_value)

        if "machine_id" in fields:
            if machine_id in [None, ""]:
                set_field("machine", None)
            else:
                machine = get_object_or_404(Machine, id=machine_id, company=company)
                set_field("machine", machine)

        if "stage_id" in fields:
            if stage_id in [None, ""]:
                set_field("stage", None)
            else:
                stage = get_company_stage(company, stage_id)
                if not stage:
                    raise ValueError(f"Invalid stage for WO #{work_order.id}.")
                set_field("stage", stage)

        if "current_stage_id" in fields:
            if current_stage_id in [None, ""]:
                set_field("current_stage", None)
            else:
                current_stage = get_company_stage(company, current_stage_id)
                if not current_stage:
                    raise ValueError(f"Invalid current stage for WO #{work_order.id}.")
                set_field("current_stage", current_stage)

        if "start_date" in fields:
            parsed = parse_client_datetime(fields.get("start_date")) if fields.get("start_date") else None
            set_field("start_date", parsed)

        if "end_date" in fields:
            parsed = parse_client_datetime(fields.get("end_date")) if fields.get("end_date") else None
            set_field("end_date", parsed)

        if "scheduled_start_date" in fields:
            parsed = parse_client_datetime(fields.get("scheduled_start_date")) if fields.get("scheduled_start_date") else None
            set_field("scheduled_start_date", parsed)
        elif "start_date" in fields:
            parsed = parse_client_datetime(fields.get("start_date")) if fields.get("start_date") else None
            set_field("scheduled_start_date", parsed)

        if "operation_flow_mode" in fields:
            mode = str(fields.get("operation_flow_mode") or "").strip().lower() or "series"
            if mode not in {"series", "parallel"}:
                mode = "series"
            set_field("operation_flow_mode", mode)

        if "next_stage_ready" in fields:
            set_field("next_stage_ready", bool(fields.get("next_stage_ready")))

        if "planner_action_required" in fields:
            set_field("planner_action_required", bool(fields.get("planner_action_required")))

        if "closed_by_planner" in fields:
            set_field("closed_by_planner", bool(fields.get("closed_by_planner")))

        if "assigned_worker_id" in fields:
            if assigned_worker_id in [None, ""]:
                set_field("assigned_worker", None)
            else:
                from django.contrib.auth.models import User
                worker = get_object_or_404(User, id=assigned_worker_id, profile__company=company)
                set_field("assigned_worker", worker)

        if "assignment_type" in fields:
            assignment_type = str(fields.get("assignment_type") or "").strip().lower() or "auto"
            if assignment_type not in {"auto", "manual"}:
                assignment_type = "auto"
            set_field("assignment_type", assignment_type)

        if "planner_start_at" in fields:
            parsed = parse_client_datetime(fields.get("planner_start_at")) if fields.get("planner_start_at") else None
            set_field("planner_start_at", parsed)

        if update_fields:
            work_order.save(update_fields=list(dict.fromkeys(update_fields)))

class ShopFloorUpdateView(LoginRequiredMixin, View):
    """API for Shop Floor Kiosk updates (JSON)."""
    def post(self, request, wo_id):
        try:
            data = json.loads(request.body)
            new_status = data.get('status')
            company = require_company(request.user)
            wo = get_object_or_404(WorkOrder, id=wo_id, company=company)
            strict_worker_mode = acts_as_worker(request.user)

            if not user_has_role(request.user, ['worker', 'supervisor', 'admin']):
                return JsonResponse({'status': 'error', 'message': 'Unauthorized'}, status=403)

            if not can_user_see_work_order(request.user, wo):
                return JsonResponse({'status': 'error', 'message': 'Unauthorized'}, status=403)

            if wo.assigned_worker and wo.assigned_worker != request.user:
                if strict_worker_mode or not user_has_role(request.user, ['supervisor', 'admin']):
                    return JsonResponse({'status': 'error', 'message': 'Not assigned to this work order'}, status=403)

            if not wo.assigned_worker and (strict_worker_mode or not user_has_role(request.user, ['supervisor', 'admin'])):
                return JsonResponse({'status': 'error', 'message': 'Work order has no assigned worker'}, status=403)

            if new_status:
                if new_status not in ['in_progress', 'completed']:
                    return JsonResponse({'status': 'error', 'message': 'Invalid status'}, status=400)

                if new_status == 'in_progress':
                    readiness_actor = request.user if strict_worker_mode else None
                    readiness = get_workorder_execution_readiness(wo, actor=readiness_actor)
                    if not readiness.get('can_start'):
                        audit_request_action(
                            request,
                            'update',
                            target=wo,
                            details={
                                'event': 'production_start_blocked',
                                'work_order_id': wo.id,
                                'reason_code': readiness.get('reason_code'),
                                'reason': readiness.get('reason'),
                            },
                        )
                        return JsonResponse(
                            {
                                'status': 'error',
                                'message': readiness.get('reason') or 'Production cannot start yet.',
                                'reason_code': readiness.get('reason_code'),
                                'readiness': readiness,
                            },
                            status=409,
                        )

                try:
                    WorkOrderLifecycle.apply_transition(
                        wo,
                        new_status,
                        actor=request.user,
                        save=False,
                    )
                except WorkOrderLifecycleError as exc:
                    return JsonResponse({'status': 'error', 'message': str(exc)}, status=400)

                update_fields = ['status']
                if new_status == 'in_progress':
                    now = timezone.now()
                    if not wo.assigned_to:
                        wo.assigned_to = request.user
                        update_fields.append('assigned_to')
                    if not wo.assigned_worker and user_has_role(request.user, ['supervisor', 'admin']) and not strict_worker_mode:
                        wo.assigned_worker = request.user
                        wo.assignment_type = 'manual'
                        update_fields.extend(['assigned_worker', 'assignment_type'])
                    if wo.start_date and not wo.scheduled_start_date:
                        wo.scheduled_start_date = wo.start_date
                        update_fields.append('scheduled_start_date')
                    wo.start_date = now
                    update_fields.append('start_date')
                    # Track real worker start independently for reporting.
                    if not wo.worker_start_at:
                        wo.worker_start_at = now
                        update_fields.append('worker_start_at')
                
                if new_status == 'completed':
                    wo.end_date = timezone.now()
                    update_fields.append('end_date')
                    
                wo.save(update_fields=list(dict.fromkeys(update_fields)))
                audit_request_action(
                    request,
                    'update',
                    target=wo,
                    details={
                        'event': 'production_start_approved' if new_status == 'in_progress' else 'shop_floor_status_updated',
                        'status': wo.status,
                        'assigned_worker_id': wo.assigned_worker_id,
                        'worker_start_at': wo.worker_start_at.isoformat() if wo.worker_start_at else None,
                        'end_date': wo.end_date.isoformat() if wo.end_date else None,
                    },
                )
                return JsonResponse({'status': 'success', 'message': f'Order #{wo.id} updated'})
            return JsonResponse({'status': 'error', 'message': 'No status provided'})
        except Exception as e:
            return JsonResponse({'status': 'error', 'message': str(e)})


class WorkOrderDetailsAPI(LoginRequiredMixin, View):
    def get(self, request, wo_id):
        try:
            from django.db.models import Q
            from manufacturing.models import ProductionStage, BOMOperation

            company = require_company(request.user)
            wo = (
                WorkOrder.objects.select_related("parent", "bom", "parent__bom")
                .get(id=wo_id, company=company)
            )
            if not can_user_see_work_order(request.user, wo):
                return JsonResponse({"success": False, "error": "Unauthorized"}, status=403)
            route_bom = resolve_bom_for_work_order(wo, company)
            base_qty, compensation_qty, adjusted_qty = get_workorder_quantity_breakdown(wo)
            approved_qty = wo.production_logs.filter(status='approved').aggregate(
                total=Sum('quantity')
            )['total'] or 0
            pending_qty = wo.production_logs.filter(status='pending').aggregate(
                total=Sum('quantity')
            )['total'] or 0
            produced_qty = int(approved_qty) + int(pending_qty)
            remaining_qty = max(int(adjusted_qty) - int(produced_qty), 0)
            released_qty = int(getattr(wo, 'released_qty', 0) or 0)
            qc_required = bool(getattr(wo, 'qc_requirement', False) or (wo.stage and wo.stage.is_quality_check))
            qc_pending = False
            qc_good_qty = 0
            if qc_required:
                from manufacturing.models import QualityCheck
                qc_pending = QualityCheck.objects.filter(work_order=wo, status='new').exists()
                qc_good_qty = QualityCheck.objects.filter(
                    work_order=wo,
                    status='processed'
                ).aggregate(total=Sum('good_quantity'))['total'] or 0
                available_release_qty = max(int(qc_good_qty) - released_qty, 0)
            else:
                # Only approved quantity can be released to next stage
                available_release_qty = max(int(approved_qty) - released_qty, 0)
            active_machine_objects = list(
                Machine.objects.filter(company=company, is_active=True)
            )

            def build_machine_payload(machine_obj):
                if not machine_obj:
                    return None
                return {
                    'id': machine_obj.id,
                    'name': machine_obj.name,
                    'display_name': machine_obj.display_label,
                    'code': machine_obj.code,
                    'status': machine_obj.status,
                    'type': machine_obj.type,
                    'category': machine_obj.category,
                }

            def machine_matches_required_type(machine_obj, required_type):
                target = str(required_type or '').strip().lower()
                if not target:
                    return True
                haystack = ' '.join([
                    str(getattr(machine_obj, 'name', '') or ''),
                    str(getattr(machine_obj, 'type', '') or ''),
                    str(getattr(machine_obj, 'category', '') or ''),
                ]).strip().lower()
                if not haystack:
                    return False
                if target in haystack:
                    return True
                hay_tokens = set(haystack.replace('-', ' ').replace('_', ' ').split())
                target_tokens = set(target.replace('-', ' ').replace('_', ' ').split())
                return bool(hay_tokens.intersection(target_tokens))

            machines = [
                build_machine_payload(machine_obj)
                for machine_obj in active_machine_objects
                if machine_obj
            ]
            # Fallback current stage: if missing, assume first BOM operation
            effective_current_stage_id = wo.current_stage_id or wo.stage_id
            if not effective_current_stage_id and route_bom:
                first_op = route_bom.operations.select_related('stage').order_by('order').first()
                if first_op and first_op.stage_id:
                    effective_current_stage_id = first_op.stage_id
            company_default_operation_flow_mode = get_company_default_operation_flow_mode(company)
            effective_operation_flow_mode = get_work_order_operation_flow_mode(wo)
            existing_route_tasks = {}
            active_split_children_by_source = {}
            route_container_wo = wo.parent if wo.parent_id and wo.parent else wo
            if route_container_wo:
                for child in (
                    WorkOrder.objects.filter(company=company, parent=route_container_wo)
                    .exclude(status__in=['canceled', 'archived'])
                    .select_related('machine')
                    .order_by('stage_id', 'id')
                ):
                    stage_key = child.stage_id or child.current_stage_id
                    if stage_key and stage_key not in existing_route_tasks:
                        existing_route_tasks[stage_key] = child
                    if child.source_task_id:
                        active_split_children_by_source.setdefault(child.source_task_id, []).append(child)

            def build_stage_candidate_machines(stage_obj, *, machine_type_hint=None, default_machine_id=None):
                required_type = (
                    machine_type_hint
                    or (stage_obj.category if stage_obj else None)
                    or (stage_obj.machine.category if stage_obj and stage_obj.machine else None)
                    or (stage_obj.machine.type if stage_obj and stage_obj.machine else None)
                )
                candidates = []
                seen_machine_ids = set()

                preferred_ids = [
                    default_machine_id,
                    (stage_obj.machine_id if stage_obj else None),
                ]
                for machine_id in preferred_ids:
                    if not machine_id:
                        continue
                    machine_obj = next((machine for machine in active_machine_objects if machine.id == machine_id), None)
                    if machine_obj and machine_obj.id not in seen_machine_ids:
                        payload = build_machine_payload(machine_obj)
                        if payload:
                            candidates.append(payload)
                            seen_machine_ids.add(machine_obj.id)

                for machine_obj in active_machine_objects:
                    if machine_obj.id in seen_machine_ids:
                        continue
                    if not machine_matches_required_type(machine_obj, required_type):
                        continue
                    payload = build_machine_payload(machine_obj)
                    if payload:
                        candidates.append(payload)
                        seen_machine_ids.add(machine_obj.id)

                return candidates

            def build_stage_payload(stage_obj, *, order_value=None, default_machine_id=None, machine_type_hint=None, is_bom_stage=False, operation=None):
                if not stage_obj:
                    return None
                planned_task = existing_route_tasks.get(stage_obj.id)
                split_children = active_split_children_by_source.get(planned_task.id, []) if planned_task else []
                setup_time = float(getattr(operation, 'setup_time', 0) or 0) if operation else 0
                run_time = float(getattr(operation, 'run_time', 0) or 0) if operation else 0
                duration_minutes = int(getattr(operation, 'duration_minutes', 0) or 0) if operation else 0
                estimated_duration_minutes = 0
                if operation:
                    estimated_duration_minutes = int(round(setup_time + (run_time * float(adjusted_qty or 0))))
                    if estimated_duration_minutes <= 0:
                        estimated_duration_minutes = duration_minutes
                return {
                    'id': stage_obj.id,
                    'name': stage_obj.name,
                    'order': order_value if order_value is not None else stage_obj.order,
                    'setup_time': setup_time,
                    'run_time': run_time,
                    'duration_minutes': duration_minutes,
                    'estimated_duration_minutes': estimated_duration_minutes,
                    'default_machine_id': default_machine_id if default_machine_id is not None else stage_obj.machine_id,
                    'assigned_machine_id': planned_task.machine_id if planned_task else None,
                    'planned_task_id': planned_task.id if planned_task else None,
                    'split_child_ids': [child.id for child in split_children],
                    'split_child_count': len(split_children),
                    'planned_start_date': planned_task.start_date.isoformat() if planned_task and planned_task.start_date else None,
                    'planned_end_date': planned_task.end_date.isoformat() if planned_task and planned_task.end_date else None,
                    'is_bom_stage': bool(is_bom_stage),
                    'machine_type': (
                        machine_type_hint
                        or stage_obj.category
                        or (stage_obj.machine.category if stage_obj.machine else None)
                        or (stage_obj.machine.type if stage_obj.machine else None)
                        or (stage_obj.name.split(':')[-1].strip() if stage_obj.name else None)
                    ),
                    'candidate_machines': build_stage_candidate_machines(
                        stage_obj,
                        machine_type_hint=machine_type_hint,
                        default_machine_id=default_machine_id,
                    ),
                }

            stages_payload = []
            seen_stage_ids = set()

            # 1) BOM stages first (in BOM order) so WO flow stays intuitive.
            if route_bom:
                bom_ops = route_bom.operations.select_related('stage', 'machine', 'stage__machine').order_by('order')
                for op in bom_ops:
                    if not op.stage_id or op.stage_id in seen_stage_ids:
                        continue
                    entry = build_stage_payload(
                        op.stage,
                        order_value=op.order,
                        default_machine_id=(op.machine.id if op.machine else None),
                        machine_type_hint=(
                            op.machine_type
                            or (op.machine.category if op.machine else None)
                            or (op.machine.type if op.machine else None)
                            or (op.stage.category if op.stage else None)
                        ),
                        is_bom_stage=True,
                        operation=op,
                    )
                    if entry:
                        stages_payload.append(entry)
                        seen_stage_ids.add(op.stage_id)

            # 2) Add remaining company stages (created in Factory Setup but not yet in BOM).
            stage_ids_from_boms = (
                BOMOperation.objects.filter(
                    bom__product__company=company,
                    stage_id__isnull=False,
                )
                .values_list("stage_id", flat=True)
                .distinct()
            )
            company_stages = (
                ProductionStage.objects.filter(
                    Q(machine__company=company) | Q(id__in=stage_ids_from_boms)
                )
                .select_related('machine')
                .distinct()
                .order_by('order', 'name', 'id')
            )
            for stage in company_stages:
                if stage.id in seen_stage_ids:
                    continue
                entry = build_stage_payload(stage)
                if entry:
                    stages_payload.append(entry)
                    seen_stage_ids.add(stage.id)

            route_stages = [stage for stage in stages_payload if stage.get('is_bom_stage')]
            route_plannable = (not wo.parent_id) and bool(route_stages)
            material_source_wo = wo.parent if wo.parent_id and wo.parent else wo
            material_readiness = get_workorder_material_readiness_payload(material_source_wo, company)
            execution_readiness = get_workorder_execution_readiness(wo)
            latest_bom = get_latest_active_bom_for_work_order(wo)
            can_apply_latest_bom, apply_latest_bom_blocker = get_apply_latest_bom_eligibility(wo)
            bom_change = get_workorder_bom_change_payload(wo)
            bom_attachment = serialize_bom_attachment(route_bom, request)

            data = {
                'success': True,
                'work_order': {
                    'id': wo.id,
                    'parent_id': wo.parent_id,
                    'source_task_id': getattr(wo, 'source_task_id', None),
                    'display_work_order_id': wo.parent_id or wo.id,
                    'display_work_order_code': f"WO-{wo.parent_id or wo.id}",
                    'product_name': wo.product_name,
                    'quantity': adjusted_qty,
                    'base_quantity': int(base_qty),
                    'scrap_compensation_qty': int(compensation_qty),
                    'has_scrap_compensation': bool(compensation_qty > 0),
                    'is_scrap_compensation_task': bool(getattr(wo, 'is_scrap_compensation_task', False)),
                    'scrap_source_qc_id': getattr(wo, 'scrap_source_quality_check_id', None),
                    'approved_qty': int(approved_qty),
                    'pending_qty': int(pending_qty),
                    'produced_qty': int(produced_qty),
                    'remaining_qty': int(remaining_qty),
                    'released_qty': int(released_qty),
                    'available_release_qty': int(available_release_qty),
                    'qc_required': qc_required,
                    'qc_pending': qc_pending,
                    'qc_good_qty': int(qc_good_qty),
                    'status': wo.status,
                    'priority': wo.priority,
                    'progress': float(wo.progress),
                    'start_date': wo.start_date.isoformat() if wo.start_date else None,
                    'end_date': wo.end_date.isoformat() if wo.end_date else None,
                    'machine_id': wo.machine.id if wo.machine else None,
                    'machine_name': wo.machine.display_label if wo.machine else None,
                    'assigned_to_id': wo.assigned_to.id if wo.assigned_to else None,
                    'assigned_to_name': wo.assigned_to.username if wo.assigned_to else None,
                    'assigned_worker': wo.assigned_worker.id if wo.assigned_worker else None,
                    'assigned_worker_name': wo.assigned_worker.username if wo.assigned_worker else None,
                    'assignment_type': wo.assignment_type,
                    'instructions': wo.instructions,
                    'current_stage_id': effective_current_stage_id,
                    'stage_id': wo.stage_id,
                    'operation_flow_mode': effective_operation_flow_mode,
                    'company_default_operation_flow_mode': company_default_operation_flow_mode,
                    'cycle_state': WorkOrderCycleService.describe(wo),
                    'next_stage_ready': getattr(wo, 'next_stage_ready', False),
                    'has_sub_tasks': wo.sub_tasks.exists(),
                    'route_container': route_plannable,
                    'material_readiness_status': material_readiness["status"],
                    'material_shortage_note': material_readiness["shortage_note"],
                    'material_readiness': material_readiness,
                    'execution_readiness': execution_readiness,
                    'bom_id': wo.bom_id,
                    'bom_version': wo.bom_version or (wo.bom.version if wo.bom else ''),
                    'bom_attachment': bom_attachment,
                    'latest_bom_id': latest_bom.id if latest_bom else None,
                    'latest_bom_version': latest_bom.version if latest_bom else '',
                    'can_apply_latest_bom': can_apply_latest_bom,
                    'apply_latest_bom_blocker': apply_latest_bom_blocker,
                    'bom_change': bom_change,
                    'bom_change_status': bom_change["status"],
                    'bom_change_action_required': bom_change["action_required"],
                },
                'machines': machines,
                'stages': stages_payload,
                'route_stages': route_stages if route_plannable else [],
            }
            return JsonResponse(data)
        except Exception as e:
            return JsonResponse({'success': False, 'error': str(e)})


class WorkOrderLogAPI(LoginRequiredMixin, View):
    def get(self, request, wo_id):
        try:
            company = require_company(request.user)
            wo = get_object_or_404(WorkOrder, id=wo_id, company=company)
            AuditLog = apps.get_model("manufacturing", "AuditLog")
            # Include logs of child stage tasks when viewing a parent WO row.
            target_wo_ids = [wo.id]
            target_wo_ids.extend(list(WorkOrder.objects.filter(parent=wo).values_list('id', flat=True)))

            scoped_wos = WorkOrder.objects.filter(id__in=target_wo_ids).prefetch_related(
                'production_logs__worker',
                'production_logs__reviewed_by',
            )
            prod_logs = []
            for entry in scoped_wos:
                prod_logs.extend(list(entry.production_logs.all()))
            prod_logs.sort(key=lambda x: x.created_at, reverse=True)

            change_logs = WorkOrderChangeLog.objects.filter(
                work_order_id__in=target_wo_ids
            ).select_related('changed_by', 'work_order').order_by('-created_at')
            audit_logs = (
                AuditLog.objects.filter(company=company)
                .filter(
                    Q(model_name='WorkOrder', object_id__in=target_wo_ids)
                    | Q(details__work_order_id__in=target_wo_ids)
                    | Q(details__source_work_order_id__in=target_wo_ids)
                    | Q(details__target_work_order_id__in=target_wo_ids)
                )
                .exclude(model_name='ProductionLog')
                .select_related('user')
                .order_by('-timestamp')
            )

            items = []
            for log in prod_logs:
                action_title = "Production logged"
                if log.work_order_id != wo.id:
                    action_title = f"Production logged (Stage WO #{log.work_order_id})"
                items.append({
                    "timestamp": log.created_at.strftime("%Y-%m-%d %H:%M"),
                    "sort_ts": log.created_at.isoformat(),
                    "action": action_title,
                    "quantity": log.quantity,
                    "status": log.status,
                    "source": "production",
                    "worker": log.worker.username if log.worker else None,
                    "actor": log.worker.username if log.worker else None,
                    "reviewed_by": log.reviewed_by.username if log.reviewed_by else None,
                    "reviewed_at": log.reviewed_at.strftime("%Y-%m-%d %H:%M") if log.reviewed_at else None,
                    "event": "production_logged",
                    "note": log.note or "",
                    "related_work_order_id": log.work_order_id,
                    "scope": "route" if log.work_order_id == wo.id else "stage",
                })

            for ch in change_logs:
                details = ""
                if ch.old_value or ch.new_value:
                    details = f"{ch.old_value or '-'} -> {ch.new_value or '-'}"
                action_title = ch.action or "Work order updated"
                if ch.work_order_id != wo.id:
                    action_title = f"{action_title} (Stage WO #{ch.work_order_id})"
                items.append({
                    "timestamp": ch.created_at.strftime("%Y-%m-%d %H:%M"),
                    "sort_ts": ch.created_at.isoformat(),
                    "action": action_title,
                    "quantity": None,
                    "status": "change",
                    "source": "change",
                    "editor": ch.changed_by.username if ch.changed_by else None,
                    "actor": ch.changed_by.username if ch.changed_by else None,
                    "details": details,
                    "note": ch.note or "",
                    "related_work_order_id": ch.work_order_id,
                    "scope": "route" if ch.work_order_id == wo.id else "stage",
                })

            for entry in audit_logs:
                details = entry.details or {}
                event_label = readable_audit_event(details.get("event"))
                action_title = event_label or f"{entry.action.title()} event"
                related_work_order_id = None
                if entry.model_name == 'WorkOrder' and entry.object_id in target_wo_ids:
                    related_work_order_id = entry.object_id
                else:
                    for key in ("work_order_id", "source_work_order_id", "target_work_order_id"):
                        candidate_id = details.get(key)
                        if candidate_id in target_wo_ids:
                            related_work_order_id = candidate_id
                            break
                if related_work_order_id and related_work_order_id != wo.id:
                    action_title = f"{action_title} (Stage WO #{related_work_order_id})"

                items.append({
                    "timestamp": entry.timestamp.strftime("%Y-%m-%d %H:%M"),
                    "sort_ts": entry.timestamp.isoformat(),
                    "action": action_title,
                    "quantity": details.get("quantity"),
                    "status": entry.action,
                    "source": "audit",
                    "actor": entry.user.username if entry.user else None,
                    "editor": entry.user.username if entry.user else None,
                    "event": details.get("event"),
                    "event_label": event_label,
                    "details": audit_summary_text(details),
                    "note": details.get("notes") or details.get("note") or "",
                    "model_name": entry.model_name,
                    "target_id": entry.object_id,
                    "related_work_order_id": related_work_order_id,
                    "scope": "route" if not related_work_order_id or related_work_order_id == wo.id else "stage",
                })

            items.sort(key=lambda x: x.get("sort_ts", ""), reverse=True)
            for item in items:
                item.pop("sort_ts", None)

            return JsonResponse({"success": True, "logs": items})
        except Exception as e:
            return JsonResponse({"success": False, "error": str(e)})

    def post(self, request, wo_id):
        # Handle 'update_work_order' logic (Edit Modal)
        try:
            if not user_has_role(request.user, ['planner', 'admin']):
                return JsonResponse({'success': False, 'error': 'Unauthorized'}, status=403)

            company = require_company(request.user)
            wo = WorkOrder.objects.get(id=wo_id, company=company)
            
            wo.product_name = request.POST.get('product_name', wo.product_name)
            qty_value = request.POST.get('quantity')
            if qty_value:
                wo.quantity = int(qty_value)
            new_status = request.POST.get('status')
            if new_status:
                try:
                    WorkOrderLifecycle.apply_transition(
                        wo,
                        new_status,
                        actor=request.user,
                        save=False,
                    )
                except WorkOrderLifecycleError as exc:
                    return JsonResponse({'success': False, 'error': str(exc)}, status=400)
            wo.priority = request.POST.get('priority', wo.priority)
            
            start_date = request.POST.get('start_date')
            if start_date:
                wo.start_date = parse_client_datetime(start_date)
                
            end_date = request.POST.get('end_date')
            if end_date:
                wo.end_date = parse_client_datetime(end_date)

            wo.save()
            return JsonResponse({'success': True, 'message': 'Updated successfully'})
        except Exception as e:
            return JsonResponse({'success': False, 'error': str(e)})


class MachineLogAPI(LoginRequiredMixin, View):
    def get(self, request, machine_id):
        try:
            company = require_company(request.user)
            machine = get_object_or_404(Machine, id=machine_id, company=company)
            AuditLog = apps.get_model("manufacturing", "AuditLog")

            audit_logs = (
                AuditLog.objects.filter(company=company)
                .filter(
                    Q(model_name='Machine', object_id=machine.id)
                    | Q(details__machine_id=machine.id)
                    | Q(details__target_machine_id=machine.id)
                )
                .select_related('user')
                .order_by('-timestamp')[:100]
            )

            items = []
            for entry in audit_logs:
                details = entry.details or {}
                event_label = readable_audit_event(details.get("event"))
                action_title = event_label or f"{entry.action.title()} event"

                items.append({
                    "timestamp": entry.timestamp.strftime("%Y-%m-%d %H:%M"),
                    "sort_ts": entry.timestamp.isoformat(),
                    "action": action_title,
                    "status": entry.action,
                    "source": "audit",
                    "actor": entry.user.username if entry.user else None,
                    "event_label": event_label,
                    "details": audit_summary_text(details),
                    "note": details.get("description") or details.get("notes") or details.get("note") or "",
                    "event": details.get("event"),
                    "model_name": entry.model_name,
                    "target_id": entry.object_id,
                })

            return JsonResponse({
                "success": True,
                "machine": {
                    "id": machine.id,
                    "name": machine.name,
                    "display_name": machine.display_label,
                },
                "logs": items,
            })
        except Exception as e:
            return JsonResponse({"success": False, "error": str(e)})


class WOMaterialsAPI(LoginRequiredMixin, View):
    def get(self, request, wo_id):
        try:
            wo = WorkOrder.objects.get(id=wo_id, company=require_company(request.user))
            materials = []
            if wo.bom:
                components_qs = wo.bom.components.all()
                current_stage_id = wo.current_stage_id or wo.stage_id
                current_machine_id = wo.machine_id
                target_operation = None

                operation_qs = wo.bom.operations.all()
                if current_stage_id:
                    target_operation = operation_qs.filter(stage_id=current_stage_id).order_by('order').first()
                if target_operation is None and current_machine_id:
                    target_operation = operation_qs.filter(machine_id=current_machine_id).order_by('order').first()

                if target_operation is not None:
                    component_ids = list(target_operation.material_links.values_list('component_id', flat=True))
                    if component_ids:
                        components_qs = components_qs.filter(id__in=component_ids)

                for comp in components_qs:
                    ratio = (comp.quantity / wo.bom.base_quantity) if wo.bom.base_quantity else 0
                    materials.append({
                        "name": comp.material_name,
                        "qty": round(ratio * wo.quantity, 3),
                        "unit": comp.unit
                    })
            return JsonResponse({"success": True, "materials": materials})
        except Exception as e:
             return JsonResponse({"error": str(e)}, status=500)

class WOCriteriaAPI(LoginRequiredMixin, View):
    def get(self, request, wo_id):
        try:
            wo = WorkOrder.objects.get(id=wo_id, company=require_company(request.user))
            if not wo.bom: return JsonResponse({"success": False, "error": "No BOM assigned."})
            criteria = wo.bom.acceptance_criteria.all().values(
                 'id', 'parameter', 'method', 'criteria_min', 'criteria_max', 
                 'target_value', 'tolerance', 'pass_fail', 'is_critical'
            )
            return JsonResponse({"success": True, "criteria": list(criteria)})
        except Exception as e:
             return JsonResponse({"success": False, "error": str(e)})

# ---------------------------------------------------------------------
# 🔮 Simulation & Analysis
# ---------------------------------------------------------------------

class SimulationView(LoginRequiredMixin, View):
    def post(self, request):
        try:
            data = json.loads(request.body)
            bom_id = data.get("bom_id")
            quantity = int(data.get("quantity", 1))
            
            company = require_company(request.user)
            bom = BillOfMaterial.objects.filter(product__company=company).get(id=bom_id)
            
            result = BOMService.simulate_run(bom, quantity)
            return JsonResponse({"success": True, "data": result})
        except Exception as e:
            return JsonResponse({"success": False, "error": str(e)})

class QualityAnalysisView(LoginRequiredMixin, View):
    def post(self, request):
        try:
            if 'image' not in request.FILES: return JsonResponse({"success": False, "error": "No image"})
            result = QualityService.analyze_image(request.FILES['image'])
            return JsonResponse({"success": True, "data": result})
        except Exception as e:
             return JsonResponse({"success": False, "error": str(e)})

# ---------------------------------------------------------------------
# 🔔 Notifications
# ---------------------------------------------------------------------
class NotificationAPI(LoginRequiredMixin, View):
    def get(self, request):
        notifs = Notification.objects.filter(recipient=request.user, is_read=False).order_by('-created_at')
        data = []
        for n in notifs:
            link = n.link or ""
            if "/manufacturing/planner/" in link:
                link = link.replace("/manufacturing/planner/", "/manufacturing/dashboard/")
            data.append({
                "id": n.id,
                "title": n.title,
                "message": n.message,
                "link": link,
                "created_at": n.created_at.strftime("%Y-%m-%d %H:%M"),
            })
        return JsonResponse({"success": True, "notifications": data})

class NotificationReadView(LoginRequiredMixin, View):
    def get(self, request, notif_id):
        try:
            n = Notification.objects.get(id=notif_id, recipient=request.user)
            n.is_read = True
            n.save()
            return JsonResponse({"success": True})
        except: return JsonResponse({"success": False, "error": "Not found"})

# ---------------------------------------------------------------------
# 📊 Timeline Data
# ---------------------------------------------------------------------
class TimelineDataView(LoginRequiredMixin, View):
    def get(self, request):
        company = require_company(request.user)
        if not company:
            return JsonResponse(
                {
                    "success": False,
                    "error": "Company context not found for the current tenant session.",
                },
                status=409,
            )
        include_unscheduled = request.GET.get('include_unscheduled') in ['1', 'true', 'yes']
        if not include_unscheduled:
            include_unscheduled = user_has_role(request.user, ['planner', 'admin'])
        status_filter = str(request.GET.get('status') or '').strip().lower()
        if status_filter not in {'canceled'}:
            status_filter = None

        if user_has_role(request.user, ['planner', 'admin']):
            viewer_role = 'planner'
        elif user_has_role(request.user, ['supervisor']):
            viewer_role = 'supervisor'
        elif user_has_role(request.user, ['worker']):
            viewer_role = 'worker'
        else:
            viewer_role = 'supervisor'

        data = DashboardService.get_timeline_data(
            company,
            include_unscheduled=include_unscheduled,
            viewer_role=viewer_role,
            viewer=request.user,
            status_filter=status_filter,
        )
        return JsonResponse({"success": True, **data}, encoder=DjangoJSONEncoder)


class TimelineSnapAPIView(LoginRequiredMixin, View):
    def post(self, request):
        if not user_has_role(request.user, ['planner', 'admin']):
            return JsonResponse({'success': False, 'error': 'Unauthorized'}, status=403)

        company = require_company(request.user)
        try:
            payload = json.loads(request.body or '{}')
        except Exception:
            payload = {}

        snap_minutes = payload.get('snap_minutes') or request.POST.get('snap_minutes') or 0
        try:
            result = WorkOrderService.snap_scheduled_work_orders(company, snap_minutes)
        except ValueError as exc:
            return JsonResponse({'success': False, 'error': str(exc)}, status=400)
        except Exception as exc:
            return JsonResponse({'success': False, 'error': str(exc)}, status=500)

        return JsonResponse({
            'success': True,
            'message': f"Snapped {result['changed_count']} work order(s).",
            **result,
        })

# ---------------------------------------------------------------------
# 🏗️ Helper Actions (Create Machine, Stage, Assign)
# ---------------------------------------------------------------------
class AssignWorkOrderView(LoginRequiredMixin, View):
    def post(self, request):
        try:
            if not user_has_role(request.user, ['planner', 'admin']):
                return JsonResponse({'success': False, 'error': "Unauthorized: Access Denied"})

            company = require_company(request.user)
            wo_id = request.POST.get('wo_id')
            machine_id = request.POST.get('machine_id')
            stage_id = request.POST.get('stage_id')
            status = str(request.POST.get('status', '') or '').strip().lower()
            valid_statuses = {choice[0] for choice in WorkOrder.STATUS_CHOICES}
            if status and status not in valid_statuses:
                return JsonResponse({
                    'success': False,
                    'error': f"Invalid work order status '{status}'."
                }, status=400)
            
            # 🆕 Edit Inputs
            quantity = request.POST.get('quantity')
            priority = request.POST.get('priority')
            start_date_str = request.POST.get('start_date')
            
            try:
                wo = WorkOrder.objects.filter(company=company).get(id=wo_id)
            except WorkOrder.DoesNotExist:
                return JsonResponse({'success': False, 'error': f"Work Order #{wo_id} not found."})

            material_blocker = get_material_readiness_planning_blocker(wo) if (machine_id or stage_id or start_date_str) else None
            if material_blocker:
                source_wo = wo.parent if wo.parent_id and wo.parent else wo
                return JsonResponse(
                    {
                        'success': False,
                        'error': material_blocker,
                        'requires_store_material_action': True,
                        'material_readiness_status': source_wo.material_readiness_status,
                        'material_shortage_note': source_wo.material_shortage_note or '',
                        'material_available_qty': source_wo.material_available_qty,
                        'material_available_percent': (
                            float(source_wo.material_available_percent)
                            if source_wo.material_available_percent is not None
                            else None
                        ),
                        'material_expected_delivery_date': (
                            source_wo.material_expected_delivery_date.isoformat()
                            if source_wo.material_expected_delivery_date
                            else ''
                        ),
                    },
                    status=409,
                )

            # Update Basic Fields
            if quantity:
                try:
                    requested_quantity = int(quantity)
                except Exception:
                    return JsonResponse({'success': False, 'error': "Invalid quantity"}, status=400)
                if requested_quantity <= 0:
                    return JsonResponse({'success': False, 'error': "Quantity must be greater than zero."}, status=400)
                quantity_floor = get_workorder_reported_quantity_floor(wo)
                if requested_quantity < int(quantity_floor['reported']):
                    return JsonResponse({
                        'success': False,
                        'error': f"Quantity cannot be below already reported output ({int(quantity_floor['reported'])})."
                    }, status=400)
                wo.quantity = requested_quantity
            if priority: wo.priority = priority
            
            # Parse and set start date
            if start_date_str:
                try:
                    wo.start_date = parse_client_datetime(start_date_str)
                    
                    # 🆕 AUTO-CALCULATE END DATE (Critical Fix)
                    # Only calculate if end_date is not already set
                    if not wo.end_date:
                        duration_minutes = 60  # Default: 1 hour
                        
                        # Try to get duration from BOM operations
                        if wo.bom and wo.bom.operations.exists():
                            # Sum all operation durations (setup + run per unit)
                            total_duration = sum(
                                WorkOrderService._compute_operation_duration(op, wo.quantity)
                                for op in wo.bom.operations.all()
                            )
                            if total_duration > 0:
                                duration_minutes = total_duration
                        
                        wo.end_date = wo.start_date + timedelta(minutes=duration_minutes)
                        
                except Exception:
                    return JsonResponse({'success': False, 'error': "Invalid start date format"})
            
            from django.db.models import Sum

            def validate_machine_change(target_wo, new_machine_id):
                if not new_machine_id or str(target_wo.machine_id) == str(new_machine_id):
                    return None
                if target_wo.status in ['completed', 'canceled', 'archived']:
                    return "Cannot change machine for a completed or inactive work order."
                logged_qty = target_wo.production_logs.exclude(status='rejected').aggregate(
                    total=Sum('quantity')
                )['total'] or 0
                if target_wo.status == 'in_progress' and logged_qty > 0:
                    return "Cannot change machine after production has started. Split the remaining quantity instead."
                if target_wo.status == 'in_progress' and logged_qty == 0:
                    return "Cannot move an in-progress work order back to pending."
                return None

            # Machine assignment is now optional
            machine = None
            if machine_id:
                machine = Machine.objects.filter(company=company).get(id=machine_id)

            wo.save() # Save all updates

            
            # 🆕 Manual Stage Assignment Logic
            if stage_id:
                stage = get_company_stage(company, stage_id)
                if not stage:
                    return JsonResponse({'success': False, 'error': "Invalid stage for this company."})
                
                # If no machine selected, try to fallback to Stage's default machine
                if not machine and stage.machine:
                    machine = stage.machine
                
                if not machine:
                     return JsonResponse({'success': False, 'error': "A machine must be selected to schedule this stage."})

                if not wo.parent_id and not wo.stage_id and wo.bom and wo.bom.operations.exists():
                    scheduled_start = wo.start_date or timezone.now()
                    route_result = WorkOrderService.schedule_full_route(
                        wo,
                        stage,
                        machine,
                        scheduled_start,
                        actor=request.user,
                        company=company,
                    )
                    return JsonResponse({
                        'success': True,
                    'message': f"Assigned route starting on {machine.display_label}",
                        'wo_id': wo.id,
                        'first_stage_wo_id': route_result['first_task'].id,
                        'end_date': route_result['final_end'].isoformat(),
                    })

                # Check if subtask already exists
                subtask = WorkOrder.objects.filter(company=company, parent=wo, stage=stage).first()
                
                if not subtask:
                    # Determine start/end date
                    final_start = None
                    final_end = None
                    duration_minutes = 60 # Default
                    
                    # Try to derive duration from BOM stage operation
                    if wo.bom:
                        op = wo.bom.operations.filter(stage=stage).first()
                        if op:
                            duration_minutes = WorkOrderService._compute_operation_duration(op, wo.quantity)
                    
                    # ⚠️ Availability Check (if start_date provided)
                    if wo.start_date:
                        check_start, check_end = WorkOrderService.find_next_available_slot(
                            machine, duration_minutes, wo.start_date, exclude_wo_id=[wo.id] # Exclude Parent from collision
                        )
                        # Conflict Detection:
                        # If returned next_start is significantly later than requested start_date (allowing 1 min buffer)
                        if abs((check_start - wo.start_date).total_seconds()) > 60:
                            sug_time = check_start.strftime('%H:%M')
                            return JsonResponse({'success': False, 'error': f"Machine occupied at this time. Next available: {sug_time}"})
                        else:
                            final_start = wo.start_date
                            final_end = check_end
                    else:
                        # Auto-Schedule
                        final_start, final_end = WorkOrderService.find_next_available_slot(machine, duration_minutes, timezone.now())

                    # Create new subtask for this stage
                    subtask = WorkOrder.objects.create(
                        parent=wo,
                        product_name=f"{wo.product_name} - {stage.name}",
                        bom=wo.bom,
                        quantity=wo.quantity, # Assume full batch for now
                        machine=machine,
                        stage=stage,
                        assigned_to=request.user,
                        status='pending',
                        start_date=final_start,
                        end_date=final_end,
                        company=company,
                        qc_requirement=getattr(stage, 'is_quality_check', False)
                    )
                else:
                    change_error = validate_machine_change(subtask, machine.id if machine else None)
                    if change_error:
                        return JsonResponse({'success': False, 'error': change_error})

                    # Update existing subtask
                    subtask.machine = machine
                    if subtask.status in ['canceled', 'archived']:
                        return JsonResponse({
                            'success': False,
                            'error': f"Cannot assign machine in status '{subtask.status}'."
                        }, status=400)
                    if subtask.qc_requirement != getattr(stage, 'is_quality_check', False):
                        subtask.qc_requirement = getattr(stage, 'is_quality_check', False)
                    
                    # ⚠️ Availability Check (if start_date changed/provided)
                    if wo.start_date and subtask.status != 'completed':
                        duration_minutes = 60
                        if subtask.end_date and subtask.start_date:
                             duration_minutes = int((subtask.end_date - subtask.start_date).total_seconds() / 60)
                             
                        check_start, check_end = WorkOrderService.find_next_available_slot(
                            machine, duration_minutes, wo.start_date, exclude_wo_id=[wo.id, subtask.id] # Exclude Parent & Self
                        )
                        if abs((check_start - wo.start_date).total_seconds()) > 60:
                            sug_time = check_start.strftime('%H:%M')
                            return JsonResponse({'success': False, 'error': f"Machine occupied at this time. Next available: {sug_time}"})
                        
                        subtask.start_date = wo.start_date
                        subtask.end_date = check_end
                    
                    subtask.save()
                    
                target_wo = subtask
            else:
                # Standard Assignment
                if not machine:
                     return JsonResponse({'success': False, 'error': "Please select a machine."})

                change_error = validate_machine_change(wo, machine.id if machine else None)
                if change_error:
                    return JsonResponse({'success': False, 'error': change_error})

                target_wo = wo
                target_wo.machine = machine
                if target_wo.status in ['canceled', 'archived']:
                    return JsonResponse({
                        'success': False,
                        'error': f"Cannot assign machine in status '{target_wo.status}'."
                    }, status=400)
                
                # ⚠️ Availability Check
                duration_minutes = 60
                if target_wo.end_date and target_wo.start_date:
                     duration_minutes = int((target_wo.end_date - target_wo.start_date).total_seconds() / 60)

                if wo.start_date and target_wo.status != 'completed': # User provided specific time
                    check_start, check_end = WorkOrderService.find_next_available_slot(
                        machine, duration_minutes, wo.start_date, exclude_wo_id=wo.id
                    )
                    if abs((check_start - wo.start_date).total_seconds()) > 60:
                        sug_time = check_start.strftime('%H:%M')
                        return JsonResponse({'success': False, 'error': f"Machine occupied at this time. Next available: {sug_time}"})
                    
                    target_wo.start_date = wo.start_date
                    target_wo.end_date = check_end
                elif target_wo.status != 'completed':
                    # Auto-Schedule
                    start, end = WorkOrderService.find_next_available_slot(machine, duration_minutes, timezone.now(), exclude_wo_id=wo.id)
                    target_wo.start_date = start
                    target_wo.end_date = end
                    
                target_wo.save()
            
            if status == 'completed':
                try:
                    with transaction.atomic():
                        if target_wo.status == 'pending':
                            WorkOrderLifecycle.apply_transition(
                                target_wo,
                                'in_progress',
                                actor=None,
                                allow_system=True,
                                save=False,
                                enforce_guards=False,
                            )
                        if target_wo.status != 'completed':
                            try:
                                WorkOrderLifecycle.apply_transition(
                                    target_wo,
                                    'completed',
                                    actor=request.user,
                                    save=False,
                                )
                            except WorkOrderLifecycleError:
                                WorkOrderLifecycle.apply_transition(
                                    target_wo,
                                    'completed',
                                    actor=None,
                                    allow_system=True,
                                    save=False,
                                )
                        target_wo.end_date = timezone.now()
                        target_wo.save(update_fields=['status', 'end_date'])

                        close_target = target_wo.parent if target_wo.parent_id else target_wo
                        if target_wo.parent_id:
                            WorkOrderService.create_next_stage_task(target_wo, request.user, auto_create=False)
                            close_target.refresh_from_db()

                        if (
                            close_target
                            and not close_target.parent_id
                            and close_target.status == 'completed'
                            and not getattr(close_target, 'closed_by_planner', False)
                        ):
                            WorkOrderLifecycle.close(
                                close_target,
                                actor=request.user,
                                require_ready=False,
                            )
                            return JsonResponse({
                                'success': True,
                                'message': f'WO #{close_target.id} completed and closed.'
                            })

                        return JsonResponse({
                            'success': True,
                            'message': f'WO #{close_target.id if close_target else target_wo.id} marked completed.'
                        })
                except (WorkOrderLifecycleError, ValueError) as exc:
                    return JsonResponse({'success': False, 'error': str(exc)}, status=400)

            elif status:
                update_fields = ['status']
                target_wo.status = status
                if status == 'in_progress' and not target_wo.start_date:
                    target_wo.start_date = timezone.now()
                    update_fields.append('start_date')
                if status in {'canceled', 'archived'} and not target_wo.end_date:
                    target_wo.end_date = timezone.now()
                    update_fields.append('end_date')
                target_wo.save(update_fields=list(dict.fromkeys(update_fields)))
                return JsonResponse({
                    'success': True,
                    'message': f"WO #{target_wo.parent_id or target_wo.id} moved to {status.replace('_', ' ')}."
                })

            return JsonResponse({'success': True, 'message': f'Assigned to {machine.display_label}'})
        except Exception as e:
             return JsonResponse({'success': False, 'error': str(e)})


class AssignWorkerAPIView(LoginRequiredMixin, View):
    """
    API to assign a worker to a specific Work Order.
    Used by Supervisors/Planners.
    """
    def post(self, request):
        try:
            if not user_has_role(request.user, ['admin', 'planner', 'supervisor']):
                 return JsonResponse({'success': False, 'error': "Unauthorized: Access Denied"})

            company = require_company(request.user)
            
            wo_id = request.POST.get("work_order_id")
            worker_id = request.POST.get("worker_id")
            notes = request.POST.get("notes") # Optional
            
            if not wo_id or not worker_id:
                return JsonResponse({"success": False, "error": "Missing Work Order ID or Worker ID"})

            # Get Work Order
            wo = get_object_or_404(WorkOrder, id=wo_id, company=company)

            # If parent WO, assign the active child stage task
            target_wo = wo
            if wo.sub_tasks.exists():
                candidate = WorkOrderService.get_active_stage_task(wo)
                if candidate:
                    target_wo = candidate

            if target_wo.status not in ['pending', 'in_progress']:
                return JsonResponse({"success": False, "error": f"Work order is not assignable in status '{target_wo.status}'"}, status=400)
            
            # Get Worker & Validate
            from django.contrib.auth.models import User
            worker = get_object_or_404(User, id=worker_id)
            
            # Check if worker belongs to company
            # We assume profile link exists
            if not hasattr(worker, 'profile') or worker.profile.company != company:
                 return JsonResponse({"success": False, "error": "Worker does not belong to your company."})

            # 2. Update Assignment
            target_wo.assigned_worker = worker
            target_wo.assignment_type = 'manual'
            if notes:
                 target_wo.instructions = f"{target_wo.instructions or ''}\n[Assignment Note]: {notes}".strip()
            
            target_wo.save()
            audit_request_action(
                request,
                'update',
                target=target_wo,
                details={
                    'event': 'worker_assigned',
                    'worker_id': worker.id,
                    'worker_username': worker.username,
                    'notes': notes or '',
                },
            )
            
            # 3. Notify Worker (Optional)
            try:
                Notification.objects.create(
                    recipient=worker,
                    title="New Assignment",
                    message=f"You have been assigned to {wo.product_name} on {wo.machine.display_label if wo.machine else 'Pending Machine'}.",
                    link="/manufacturing/shop-floor/"
                )
            except: pass

            return JsonResponse({
                "success": True, 
                "message": f"Assigned {worker.username} to WO #{target_wo.id}",
                "worker_name": worker.username
            })

        except Exception as e:
            return JsonResponse({"success": False, "error": str(e)})


class ReportFaultAPIView(LoginRequiredMixin, View):
    """
    API to report a machine fault.
    Triggers 'maintenance' status if urgent.
    """
    def post(self, request):
        try:
            company = require_company(request.user)
            machine_id = request.POST.get("machine_id")
            description = request.POST.get("description")
            priority = request.POST.get("priority", "normal")
            
            if not machine_id or not description:
                return JsonResponse({"success": False, "error": "Missing Machine ID or Description"})

            machine = get_object_or_404(Machine, id=machine_id, company=company)
            
            # Create Fault Record
            from .models import MachineFault
            fault = MachineFault.objects.create(
                machine=machine,
                reported_by=request.user,
                description=description,
                priority=priority if priority else 'normal',
                status='open' # or we could add priority to model if needed, but keeping it simple
            )
            audit_request_action(
                request,
                'create',
                target=fault,
                details={
                    'event': 'machine_fault_reported',
                    'machine_id': machine.id,
                    'priority': fault.priority,
                    'status': fault.status,
                },
            )
            
            # Update Machine Status if urgent
            status_changed = False
            if priority in ['urgent', 'high']:
                machine.status = 'maintenance' # or 'broken'
                machine.maintenance_note = f"Fault reported by {request.user.username}: {description}"
                machine.save()
                status_changed = True
            
            # Notify Maintenance (Placeholder)
            # Notification.objects.create(recipient=maintenance_user, ...)
            
            return JsonResponse({
                "success": True, 
                "message": f"Fault reported for {machine.display_label}",
                "ticket_id": fault.id,
                "status_changed": status_changed
            })
            
        except Exception as e:
            return JsonResponse({"success": False, "error": str(e)})
