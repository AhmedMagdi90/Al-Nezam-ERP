from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.mixins import LoginRequiredMixin
from django.views import View
from django.http import JsonResponse, HttpResponseForbidden
from django.contrib import messages
from django.utils import timezone
from django.contrib.auth.models import User
from django.core.exceptions import ObjectDoesNotExist
from decimal import Decimal
import logging
import json

from manufacturing.bom_attachments import serialize_bom_attachment
from manufacturing.models import (
    WorkOrder, ProductionLog, Product, WorkerCertification, BOMAcceptanceCriteria,
    MachineFault, WorkOrderChangeLog, MaterialUsage, ProductionStage, Machine
)
from manufacturing.forms import ProductionLogForm
from manufacturing.security import audit_request_action
from .dashboard import require_company, user_has_role
from accounts.constants import RoleType
from manufacturing.services import DashboardService, ProductionLogService
from django.db.models import Sum
from django.db.models import Q
from manufacturing.access_control import acts_as_worker

logger = logging.getLogger(__name__)


def _strict_worker_mode(user):
    return acts_as_worker(user)


def _assigned_leaf_work_orders(company, user):
    return WorkOrder.objects.filter(
        company=company,
        sub_tasks__isnull=True,
        assigned_worker_id=getattr(user, "id", None),
    )


def _safe_related(instance, attr_name):
    try:
        return getattr(instance, attr_name)
    except ObjectDoesNotExist:
        return None


def _prepare_shop_floor_work_order(wo):
    if not wo:
        return None

    safe_parent = _safe_related(wo, "parent")
    safe_stage = _safe_related(wo, "stage")
    safe_current_stage = _safe_related(wo, "current_stage")
    safe_machine = _safe_related(wo, "machine")
    safe_bom = _safe_related(wo, "bom")
    safe_assigned_worker = _safe_related(wo, "assigned_worker")

    wo.safe_parent = safe_parent
    wo.safe_stage = safe_stage
    wo.safe_current_stage = safe_current_stage
    wo.safe_machine = safe_machine
    wo.safe_bom = safe_bom
    wo.safe_assigned_worker = safe_assigned_worker
    wo.display_stage = safe_stage or safe_current_stage
    wo.display_product_name = (
        safe_parent.product_name
        if safe_parent and getattr(safe_parent, "product_name", None)
        else wo.product_name
    )
    wo.display_machine_label = (
        safe_machine.display_label
        if safe_machine and getattr(safe_machine, "display_label", None)
        else "Manual"
    )
    return wo


def _shop_floor_section_name(wo):
    stage_obj = getattr(wo, "safe_stage", None) or getattr(wo, "safe_current_stage", None)
    machine_obj = getattr(wo, "safe_machine", None)
    for value in (
        getattr(stage_obj, "category", None),
        getattr(machine_obj, "category", None),
        getattr(stage_obj, "name", None),
        getattr(machine_obj, "type", None),
    ):
        if str(value or "").strip():
            return str(value).strip()
    return "-"


def _shop_floor_worker_name(wo):
    worker = getattr(wo, "safe_assigned_worker", None)
    if not worker:
        return "Not assigned"
    return worker.get_full_name() or worker.username


def _empty_shop_floor_context(*, error=None):
    return {
        "active_wo": None,
        "selected_wo": None,
        "queue": [],
        "assigned_tasks": [],
        "pending_approval_tasks": [],
        "active_tasks": [],
        "ready_tasks": [],
        "future_pending_tasks": [],
        "section_upcoming_tasks": [],
        "ready_task_ids": [],
        "today_output": 0,
        "role": "worker",
        "materials": [],
        "certification": None,
        "acceptance_criteria": [],
        "error": error,
    }


