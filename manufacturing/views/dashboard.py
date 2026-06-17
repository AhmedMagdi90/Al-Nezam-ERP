from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.mixins import LoginRequiredMixin
from django.views import View
from django.http import JsonResponse, HttpResponseForbidden
from django.template.loader import render_to_string
from django.contrib import messages
from django.utils import timezone
from django.conf import settings
from django.core.serializers.json import DjangoJSONEncoder
from django.db import DatabaseError
from django.db.models import Q, F
from django.db.models.functions import Coalesce
from django.middleware.csrf import get_token
import logging
import json

from manufacturing.models import (
    Company, WorkOrder, Machine, ProductionStage, BillOfMaterial, ProductionLog, MachineFault, Product, Customer, QualityCheck
)

from manufacturing.forms import WorkOrderForm
from manufacturing.services import (
    DashboardService,
    WorkOrderService,
    WorkOrderCycleService,
    get_workorder_execution_readiness,
    get_workorder_quantity_breakdown,
    get_stage_time_breakdown,
)
from accounts.models import Profile, User
from accounts.constants import RoleType
from manufacturing.access_control import (
    is_worker_mode_enabled,
    user_has_access as _user_has_access,
    user_has_capability as _user_has_capability,
)
from tenancy.context import get_current_tenant_db

# ...
logger = logging.getLogger(__name__)

def user_has_role(user, allowed_roles):
    # Backward-compatible wrapper:
    # - iterable => role names
    # - string => capability key (preferred) OR role name
    return _user_has_access(user, allowed_roles)


def user_has_capability(user, capability):
    return _user_has_capability(user, capability)

def require_company(user):
    if not getattr(user, "is_authenticated", False):
        return None

    db_alias = (
        get_current_tenant_db()
        or getattr(getattr(user, "_state", None), "db", None)
        or "default"
    )
    try:
        profile_row = (
            Profile.objects.using(db_alias)
            .filter(user_id=user.id)
            .values("id", "company_id")
            .first()
        )
        if not profile_row:
            return None

        company_id = profile_row.get("company_id")
        if company_id:
            company = Company.objects.using(db_alias).filter(id=company_id).first()
            if company:
                return company

        company = Company.objects.using(db_alias).order_by("-created_at").first()
        if not company:
            return None

        profile_id = profile_row.get("id")
        if profile_id:
            Profile.objects.using(db_alias).filter(
                id=profile_id,
                company_id__isnull=True
            ).update(company_id=company.id)
        return company
    except DatabaseError:
        logger.exception(
            "Company resolution failed for user_id=%s on db=%s",
            getattr(user, "id", None),
            db_alias,
        )
        return None


def company_stage_queryset(company):
    """
    Resolve stages that belong to a company.

    ProductionStage does not carry a direct company FK, so ownership is inferred
    from either:
    - the stage's assigned machine.company, or
    - a BOM operation on a BOM whose product belongs to the company.
    """
    return ProductionStage.objects.filter(
        Q(machine__company=company) | Q(bomoperation__bom__product__company=company)
    ).distinct()


def get_company_stage(company, stage_id):
    if stage_id in [None, ""]:
        return None
    return company_stage_queryset(company).select_related("machine").filter(id=stage_id).first()


def get_company_stage_or_404(company, stage_id):
    return get_object_or_404(company_stage_queryset(company).select_related("machine"), id=stage_id)


def redirect_to_role_home(user):
    """Route users to their module when planner URL is opened by stale links."""
    if user_has_role(user, "ui.planner.dashboard"):
        return redirect("dashboard")
    if user_has_role(user, [RoleType.SUPERVISOR]):
        return redirect("supervisor_dashboard")
    if user_has_role(user, [RoleType.QUALITY]):
        return redirect("quality_check")
    if user_has_role(user, [RoleType.WORKER]):
        return redirect("record_output")
    if user_has_role(user, [RoleType.STORE]):
        return redirect("store_dashboard")
    if user_has_role(user, [RoleType.MAINTENANCE]):
        return redirect("maintenance_dashboard")
    return redirect("login")

