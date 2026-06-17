from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.models import User
from django.db.models import Q, Sum, F, IntegerField, ExpressionWrapper, OuterRef, Subquery, Exists, Case, When, Value
from django.db.models.functions import Coalesce
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.utils import timezone
from django.views import View

from accounts.constants import RoleType
from manufacturing.models import WorkOrder, QualityCheck, SystemSettings, ProductionLog
from .dashboard import require_company, user_has_role


class QualityCheckView(LoginRequiredMixin, View):
    def _get_role_name(self, user):
        try:
            return user.profile.role.name.lower()
        except Exception:
            return ''

    def _get_settings(self, company):
        settings = SystemSettings.objects.filter(company=company).first()
        if not settings:
            settings = SystemSettings.objects.create(company=company)
        return settings

    def _format_sla(self, delta):
        seconds = int(abs(delta.total_seconds()))
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        if delta.total_seconds() >= 0:
            return f"{hours}h {minutes}m left"
        return f"{hours}h {minutes}m overdue"

    def _with_qc_metrics(self, queryset):
        approved_sq = ProductionLog.objects.filter(
            work_order_id=OuterRef('pk'),
            status='approved'
        ).values('work_order_id').annotate(
            total=Sum('quantity')
        ).values('total')[:1]

        qc_good_sq = QualityCheck.objects.filter(
            work_order_id=OuterRef('pk'),
            status='processed'
        ).values('work_order_id').annotate(
            total=Sum('good_quantity')
        ).values('total')[:1]

        qc_repair_sq = QualityCheck.objects.filter(
            work_order_id=OuterRef('pk'),
            status='processed'
        ).values('work_order_id').annotate(
            total=Sum('repair_quantity')
        ).values('total')[:1]

        qc_faulty_sq = QualityCheck.objects.filter(
            work_order_id=OuterRef('pk'),
            status='processed'
        ).values('work_order_id').annotate(
            total=Sum('faulty_quantity')
        ).values('total')[:1]

        return queryset.annotate(
            approved_qty=Coalesce(Subquery(approved_sq, output_field=IntegerField()), 0),
            qc_good=Coalesce(Subquery(qc_good_sq, output_field=IntegerField()), 0),
            qc_repair=Coalesce(Subquery(qc_repair_sq, output_field=IntegerField()), 0),
            qc_faulty=Coalesce(Subquery(qc_faulty_sq, output_field=IntegerField()), 0),
            has_new_qc=Exists(QualityCheck.objects.filter(work_order_id=OuterRef('pk'), status='new')),
        ).annotate(
            qc_checked=ExpressionWrapper(F('qc_good') + F('qc_repair') + F('qc_faulty'), output_field=IntegerField())
        )

    def _compute_work_order_metrics(self, company):
        base_qs = WorkOrder.objects.filter(
            company=company,
            qc_requirement=True
        ).exclude(
            status__in=['canceled', 'archived']
        )
        return self._with_qc_metrics(base_qs)

    def _attach_metrics(self, qc_items, metrics_map, sla_hours):
        now = timezone.now()
        for qc in qc_items:
            wo_metrics = metrics_map.get(qc.work_order_id)
            approved_qty = int(getattr(wo_metrics, 'approved_qty', 0) or 0)
            qc_checked = int(getattr(wo_metrics, 'qc_checked', 0) or 0)
            qc.approved_qty = approved_qty
            qc.qc_checked = qc_checked
            qc.pending_qty = max(approved_qty - qc_checked, 0)
            qc.stage_name = qc.work_order.stage.name if qc.work_order and qc.work_order.stage else '-'
            qc.machine_name = qc.work_order.machine.display_label if qc.work_order and qc.work_order.machine else 'Manual'
            due_at = qc.created_at + timedelta(hours=sla_hours)
            qc.sla_due_at = due_at
            remaining = due_at - now
            qc.sla_overdue = remaining.total_seconds() < 0
            qc.sla_display = self._format_sla(remaining)
        return qc_items

    def _pending_qty_for_work_order(self, wo):
        metrics = self._with_qc_metrics(WorkOrder.objects.filter(id=wo.id)).first()
        if not metrics:
            return 0
        return max(int(metrics.approved_qty or 0) - int(metrics.qc_checked or 0), 0)

    def get(self, request):
        if not user_has_role(request.user, [RoleType.QUALITY, RoleType.ADMIN, RoleType.SUPERVISOR, RoleType.PLANNER, RoleType.WORKER]):
            return redirect('dashboard')

        company = require_company(request.user)
        if not company:
            return redirect('dashboard')

        role_name = self._get_role_name(request.user)
        mode = request.GET.get('mode')
        if mode == 'planner' or role_name == RoleType.PLANNER.value:
            read_only = True
        else:
            read_only = False

        if read_only:
            work_orders = self._with_qc_metrics(WorkOrder.objects.filter(
                company=company,
                qc_requirement=True
            ).exclude(
                status__in=['canceled', 'archived']
            )).filter(
                Q(has_new_qc=True) | Q(approved_qty__gt=F('qc_checked'))
            ).annotate(
                pending_qc_qty=Case(
                    When(approved_qty__gt=F('qc_checked'), then=F('approved_qty') - F('qc_checked')),
                    default=Value(0),
                    output_field=IntegerField()
                )
            ).select_related('machine', 'bom').order_by('-end_date', '-id')

            pending_qty = work_orders.aggregate(total=Sum('pending_qc_qty'))['total'] or 0
            machines_affected = work_orders.values('machine_id').distinct().count()
            return render(request, 'manufacturing/quality_check.html', {
                "work_orders": work_orders,
                "read_only": read_only,
                "pending_qty": pending_qty,
                "machines_affected": machines_affected
            })

        settings = self._get_settings(company)
        sla_hours = int(getattr(settings, 'qc_sla_hours', 8) or 8)
        qc_auto_release = bool(getattr(settings, 'qc_auto_release', False))

        qc_queryset = QualityCheck.objects.filter(
            work_order__company=company
        ).select_related(
            'work_order', 'work_order__machine', 'work_order__stage',
            'assigned_supervisor', 'assigned_worker', 'completed_by', 'approved_by'
        ).order_by('-created_at')

        metrics_map = {wo.id: wo for wo in self._compute_work_order_metrics(company)}
        qc_items = self._attach_metrics(list(qc_queryset), metrics_map, sla_hours)
        sla_overdue_count = sum(1 for qc in qc_items if getattr(qc, 'sla_overdue', False))

        role_mode = 'viewer'
        if role_name in [RoleType.QUALITY.value, RoleType.ADMIN.value] or request.user.is_superuser:
            role_mode = 'head'
        elif role_name == RoleType.SUPERVISOR.value:
            role_mode = 'supervisor'
        elif role_name == RoleType.WORKER.value:
            role_mode = 'worker'

        active_tab = request.GET.get('tab')
        selected_qc_id = request.GET.get('qc')

        head_inbox = [qc for qc in qc_items if qc.status == 'new' and qc.assigned_supervisor_id is None]
        head_assigned = [qc for qc in qc_items if qc.status == 'new' and qc.assigned_supervisor_id is not None]
        head_sla = [qc for qc in qc_items if getattr(qc, 'sla_overdue', False)]
        head_history = [qc for qc in qc_items if qc.status == 'processed']

        supervisor_assigned = [qc for qc in qc_items if qc.assigned_supervisor_id == request.user.id]
        supervisor_queue = [qc for qc in supervisor_assigned if qc.status == 'new' and (qc.assigned_worker_id is None or not qc.completed_at)]
        supervisor_review = [qc for qc in supervisor_assigned if qc.status == 'new' and qc.completed_at]
        supervisor_history = [qc for qc in supervisor_assigned if qc.status == 'processed']

        worker_queue = [qc for qc in qc_items if qc.assigned_worker_id == request.user.id and qc.status == 'new' and not qc.completed_at]
        worker_history = [qc for qc in qc_items if qc.assigned_worker_id == request.user.id and qc.completed_at]

        tabs = []
        active_list = []
        kpi_open_count = 0
        kpi_review_count = 0
        kpi_sla_count = 0
        kpi_history_count = 0

        if role_mode == 'head':
            tabs = [
                {"id": "inbox", "label": "Inbox", "count": len(head_inbox)},
                {"id": "assigned", "label": "Assignments", "count": len(head_assigned)},
                {"id": "sla", "label": "SLA", "count": len(head_sla)},
                {"id": "history", "label": "History", "count": len(head_history)},
            ]
            if not active_tab:
                active_tab = "inbox"
            if active_tab == "assigned":
                active_list = head_assigned
            elif active_tab == "sla":
                active_list = head_sla
            elif active_tab == "history":
                active_list = head_history
            else:
                active_list = head_inbox
            kpi_open_count = len(head_inbox)
            kpi_review_count = len(head_assigned)
            kpi_sla_count = len(head_sla)
            kpi_history_count = len(head_history)
        elif role_mode == 'supervisor':
            tabs = [
                {"id": "assigned", "label": "Queue", "count": len(supervisor_queue)},
                {"id": "review", "label": "In Review", "count": len(supervisor_review)},
                {"id": "history", "label": "History", "count": len(supervisor_history)},
            ]
            if not active_tab:
                active_tab = "assigned"
            active_list = supervisor_queue if active_tab == "assigned" else supervisor_review if active_tab == "review" else supervisor_history
            kpi_open_count = len(supervisor_queue)
            kpi_review_count = len(supervisor_review)
            kpi_sla_count = sum(1 for qc in supervisor_assigned if getattr(qc, 'sla_overdue', False))
            kpi_history_count = len(supervisor_history)
        elif role_mode == 'worker':
            tabs = [
                {"id": "my", "label": "My Task", "count": len(worker_queue)},
                {"id": "history", "label": "History", "count": len(worker_history)},
            ]
            if not active_tab:
                active_tab = "my"
            active_list = worker_queue if active_tab == "my" else worker_history

        selected_qc = None
        if selected_qc_id:
            selected_qc = next((qc for qc in qc_items if str(qc.id) == str(selected_qc_id)), None)
        if not selected_qc and active_list:
            selected_qc = active_list[0]

        supervisors = User.objects.filter(profile__company=company, profile__role__name=RoleType.SUPERVISOR.value)
        workers = User.objects.filter(profile__company=company, profile__role__name=RoleType.WORKER.value)
        criteria_list = []
        if selected_qc and selected_qc.work_order and selected_qc.work_order.bom:
            criteria_list = list(selected_qc.work_order.bom.acceptance_criteria.all())

        return render(request, 'manufacturing/quality_check.html', {
            "read_only": read_only,
            "role_mode": role_mode,
            "tabs": tabs,
            "active_tab": active_tab,
            "active_list": active_list,
            "selected_qc": selected_qc,
            "sla_hours": sla_hours,
            "sla_overdue_count": sla_overdue_count,
            "kpi_open_count": kpi_open_count,
            "kpi_review_count": kpi_review_count,
            "kpi_sla_count": kpi_sla_count,
            "kpi_history_count": kpi_history_count,
            "qc_auto_release": qc_auto_release,
            "supervisors": supervisors,
            "workers": workers,
            "criteria_list": criteria_list
        })

    def post(self, request):
        if not user_has_role(request.user, [RoleType.QUALITY, RoleType.ADMIN, RoleType.SUPERVISOR, RoleType.WORKER]):
            messages.error(request, "Unauthorized")
            return redirect('quality_check')

        company = require_company(request.user)
        if not company:
            messages.error(request, "No company")
            return redirect('quality_check')

        action = request.POST.get('action')
        next_url = request.POST.get('next') or reverse('quality_check')
        role_name = self._get_role_name(request.user)
        settings = self._get_settings(company)

        if action == 'toggle_auto_release':
            if role_name not in [RoleType.QUALITY.value, RoleType.ADMIN.value] and not request.user.is_superuser:
                messages.error(request, "Unauthorized")
                return redirect(next_url)
            settings.qc_auto_release = request.POST.get('auto_release') == '1'
            settings.save(update_fields=['qc_auto_release'])
            messages.success(request, "Quality release mode updated.")
            return redirect(next_url)

        qc_id = request.POST.get('qc_id')
        if not qc_id:
            messages.error(request, "Missing quality check.")
            return redirect(next_url)

        qc = get_object_or_404(QualityCheck, id=qc_id, work_order__company=company)
        wo = qc.work_order

        try:
            if action == 'assign_supervisor':
                if role_name not in [RoleType.QUALITY.value, RoleType.ADMIN.value] and not request.user.is_superuser:
                    messages.error(request, "Unauthorized")
                    return redirect(next_url)
                supervisor_id = request.POST.get('supervisor_id')
                supervisor = User.objects.filter(id=supervisor_id, profile__company=company, profile__role__name=RoleType.SUPERVISOR.value).first()
                if not supervisor:
                    messages.error(request, "Supervisor not found.")
                    return redirect(next_url)
                qc.assigned_supervisor = supervisor
                qc.save(update_fields=['assigned_supervisor'])
                messages.success(request, "Assigned to supervisor.")

            elif action == 'assign_worker':
                if role_name not in [RoleType.SUPERVISOR.value, RoleType.QUALITY.value, RoleType.ADMIN.value] and not request.user.is_superuser:
                    messages.error(request, "Unauthorized")
                    return redirect(next_url)
                worker_id = request.POST.get('worker_id')
                worker = User.objects.filter(id=worker_id, profile__company=company, profile__role__name=RoleType.WORKER.value).first()
                if not worker:
                    messages.error(request, "Worker not found.")
                    return redirect(next_url)
                qc.assigned_worker = worker
                qc.save(update_fields=['assigned_worker'])
                messages.success(request, "Assigned to worker.")

            elif action == 'complete_qc':
                if role_name not in [RoleType.WORKER.value, RoleType.SUPERVISOR.value, RoleType.QUALITY.value, RoleType.ADMIN.value] and not request.user.is_superuser:
                    messages.error(request, "Unauthorized")
                    return redirect(next_url)
                good_qty = int(request.POST.get('good_quantity', 0) or 0)
                repair_qty = int(request.POST.get('repair_quantity', 0) or 0)
                faulty_qty = int(request.POST.get('faulty_quantity', 0) or 0)
                notes = request.POST.get('notes', '')
                total = good_qty + repair_qty + faulty_qty
                pending_qty = self._pending_qty_for_work_order(wo)
                if total <= 0:
                    messages.error(request, "Enter inspected quantities before submitting.")
                    return redirect(next_url)
                if total != pending_qty:
                    messages.error(request, f"Inspected quantity must equal pending ({pending_qty}).")
                    return redirect(next_url)

                qc.good_quantity = good_qty
                qc.repair_quantity = repair_qty
                qc.faulty_quantity = faulty_qty
                qc.notes = notes
                qc.completed_by = request.user
                qc.completed_at = timezone.now()
                qc.checked_by = request.user
                qc.save(update_fields=[
                    'good_quantity', 'repair_quantity', 'faulty_quantity',
                    'notes', 'completed_by', 'completed_at', 'checked_by'
                ])
                messages.success(request, "Inspection submitted for supervisor review.")

            elif action in ['approve_qc', 'override_qc']:
                if role_name not in [RoleType.SUPERVISOR.value, RoleType.QUALITY.value, RoleType.ADMIN.value] and not request.user.is_superuser:
                    messages.error(request, "Unauthorized")
                    return redirect(next_url)
                if not qc.completed_at and action != 'override_qc':
                    messages.error(request, "Waiting for worker completion.")
                    return redirect(next_url)

                pending_qty = self._pending_qty_for_work_order(wo)
                if 'good_quantity' in request.POST:
                    qc.good_quantity = int(request.POST.get('good_quantity', qc.good_quantity) or 0)
                    qc.repair_quantity = int(request.POST.get('repair_quantity', qc.repair_quantity) or 0)
                    qc.faulty_quantity = int(request.POST.get('faulty_quantity', qc.faulty_quantity) or 0)
                if action == 'override_qc':
                    qc.override_notes = request.POST.get('override_notes', '')
                qc_total = int(qc.good_quantity or 0) + int(qc.repair_quantity or 0) + int(qc.faulty_quantity or 0)
                if qc_total != pending_qty:
                    messages.error(request, f"Inspected quantity must equal pending ({pending_qty}).")
                    return redirect(next_url)
                qc.status = 'processed'
                qc.approved_by = request.user
                qc.approved_at = timezone.now()
                qc.checked_by = request.user
                qc.save(update_fields=[
                    'good_quantity', 'repair_quantity', 'faulty_quantity', 'override_notes',
                    'status', 'approved_by', 'approved_at', 'checked_by'
                ])

                from manufacturing.services import WorkOrderService
                try:
                    WorkOrderService.create_next_stage_task(wo, request.user, auto_create=settings.qc_auto_release)
                except Exception:
                    pass

                messages.success(request, "Quality check approved.")
            else:
                messages.error(request, "Unknown action.")

        except Exception as e:
            messages.error(request, f"Error saving inspection: {e}")
        return redirect(next_url)