class RecordOutputView(LoginRequiredMixin, View):
    """
    View for workers to record their finished work.
    """
    def get(self, request):
        company = require_company(request.user)
        if not company: return redirect("dashboard")

        if not user_has_role(request.user, [RoleType.WORKER, RoleType.SUPERVISOR, RoleType.ADMIN]):
            return HttpResponseForbidden("Unauthorized")
        
        # Supervisors with worker mode enabled should behave like a worker on this surface.
        if user_has_role(request.user, ['admin', 'supervisor']) and not _strict_worker_mode(request.user):
            active_wo = WorkOrder.objects.filter(
                company=company,
                status='in_progress',
                sub_tasks__isnull=True
            ).order_by('-worker_start_at', '-start_date', '-id').first()
        else:
            active_wo = _assigned_leaf_work_orders(company, request.user).filter(
                status='in_progress',
            ).order_by('-worker_start_at', '-start_date', '-id').first()
        
        if not active_wo:
            messages.warning(request, "You don't have an active job running.")
            return redirect('shop_floor')
            
        form = ProductionLogForm(initial={'work_order': active_wo})
        # Filter products that are "Material" type for the usage section
        materials = Product.objects.filter(company=company, material_type__in=['raw', 'semi', 'packaging']).order_by('name')
        
        return render(request, 'manufacturing/record_output.html', {
            'active_wo': active_wo,
            'form': form,
            'materials': materials
        })

    def post(self, request):
        company = require_company(request.user)
        if not company: return HttpResponseForbidden("No company")

        if not user_has_role(request.user, [RoleType.WORKER, RoleType.SUPERVISOR, RoleType.ADMIN]):
            return HttpResponseForbidden("Unauthorized")
        
        # Supervisors with worker mode enabled should behave like a worker on this surface.
        if user_has_role(request.user, ['admin', 'supervisor']) and not _strict_worker_mode(request.user):
            active_wo = WorkOrder.objects.filter(
                company=company, 
                status='in_progress',
                sub_tasks__isnull=True
            ).order_by('-worker_start_at', '-start_date', '-id').first() # Grab most recent active leaf task
            
            # If specific WO passed in hidden field, try to use that
            if request.POST.get('work_order'):
                 specific_wo = WorkOrder.objects.filter(company=company, id=request.POST.get('work_order')).first()
                 if specific_wo:
                     active_wo = specific_wo
        else:
            active_wo = _assigned_leaf_work_orders(company, request.user).filter(
                status='in_progress',
            ).order_by('-worker_start_at', '-start_date', '-id').first()

        if not active_wo:
             messages.error(request, "No active job found.")
             return redirect('shop_floor')

        form = ProductionLogForm(request.POST)
        if form.is_valid():
            quantity = form.cleaned_data.get('quantity')
            shift = form.cleaned_data.get('shift')
            note = form.cleaned_data.get('note', '')

            completion_requested = bool(request.POST.get('complete_wo'))
            if completion_requested:
                note = f"{note}\nWorker indicated task complete.".strip()

            # Record Material Usage
            mat_ids = request.POST.getlist('material_id[]')
            mat_qtys = request.POST.getlist('material_qty[]')
            materials_payload = []
            for i, mat_id in enumerate(mat_ids):
                if i < len(mat_qtys) and mat_qtys[i]:
                    materials_payload.append({
                        'product_id': mat_id,
                        'quantity': mat_qtys[i]
                    })

            try:
                ProductionLogService.create_log(
                    work_order=active_wo,
                    worker=request.user,
                    quantity=quantity,
                    shift=shift,
                    note=note,
                    materials=materials_payload,
                    completion_requested=completion_requested
                )
                if completion_requested:
                    messages.success(request, "Completion submitted for supervisor approval.")
                else:
                    messages.success(request, "Production reported. Waiting for supervisor approval.")
                return redirect('shop_floor')
            except Exception as exc:
                messages.error(request, f"Error: {exc}")
                return redirect('shop_floor')
        else:
            messages.error(request, "❌ Error in form submission.")
            return self.get(request)

