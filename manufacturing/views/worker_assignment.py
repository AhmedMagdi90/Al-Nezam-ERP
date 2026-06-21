from django.views import View
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.contrib.auth.models import User
from django.utils import timezone
import json

from manufacturing.models import ShiftAssignment, WorkOrder
from .dashboard import require_company, user_has_role
from manufacturing.services import NotificationService, WorkOrderService
from manufacturing.access_control import worker_eligible_user_q
from manufacturing.security import audit_request_action
from manufacturing.work_order_visibility import (
    can_user_see_work_order,
    get_current_shift_window_for_company,
)


class AssignWorkerToWOView(LoginRequiredMixin, View):
    """
    Assigns a worker to a Work Order.
    
    Spec: "The supervisor receives tasks and assigns them to workers. 
    He can assign the same worker to many WOs at the same time."
    
    POST /manufacturing/api/assign-worker/
    Body: {
        "wo_id": 123,
        "worker_id": 45,
        "notes": "Urgent - prioritize this"  # optional
    }
    """
    
    def post(self, request):
        try:
            if not user_has_role(request.user, ['supervisor', 'admin', 'planner']):
                return JsonResponse({"success": False, "error": "Unauthorized"}, status=403)

            company = require_company(request.user)
            data = json.loads(request.body)
            
            wo_id = data.get('wo_id')
            worker_id = data.get('worker_id')
            notes = data.get('notes', '')
            
            # Validate inputs
            if not wo_id or not worker_id:
                return JsonResponse({
                    "success": False, 
                    "error": "Work Order ID and Worker ID are required"
                }, status=400)
            
            # Get Work Order
            wo = get_object_or_404(WorkOrder, pk=wo_id, company=company)

            # If parent WO, assign the active child stage task
            target_wo = wo
            if wo.sub_tasks.exists():
                candidate = WorkOrderService.get_active_stage_task(wo)
                if candidate:
                    target_wo = candidate

            if target_wo.status not in ['pending', 'in_progress']:
                return JsonResponse({
                    "success": False,
                    "error": f"Work order is not assignable in status '{target_wo.status}'"
                }, status=400)

            if user_has_role(request.user, ['supervisor']) and not can_user_see_work_order(request.user, target_wo):
                return JsonResponse({"success": False, "error": "Unauthorized"}, status=403)
            
            # Get Worker
            worker = get_object_or_404(
                User, 
                pk=worker_id,
                profile__company=company
            )
            if not User.objects.filter(pk=worker.pk).filter(worker_eligible_user_q()).exists():
                return JsonResponse({
                    "success": False,
                    "error": "Selected user is not eligible for worker assignments"
                }, status=400)
            
            # Assign worker to WO
            target_wo.assigned_worker = worker
            target_wo.assignment_type = 'manual'  # Supervisor manually assigned
            if not target_wo.supervisor_start_at:
                target_wo.supervisor_start_at = timezone.now()
            
            # Add notes if provided
            if notes:
                existing_notes = target_wo.instructions or ''
                target_wo.instructions = f"{existing_notes}\n[{timezone.now().strftime('%Y-%m-%d %H:%M')}] Assigned to {worker.get_full_name() or worker.username}: {notes}".strip()
            
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

            NotificationService.notify_users(
                [worker],
                title="New work order assignment",
                message=f"You have been assigned to WO #{wo.id} ({wo.product_name}).",
                link="/manufacturing/shop-floor/"
            )
            
            return JsonResponse({
                "success": True,
                "message": f"Work Order #{target_wo.id} assigned to {worker.get_full_name() or worker.username}",
                "wo_id": target_wo.id,
                "worker_name": worker.get_full_name() or worker.username,
                "status": target_wo.status
            })
            
        except Exception as e:
            return JsonResponse({
                "success": False,
                "error": str(e)
            }, status=500)
    
class GetAvailableWorkersView(LoginRequiredMixin, View):
    """
    Returns a list of available workers for a specific machine.
    
    GET /manufacturing/api/available-workers/?machine_id=123
    """
    
    def get(self, request):
        try:
            if not user_has_role(request.user, ['supervisor', 'admin', 'planner']):
                return JsonResponse({"success": False, "error": "Unauthorized"}, status=403)

            company = require_company(request.user)
            machine_id = request.GET.get('machine_id')
            
            # Worker-mode supervisors can be assigned as operators when enabled.
            workers = User.objects.filter(
                profile__company=company
            ).filter(
                worker_eligible_user_q()
            ).select_related('profile')

            if machine_id and user_has_role(request.user, ['supervisor']):
                shift_window = get_current_shift_window_for_company(company)
                if shift_window.get("is_active"):
                    active_worker_ids = ShiftAssignment.objects.filter(
                        machine_id=machine_id,
                        machine__company=company,
                        shift_type=shift_window["shift_type"],
                        date=shift_window["assignment_date"],
                    ).values_list("worker_id", flat=True)
                    workers = workers.filter(id__in=active_worker_ids)
                else:
                    workers = workers.none()
            
            workers_data = [
                {
                    "id": w.id,
                    "name": w.get_full_name() or w.username,
                    "username": w.username,
                    "active_wos": w.assigned_work_orders.filter(
                        status__in=['pending', 'in_progress']
                    ).count()
                }
                for w in workers
            ]
            
            return JsonResponse({
                "success": True,
                "workers": workers_data
            })
            
        except Exception as e:
            return JsonResponse({
                "success": False,
                "error": str(e)
            }, status=500)