class PlannerDashboardView(LoginRequiredMixin, View):
    # ...
    def get(self, request):
        # Supervisors should never stay on planner UI; send them to their dashboard.
        if user_has_role(request.user, [RoleType.SUPERVISOR]):
            return redirect("supervisor_dashboard")
        if user_has_role(request.user, [RoleType.STORE]):
            return redirect("store_dashboard")

        if not user_has_role(request.user, "ui.planner.dashboard"):
            messages.warning(request, "Your account is not assigned to Planner module.")
            return redirect_to_role_home(request.user)

        company = require_company(request.user)
        if not company:
            messages.warning(request, "Please set up your company profile first.")
            return redirect("factory_setup")

        if request.session.get("first_time_company_setup"):
            messages.info(request, "Finish or skip the setup wizard first. You can continue adding data later.")
            return redirect("onboarding_data")

        # Ensure CSRF secret exists in session/cookie before rendering any POST forms.
        get_token(request)
        context = self.get_context_data(request, company)
        context['planner_nav_mode'] = True
        return render(request, "manufacturing/planner_dashboard.html", context)

    def post(self, request):
        if user_has_role(request.user, [RoleType.SUPERVISOR]):
             return redirect("supervisor_dashboard")

        if not user_has_role(request.user, "ui.planner.manage"):
             messages.warning(request, "Your account is not assigned to Planner actions.")
             return redirect_to_role_home(request.user)
        
        company = require_company(request.user)
        if not company:
            return HttpResponseForbidden("No company")

        if request.session.get("first_time_company_setup"):
            messages.warning(request, "Finish or skip the setup wizard first before planner actions.")
            return redirect("onboarding_data")

        if "create_work_order" in request.POST:
            form = WorkOrderForm(request.POST)
            # Filter API
            form.fields['bom'].queryset = BillOfMaterial.objects.filter(product__company=company, status='active')
            if form.is_valid():
                wo = form.save(commit=False)
                wo.company = company
                wo.assigned_to = request.user
                wo.save()
                messages.success(request, "Work Order Created")
                return redirect("dashboard")
            else:
                context = self.get_context_data(request, company)
                context['form'] = form
                return render(request, "manufacturing/planner_dashboard.html", context)
        
        elif "create_repair_wo" in request.POST:
            qc_id = request.POST.get("quality_check_id")
            qc = get_object_or_404(QualityCheck, id=qc_id, work_order__company=company)
            
            # Create Repair WO
            repair_wo = WorkOrder.objects.create(
                company=company,
                product_name=qc.work_order.bom.product.name if qc.work_order.bom and qc.work_order.bom.product else qc.work_order.product_name,
                bom=qc.work_order.bom,
                quantity=qc.repair_quantity,
                due_date=timezone.now().date(),
                status='pending',
                order_type='repair',
                # Priority could be higher for repairs
            )
            
            qc.status = 'processed'
            qc.generated_wo = repair_wo
            qc.save()
            
            messages.success(request, f"Repair Work Order #{repair_wo.id} created from QC.")
            return redirect("dashboard")

        elif "compensate_scrap_wo" in request.POST:
            qc_id = request.POST.get("quality_check_id")
            qty_raw = request.POST.get("compensation_qty")
            qc = get_object_or_404(QualityCheck, id=qc_id, work_order__company=company)
            try:
                compensation_qty = int(qty_raw or 0)
                result = WorkOrderService.compensate_scrap_from_quality_check(
                    qc,
                    compensation_qty,
                    request.user
                )
                parent_wo = result.get("parent_work_order")
                compensation_task = result.get("compensation_task")
                if compensation_task:
                    messages.success(
                        request,
                        f"WO #{parent_wo.id} increased by {compensation_qty}. "
                        f"Compensation task #{compensation_task.id} created."
                    )
                else:
                    messages.success(
                        request,
                        f"WO #{parent_wo.id} increased by {compensation_qty} for scrap compensation."
                    )
            except Exception as exc:
                messages.error(request, f"Scrap compensation failed: {exc}")
            return redirect("dashboard")

        elif "accept_scrap_wo" in request.POST:
            qc_id = request.POST.get("quality_check_id")
            qty_raw = request.POST.get("accepted_qty")
            qc = get_object_or_404(QualityCheck, id=qc_id, work_order__company=company)
            try:
                accepted_qty = int(qty_raw or 0)
                result = WorkOrderService.accept_scrap_from_quality_check(
                    qc,
                    accepted_qty,
                    request.user
                )
                parent_wo = result.get("parent_work_order")
                messages.success(
                    request,
                    f"WO #{parent_wo.id} target reduced by {accepted_qty}. Scrap accepted."
                )
            except Exception as exc:
                messages.error(request, f"Accept scrap failed: {exc}")
            return redirect("dashboard")
        
        return redirect("dashboard")