class ShopFloorKioskView(LoginRequiredMixin, View):
    """
    Kiosk Mode View for Shop Floor Workers.
    """
    def get(self, request):
        company = require_company(request.user)
        if not company:
            return render(
                request,
                'manufacturing/shop_floor.html',
                _empty_shop_floor_context(error="No company assigned to this account."),
            )
        now = timezone.now()

        if not user_has_role(request.user, [RoleType.WORKER, RoleType.SUPERVISOR, RoleType.ADMIN]):
            return HttpResponseForbidden("Unauthorized")
        try:
            selected_wo_id = request.GET.get("wo")
            base_qs = WorkOrder.objects.filter(
                company=company,
                sub_tasks__isnull=True,
            )

            # Supervisors with worker mode enabled should see only their own queue on this surface.
            if user_has_role(request.user, ['admin', 'supervisor']) and not _strict_worker_mode(request.user):
                # Show only leaf tasks (no container rows).
                in_progress_qs = base_qs.filter(
                    status='in_progress',
                ).order_by('-worker_start_at', '-start_date', '-id')
                pending_qs = base_qs.filter(
                    status='pending',
                    assigned_worker__isnull=False,  # Only jobs already pushed by supervisor.
                ).order_by('start_date', 'scheduled_start_date', 'id')
                if user_has_role(request.user, ['supervisor']):
                    in_progress_ids = [
                        wo.id for wo in DashboardService._filter_work_orders_for_viewer(
                            in_progress_qs,
                            viewer_role='supervisor',
                            viewer=request.user,
                            restrict_to_current_shift=False,
                            include_future_pending=True,
                        )
                    ]
                    pending_ids = [
                        wo.id for wo in DashboardService._filter_work_orders_for_viewer(
                            pending_qs,
                            viewer_role='supervisor',
                            viewer=request.user,
                            restrict_to_current_shift=False,
                            include_future_pending=True,
                        )
                    ]
                    in_progress_qs = in_progress_qs.filter(id__in=in_progress_ids)
                    pending_qs = pending_qs.filter(id__in=pending_ids)
            else:
                # Worker View (strict assignment).
                assigned_qs = _assigned_leaf_work_orders(company, request.user)
                in_progress_qs = assigned_qs.filter(
                    status='in_progress',
                ).order_by('-worker_start_at', '-start_date', '-id')
                pending_qs = assigned_qs.filter(
                    status='pending',
                ).order_by('start_date', 'scheduled_start_date', 'id')

            future_start_filter = (
                Q(scheduled_start_date__gt=now)
                | Q(scheduled_start_date__isnull=True, start_date__gt=now)
            )
            if user_has_role(request.user, ['admin', 'supervisor']) and not _strict_worker_mode(request.user):
                section_upcoming_base = base_qs.filter(
                    future_start_filter,
                    status='pending',
                ).select_related(
                    'machine',
                    'assigned_worker',
                    'stage',
                    'current_stage',
                    'parent',
                ).order_by('scheduled_start_date', 'start_date', 'id')
                viewer_role = 'supervisor' if user_has_role(request.user, ['supervisor']) else 'admin'
            else:
                section_upcoming_base = assigned_qs.filter(
                    future_start_filter,
                    status='pending',
                ).select_related(
                    'machine',
                    'assigned_worker',
                    'stage',
                    'current_stage',
                    'parent',
                ).order_by('scheduled_start_date', 'start_date', 'id')
                viewer_role = 'worker'

            section_upcoming_ids = [
                wo.id for wo in DashboardService._filter_work_orders_for_viewer(
                    section_upcoming_base,
                    viewer_role=viewer_role,
                    viewer=request.user,
                    restrict_to_current_shift=False,
                    include_future_pending=True,
                )
            ]
            section_upcoming_tasks = [
                _prepare_shop_floor_work_order(wo)
                for wo in section_upcoming_base.filter(id__in=section_upcoming_ids)
            ]
            for wo in section_upcoming_tasks:
                wo.upcoming_section_name = _shop_floor_section_name(wo)
                wo.upcoming_worker_name = _shop_floor_worker_name(wo)

            pending_tasks = list(pending_qs)
            pending_tasks = [_prepare_shop_floor_work_order(wo) for wo in pending_tasks]
            queue_groups = DashboardService.classify_worker_queue_work_orders(
                pending_tasks,
                now=now,
            )
            ready_task_ids = list(queue_groups["ready_ids"])
            ready_tasks = list(queue_groups["ready"])
            future_pending_tasks = list(queue_groups["future_pending"])

            # Default active task = most recent in-progress.
            active_wo = in_progress_qs.first()
            active_wo = _prepare_shop_floor_work_order(active_wo)
            selected_wo = active_wo
            if selected_wo_id:
                try:
                    selected_wo_id_int = int(selected_wo_id)
                    selected_wo = in_progress_qs.filter(id=selected_wo_id_int).first()
                    selected_wo = _prepare_shop_floor_work_order(selected_wo)
                    if not selected_wo:
                        selected_wo = next((wo for wo in pending_tasks if wo.id == selected_wo_id_int), None)
                except (TypeError, ValueError):
                    pass
            if not selected_wo:
                selected_wo = (ready_tasks[0] if ready_tasks else None) or (future_pending_tasks[0] if future_pending_tasks else None)
            selected_wo = _prepare_shop_floor_work_order(selected_wo)

            active_tasks = [_prepare_shop_floor_work_order(wo) for wo in in_progress_qs]
            active_task_ids = [wo.id for wo in active_tasks]
            pending_approval_ids = set(
                ProductionLog.objects.filter(
                    work_order_id__in=active_task_ids,
                    status='pending',
                    completion_requested=True,
                ).values_list('work_order_id', flat=True)
            ) if active_task_ids else set()

            for wo in active_tasks:
                wo.completion_pending = wo.id in pending_approval_ids

            pending_approval_tasks = [wo for wo in active_tasks if wo.completion_pending]
            active_tasks = [wo for wo in active_tasks if not wo.completion_pending]

            # Flat list retained for total counts and any legacy template usage.
            assigned_tasks = []
            assigned_tasks.extend(pending_approval_tasks)
            assigned_tasks.extend(active_tasks)
            assigned_tasks.extend(ready_tasks)
            assigned_tasks.extend(future_pending_tasks)

            today_output = sum(
                log.quantity
                for log in ProductionLog.objects.filter(
                    worker_id=request.user.id,
                    created_at__date=timezone.now().date(),
                    status='approved',
                )
            )

            materials = Product.objects.filter(company=company, material_type__in=['raw', 'semi', 'packaging']).order_by('name')

            certification = None
            if selected_wo and selected_wo.safe_machine:
                certification = WorkerCertification.objects.filter(
                    worker_id=request.user.id,
                    machine=selected_wo.safe_machine
                ).first()

            acceptance_criteria = []
            bom_attachment = None
            if selected_wo and selected_wo.safe_bom:
                acceptance_criteria = BOMAcceptanceCriteria.objects.filter(bom=selected_wo.safe_bom)
                bom_attachment = serialize_bom_attachment(selected_wo.safe_bom, request)

            if selected_wo:
                selected_wo.completion_pending = selected_wo.id in pending_approval_ids
                approved_qty = ProductionLog.objects.filter(work_order=selected_wo, status='approved').aggregate(
                    total=Sum('quantity')
                )['total'] or 0
                pending_qty = ProductionLog.objects.filter(work_order=selected_wo, status='pending').aggregate(
                    total=Sum('quantity')
                )['total'] or 0
                produced_qty = int(approved_qty) + int(pending_qty)
                selected_wo.produced_qty = produced_qty
                selected_wo.remaining_qty = max(int(selected_wo.quantity) - produced_qty, 0)
                selected_wo.completion_pending = ProductionLog.objects.filter(
                    work_order=selected_wo,
                    status='pending',
                    completion_requested=True
                ).exists()

            context = {
                "active_wo": active_wo,
                "selected_wo": selected_wo,
                "queue": pending_qs,  # Backward compatibility for any legacy template references.
                "assigned_tasks": assigned_tasks,
                "pending_approval_tasks": pending_approval_tasks,
                "active_tasks": active_tasks,
                "ready_tasks": ready_tasks,
                "future_pending_tasks": future_pending_tasks,
                "section_upcoming_tasks": section_upcoming_tasks,
                "ready_task_ids": ready_task_ids,
                "today_output": today_output,
                "role": "worker",
                "materials": materials,
                "certification": certification,
                "acceptance_criteria": acceptance_criteria,
                "bom_attachment": bom_attachment,
                "error": None,
            }
        except Exception:
            logger.exception(
                "Shop floor render failed for user_id=%s tenant_code=%s tenant_db=%s",
                getattr(request.user, "id", None),
                getattr(getattr(request, "tenant", None), "code", None) or request.session.get("tenant_code"),
                getattr(request, "tenant_db_alias", None),
            )
            context = _empty_shop_floor_context(
                error="Shop floor data is temporarily unavailable. Refresh the page and try again.",
            )
        return render(request, 'manufacturing/shop_floor.html', context)

