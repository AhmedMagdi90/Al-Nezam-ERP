from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.models import User
from django.db.models import Q
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.utils import timezone
from django.views import View

from accounts.constants import RoleType
from manufacturing.models import Machine, MachineFault, SystemSettings
from .dashboard import require_company, user_has_role


class MaintenanceDashboardView(LoginRequiredMixin, View):
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

    def _attach_sla(self, faults, sla_hours):
        now = timezone.now()
        for fault in faults:
            due_at = fault.created_at + timedelta(hours=sla_hours)
            fault.sla_due_at = due_at
            remaining = due_at - now
            fault.sla_overdue = remaining.total_seconds() < 0
            fault.sla_display = self._format_sla(remaining)
        return faults

    def get(self, request):
        if not user_has_role(request.user, [RoleType.MAINTENANCE, RoleType.ADMIN, RoleType.SUPERVISOR, RoleType.PLANNER, RoleType.WORKER]):
            return redirect('login')

        company = require_company(request.user)
        if not company:
            return redirect('login')

        role_name = self._get_role_name(request.user)
        mode = request.GET.get('mode')
        if mode == 'planner' or role_name == RoleType.PLANNER.value:
            read_only = True
        else:
            read_only = False

        if read_only:
            machines = Machine.objects.filter(company=company).order_by('name')
            status_counts = {
                'operational': machines.filter(status='operational').count(),
                'maintenance': machines.filter(status='maintenance').count(),
                'broken': machines.filter(status__in=['broken', 'faulty', 'breakdown']).count(),
                'inactive': machines.filter(status='inactive').count(),
            }
            return render(request, 'manufacturing/maintenance_dashboard.html', {
                'read_only': read_only,
                'machines': machines,
                'broken_machines': machines.filter(status__in=['broken', 'maintenance', 'faulty']),
                'status_counts': status_counts,
            })

        settings = self._get_settings(company)
        sla_hours = int(getattr(settings, 'maintenance_sla_hours', 8) or 8)
        auto_close = bool(getattr(settings, 'maintenance_auto_close', False))

        faults_qs = MachineFault.objects.filter(
            machine__company=company
        ).select_related(
            'machine', 'reported_by', 'assigned_supervisor', 'assigned_worker', 'completed_by', 'approved_by'
        ).order_by('-created_at')

        faults = self._attach_sla(list(faults_qs), sla_hours)

        role_mode = 'viewer'
        if role_name in [RoleType.MAINTENANCE.value, RoleType.ADMIN.value] or request.user.is_superuser:
            role_mode = 'head'
        elif role_name == RoleType.SUPERVISOR.value:
            role_mode = 'supervisor'
        elif role_name == RoleType.WORKER.value:
            role_mode = 'worker'

        active_tab = request.GET.get('tab')
        selected_id = request.GET.get('ticket')

        head_inbox = [f for f in faults if f.status == 'open' and f.assigned_supervisor_id is None]
        head_assigned = [f for f in faults if f.status in ['open', 'assigned', 'in_progress', 'completed'] and f.assigned_supervisor_id is not None]
        head_history = [f for f in faults if f.status in ['approved', 'resolved']]

        supervisor_assigned = [f for f in faults if f.assigned_supervisor_id == request.user.id]
        supervisor_queue = [f for f in supervisor_assigned if f.status in ['open', 'assigned', 'in_progress'] and not f.completed_at]
        supervisor_review = [f for f in supervisor_assigned if f.status in ['completed']]
        supervisor_history = [f for f in supervisor_assigned if f.status in ['approved', 'resolved']]

        worker_queue = [f for f in faults if f.assigned_worker_id == request.user.id and f.status in ['assigned', 'in_progress'] and not f.completed_at]
        worker_history = [f for f in faults if f.assigned_worker_id == request.user.id and f.completed_at]

        tabs = []
        active_list = []
        if role_mode == 'head':
            tabs = [
                {"id": "inbox", "label": "Inbox", "count": len(head_inbox)},
                {"id": "assigned", "label": "Assigned", "count": len(head_assigned)},
                {"id": "history", "label": "History", "count": len(head_history)},
            ]
            if not active_tab:
                active_tab = 'inbox'
            active_list = head_inbox if active_tab == 'inbox' else head_assigned if active_tab == 'assigned' else head_history
        elif role_mode == 'supervisor':
            tabs = [
                {"id": "assigned", "label": "Assigned", "count": len(supervisor_queue)},
                {"id": "review", "label": "Review", "count": len(supervisor_review)},
                {"id": "history", "label": "History", "count": len(supervisor_history)},
            ]
            if not active_tab:
                active_tab = 'assigned'
            active_list = supervisor_queue if active_tab == 'assigned' else supervisor_review if active_tab == 'review' else supervisor_history
        elif role_mode == 'worker':
            tabs = [
                {"id": "my", "label": "My Jobs", "count": len(worker_queue)},
                {"id": "history", "label": "History", "count": len(worker_history)},
            ]
            if not active_tab:
                active_tab = 'my'
            active_list = worker_queue if active_tab == 'my' else worker_history

        selected_ticket = None
        if selected_id:
            selected_ticket = next((f for f in faults if str(f.id) == str(selected_id)), None)
        if not selected_ticket and active_list:
            selected_ticket = active_list[0]

        supervisors = User.objects.filter(profile__company=company, profile__role__name=RoleType.SUPERVISOR.value)
        workers = User.objects.filter(profile__company=company, profile__role__name=RoleType.WORKER.value)

        return render(request, 'manufacturing/maintenance_dashboard.html', {
            'read_only': read_only,
            'role_mode': role_mode,
            'tabs': tabs,
            'active_tab': active_tab,
            'active_list': active_list,
            'selected_ticket': selected_ticket,
            'sla_hours': sla_hours,
            'auto_close': auto_close,
            'supervisors': supervisors,
            'workers': workers,
        })

    def post(self, request):
        if not user_has_role(request.user, [RoleType.MAINTENANCE, RoleType.ADMIN, RoleType.SUPERVISOR, RoleType.WORKER]):
            messages.error(request, "Unauthorized")
            return redirect('maintenance_dashboard')

        company = require_company(request.user)
        if not company:
            messages.error(request, "No company")
            return redirect('maintenance_dashboard')

        action = request.POST.get('action')
        next_url = request.POST.get('next') or reverse('maintenance_dashboard')
        role_name = self._get_role_name(request.user)
        settings = self._get_settings(company)

        if action == 'toggle_auto_close':
            if role_name not in [RoleType.MAINTENANCE.value, RoleType.ADMIN.value] and not request.user.is_superuser:
                messages.error(request, "Unauthorized")
                return redirect(next_url)
            settings.maintenance_auto_close = request.POST.get('auto_close') == '1'
            settings.save(update_fields=['maintenance_auto_close'])
            messages.success(request, "Maintenance auto close updated.")
            return redirect(next_url)

        ticket_id = request.POST.get('ticket_id')
        if not ticket_id:
            messages.error(request, "Missing maintenance ticket.")
            return redirect(next_url)

        fault = get_object_or_404(MachineFault, id=ticket_id, machine__company=company)

        try:
            if action == 'assign_supervisor':
                if role_name not in [RoleType.MAINTENANCE.value, RoleType.ADMIN.value] and not request.user.is_superuser:
                    messages.error(request, "Unauthorized")
                    return redirect(next_url)
                supervisor_id = request.POST.get('supervisor_id')
                supervisor = User.objects.filter(id=supervisor_id, profile__company=company, profile__role__name=RoleType.SUPERVISOR.value).first()
                if not supervisor:
                    messages.error(request, "Supervisor not found.")
                    return redirect(next_url)
                fault.assigned_supervisor = supervisor
                fault.status = 'assigned'
                fault.save(update_fields=['assigned_supervisor', 'status'])
                messages.success(request, "Assigned to supervisor.")

            elif action == 'assign_worker':
                if role_name not in [RoleType.SUPERVISOR.value, RoleType.MAINTENANCE.value, RoleType.ADMIN.value] and not request.user.is_superuser:
                    messages.error(request, "Unauthorized")
                    return redirect(next_url)
                worker_id = request.POST.get('worker_id')
                worker = User.objects.filter(id=worker_id, profile__company=company, profile__role__name=RoleType.WORKER.value).first()
                if not worker:
                    messages.error(request, "Worker not found.")
                    return redirect(next_url)
                fault.assigned_worker = worker
                fault.status = 'in_progress'
                fault.save(update_fields=['assigned_worker', 'status'])
                messages.success(request, "Assigned to technician.")

            elif action == 'complete_task':
                if role_name not in [RoleType.WORKER.value, RoleType.SUPERVISOR.value, RoleType.MAINTENANCE.value, RoleType.ADMIN.value] and not request.user.is_superuser:
                    messages.error(request, "Unauthorized")
                    return redirect(next_url)
                notes = request.POST.get('resolution_notes', '')
                fault.completed_by = request.user
                fault.completed_at = timezone.now()
                fault.resolution_notes = notes
                fault.status = 'completed'
                fault.save(update_fields=['completed_by', 'completed_at', 'resolution_notes', 'status'])
                messages.success(request, "Work completed. Waiting for approval.")

            elif action in ['approve_task', 'override_task']:
                if role_name not in [RoleType.SUPERVISOR.value, RoleType.MAINTENANCE.value, RoleType.ADMIN.value] and not request.user.is_superuser:
                    messages.error(request, "Unauthorized")
                    return redirect(next_url)
                if not fault.completed_at and action != 'override_task':
                    messages.error(request, "Waiting for technician completion.")
                    return redirect(next_url)

                if action == 'override_task':
                    fault.override_notes = request.POST.get('override_notes', '')
                fault.status = 'approved'
                fault.approved_by = request.user
                fault.approved_at = timezone.now()
                fault.resolved_at = timezone.now()
                fault.save(update_fields=['status', 'approved_by', 'approved_at', 'override_notes', 'resolved_at'])

                if settings.maintenance_auto_close and fault.machine:
                    fault.machine.status = 'operational'
                    fault.machine.maintenance_note = ''
                    fault.machine.save(update_fields=['status', 'maintenance_note'])

                messages.success(request, "Maintenance task approved.")
            else:
                messages.error(request, "Unknown action.")

        except Exception as e:
            messages.error(request, f"Error updating maintenance ticket: {e}")

        return redirect(next_url)
