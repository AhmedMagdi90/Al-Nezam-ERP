from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.models import User
from django.shortcuts import redirect, render
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views import View

from accounts.constants import RoleType
from accounts.models import Profile
from manufacturing.access_control import resolve_user_role
from manufacturing.models import EmployeeShiftChangeLog, SystemSettings
from manufacturing.shift_utils import SHIFT_LABELS, normalize_shift_configuration

from .dashboard import require_company, user_has_role


class EmployeeShiftPlannerView(LoginRequiredMixin, View):
    EMPLOYEE_ROLE_NAMES = {RoleType.SUPERVISOR.value, RoleType.WORKER.value}

    def _tenant_db_alias(self, request):
        return getattr(request, "tenant_db_alias", "default")

    def _split_department_values(self, value):
        if not value:
            return []
        seen = set()
        values = []
        for item in str(value).replace(";", "\n").splitlines():
            label = " ".join(item.strip().split())
            key = label.lower()
            if label and key not in seen:
                seen.add(key)
                values.append(label)
        return values

    def _shift_options(self, settings):
        configured = normalize_shift_configuration(
            settings.shift_configuration,
            shift_mode=getattr(settings, "shift_mode", "3"),
            default_enabled=True,
        )
        options = []
        for key in ("morning", "afternoon", "night"):
            window = configured.get(key, {})
            if not window.get("enabled"):
                continue
            value = "evening" if key == "afternoon" else key
            start = str(window.get("start") or "").strip()
            end = str(window.get("end") or "").strip()
            options.append(
                {
                    "value": value,
                    "label": SHIFT_LABELS.get(key, key.title()),
                    "window": f"{start} - {end}" if start and end else "",
                }
            )
        return options

    def _all_department_options(self, settings):
        catalog = settings.department_catalog or {}
        seen = set()
        options = []
        for values in catalog.values():
            if not isinstance(values, list):
                continue
            for value in values:
                label = " ".join(str(value or "").strip().split())
                key = label.lower()
                if label and key not in seen:
                    seen.add(key)
                    options.append(label)
        return options

    def _active_shift(self, profile, today=None):
        today = today or timezone.localdate()
        planned_shift = (getattr(profile, "planned_shift", None) or "").strip().lower()
        planned_start = getattr(profile, "planned_shift_start_date", None)
        if planned_shift and planned_start and planned_start <= today:
            return planned_shift
        return (getattr(profile, "shift", None) or "").strip().lower()

    def _shift_label_map(self, shift_options):
        labels = {"": "Unassigned"}
        for option in shift_options:
            labels[option["value"]] = option["label"]
        return labels

    def _employee_queryset(self, company, db_alias):
        return (
            User.objects.using(db_alias)
            .filter(profile__company=company, profile__role__name__in=self.EMPLOYEE_ROLE_NAMES)
            .select_related("profile", "profile__role")
            .order_by("profile__role__name", "first_name", "last_name", "username")
        )

    def _employee_rows(self, request, company, db_alias, shift_options):
        department_filter = (request.GET.get("department") or "").strip().lower()
        role_filter = (request.GET.get("role") or "").strip().lower()
        shift_filter = (request.GET.get("shift") or "").strip().lower()
        query = (request.GET.get("q") or "").strip().lower()
        labels = self._shift_label_map(shift_options)

        rows = []
        for employee in self._employee_queryset(company, db_alias):
            profile = employee.profile
            departments = self._split_department_values(profile.department)
            role_name = (getattr(profile.role, "name", "") or "").strip().lower()
            active_shift = self._active_shift(profile)
            search_text = " ".join(
                [
                    employee.get_full_name(),
                    employee.username,
                    employee.email,
                    getattr(profile, "phone", "") or "",
                    " ".join(departments),
                    role_name,
                ]
            ).lower()

            if query and query not in search_text:
                continue
            if role_filter and role_name != role_filter:
                continue
            if shift_filter and active_shift != shift_filter:
                continue
            if department_filter and department_filter not in {item.lower() for item in departments}:
                continue

            rows.append(
                {
                    "user": employee,
                    "profile": profile,
                    "role_name": role_name,
                    "departments": departments,
                    "active_shift": active_shift,
                    "active_shift_label": labels.get(active_shift, active_shift.title() if active_shift else "Unassigned"),
                    "planned_shift_label": labels.get(
                        profile.planned_shift or "",
                        str(profile.planned_shift or "").title(),
                    ),
                }
            )
        return rows

    def _shift_board(self, rows, shift_options):
        columns = []
        for index, option in enumerate(shift_options):
            columns.append(
                {
                    **option,
                    "rows": [row for row in rows if row["active_shift"] == option["value"]],
                    "tone": ["cyan", "violet", "emerald"][index % 3],
                }
            )
        columns.append(
            {
                "value": "",
                "label": "Unassigned",
                "window": "",
                "rows": [row for row in rows if not row["active_shift"]],
                "tone": "slate",
            }
        )
        return columns

    def _selected_profiles(self, request, company, db_alias):
        selected_ids = request.POST.getlist("employee_ids")
        csv_ids = request.POST.get("employee_ids_csv")
        if csv_ids:
            selected_ids.extend(item.strip() for item in csv_ids.split(",") if item.strip())
        if not selected_ids:
            return []
        selected_ids = list(dict.fromkeys(selected_ids))
        return list(
            Profile.objects.using(db_alias)
            .filter(
                user_id__in=selected_ids,
                company=company,
                role__name__in=self.EMPLOYEE_ROLE_NAMES,
            )
            .select_related("user", "role")
        )

    def _parse_start_date(self, request):
        start_date = parse_date(request.POST.get("start_date") or "")
        if not start_date:
            messages.error(request, "Choose the shift change start date.")
            return None
        if start_date < timezone.localdate():
            messages.error(request, "Start date cannot be in the past.")
            return None
        return start_date

    def _valid_shift_values(self, shift_options):
        return {option["value"] for option in shift_options}

    def _log_change(self, db_alias, company, actor, profile, action, new_shift, start_date, note=""):
        EmployeeShiftChangeLog.objects.using(db_alias).create(
            company=company,
            employee=profile.user,
            changed_by=actor,
            action=action,
            previous_shift=profile.shift,
            new_shift=new_shift,
            previous_planned_shift=profile.planned_shift,
            previous_planned_shift_start_date=profile.planned_shift_start_date,
            effective_start_date=start_date,
            note=note,
        )

    def get(self, request):
        if not user_has_role(request.user, "ui.settings.view"):
            messages.error(request, "You are not authorized to access shift planning.")
            return redirect("dashboard")

        company = require_company(request.user)
        if not company:
            return redirect("factory_setup")

        db_alias = self._tenant_db_alias(request)
        settings, _ = SystemSettings.objects.using(db_alias).get_or_create(company=company)
        shift_options = self._shift_options(settings)
        rows = self._employee_rows(request, company, db_alias, shift_options)
        recent_changes = list(
            EmployeeShiftChangeLog.objects.using(db_alias)
            .filter(company=company)
            .select_related("employee", "changed_by")
            .order_by("-created_at")[:12]
        )

        context = {
            "company": company,
            "current_role_name": resolve_user_role(request.user) or "",
            "department_options": self._all_department_options(settings),
            "shift_options": shift_options,
            "employee_rows": rows,
            "shift_board": self._shift_board(rows, shift_options),
            "recent_changes": recent_changes,
            "selected_department": request.GET.get("department", ""),
            "selected_role": request.GET.get("role", ""),
            "selected_shift": request.GET.get("shift", ""),
            "query": request.GET.get("q", ""),
            "today": timezone.localdate().isoformat(),
        }
        return render(request, "manufacturing/employee_shift_planner.html", context)

    def post(self, request):
        if not user_has_role(request.user, "ui.settings.view"):
            messages.error(request, "You are not authorized to update shift planning.")
            return redirect("dashboard")

        company = require_company(request.user)
        if not company:
            return redirect("factory_setup")

        db_alias = self._tenant_db_alias(request)
        settings, _ = SystemSettings.objects.using(db_alias).get_or_create(company=company)
        shift_options = self._shift_options(settings)
        valid_shifts = self._valid_shift_values(shift_options)
        action = (request.POST.get("bulk_action") or "").strip().lower()
        profiles = self._selected_profiles(request, company, db_alias)

        if not profiles:
            messages.error(request, "Select at least one employee first.")
            return redirect("employee_shift_planner")

        if action == "clear":
            for profile in profiles:
                self._log_change(db_alias, company, request.user, profile, "clear", None, None, note="Cleared planned shift")
                profile.planned_shift = None
                profile.planned_shift_start_date = None
                profile.save(using=db_alias, update_fields=["planned_shift", "planned_shift_start_date"])
            messages.success(request, f"Cleared planned shift changes for {len(profiles)} employee(s).")
            return redirect("employee_shift_planner")

        start_date = self._parse_start_date(request)
        if not start_date:
            return redirect("employee_shift_planner")

        if action == "assign":
            new_shift = (request.POST.get("new_shift") or "").strip().lower()
            if new_shift not in valid_shifts:
                messages.error(request, "Choose a valid target shift.")
                return redirect("employee_shift_planner")

            for profile in profiles:
                self._log_change(db_alias, company, request.user, profile, "assign", new_shift, start_date)
                profile.planned_shift = new_shift
                profile.planned_shift_start_date = start_date
                profile.save(using=db_alias, update_fields=["planned_shift", "planned_shift_start_date"])
            messages.success(request, f"Planned {len(profiles)} employee(s) for {new_shift.title()} from {start_date}.")
            return redirect("employee_shift_planner")

        if action == "swap":
            from_shift = (request.POST.get("from_shift") or "").strip().lower()
            to_shift = (request.POST.get("to_shift") or "").strip().lower()
            if from_shift not in valid_shifts or to_shift not in valid_shifts or from_shift == to_shift:
                messages.error(request, "Choose two different valid shifts to swap.")
                return redirect("employee_shift_planner")

            changed = 0
            skipped = 0
            for profile in profiles:
                active_shift = self._active_shift(profile)
                if active_shift == from_shift:
                    new_shift = to_shift
                elif active_shift == to_shift:
                    new_shift = from_shift
                else:
                    skipped += 1
                    continue

                self._log_change(
                    db_alias,
                    company,
                    request.user,
                    profile,
                    "swap",
                    new_shift,
                    start_date,
                    note=f"Swapped {from_shift} with {to_shift}",
                )
                profile.planned_shift = new_shift
                profile.planned_shift_start_date = start_date
                profile.save(using=db_alias, update_fields=["planned_shift", "planned_shift_start_date"])
                changed += 1

            if changed:
                messages.success(request, f"Planned shift swap for {changed} employee(s) from {start_date}.")
            if skipped:
                messages.warning(request, f"Skipped {skipped} employee(s) outside the selected shift pair.")
            return redirect("employee_shift_planner")

        messages.error(request, "Choose a bulk shift action.")
        return redirect("employee_shift_planner")