class LogProductionAPI(LoginRequiredMixin, View):
    """Handle production reporting from the shop floor via API."""
    def post(self, request):
        try:
            if not user_has_role(request.user, [RoleType.WORKER, RoleType.SUPERVISOR, RoleType.ADMIN]):
                return JsonResponse({'success': False, 'error': 'Unauthorized'}, status=403)

            data = json.loads(request.body) if request.content_type == 'application/json' else request.POST
            wo_id = data.get('work_order_id')
            qty = int(data.get('quantity', 0))
            note = data.get('note', '')
            shift = data.get('shift')
            materials = data.get('materials', [])
            completion_requested = data.get('completion_requested') or data.get('complete_wo')
            
            company = require_company(request.user)
            work_order_qs = WorkOrder.objects.filter(company=company)
            if _strict_worker_mode(request.user):
                work_order_qs = work_order_qs.filter(assigned_worker_id=request.user.id)
            work_order = work_order_qs.get(id=wo_id)

            log = ProductionLogService.create_log(
                work_order=work_order,
                worker=request.user,
                quantity=qty,
                shift=shift,
                note=note,
                materials=materials,
                completion_requested=completion_requested
            )
            audit_request_action(
                request,
                'create',
                target=log,
                details={
                    'event': 'production_logged',
                    'work_order_id': work_order.id,
                    'quantity': int(log.quantity),
                    'shift': log.shift,
                    'completion_requested': bool(log.completion_requested),
                },
            )
            approved_qty = ProductionLog.objects.filter(work_order=work_order, status='approved').aggregate(
                total=Sum('quantity')
            )['total'] or 0
            pending_qty = ProductionLog.objects.filter(work_order=work_order, status='pending').aggregate(
                total=Sum('quantity')
            )['total'] or 0
            remaining_qty = max(int(work_order.quantity) - int(approved_qty) - int(pending_qty), 0)
            return JsonResponse({
                'success': True,
                'message': 'Production logged successfully!',
                'log_id': log.id,
                'logged_quantity': int(log.quantity),
                'remaining_qty': int(remaining_qty),
            })
        except Exception as e:
            return JsonResponse({'success': False, 'error': str(e)})