# ...




    def handle_ajax(self, request, company, ajax_type):
        if ajax_type == "workorders":
            status_filter = request.GET.get("status", "")
            search = request.GET.get("search", "")
            work_orders = WorkOrder.objects.filter(company=company).select_related("machine", "bom", "assigned_to").order_by("-id")
            if status_filter:
                work_orders = work_orders.filter(status=status_filter)
            if search:
                work_orders = work_orders.filter(product_name__icontains=search)
            html = render_to_string("manufacturing/partials/workorders_list.html", {"work_orders": work_orders})
            return JsonResponse({"success": True, "html": html})

        elif ajax_type == "boms":
            boms = BillOfMaterial.objects.filter(product__company=company).select_related("product", "created_by").order_by("-created_at")
            html = render_to_string("manufacturing/partials/boms_list.html", {"boms": boms})
            return JsonResponse({"success": True, "html": html})

        elif ajax_type == "stages":
            stages = ProductionStage.objects.filter(machine__company=company).select_related("machine").order_by("order")
            html = render_to_string("manufacturing/partials/stages_list.html", {"stages": stages})
            return JsonResponse({"success": True, "html": html})
        
        elif ajax_type == "timeline":
            # Using DashboardService logic but tailored for the partial return
            # Although get_timeline_data returns JSON structure, here we need HTML?
            # Original code rendered "manufacturing/partials/timeline.html"
            
            machines = list(Machine.objects.for_company(company)) 
            work_orders = WorkOrder.objects.for_company(company).filter(sub_tasks__isnull=True).select_related(
                "machine"
            ).prefetch_related(
                "production_logs"
            ).order_by("start_date")
            
            active_wos = {wo.machine_id: wo for wo in work_orders if wo.status == 'in_progress' and wo.machine_id}
            for m in machines:
                 m.active_wo = active_wos.get(m.id)

            machines_data = [{"id": m.id, "name": m.name, "status": m.status} for m in machines]
            tasks_data = []
            for wo in work_orders:
                if not wo.machine:
                    continue
                base_qty, compensation_qty, adjusted_qty = get_workorder_quantity_breakdown(wo)
                setup_minutes, estimated_duration_minutes = get_stage_time_breakdown(wo)
                reported_qty = 0
                for log in wo.production_logs.all():
                    if log.status == 'rejected':
                        continue
                    reported_qty += log.quantity

                tasks_data.append({
                    "id": wo.id,
                    "machine_id": wo.machine.id if wo.machine else None,
                    "product": wo.product_name,
                    "start": wo.start_date.isoformat() if wo.start_date else None,
                    "end": wo.end_date.isoformat() if wo.end_date else None,
                    "status": wo.status,
                    "progress": getattr(wo, 'progress', 0),
                    "quantity": adjusted_qty,
                    "base_quantity": base_qty,
                    "scrap_compensation_qty": compensation_qty,
                    "has_scrap_compensation": compensation_qty > 0,
                    "is_scrap_compensation_task": bool(getattr(wo, 'is_scrap_compensation_task', False)),
                    "scrap_source_qc_id": getattr(wo, 'scrap_source_quality_check_id', None),
                    "setup_minutes": int(setup_minutes),
                    "estimated_duration_minutes": int(estimated_duration_minutes),
                    "progress_stats": {
                        "target": adjusted_qty,
                        "actual": int(reported_qty)
                    },
                    "finished_qty": int(reported_qty)
                })
            
            hours = list(range(24))
            html = render_to_string("manufacturing/partials/timeline_v2.html", {
                "machines": machines, 
                "work_orders": work_orders,
                "pending_wos": WorkOrder.objects.filter(company=company, status='pending', machine__isnull=True),
                "machines_json": json.dumps(machines_data, cls=DjangoJSONEncoder),
                "tasks_json": json.dumps(tasks_data, cls=DjangoJSONEncoder),
                "hours": hours,
            })
            return JsonResponse({"success": True, "html": html})
            
        return JsonResponse({"success": False, "error": "Invalid AJAX type"})

    def get_context_data(self, request, company):
        context = DashboardService.get_dashboard_context(
            company,
            viewer_role='planner',
            viewer=request.user,
        )
        context['user_role'] = 'planner'  # Explicit role for frontend logic
        context['reset_planner_workspace_state'] = bool(request.session.pop('reset_planner_workspace_state', False))
        context['first_workspace_tour'] = bool(request.session.pop('first_workspace_tour_pending', False))
        
        # Filter out drafts to ensure only 'pending' orders are shown
        if 'pending_wos' in context:
            context['pending_wos'] = context['pending_wos'].distinct()
            context['pending_count'] = context['pending_wos'].count() # Pass explicit int
            context['pending_wos_count'] = context['pending_count']
        
        if 'pending_orders' in context:
            context['pending_orders'] = context['pending_orders'].distinct()
        
        # Quality Alerts
        quality_alerts_qs = QualityCheck.objects.filter(
            work_order__company=company
        ).filter(
            Q(status='new', repair_quantity__gt=0) |
            Q(status='processed', faulty_quantity__gt=F('scrap_compensated_qty'))
        ).select_related('work_order', 'work_order__parent')

        quality_alerts = list(quality_alerts_qs)
        for qc in quality_alerts:
            qc.scrap_remaining = max(
                int(qc.faulty_quantity or 0) - int(getattr(qc, 'scrap_compensated_qty', 0) or 0),
                0
            )
            qc.target_work_order = qc.work_order.parent if qc.work_order and qc.work_order.parent_id else qc.work_order

        context['quality_alerts'] = quality_alerts
        context['quality_alerts_count'] = len(quality_alerts)

        # Add profile info if needed or specific planner overrides
        context['active_tab'] = request.GET.get('tab', 'manufacturing')
        
        # Add user-specific form
        form = WorkOrderForm()
        form.fields['bom'].queryset = BillOfMaterial.objects.filter(product__company=company, status='active')
        context['form'] = form
        context['open_wos_count'] = int(context.get('pending_wos_count') or 0) + int(
            context.get('planner_actions_count') or 0
        )
        
        return context