class ApproveLogView(LoginRequiredMixin, View):
    """
    View for supervisors to approve/reject production logs.
    """
    def get(self, request, log_id):
        if not user_has_role(request.user, ['supervisor', 'admin', 'planner']):
            return JsonResponse({"error": "Unauthorized"}, status=403)

        try:
            company = require_company(request.user)
            log = ProductionLog.objects.filter(work_order__company=company).select_related(
                'work_order', 'worker', 'work_order__bom', 'work_order__machine',
                'work_order__current_stage', 'work_order__stage'
            ).prefetch_related('material_usage').get(id=log_id)

            wo = log.work_order
            stage = wo.current_stage or wo.stage
            approved_qty = wo.production_logs.filter(status='approved').aggregate(
                total=Sum('quantity')
            )['total'] or 0
            other_pending_qty = wo.production_logs.filter(status='pending').exclude(id=log.id).aggregate(
                total=Sum('quantity')
            )['total'] or 0
            remaining_before_log = max(int(wo.quantity or 0) - int(approved_qty) - int(other_pending_qty), 0)
            materials = [{
                "id": m.id,
                "name": m.material_name,
                "planned_quantity": float(m.planned_quantity) if m.planned_quantity is not None else None,
                "quantity": float(m.quantity_used),
                "unit": m.unit
            } for m in log.material_usage.all()]

            return JsonResponse({
                "success": True,
                "log": {
                    "id": log.id,
                    "quantity": log.quantity,
                    "shift": log.shift,
                    "note": log.note or "",
                    "created_at": log.created_at.isoformat(),
                    "status": log.status,
                    "worker": log.worker.username if log.worker else "-",
                    "worker_id": log.worker.id if log.worker else None,
                    "completion_requested": bool(log.completion_requested),
                    "work_order_id": wo.id,
                    "display_work_order_id": wo.display_work_order_id,
                    "product": wo.product_name or (wo.bom.product.name if wo.bom and wo.bom.product else "Work Order"),
                    "stage": stage.name if stage else "-",
                    "stage_id": stage.id if stage else None,
                    "machine": wo.machine.display_label if wo.machine else "Manual",
                    "machine_id": wo.machine.id if wo.machine else None,
                    "work_order_quantity": wo.quantity,
                    "approved_quantity": int(approved_qty),
                    "other_pending_quantity": int(other_pending_qty),
                    "remaining_before_log": int(remaining_before_log),
                    "start_date": wo.start_date.isoformat() if wo.start_date else None,
                    "uom": wo.bom.uom if wo.bom else "pcs",
                    "materials": materials,
                }
            })
        except ProductionLog.DoesNotExist:
            return JsonResponse({"error": "Log not found"}, status=404)

    def post(self, request, log_id):
        # Planner dashboard now exposes the same approval actions as supervisor UI.
        if not user_has_role(request.user, ['supervisor', 'admin', 'planner']):
            return JsonResponse({"error": "Unauthorized"}, status=403)
            
        try:
            company = require_company(request.user)
            log = ProductionLog.objects.filter(work_order__company=company).get(id=log_id)
            action = request.POST.get("action")
            
            if action == 'approve':
                ProductionLogService.approve_log(log, request.user)
                audit_request_action(
                    request,
                    'approve',
                    target=log,
                    details={
                        'event': 'production_log_approved',
                        'work_order_id': log.work_order_id,
                        'quantity': int(log.quantity),
                    },
                )
                return JsonResponse({"success": True, "message": "Log approved."})
            elif action == 'reject':
                reason = (request.POST.get("reason") or "").strip()
                if not reason:
                    return JsonResponse({"success": False, "error": "Rejection reason is required."}, status=400)
                ProductionLogService.reject_log(log, request.user, reason=reason)
                audit_request_action(
                    request,
                    'reject',
                    target=log,
                    details={
                        'event': 'production_log_rejected',
                        'work_order_id': log.work_order_id,
                        'quantity': int(log.quantity),
                        'reason': reason,
                    },
                )
                return JsonResponse({"success": True, "message": "Log rejected."})
                
            return JsonResponse({"error": "Invalid action"}, status=400)
        except ProductionLog.DoesNotExist:
            return JsonResponse({"error": "Log not found"}, status=404)


class WorkOrderStartDateUpdateView(LoginRequiredMixin, View):
    """Allow supervisor/admin/planner to edit a work order start date."""
    def post(self, request, wo_id):
        if not user_has_role(request.user, ['supervisor', 'admin', 'planner']):
            return JsonResponse({"success": False, "error": "Unauthorized"}, status=403)

        try:
            company = require_company(request.user)
            data = json.loads(request.body) if request.content_type == 'application/json' else request.POST
            start_date_raw = data.get('start_date')
            if not start_date_raw:
                return JsonResponse({"success": False, "error": "start_date is required"}, status=400)

            from datetime import datetime
            dt = datetime.fromisoformat(start_date_raw.replace('T', ' '))
            if timezone.is_naive(dt):
                dt = timezone.make_aware(dt)

            wo = WorkOrder.objects.filter(company=company).get(id=wo_id)
            old_start = wo.start_date
            wo.start_date = dt
            wo.save(update_fields=['start_date'])

            # Audit log for supervisor edits
            old_val = old_start.isoformat() if old_start else ''
            new_val = wo.start_date.isoformat() if wo.start_date else ''
            if old_val != new_val:
                WorkOrderChangeLog.objects.create(
                    work_order=wo,
                    changed_by=request.user,
                    action="Start date updated",
                    field_name="start_date",
                    old_value=old_val,
                    new_value=new_val,
                    note="Supervisor edited start date"
                )
                audit_request_action(
                    request,
                    'update',
                    target=wo,
                    details={
                        'event': 'work_order_start_date_updated',
                        'old_start_date': old_val or None,
                        'new_start_date': new_val or None,
                    },
                )
            return JsonResponse({"success": True, "start_date": wo.start_date.isoformat()})
        except WorkOrder.DoesNotExist:
            return JsonResponse({"success": False, "error": "Work order not found"}, status=404)
        except Exception as e:
            return JsonResponse({"success": False, "error": str(e)}, status=400)