class SupervisorDashboardView(LoginRequiredMixin, View):
    """
    The Control Tower for the Supervisor.
    Redesigned to separate Planning (Machine) from Execution (Worker).
    """
    def get(self, request):
        if not user_has_role(request.user, "ui.supervisor.dashboard"):
           return HttpResponseForbidden("Unauthorized")

        company = require_company(request.user)
        if not company:
            return redirect("factory_setup")

        context = DashboardService.get_dashboard_context(
            company,
            viewer_role='supervisor',
            viewer=request.user,
        )
        context['user_role'] = 'supervisor'  # Explicit role for frontend logic
        context['worker_mode_enabled'] = is_worker_mode_enabled(request.user)
        
        # ========================================
        # REQUIREMENT 1: Supervisor execution queues
        # ========================================
        # Build one execution-visible pool first, then split into:
        # - pending_tasks: all scheduled but not started yet, including upcoming
        # - active_tasks: already in progress on the floor
        # - assignment_tasks: pending jobs still waiting for worker assignment
        execution_tasks_base = WorkOrder.objects.filter(
            company=company,
            machine__isnull=False,  # Machine IS assigned (Planning complete)
            status__in=['pending', 'in_progress'],
            sub_tasks__isnull=True # Show only leaf tasks (ignore containers)
        ).exclude(
            is_scrap_compensation_task=True,
            start_date__isnull=True
        ).select_related(
            'machine',
            'assigned_worker',
            'bom__product',
            'customer',
            'parent',
            'parent__current_stage',
        ).order_by('start_date', 'due_date').distinct()

        execution_task_ids = [
            wo.id for wo in DashboardService._filter_work_orders_for_viewer(
                execution_tasks_base,
                viewer_role='supervisor',
                shift_config=context.get('shift_config'),
                viewer=request.user,
                restrict_to_current_shift=False,
                include_future_pending=True,
            )
        ]
        execution_tasks = execution_tasks_base.filter(id__in=execution_task_ids)
        pending_tasks = execution_tasks.filter(status='pending').order_by('start_date', 'due_date', 'id')
        active_tasks = execution_tasks.filter(status='in_progress').order_by('-worker_start_at', 'start_date', 'id')
        assignment_tasks = pending_tasks.filter(assigned_worker__isnull=True)
        ready_dispatch_tasks = assignment_tasks.filter(
            machine__status='operational',
            closed_by_planner=False,
        ).annotate(
            dispatch_start_at=Coalesce('scheduled_start_date', 'start_date')
        ).order_by('dispatch_start_at', 'due_date', 'id')
        assigned_pending_tasks = pending_tasks.filter(assigned_worker__isnull=False).order_by('start_date', 'due_date', 'id')

        def with_cycle_state(queryset):
            rows = list(queryset)
            for row in rows:
                row.cycle_state = WorkOrderCycleService.describe(row)
                row.execution_readiness = get_workorder_execution_readiness(row)
                row.dispatch_block_reason = row.execution_readiness.get("reason")
                row.dispatch_block_code = row.execution_readiness.get("reason_code")
            return rows

        upcoming_pending_tasks_count = 0
        hidden_waiting_stage_count = 0
        hidden_other_execution_count = 0
        hidden_visibility_notes = []
        execution_task_list = list(execution_tasks_base.select_related('stage', 'current_stage'))
        visible_task_ids = set(execution_task_ids)
        current_time = timezone.now()
        shift_window = DashboardService.get_current_shift_window(
            shift_config=context.get('shift_config')
        )
        current_shift_start = shift_window.get('start') if shift_window.get('is_active', True) else None
        apply_department_filter = DashboardService._should_apply_supervisor_department_filter(request.user)
        viewer_departments = (
            DashboardService._resolve_viewer_departments(request.user)
            if apply_department_filter
            else set()
        )
        company_departments = (
            DashboardService._resolve_company_supervisor_departments(request.user)
            if apply_department_filter
            else set()
        )
        explicit_company_departments = {
            department for department in company_departments
            if department != 'production'
        }

        def _stage_rank(target_wo):
            stage_obj = getattr(target_wo, 'stage', None) or getattr(target_wo, 'current_stage', None)
            try:
                return int(getattr(stage_obj, 'order', 10**6) or 10**6)
            except (TypeError, ValueError):
                return 10**6

        open_parent_rank_map = {}
        for target_wo in execution_task_list:
            if not target_wo.parent_id:
                continue
            open_parent_rank_map.setdefault(target_wo.parent_id, []).append(_stage_rank(target_wo))

        for hidden_wo in execution_task_list:
            if hidden_wo.id in visible_task_ids:
                continue

            if viewer_departments:
                work_order_departments = DashboardService._resolve_work_order_department_keys(hidden_wo)
                department_match = (
                    viewer_departments & work_order_departments
                    or (
                        "production" in viewer_departments
                        and not (work_order_departments & explicit_company_departments)
                    )
                )
                if not department_match:
                    hidden_other_execution_count += 1
                    continue

            scheduled_start = getattr(hidden_wo, 'scheduled_start_date', None) or getattr(hidden_wo, 'start_date', None)
            if hidden_wo.status == 'pending' and scheduled_start and scheduled_start > current_time:
                upcoming_pending_tasks_count += 1
                continue

            flow_mode = str(
                getattr(hidden_wo, 'operation_flow_mode', '')
                or getattr(getattr(hidden_wo, 'parent', None), 'operation_flow_mode', '')
                or 'series'
            ).strip().lower()
            if hidden_wo.parent_id and flow_mode != 'parallel':
                sibling_ranks = open_parent_rank_map.get(hidden_wo.parent_id, [])
                if sibling_ranks and _stage_rank(hidden_wo) > min(sibling_ranks):
                    hidden_waiting_stage_count += 1
                    continue

            hidden_other_execution_count += 1

        if hidden_waiting_stage_count:
            hidden_visibility_notes.append(
                f"{hidden_waiting_stage_count} staged jobs are still waiting for their previous operation to finish."
            )
        if upcoming_pending_tasks_count:
            hidden_visibility_notes.append(
                f"{upcoming_pending_tasks_count} additional jobs are scheduled for later but still visible in Pending Work Orders."
            )
        if hidden_other_execution_count:
            hidden_visibility_notes.append(
                f"{hidden_other_execution_count} additional jobs are hidden because they are outside this supervisor view."
            )

        shift_handover_tasks = []
        if current_shift_start:
            shift_handover_tasks = list(
                execution_tasks.filter(
                    status__in=['pending', 'in_progress'],
                    start_date__lt=current_shift_start,
                )
                .select_related('machine', 'assigned_worker', 'stage', 'current_stage')
                .order_by('status', 'start_date', 'id')
            )
            for handover_wo in shift_handover_tasks:
                handover_wo.handover_worker_name = (
                    handover_wo.assigned_worker.get_full_name()
                    if handover_wo.assigned_worker and handover_wo.assigned_worker.get_full_name()
                    else (
                        handover_wo.assigned_worker.username
                        if handover_wo.assigned_worker
                        else "Not assigned"
                    )
                )
                handover_wo.handover_status_label = handover_wo.get_status_display()
                handover_stage = getattr(handover_wo, 'stage', None) or getattr(handover_wo, 'current_stage', None)
                handover_wo.handover_stage_name = getattr(handover_stage, 'name', None) or "-"

        def _upcoming_section_name(target_wo):
            stage_obj = getattr(target_wo, 'stage', None) or getattr(target_wo, 'current_stage', None)
            machine_obj = getattr(target_wo, 'machine', None)
            for value in (
                getattr(stage_obj, 'category', None),
                getattr(machine_obj, 'category', None),
                getattr(stage_obj, 'name', None),
            ):
                if str(value or '').strip():
                    return str(value).strip()
            return "-"

        def _upcoming_worker_name(target_wo):
            worker = getattr(target_wo, 'assigned_worker', None)
            if not worker:
                return "Not assigned"
            return worker.get_full_name() or worker.username

        upcoming_tasks = with_cycle_state(
            pending_tasks.filter(start_date__gt=current_time)
            .select_related('assigned_worker', 'machine', 'stage', 'current_stage')
            .order_by('start_date', 'id')
        )
        for upcoming_wo in upcoming_tasks:
            upcoming_wo.upcoming_section_name = _upcoming_section_name(upcoming_wo)
            upcoming_wo.upcoming_worker_name = _upcoming_worker_name(upcoming_wo)

        section_filter_labels = []
        if apply_department_filter:
            profile = getattr(request.user, "profile", None)
            section_filter_labels = DashboardService._split_department_values(
                getattr(profile, "department", None) if profile else None
            )

        context['pending_tasks'] = pending_tasks
        context['active_tasks'] = with_cycle_state(active_tasks)
        context['active_tasks_count'] = active_tasks.count()
        context['assignment_tasks'] = assignment_tasks
        context['assignment_tasks_count'] = assignment_tasks.count()
        context['ready_assignment_tasks'] = with_cycle_state(assignment_tasks)
        context['ready_assignment_count'] = assignment_tasks.count()
        context['ready_dispatch_tasks'] = with_cycle_state(ready_dispatch_tasks)
        context['ready_dispatch_count'] = ready_dispatch_tasks.count()
        context['assigned_pending_tasks'] = with_cycle_state(assigned_pending_tasks)
        context['assigned_pending_count'] = assigned_pending_tasks.count()
        # Keep all supervisor intake widgets bound to the execution-filtered queue only.
        # The shared dashboard context also exposes `unmanned_orders`, but that list is
        # broader and can include future reserved stages that supervisors must not see.
        context['unmanned_orders'] = assignment_tasks
        context['pending_tasks_count'] = pending_tasks.count()
        
        # Split WOs Pending Assignment
        split_pending_tasks = assignment_tasks.filter(parent__isnull=False).select_related('machine', 'parent').distinct()
        context['split_pending_tasks'] = split_pending_tasks
        context['split_pending_count'] = split_pending_tasks.count()
        
        # Supervisor Specific: Filter out draft work orders (drafts are planner-only)
        context['pending_orders'] = execution_tasks
        context['pending_wos'] = pending_tasks
        context['pending_wos_count'] = pending_tasks.count()
        context['open_wos_count'] = pending_tasks.count() + active_tasks.count()
        context['upcoming_tasks'] = upcoming_tasks
        context['upcoming_pending_tasks_count'] = len(upcoming_tasks)
        context['upcoming_section_filter_label'] = ", ".join(section_filter_labels)
        context['hidden_waiting_stage_count'] = hidden_waiting_stage_count
        context['hidden_other_execution_count'] = hidden_other_execution_count
        context['hidden_visibility_notes'] = hidden_visibility_notes
        context['shift_handover_tasks'] = shift_handover_tasks
        context['shift_handover_count'] = len(shift_handover_tasks)
        context['shift_handover_window'] = shift_window
        context['now'] = current_time

        # Supervisor specific additions
        context['users'] = User.objects.filter(profile__company=company)
        context['material_catalog'] = Product.objects.filter(
            company=company,
            material_type__in=['raw', 'semi', 'packaging'],
        ).order_by('name')
        context['active_tab'] = request.GET.get('tab', 'board')
        
        return render(request, 'manufacturing/supervisor_dashboard.html', context)