class ProductionLogEditView(LoginRequiredMixin, View):
    """Allow supervisor/admin/planner to edit a pending production log and related WO fields."""
    def post(self, request, log_id):
        if not user_has_role(request.user, ['supervisor', 'admin', 'planner']):
            return JsonResponse({"success": False, "error": "Unauthorized"}, status=403)

        try:
            company = require_company(request.user)
            data = json.loads(request.body) if request.content_type == 'application/json' else request.POST

            log = ProductionLog.objects.select_related('work_order').get(id=log_id, work_order__company=company)
            if log.status != 'pending':
                return JsonResponse({"success": False, "error": "Only pending logs can be edited."}, status=400)

            wo = log.work_order

            def record_change(field, old, new, action="Production log updated", note="Supervisor edited log"):
                old_str = "" if old is None else str(old)
                new_str = "" if new is None else str(new)
                if old_str != new_str:
                    WorkOrderChangeLog.objects.create(
                        work_order=wo,
                        changed_by=request.user,
                        action=action,
                        field_name=field,
                        old_value=old_str,
                        new_value=new_str,
                        note=note
                    )

            # Quantity
            if data.get('quantity') is not None:
                try:
                    new_qty = int(data.get('quantity'))
                except Exception:
                    return JsonResponse({"success": False, "error": "Quantity must be a number."}, status=400)
                if new_qty <= 0:
                    return JsonResponse({"success": False, "error": "Quantity must be positive."}, status=400)
                approved_qty = ProductionLog.objects.filter(work_order=wo, status='approved').aggregate(
                    total=Sum('quantity')
                )['total'] or 0
                other_pending_qty = ProductionLog.objects.filter(
                    work_order=wo,
                    status='pending'
                ).exclude(id=log.id).aggregate(total=Sum('quantity'))['total'] or 0
                remaining_capacity = max(int(wo.quantity) - int(approved_qty) - int(other_pending_qty), 0)
                if new_qty > remaining_capacity:
                    return JsonResponse({
                        "success": False,
                        "error": f"Quantity exceeds remaining work ({remaining_capacity})."
                    }, status=400)
                if new_qty != log.quantity:
                    record_change("log.quantity", log.quantity, new_qty)
                    log.quantity = new_qty

            # Shift
            new_shift = data.get('shift')
            if new_shift:
                valid_shifts = {choice[0] for choice in ProductionLog.SHIFT_CHOICES}
                if new_shift not in valid_shifts:
                    return JsonResponse({"success": False, "error": "Invalid shift."}, status=400)
                if new_shift != log.shift:
                    record_change("log.shift", log.shift, new_shift)
                    log.shift = new_shift

            # Note
            if 'note' in data:
                new_note = data.get('note') or ''
                if new_note != (log.note or ''):
                    record_change("log.note", log.note or '', new_note)
                    log.note = new_note

            # Worker
            worker_id = data.get('worker_id')
            if worker_id:
                try:
                    worker_id = int(worker_id)
                except Exception:
                    return JsonResponse({"success": False, "error": "Invalid worker ID."}, status=400)
                worker = User.objects.filter(id=worker_id, profile__company=company).first()
                if not worker:
                    return JsonResponse({"success": False, "error": "Worker not found."}, status=404)
                if log.worker_id != worker.id:
                    record_change("log.worker", log.worker.username if log.worker else "-", worker.username)
                    log.worker = worker
                if wo.assigned_worker_id != worker.id:
                    record_change("assigned_worker", wo.assigned_worker.username if wo.assigned_worker else "-", worker.username,
                                  action="Work order updated", note="Supervisor updated assigned worker")
                    wo.assigned_worker = worker

            # Stage
            stage_id = data.get('stage_id')
            if stage_id:
                try:
                    stage_id = int(stage_id)
                except Exception:
                    return JsonResponse({"success": False, "error": "Invalid stage ID."}, status=400)
                stage = ProductionStage.objects.filter(id=stage_id).first()
                if stage and stage.machine and stage.machine.company != company:
                    return JsonResponse({"success": False, "error": "Invalid stage for this company."}, status=400)
                old_stage = wo.current_stage.name if wo.current_stage else (wo.stage.name if wo.stage else "-")
                new_stage = stage.name if stage else "-"
                if (wo.current_stage_id or wo.stage_id) != stage_id:
                    record_change("current_stage", old_stage, new_stage, action="Work order updated")
                    wo.current_stage = stage

            # Machine
            machine_id = data.get('machine_id')
            if machine_id:
                try:
                    machine_id = int(machine_id)
                except Exception:
                    return JsonResponse({"success": False, "error": "Invalid machine ID."}, status=400)
                machine = Machine.objects.filter(id=machine_id, company=company).first()
                if not machine:
                    return JsonResponse({"success": False, "error": "Machine not found."}, status=404)
                if wo.machine_id != machine.id:
                    record_change("machine", wo.machine.display_label if wo.machine else "-", machine.display_label,
                                  action="Work order updated")
                    wo.machine = machine

            # Start date
            if data.get('start_date'):
                from datetime import datetime
                dt = datetime.fromisoformat(data.get('start_date').replace('T', ' '))
                if timezone.is_naive(dt):
                    dt = timezone.make_aware(dt)
                old_start = wo.start_date.isoformat() if wo.start_date else ""
                new_start = dt.isoformat()
                if old_start != new_start:
                    record_change("start_date", old_start, new_start, action="Start date updated",
                                  note="Supervisor edited start date")
                    wo.start_date = dt

            # Materials
            materials = data.get('materials', [])
            if isinstance(materials, list):
                for item in materials:
                    mat_id = item.get('id')
                    qty_val = item.get('quantity')
                    if not mat_id:
                        continue
                    mu = MaterialUsage.objects.filter(id=mat_id, production_log=log).first()
                    if not mu:
                        continue
                    try:
                        new_qty = Decimal(str(qty_val))
                    except Exception:
                        continue
                    if new_qty != mu.quantity_used:
                        record_change(f"material:{mu.material_name}", mu.quantity_used, new_qty,
                                      action="Material usage updated")
                        mu.quantity_used = new_qty
                        mu.save(update_fields=['quantity_used'])

            log.save()
            wo.save()
            audit_request_action(
                request,
                'update',
                target=log,
                details={
                    'event': 'production_log_updated',
                    'work_order_id': wo.id,
                    'quantity': int(log.quantity),
                    'shift': log.shift,
                    'assigned_worker_id': wo.assigned_worker_id,
                    'machine_id': wo.machine_id,
                    'stage_id': wo.current_stage_id or wo.stage_id,
                },
            )

            return JsonResponse({"success": True, "message": "Log updated."})
        except ProductionLog.DoesNotExist:
            return JsonResponse({"success": False, "error": "Log not found."}, status=404)
        except Exception as e:
            return JsonResponse({"success": False, "error": str(e)}, status=400)

class ReportFaultAPI(LoginRequiredMixin, View):
    """API for workers to report machine faults."""
    def post(self, request):
        try:
            if not user_has_role(request.user, [RoleType.WORKER, RoleType.SUPERVISOR, RoleType.ADMIN]):
                return JsonResponse({'success': False, 'error': 'Unauthorized'}, status=403)

            data = json.loads(request.body) if request.content_type == 'application/json' else request.POST
            wo_id = data.get('work_order_id')
            description = data.get('description')
            priority = data.get('priority') or 'normal'
            
            company = require_company(request.user)
            work_order_qs = WorkOrder.objects.filter(company=company)
            if _strict_worker_mode(request.user):
                work_order_qs = work_order_qs.filter(assigned_worker_id=request.user.id)
            work_order = work_order_qs.get(id=wo_id)
            
            if not work_order.machine:
                return JsonResponse({'success': False, 'error': 'No machine associated with this work order.'})

            # Create Fault Record
            fault = MachineFault.objects.create(
                machine=work_order.machine,
                reported_by_id=request.user.id,
                description=description,
                priority=priority,
                status='open'
            )
            audit_request_action(
                request,
                'create',
                target=fault,
                details={
                    'event': 'shop_floor_fault_reported',
                    'work_order_id': work_order.id,
                    'machine_id': work_order.machine_id,
                    'priority': fault.priority,
                },
            )

            # Auto-flip machine status
            work_order.machine.status = 'broken'
            work_order.machine.save()

            return JsonResponse({'success': True, 'message': 'Fault reported. Machine marked as Broken.'})
        except Exception as e:
            return JsonResponse({'success': False, 'error': str(e)})
