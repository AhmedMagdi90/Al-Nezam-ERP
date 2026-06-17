import csv
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

from django.apps import apps
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Prefetch, Q, Subquery, Sum
from django.http import HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views import View

from accounts.models import Profile
from manufacturing.models import (
    BOMComponent,
    BOMOperation,
    Machine,
    MaterialUsage,
    ProductionLog,
    WorkOrder,
    WorkOrderChangeLog,
)
from manufacturing.audit_formatting import audit_summary_text, readable_audit_event
from manufacturing.services import WorkOrderService

from .dashboard import require_company, user_has_role


class ActualVsPlannedReportMixin:
    """Shared query + aggregation logic for the simplified reports screen."""

    allowed_access = "ui.reports.view"
    allowed_report_roles = ("admin", "planner")
    _time_spent_field = next(
        (
            field_name
            for field_name in ("time_spent_minutes", "time_spent")
            if any(field.name == field_name for field in ProductionLog._meta.fields)
        ),
        None,
    )
    _material_qty_field = "quantity_used" if any(
        field.name == "quantity_used" for field in MaterialUsage._meta.fields
    ) else None

    def _to_decimal(self, value, default=Decimal("0")):
        if value is None:
            return default
        try:
            return Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError):
            return default

    def _has_report_access(self, user):
        return user_has_role(user, self.allowed_report_roles)

    def _append_warning(self, filters, warning_text):
        warnings = filters.setdefault("warnings", [])
        if warning_text not in warnings:
            warnings.append(warning_text)

    def _to_2dp_float(self, value):
        return float(self._to_decimal(value).quantize(Decimal("0.01")))

    def _average_pct(self, values):
        valid_values = [self._to_decimal(value) for value in values if value is not None]
        if not valid_values:
            return None
        return self._to_2dp_float(sum(valid_values) / Decimal(len(valid_values)))

    def _inverse_efficiency_pct(self, planned_value, actual_value):
        planned = self._to_decimal(planned_value, None)
        actual = self._to_decimal(actual_value, None)
        if planned is None or actual is None:
            return None
        if planned <= 0 and actual <= 0:
            return 100.0
        if planned <= 0 or actual <= 0:
            return None
        return self._to_2dp_float((planned / actual) * Decimal("100"))

    def _direct_efficiency_pct(self, planned_value, actual_value):
        planned = self._to_decimal(planned_value, None)
        actual = self._to_decimal(actual_value, None)
        if planned is None or actual is None:
            return None
        if planned <= 0 and actual <= 0:
            return 100.0
        if planned <= 0:
            return None
        return self._to_2dp_float((actual / planned) * Decimal("100"))

    def _metric_tone(self, value):
        if value is None:
            return "slate"
        if value >= 97:
            return "emerald"
        if value >= 90:
            return "amber"
        return "rose"

    def _extract_filters(self, request):
        date_str = (request.GET.get("date") or "").strip()
        default_date = timezone.localdate()
        selected_date = parse_date(date_str) if date_str else default_date
        date_invalid = bool(date_str and not selected_date)
        if not selected_date:
            selected_date = default_date

        product_id = request.GET.get("product")
        work_order_id = request.GET.get("work_order")

        try:
            product_id = int(product_id) if product_id else None
        except (TypeError, ValueError):
            product_id = None

        try:
            work_order_id = int(work_order_id) if work_order_id else None
        except (TypeError, ValueError):
            work_order_id = None

        return {
            "selected_date": selected_date,
            "selected_product_id": product_id,
            "selected_work_order_id": work_order_id,
            "date_invalid": date_invalid,
            "warnings": [],
        }

    def _validate_filters(self, company, filters):
        if filters.get("date_invalid"):
            self._append_warning(filters, "Invalid date value was reset to today.")

        product_id = filters["selected_product_id"]
        if product_id and not WorkOrder.objects.filter(
            company=company,
            parent__isnull=True,
            bom__product_id=product_id,
        ).exists():
            filters["selected_product_id"] = None
            self._append_warning(filters, "Selected product is not available for this company.")

        work_order_id = filters["selected_work_order_id"]
        if not work_order_id:
            return filters

        selected_work_order = (
            WorkOrder.objects.filter(company=company, parent__isnull=True, id=work_order_id)
            .select_related("bom__product")
            .only("id", "product_name", "bom_id", "bom__product_id")
            .first()
        )
        if not selected_work_order:
            filters["selected_work_order_id"] = None
            self._append_warning(filters, "Selected work order is invalid for this company.")
            return filters

        selected_product_id = filters["selected_product_id"]
        if selected_product_id and selected_work_order.bom and (
            selected_work_order.bom.product_id != selected_product_id
        ):
            filters["selected_product_id"] = selected_work_order.bom.product_id
            self._append_warning(
                filters,
                "Product filter was auto-adjusted to match the selected work order."
            )
        elif selected_product_id and not selected_work_order.bom:
            filters["selected_product_id"] = None
            self._append_warning(
                filters,
                "Product filter was removed because the selected work order has no linked BOM product."
            )

        return filters

    def _work_orders_queryset(self, company, filters):
        selected_date = filters["selected_date"]
        product_id = filters["selected_product_id"]
        work_order_id = filters["selected_work_order_id"]

        qs = (
            WorkOrder.objects.filter(company=company, parent__isnull=True)
            .select_related("bom", "bom__product")
            .prefetch_related(
                Prefetch(
                    "bom__components",
                    queryset=BOMComponent.objects.select_related("product").only(
                        "id",
                        "bom_id",
                        "product_id",
                        "product__name",
                        "material_name",
                        "quantity",
                        "unit",
                    ),
                ),
                Prefetch(
                    "bom__operations",
                    queryset=BOMOperation.objects.select_related("machine", "stage").only(
                        "id",
                        "bom_id",
                        "machine_id",
                        "machine__name",
                        "stage_id",
                        "stage__name",
                        "order",
                        "setup_time",
                        "run_time",
                        "duration_minutes",
                        "machine_type",
                    ),
                ),
            )
            .only(
                "id",
                "quantity",
                "product_name",
                "bom_id",
                "bom__id",
                "bom__base_quantity",
                "bom__uom",
                "bom__product__name",
            )
        )

        if selected_date:
            logged_pairs = ProductionLog.objects.filter(
                work_order__company=company,
                date=selected_date,
            ).values_list("work_order_id", "work_order__parent_id")
            logged_root_ids = {
                parent_id or work_order_id
                for work_order_id, parent_id in logged_pairs
            }
            qs = qs.filter(
                Q(start_date__date=selected_date) | Q(id__in=logged_root_ids)
            )

        if product_id:
            qs = qs.filter(bom__product_id=product_id)

        if work_order_id:
            qs = qs.filter(id=work_order_id)

        return qs

    def _filter_options(self, company):
        product_rows = (
            WorkOrder.objects.filter(
                company=company,
                parent__isnull=True,
                bom__product__isnull=False,
            )
            .values("bom__product_id", "bom__product__name")
            .distinct()
            .order_by("bom__product__name")
        )
        products = [
            {"id": row["bom__product_id"], "name": row["bom__product__name"]}
            for row in product_rows
            if row.get("bom__product_id")
        ]

        work_order_rows = (
            WorkOrder.objects.filter(company=company, parent__isnull=True)
            .values("id", "product_name", "bom__product__name")
            .order_by("-id")[:500]
        )
        wo_options = []
        for row in work_order_rows:
            product_label = row.get("bom__product__name") or row.get("product_name") or f"WO-{row['id']}"
            wo_options.append({"id": row["id"], "label": f"WO-{row['id']} | {product_label}"})

        return products, wo_options

    def _task_tree_for_work_orders(self, company, work_orders):
        root_ids = [wo.id for wo in work_orders]
        if not root_ids:
            return {}, {}

        task_qs = (
            WorkOrder.objects.filter(company=company)
            .filter(Q(id__in=root_ids) | Q(parent_id__in=root_ids))
            .select_related("parent", "stage", "machine")
            .only("id", "parent_id", "stage_id", "machine_id", "quantity", "product_name", "status")
        )

        tasks_by_root = defaultdict(list)
        task_by_id = {}
        for task in task_qs:
            root_id = task.parent_id or task.id
            tasks_by_root[root_id].append(task)
            task_by_id[task.id] = task

        for root_id, tasks in tasks_by_root.items():
            tasks.sort(
                key=lambda item: (
                    0 if not item.parent_id else 1,
                    item.stage_id or 0,
                    item.machine_id or 0,
                    item.id,
                )
            )
            tasks_by_root[root_id] = tasks

        return dict(tasks_by_root), task_by_id

    def _log_rollups(self, company, task_by_id, filters):
        task_ids = list(task_by_id.keys())
        selected_date = filters["selected_date"]
        has_time_spent = bool(self._time_spent_field)
        has_material_qty = bool(self._material_qty_field)

        if not has_time_spent:
            self._append_warning(
                filters,
                "Actual time is not configured on production logs yet. "
                "Add a per-log time field (minutes) to enable trusted time variance.",
            )
        if not has_material_qty:
            self._append_warning(
                filters,
                "Actual material consumption field is missing. Material variance cannot be calculated.",
            )

        rollups = {
            "has_time_spent": has_time_spent,
            "has_material_qty": has_material_qty,
            "actual_minutes_by_task": defaultdict(Decimal),
            "actual_minutes_by_root": defaultdict(Decimal),
            "actual_qty_by_task": defaultdict(Decimal),
            "actual_qty_by_root": defaultdict(Decimal),
            "log_count_by_root": defaultdict(int),
            "actual_material_by_task": defaultdict(Decimal),
            "actual_material_by_root": defaultdict(Decimal),
            "material_record_count_by_root": defaultdict(int),
            "material_items_by_root": defaultdict(dict),
        }

        if not task_ids:
            return rollups

        logs_qs = ProductionLog.objects.filter(
            work_order__company=company,
            work_order_id__in=task_ids,
        ).select_related("work_order")
        if selected_date:
            logs_qs = logs_qs.filter(date=selected_date)

        for log in logs_qs:
            task = task_by_id.get(log.work_order_id)
            if not task:
                continue

            root_id = task.parent_id or task.id
            quantity = self._to_decimal(log.quantity)
            rollups["actual_qty_by_task"][task.id] += quantity
            rollups["actual_qty_by_root"][root_id] += quantity
            rollups["log_count_by_root"][root_id] += 1

            if has_time_spent:
                spent_minutes = self._to_decimal(getattr(log, self._time_spent_field, None))
                rollups["actual_minutes_by_task"][task.id] += spent_minutes
                rollups["actual_minutes_by_root"][root_id] += spent_minutes

        if not has_material_qty:
            return rollups

        material_qs = MaterialUsage.objects.filter(
            production_log__work_order_id__in=task_ids,
        ).select_related("production_log", "product")
        if selected_date:
            material_qs = material_qs.filter(production_log__date=selected_date)

        for usage in material_qs:
            task = task_by_id.get(usage.production_log.work_order_id)
            if not task:
                continue

            root_id = task.parent_id or task.id
            used_qty = self._to_decimal(getattr(usage, self._material_qty_field, None))
            rollups["actual_material_by_task"][task.id] += used_qty
            rollups["actual_material_by_root"][root_id] += used_qty
            rollups["material_record_count_by_root"][root_id] += 1

            material_key = (
                f"product:{usage.product_id}"
                if usage.product_id
                else f"name:{(usage.material_name or '').strip().lower()}::{usage.unit or ''}"
            )
            material_entry = rollups["material_items_by_root"][root_id].setdefault(
                material_key,
                {
                    "label": usage.product.name if usage.product_id else (usage.material_name or "-"),
                    "unit": usage.unit or "",
                    "actual_qty": Decimal("0"),
                },
            )
            material_entry["actual_qty"] += used_qty

        return rollups

    def _resolve_output_metrics(self, work_order, tasks_by_root, actual_qty_by_task):
        stage_tasks = [task for task in tasks_by_root.get(work_order.id, []) if task.parent_id]
        if not stage_tasks:
            actual_qty = actual_qty_by_task.get(work_order.id, Decimal("0"))
            return actual_qty, work_order.id in actual_qty_by_task

        order_by_stage_id = {}
        if work_order.bom:
            for operation in work_order.bom.operations.all():
                if operation.stage_id:
                    order_by_stage_id[operation.stage_id] = max(
                        order_by_stage_id.get(operation.stage_id, 0),
                        operation.order,
                    )

        ranked_tasks = []
        for task in stage_tasks:
            stage_order = order_by_stage_id.get(task.stage_id, 0)
            ranked_tasks.append((stage_order, task.id, task))

        if not ranked_tasks:
            return Decimal("0"), False

        max_stage_order = max(item[0] for item in ranked_tasks)
        if max_stage_order > 0:
            final_tasks = [task for order, _task_id, task in ranked_tasks if order == max_stage_order]
        else:
            final_tasks = [max(ranked_tasks, key=lambda item: (item[0], item[1]))[2]]

        actual_qty = sum((actual_qty_by_task.get(task.id, Decimal("0")) for task in final_tasks), Decimal("0"))
        actual_available = any(task.id in actual_qty_by_task for task in final_tasks)
        return actual_qty, actual_available

    def _build_actual_vs_planned_summary(self, rows, rollups):
        time_values = [row["time_efficiency_pct"] for row in rows if row["time_efficiency_pct"] is not None]
        material_values = [row["material_efficiency_pct"] for row in rows if row["material_efficiency_pct"] is not None]
        production_values = [row["production_efficiency_pct"] for row in rows if row["production_efficiency_pct"] is not None]
        overall_values = [row["overall_efficiency_pct"] for row in rows if row["overall_efficiency_pct"] is not None]

        worst_time_row = None
        time_deltas = [row for row in rows if row["time_delta_hours"] is not None and row["time_delta_hours"] > 0]
        if time_deltas:
            worst_time_row = max(time_deltas, key=lambda row: row["time_delta_hours"])

        worst_material_row = None
        material_deltas = [row for row in rows if row["material_delta_qty"] > 0]
        if material_deltas:
            worst_material_row = max(material_deltas, key=lambda row: row["material_delta_qty"])

        on_plan_count = sum(
            1
            for row in rows
            if (row["time_within_plan"] in (True, None)) and row["material_within_plan"]
        )

        time_avg = self._average_pct(time_values)
        material_avg = self._average_pct(material_values)
        production_avg = self._average_pct(production_values)
        overall_avg = self._average_pct(overall_values)
        return {
            "work_order_count": len(rows),
            "time_efficiency_pct": time_avg,
            "material_efficiency_pct": material_avg,
            "production_efficiency_pct": production_avg,
            "overall_efficiency_pct": overall_avg,
            "time_tone": self._metric_tone(time_avg),
            "material_tone": self._metric_tone(material_avg),
            "production_tone": self._metric_tone(production_avg),
            "overall_tone": self._metric_tone(overall_avg),
            "worst_time_row": worst_time_row,
            "worst_material_row": worst_material_row,
            "on_plan_count": on_plan_count,
            "has_time_spent": rollups["has_time_spent"],
            "has_material_qty": rollups["has_material_qty"],
        }

    def _actual_vs_planned_rows(self, company, filters):
        return self._actual_vs_planned_data(company, filters)["rows"]

    def _actual_vs_planned_data(self, company, filters):
        work_orders = list(self._work_orders_queryset(company, filters).order_by("-id"))
        if not work_orders:
            empty_rollups = {
                "has_time_spent": bool(self._time_spent_field),
                "has_material_qty": bool(self._material_qty_field),
                "actual_minutes_by_task": defaultdict(Decimal),
                "actual_minutes_by_root": defaultdict(Decimal),
                "actual_qty_by_task": defaultdict(Decimal),
                "actual_qty_by_root": defaultdict(Decimal),
                "log_count_by_root": defaultdict(int),
                "actual_material_by_task": defaultdict(Decimal),
                "actual_material_by_root": defaultdict(Decimal),
                "material_record_count_by_root": defaultdict(int),
                "material_items_by_root": defaultdict(dict),
            }
            return {
                "rows": [],
                "summary": self._build_actual_vs_planned_summary([], empty_rollups),
                "work_orders": [],
                "work_order_map": {},
                "tasks_by_root": {},
                "rollups": empty_rollups,
            }

        tasks_by_root, task_by_id = self._task_tree_for_work_orders(company, work_orders)
        rollups = self._log_rollups(company, task_by_id, filters)

        rows = []
        for work_order in work_orders:
            planned_material = Decimal("0")
            planned_minutes = Decimal("0")

            if work_order.bom:
                base_qty = self._to_decimal(work_order.bom.base_quantity, Decimal("1"))
                if base_qty <= 0:
                    base_qty = Decimal("1")
                scale = self._to_decimal(work_order.quantity, Decimal("0")) / base_qty

                for component in work_order.bom.components.all():
                    planned_material += self._to_decimal(component.quantity) * scale

                for operation in work_order.bom.operations.all():
                    planned_minutes += self._to_decimal(
                        WorkOrderService._compute_operation_duration(operation, work_order.quantity)
                    )

            actual_minutes = (
                rollups["actual_minutes_by_root"].get(work_order.id)
                if rollups["has_time_spent"]
                else None
            )
            planned_hours = self._to_2dp_float(planned_minutes / Decimal("60"))
            actual_hours = (
                self._to_2dp_float(actual_minutes / Decimal("60"))
                if actual_minutes is not None
                else None
            )
            actual_time_available = actual_hours is not None and (work_order.id in rollups["actual_minutes_by_root"])

            planned_material_qty = self._to_2dp_float(planned_material)
            actual_material_qty = self._to_2dp_float(
                rollups["actual_material_by_root"].get(work_order.id, Decimal("0"))
            )
            actual_material_available = bool(rollups["material_record_count_by_root"].get(work_order.id))

            actual_output_qty, actual_output_available = self._resolve_output_metrics(
                work_order,
                tasks_by_root,
                rollups["actual_qty_by_task"],
            )
            actual_output_qty_float = (
                self._to_2dp_float(actual_output_qty) if actual_output_available else None
            )

            time_delta = (
                self._to_2dp_float(self._to_decimal(actual_hours) - self._to_decimal(planned_hours))
                if actual_hours is not None
                else None
            )
            material_delta = self._to_2dp_float(
                self._to_decimal(actual_material_qty) - self._to_decimal(planned_material_qty)
            )
            production_delta = (
                self._to_2dp_float(actual_output_qty - self._to_decimal(work_order.quantity))
                if actual_output_available
                else None
            )

            time_efficiency_pct = (
                self._inverse_efficiency_pct(planned_hours, actual_hours)
                if actual_time_available
                else None
            )
            material_efficiency_pct = (
                self._inverse_efficiency_pct(planned_material_qty, actual_material_qty)
                if actual_material_available
                else None
            )
            production_efficiency_pct = (
                self._direct_efficiency_pct(work_order.quantity, actual_output_qty_float)
                if actual_output_available
                else None
            )
            overall_efficiency_pct = self._average_pct(
                [time_efficiency_pct, material_efficiency_pct, production_efficiency_pct]
            )

            product_name = (
                work_order.bom.product.name
                if work_order.bom and work_order.bom.product
                else (work_order.product_name or "-")
            )

            rows.append(
                {
                    "wo_id": work_order.id,
                    "product_name": product_name,
                    "order_qty": self._to_2dp_float(self._to_decimal(work_order.quantity)),
                    "planned_time_hours": planned_hours,
                    "actual_time_hours": actual_hours,
                    "time_delta_hours": time_delta,
                    "planned_material_qty": planned_material_qty,
                    "actual_material_qty": actual_material_qty,
                    "material_delta_qty": material_delta,
                    "actual_output_qty": actual_output_qty_float,
                    "production_delta_qty": production_delta,
                    "time_within_plan": (time_delta <= 0) if time_delta is not None else None,
                    "material_within_plan": material_delta <= 0,
                    "actual_time_available": actual_time_available,
                    "actual_material_available": actual_material_available,
                    "actual_output_available": actual_output_available,
                    "time_efficiency_pct": time_efficiency_pct,
                    "material_efficiency_pct": material_efficiency_pct,
                    "production_efficiency_pct": production_efficiency_pct,
                    "overall_efficiency_pct": overall_efficiency_pct,
                    "time_tone": self._metric_tone(time_efficiency_pct),
                    "material_tone": self._metric_tone(material_efficiency_pct),
                    "production_tone": self._metric_tone(production_efficiency_pct),
                    "overall_tone": self._metric_tone(overall_efficiency_pct),
                    "data_trust": (
                        "full"
                        if actual_time_available and actual_material_available
                        else ("material_only" if actual_material_available else "limited")
                    ),
                }
            )

        return {
            "rows": rows,
            "summary": self._build_actual_vs_planned_summary(rows, rollups),
            "work_orders": work_orders,
            "work_order_map": {work_order.id: work_order for work_order in work_orders},
            "tasks_by_root": tasks_by_root,
            "rollups": rollups,
        }

    def _build_work_order_sheet(self, report_data, root_work_order_id):
        work_order = report_data["work_order_map"].get(root_work_order_id)
        if not work_order:
            return None

        tasks = report_data["tasks_by_root"].get(root_work_order_id, [work_order])
        child_tasks = [task for task in tasks if task.parent_id]
        rollups = report_data["rollups"]

        time_rows = []
        time_warning = None
        if work_order.bom and work_order.bom.operations.exists():
            for operation in work_order.bom.operations.all():
                matched_tasks = []
                if child_tasks:
                    if operation.stage_id:
                        matched_tasks = [task for task in child_tasks if task.stage_id == operation.stage_id]
                    elif operation.machine_id:
                        matched_tasks = [task for task in child_tasks if task.machine_id == operation.machine_id]
                else:
                    matched_tasks = [work_order]

                planned_minutes = self._to_decimal(
                    WorkOrderService._compute_operation_duration(operation, work_order.quantity)
                )
                actual_minutes = None
                actual_available = False
                if rollups["has_time_spent"]:
                    actual_minutes = sum(
                        (rollups["actual_minutes_by_task"].get(task.id, Decimal("0")) for task in matched_tasks),
                        Decimal("0"),
                    )
                    actual_available = any(task.id in rollups["actual_minutes_by_task"] for task in matched_tasks)

                planned_hours = self._to_2dp_float(planned_minutes / Decimal("60"))
                actual_hours = (
                    self._to_2dp_float(actual_minutes / Decimal("60"))
                    if actual_available and actual_minutes is not None
                    else None
                )
                time_delta = (
                    self._to_2dp_float(self._to_decimal(actual_hours) - self._to_decimal(planned_hours))
                    if actual_hours is not None
                    else None
                )
                efficiency_pct = (
                    self._inverse_efficiency_pct(planned_hours, actual_hours)
                    if actual_hours is not None
                    else None
                )
                resource_name = (
                    operation.machine.display_label
                    if operation.machine_id and operation.machine
                    else (
                        operation.stage.name
                        if operation.stage_id and operation.stage
                        else (operation.machine_type or f"Operation {operation.order}")
                    )
                )

                time_rows.append(
                    {
                        "label": resource_name,
                        "planned": planned_hours,
                        "actual": actual_hours,
                        "unit": "hr",
                        "delta": time_delta,
                        "efficiency_pct": efficiency_pct,
                        "tone": self._metric_tone(efficiency_pct),
                    }
                )

        if not rollups["has_time_spent"]:
            time_warning = (
                "Actual time is not configured on production logs yet. "
                "Enable per-log minutes to unlock trusted time variance."
            )

        base_qty = Decimal("1")
        if work_order.bom:
            base_qty = self._to_decimal(work_order.bom.base_quantity, Decimal("1"))
            if base_qty <= 0:
                base_qty = Decimal("1")
        scale = self._to_decimal(work_order.quantity, Decimal("0")) / base_qty

        planned_material_map = {}
        if work_order.bom:
            for component in work_order.bom.components.all():
                component_key = (
                    f"product:{component.product_id}"
                    if component.product_id
                    else f"name:{(component.material_name or '').strip().lower()}::{component.unit or ''}"
                )
                planned_entry = planned_material_map.setdefault(
                    component_key,
                    {
                        "label": component.product.name if component.product_id else component.material_name,
                        "unit": component.unit or "",
                        "planned_qty": Decimal("0"),
                    },
                )
                planned_entry["planned_qty"] += self._to_decimal(component.quantity) * scale

        actual_material_map = report_data["rollups"]["material_items_by_root"].get(root_work_order_id, {})
        material_keys = list(planned_material_map.keys())
        for key in actual_material_map.keys():
            if key not in material_keys:
                material_keys.append(key)

        material_rows = []
        actual_material_available = bool(report_data["rollups"]["material_record_count_by_root"].get(root_work_order_id))
        for key in material_keys:
            planned_entry = planned_material_map.get(key, {})
            actual_entry = actual_material_map.get(key, {})
            planned_qty = self._to_decimal(planned_entry.get("planned_qty"), Decimal("0"))
            actual_qty = self._to_decimal(actual_entry.get("actual_qty"), Decimal("0"))
            efficiency_pct = (
                self._inverse_efficiency_pct(self._to_2dp_float(planned_qty), self._to_2dp_float(actual_qty))
                if actual_material_available
                else None
            )
            material_rows.append(
                {
                    "label": planned_entry.get("label") or actual_entry.get("label") or "-",
                    "planned": self._to_2dp_float(planned_qty),
                    "actual": self._to_2dp_float(actual_qty) if actual_material_available else None,
                    "unit": planned_entry.get("unit") or actual_entry.get("unit") or "",
                    "delta": self._to_2dp_float(actual_qty - planned_qty) if actual_material_available else None,
                    "efficiency_pct": efficiency_pct,
                    "tone": self._metric_tone(efficiency_pct),
                }
            )

        actual_output_qty, actual_output_available = self._resolve_output_metrics(
            work_order,
            report_data["tasks_by_root"],
            report_data["rollups"]["actual_qty_by_task"],
        )
        production_actual_qty = self._to_2dp_float(actual_output_qty) if actual_output_available else None
        production_efficiency_pct = (
            self._direct_efficiency_pct(work_order.quantity, production_actual_qty)
            if actual_output_available
            else None
        )
        production_row = {
            "label": (
                work_order.bom.product.name
                if work_order.bom and work_order.bom.product
                else (work_order.product_name or "-")
            ),
            "planned": work_order.quantity,
            "actual": production_actual_qty,
            "unit": work_order.bom.uom if work_order.bom else "pcs",
            "delta": (
                self._to_2dp_float(actual_output_qty - self._to_decimal(work_order.quantity))
                if actual_output_available
                else None
            ),
            "efficiency_pct": production_efficiency_pct,
            "tone": self._metric_tone(production_efficiency_pct),
        }

        summary_row = next((row for row in report_data["rows"] if row["wo_id"] == root_work_order_id), None)
        return {
            "work_order": work_order,
            "summary_row": summary_row,
            "time_rows": time_rows,
            "time_warning": time_warning,
            "material_rows": material_rows,
            "production_row": production_row,
        }


class ReportsDashboardView(LoginRequiredMixin, ActualVsPlannedReportMixin, View):
    audit_page_size = 200
    audit_presets = {
        "planning": {
            "label": "Planning",
            "events": {
                "work_order_created",
                "work_order_split_created",
                "work_order_split",
                "work_order_scheduled",
                "work_order_route_scheduled",
                "work_order_updated_from_timeline",
                "work_order_start_date_updated",
                "work_order_unscheduled",
                "work_order_closed",
                "work_order_released_to_next_stage",
            },
        },
        "assignments": {
            "label": "Assignments",
            "events": {
                "worker_assigned",
            },
        },
        "production": {
            "label": "Production",
            "events": {
                "production_logged",
                "production_log_updated",
                "production_log_approved",
                "production_log_rejected",
                "shop_floor_status_updated",
            },
        },
        "materials_bom": {
            "label": "Materials / BOM",
            "events": {
                "material_readiness_updated",
                "store_receipt_confirmed",
                "latest_bom_applied",
                "bom_change_archive_new",
                "bom_change_scrap_apply",
                "bom_change_continue_old",
            },
        },
        "machine_setup": {
            "label": "Machine Setup",
            "events": {
                "machine_created",
                "machine_updated",
                "machine_shift_updated",
            },
        },
    }

    def _selected_section(self, request):
        section = (request.GET.get("section") or "actual").strip().lower()
        return section if section in {"actual", "audit", "bi"} else "actual"

    def _pdf_escape(self, value):
        text = str(value or "")
        return (
            text.replace("\\", "\\\\")
            .replace("(", "\\(")
            .replace(")", "\\)")
            .replace("\r", " ")
            .replace("\n", " ")
        )

    def _simple_pdf_response(self, filename, title, lines):
        content_lines = [
            "BT",
            "/F1 16 Tf",
            "50 790 Td",
            f"({self._pdf_escape(title)}) Tj",
            "/F1 10 Tf",
            "0 -24 Td",
            "14 TL",
        ]
        for line in lines[:46]:
            content_lines.append(f"({self._pdf_escape(line)}) Tj")
            content_lines.append("T*")
        content_lines.append("ET")
        stream = "\n".join(content_lines).encode("latin-1", "replace")

        objects = [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
            b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n" + stream + b"\nendstream",
        ]

        pdf = bytearray(b"%PDF-1.4\n")
        offsets = []
        for index, obj in enumerate(objects, start=1):
            offsets.append(len(pdf))
            pdf.extend(f"{index} 0 obj\n".encode("ascii"))
            pdf.extend(obj)
            pdf.extend(b"\nendobj\n")
        xref_offset = len(pdf)
        pdf.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
        pdf.extend(b"0000000000 65535 f \n")
        for offset in offsets:
            pdf.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
        pdf.extend(
            (
                f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
                f"startxref\n{xref_offset}\n%%EOF"
            ).encode("ascii")
        )

        response = HttpResponse(bytes(pdf), content_type="application/pdf")
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        return response

    def _bi_dashboard_data(self, company):
        now = timezone.now()
        root_qs = (
            WorkOrder.objects.filter(company=company, parent__isnull=True)
            .select_related("bom__product")
            .order_by("-id")
        )
        work_orders = list(root_qs)
        total_count = len(work_orders)

        status_counts = defaultdict(int)
        late_count = 0
        on_time_count = 0
        cycle_hours = []
        product_counts = defaultdict(int)

        for work_order in work_orders:
            status_counts[work_order.status] += 1
            product_label = (
                work_order.bom.product.name
                if work_order.bom and work_order.bom.product
                else (work_order.product_name or "Unspecified")
            )
            product_counts[product_label] += 1

            is_complete = work_order.status == "completed" or work_order.closed_by_planner
            if work_order.due_date:
                if is_complete and work_order.end_date:
                    if work_order.end_date <= work_order.due_date:
                        on_time_count += 1
                    else:
                        late_count += 1
                elif not is_complete and work_order.due_date < now:
                    late_count += 1

            if work_order.start_date and work_order.end_date:
                elapsed = work_order.end_date - work_order.start_date
                if elapsed.total_seconds() > 0:
                    cycle_hours.append(elapsed.total_seconds() / 3600)

        logs_qs = ProductionLog.objects.filter(work_order__company=company)
        approved_qty = logs_qs.filter(status="approved").aggregate(total=Sum("quantity")).get("total") or 0
        pending_approval_count = logs_qs.filter(status="pending").count()
        audit_count = apps.get_model("manufacturing", "AuditLog").objects.filter(company=company).count()
        change_count = WorkOrderChangeLog.objects.filter(work_order__company=company).count()

        recent_activity = list(
            apps.get_model("manufacturing", "AuditLog")
            .objects.filter(company=company)
            .select_related("user")
            .order_by("-timestamp")[:6]
        )

        top_products = sorted(product_counts.items(), key=lambda item: (-item[1], item[0]))[:5]
        avg_cycle_hours = sum(cycle_hours) / len(cycle_hours) if cycle_hours else None
        closed_count = sum(1 for wo in work_orders if wo.closed_by_planner)
        material_ready_count = sum(1 for wo in work_orders if wo.material_readiness_status == "ready")
        material_shortage_count = sum(1 for wo in work_orders if wo.material_readiness_status == "shortage")
        completed_count = status_counts["completed"]
        open_count = sum(
            1
            for wo in work_orders
            if wo.status in {"pending", "in_progress", "hold"} and not wo.closed_by_planner
        )
        adherence_pct = round((on_time_count / (on_time_count + late_count)) * 100) if (on_time_count + late_count) else None

        return {
            "total_work_orders": total_count,
            "open_work_orders": open_count,
            "completed_work_orders": completed_count,
            "closed_work_orders": closed_count,
            "late_work_orders": late_count,
            "on_time_work_orders": on_time_count,
            "schedule_adherence_pct": adherence_pct,
            "pending_approval_count": pending_approval_count,
            "material_ready_count": material_ready_count,
            "material_shortage_count": material_shortage_count,
            "approved_output_qty": approved_qty,
            "avg_cycle_hours": round(avg_cycle_hours, 1) if avg_cycle_hours is not None else None,
            "audit_count": audit_count,
            "change_count": change_count,
            "status_rows": [
                {"status": status, "count": count}
                for status, count in sorted(status_counts.items(), key=lambda item: item[0])
            ],
            "top_products": [
                {"label": label, "count": count}
                for label, count in top_products
            ],
            "recent_activity": recent_activity,
        }

    def _extract_audit_filters(self, request):
        date_from_str = (request.GET.get("audit_from") or "").strip()
        date_to_str = (request.GET.get("audit_to") or "").strip()
        selected_user_id = request.GET.get("audit_user")
        selected_action = (request.GET.get("audit_action") or "").strip().lower() or None
        selected_model = (request.GET.get("audit_model") or "").strip()
        search_term = (request.GET.get("audit_search") or "").strip()
        selected_work_order_id = request.GET.get("audit_work_order")
        selected_machine_id = request.GET.get("audit_machine")
        selected_preset = (request.GET.get("audit_preset") or "").strip().lower() or None

        try:
            selected_user_id = int(selected_user_id) if selected_user_id else None
        except (TypeError, ValueError):
            selected_user_id = None

        try:
            selected_work_order_id = int(selected_work_order_id) if selected_work_order_id else None
        except (TypeError, ValueError):
            selected_work_order_id = None

        try:
            selected_machine_id = int(selected_machine_id) if selected_machine_id else None
        except (TypeError, ValueError):
            selected_machine_id = None

        return {
            "date_from": parse_date(date_from_str) if date_from_str else None,
            "date_to": parse_date(date_to_str) if date_to_str else None,
            "date_from_invalid": bool(date_from_str and not parse_date(date_from_str)),
            "date_to_invalid": bool(date_to_str and not parse_date(date_to_str)),
            "selected_user_id": selected_user_id,
            "selected_action": selected_action,
            "selected_model": selected_model or None,
            "search_term": search_term,
            "selected_work_order_id": selected_work_order_id,
            "selected_machine_id": selected_machine_id,
            "selected_preset": selected_preset,
            "warnings": [],
        }

    def _validate_audit_filters(self, company, filters):
        AuditLog = apps.get_model("manufacturing", "AuditLog")

        if filters.get("date_from_invalid"):
            self._append_warning(filters, "Invalid audit start date was ignored.")
        if filters.get("date_to_invalid"):
            self._append_warning(filters, "Invalid audit end date was ignored.")

        if filters["date_from"] and filters["date_to"] and filters["date_from"] > filters["date_to"]:
            filters["date_from"], filters["date_to"] = filters["date_to"], filters["date_from"]
            self._append_warning(filters, "Audit date range was reversed automatically.")

        selected_user_id = filters["selected_user_id"]
        if selected_user_id and not Profile.objects.filter(
            company=company,
            user_id=selected_user_id,
        ).exists():
            filters["selected_user_id"] = None
            self._append_warning(filters, "Selected user is not available for this company.")

        valid_actions = {choice[0] for choice in AuditLog.ACTION_CHOICES}
        if filters["selected_action"] and filters["selected_action"] not in valid_actions:
            filters["selected_action"] = None
            self._append_warning(filters, "Selected audit action is invalid.")

        selected_model = filters["selected_model"]
        if selected_model and not AuditLog.objects.filter(
            company=company,
            model_name=selected_model,
        ).exists():
            filters["selected_model"] = None
            self._append_warning(filters, "Selected target model is not available for this company.")

        selected_work_order_id = filters["selected_work_order_id"]
        if selected_work_order_id and not WorkOrder.objects.filter(
            company=company,
            id=selected_work_order_id,
        ).exists():
            filters["selected_work_order_id"] = None
            self._append_warning(filters, "Selected work order scope is invalid for this company.")

        selected_machine_id = filters["selected_machine_id"]
        if selected_machine_id and not Machine.objects.filter(
            company=company,
            id=selected_machine_id,
        ).exists():
            filters["selected_machine_id"] = None
            self._append_warning(filters, "Selected machine scope is invalid for this company.")

        if filters["selected_preset"] and filters["selected_preset"] not in self.audit_presets:
            filters["selected_preset"] = None
            self._append_warning(filters, "Selected audit preset is invalid.")

        return filters

    def _audit_filter_options(self, company):
        AuditLog = apps.get_model("manufacturing", "AuditLog")

        user_options = list(
            Profile.objects.filter(company=company, user_id__isnull=False)
            .select_related("user")
            .order_by("user__username")
            .values("user_id", "user__username", "user__first_name", "user__last_name")
        )
        model_options = list(
            AuditLog.objects.filter(company=company)
            .order_by("model_name")
            .values_list("model_name", flat=True)
            .distinct()
        )
        action_options = [{"value": value, "label": label} for value, label in AuditLog.ACTION_CHOICES]
        work_order_options = [
            {
                "id": row["id"],
                "label": (
                    f"WO-{row['id']} | {row['product_name'] or 'Work Order'}"
                    + (f" | Parent WO-{row['parent_id']}" if row.get("parent_id") else "")
                ),
            }
            for row in WorkOrder.objects.filter(company=company)
            .values("id", "product_name", "parent_id")
            .order_by("-id")[:500]
        ]
        machine_options = [
            {
                "id": row["id"],
                "label": " | ".join(part for part in [row.get("code"), row.get("name")] if part),
            }
            for row in Machine.objects.filter(company=company)
            .values("id", "code", "name")
            .order_by("code", "name")
        ]
        return user_options, model_options, action_options, work_order_options, machine_options

    def _audit_logs_queryset(self, company, filters):
        AuditLog = apps.get_model("manufacturing", "AuditLog")
        qs = AuditLog.objects.filter(company=company).select_related("user")

        if filters["date_from"]:
            qs = qs.filter(timestamp__date__gte=filters["date_from"])
        if filters["date_to"]:
            qs = qs.filter(timestamp__date__lte=filters["date_to"])
        if filters["selected_user_id"]:
            selected_user_id = filters["selected_user_id"]
            qs = qs.filter(
                Q(user_id=selected_user_id)
                | Q(details__worker_id=selected_user_id)
                | Q(details__assigned_worker_id=selected_user_id)
            )
        if filters["selected_action"]:
            qs = qs.filter(action=filters["selected_action"])
        if filters["selected_model"]:
            qs = qs.filter(model_name=filters["selected_model"])
        if filters["selected_preset"]:
            preset_events = self.audit_presets.get(filters["selected_preset"], {}).get("events", set())
            if preset_events:
                qs = qs.filter(details__event__in=sorted(preset_events))
        if filters["selected_work_order_id"]:
            selected_work_order_id = filters["selected_work_order_id"]
            qs = qs.filter(
                Q(model_name="WorkOrder", object_id=selected_work_order_id)
                | Q(details__work_order_id=selected_work_order_id)
                | Q(details__source_work_order_id=selected_work_order_id)
                | Q(details__target_work_order_id=selected_work_order_id)
            )
        if filters["selected_machine_id"]:
            selected_machine_id = filters["selected_machine_id"]
            qs = qs.filter(
                Q(model_name="Machine", object_id=selected_machine_id)
                | Q(details__machine_id=selected_machine_id)
                | Q(details__target_machine_id=selected_machine_id)
            )

        search_term = filters["search_term"]
        if search_term:
            search_q = (
                Q(object_repr__icontains=search_term)
                | Q(model_name__icontains=search_term)
                | Q(details__event__icontains=search_term)
            )
            if search_term.isdigit():
                search_q |= Q(object_id=int(search_term))
            qs = qs.filter(search_q)

        return qs.order_by("-timestamp")

    def _audit_rows(self, company, filters):
        qs = self._audit_logs_queryset(company, filters)
        entries = list(qs[: self.audit_page_size + 1])
        has_more = len(entries) > self.audit_page_size
        entries = entries[: self.audit_page_size]

        rows = []
        for entry in entries:
            details = entry.details or {}
            event_label = readable_audit_event(details.get("event"))

            rows.append(
                {
                    "timestamp": entry.timestamp,
                    "user": entry.user,
                    "action": entry.action,
                    "model_name": entry.model_name,
                    "object_id": entry.object_id,
                    "object_repr": entry.object_repr,
                    "ip_address": entry.ip_address,
                    "details": details,
                    "event_label": event_label,
                    "target_label": (
                        entry.object_repr
                        or (
                            f"{entry.model_name} #{entry.object_id}"
                            if entry.object_id
                            else entry.model_name
                        )
                    ),
                    "summary_text": audit_summary_text(details),
                }
            )

        return rows, has_more

    def get(self, request):
        if not self._has_report_access(request.user):
            return HttpResponseForbidden("Access Denied")

        company = require_company(request.user)
        if not company:
            return HttpResponseForbidden("No company")

        section = self._selected_section(request)

        if section == "bi":
            bi_summary = self._bi_dashboard_data(company)
            if request.GET.get("format") == "json":
                return JsonResponse(
                    {
                        "section": "bi",
                        "summary": {
                            key: value
                            for key, value in bi_summary.items()
                            if key != "recent_activity"
                        },
                        "recent_activity": [
                            {
                                "timestamp": entry.timestamp.isoformat(),
                                "user": getattr(entry.user, "username", None),
                                "action": entry.action,
                                "model_name": entry.model_name,
                                "object_id": entry.object_id,
                                "object_repr": entry.object_repr,
                                "event": (entry.details or {}).get("event"),
                            }
                            for entry in bi_summary["recent_activity"]
                        ],
                    }
                )

            return render(
                request,
                "manufacturing/reports_dashboard.html",
                {
                    "report_section": "bi",
                    "bi_summary": bi_summary,
                },
            )

        if section == "audit":
            filters = self._validate_audit_filters(company, self._extract_audit_filters(request))
            user_options, model_options, action_options, work_order_options, machine_options = self._audit_filter_options(company)
            audit_rows, audit_has_more = self._audit_rows(company, filters)
            scoped_work_order = None
            if filters["selected_work_order_id"]:
                scoped_work_order = (
                    WorkOrder.objects.filter(company=company, id=filters["selected_work_order_id"])
                    .only("id", "parent_id")
                    .first()
                )
            scoped_machine = None
            if filters["selected_machine_id"]:
                scoped_machine = (
                    Machine.objects.filter(company=company, id=filters["selected_machine_id"])
                    .only("id", "name")
                    .first()
                )

            if request.GET.get("format") == "json":
                return JsonResponse(
                    {
                        "section": "audit",
                        "filters": {
                            "date_from": filters["date_from"].isoformat() if filters["date_from"] else None,
                            "date_to": filters["date_to"].isoformat() if filters["date_to"] else None,
                            "user_id": filters["selected_user_id"],
                            "action": filters["selected_action"],
                            "model": filters["selected_model"],
                            "search": filters["search_term"],
                            "work_order_id": filters["selected_work_order_id"],
                            "machine_id": filters["selected_machine_id"],
                            "preset": filters["selected_preset"],
                        },
                        "rows": [
                            {
                                "timestamp": row["timestamp"].isoformat(),
                                "user": getattr(row["user"], "username", None),
                                "action": row["action"],
                                "model_name": row["model_name"],
                                "object_id": row["object_id"],
                                "target_label": row["target_label"],
                                "event_label": row["event_label"],
                                "summary_text": row["summary_text"],
                                "ip_address": row["ip_address"],
                                "details": row["details"],
                            }
                            for row in audit_rows
                        ],
                        "warnings": filters.get("warnings", []),
                        "meta": {
                            "has_more": audit_has_more,
                            "displayed_rows": len(audit_rows),
                        },
                    }
                )

            context = {
                "report_section": "audit",
                "audit_rows": audit_rows,
                "audit_from": filters["date_from"],
                "audit_to": filters["date_to"],
                "audit_user_id": filters["selected_user_id"],
                "audit_action": filters["selected_action"],
                "audit_model": filters["selected_model"],
                "audit_search": filters["search_term"],
                "audit_work_order_id": filters["selected_work_order_id"],
                "audit_machine_id": filters["selected_machine_id"],
                "audit_preset": filters["selected_preset"],
                "audit_user_options": user_options,
                "audit_model_options": model_options,
                "audit_action_options": action_options,
                "audit_preset_options": [
                    {"value": key, "label": value["label"]}
                    for key, value in self.audit_presets.items()
                ],
                "audit_work_order_options": work_order_options,
                "audit_machine_options": machine_options,
                "audit_filter_warnings": filters.get("warnings", []),
                "audit_has_more": audit_has_more,
                "audit_displayed_count": len(audit_rows),
                "audit_scope_work_order": scoped_work_order,
                "audit_scope_machine": scoped_machine,
            }
            return render(request, "manufacturing/reports_dashboard.html", context)

        filters = self._validate_filters(company, self._extract_filters(request))
        if request.GET.get("sheet_error") == "no_work_order":
            self._append_warning(
                filters,
                "No work order matched the current filters. Select a work order or widen the date/product scope before opening the sheet.",
            )
        report_data = self._actual_vs_planned_data(company, filters)
        product_options, work_order_options = self._filter_options(company)

        if request.GET.get("format") == "json":
            return JsonResponse(
                {
                    "filters": {
                        "date": filters["selected_date"].isoformat()
                        if filters["selected_date"]
                        else None,
                        "product_id": filters["selected_product_id"],
                        "work_order_id": filters["selected_work_order_id"],
                    },
                    "rows": report_data["rows"],
                    "summary": report_data["summary"],
                    "warnings": filters.get("warnings", []),
                    "meta": {
                        "actual_time_source": (
                            f"production_log.{self._time_spent_field}"
                            if self._time_spent_field
                            else None
                        ),
                        "actual_material_source": (
                            f"material_usage.{self._material_qty_field}"
                            if self._material_qty_field
                            else None
                        ),
                    },
                }
            )

        context = {
            "report_section": "actual",
            "selected_date": filters["selected_date"],
            "selected_product_id": filters["selected_product_id"],
            "selected_work_order_id": filters["selected_work_order_id"],
            "product_options": product_options,
            "work_order_options": work_order_options,
            "report_rows": report_data["rows"],
            "report_summary": report_data["summary"],
            "filter_warnings": filters.get("warnings", []),
        }
        return render(request, "manufacturing/reports_dashboard.html", context)


class ExportProductionCSVView(LoginRequiredMixin, ActualVsPlannedReportMixin, View):
    def get(self, request):
        if not self._has_report_access(request.user):
            return HttpResponseForbidden("Access Denied")

        company = require_company(request.user)
        if not company:
            return HttpResponseForbidden("No company")

        filters = self._validate_filters(company, self._extract_filters(request))
        report_data = self._actual_vs_planned_data(company, filters)
        rows = report_data["rows"]

        selected_date = filters["selected_date"]
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = (
            f'attachment; filename="actual_vs_planned_{selected_date}.csv"'
        )

        writer = csv.writer(response)
        writer.writerow(
            [
                "WO",
                "Product",
                "Order Qty",
                "Done Qty",
                "Planned Time (hours)",
                "Actual Time (hours)",
                "Time Delta",
                "Planned Material",
                "Actual Material",
                "Material Delta",
                "Production Delta",
                "Time Efficiency %",
                "Material Efficiency %",
                "Production Efficiency %",
                "Overall Efficiency %",
            ]
        )

        for row in rows:
            writer.writerow(
                [
                    row["wo_id"],
                    row["product_name"],
                    row["order_qty"],
                    row["actual_output_qty"] if row["actual_output_qty"] is not None else "",
                    row["planned_time_hours"],
                    row["actual_time_hours"] if row["actual_time_hours"] is not None else "",
                    row["time_delta_hours"] if row["time_delta_hours"] is not None else "",
                    row["planned_material_qty"],
                    row["actual_material_qty"],
                    row["material_delta_qty"],
                    row["production_delta_qty"] if row["production_delta_qty"] is not None else "",
                    row["time_efficiency_pct"] if row["time_efficiency_pct"] is not None else "",
                    row["material_efficiency_pct"] if row["material_efficiency_pct"] is not None else "",
                    row["production_efficiency_pct"] if row["production_efficiency_pct"] is not None else "",
                    row["overall_efficiency_pct"] if row["overall_efficiency_pct"] is not None else "",
                ]
            )
        return response


class ExportWorkOrderSheetView(LoginRequiredMixin, ActualVsPlannedReportMixin, View):
    def get(self, request):
        if not self._has_report_access(request.user):
            return HttpResponseForbidden("Access Denied")

        company = require_company(request.user)
        if not company:
            return HttpResponseForbidden("No company")

        filters = self._validate_filters(company, self._extract_filters(request))
        report_data = self._actual_vs_planned_data(company, filters)

        selected_root_id = filters["selected_work_order_id"]
        if not selected_root_id and report_data["rows"]:
            selected_root_id = report_data["rows"][0]["wo_id"]

        sheet = self._build_work_order_sheet(report_data, selected_root_id) if selected_root_id else None
        if not sheet:
            redirect_params = {
                "section": "actual",
                "sheet_error": "no_work_order",
            }
            if filters["selected_date"]:
                redirect_params["date"] = filters["selected_date"].isoformat()
            if filters["selected_product_id"]:
                redirect_params["product"] = filters["selected_product_id"]
            if filters["selected_work_order_id"]:
                redirect_params["work_order"] = filters["selected_work_order_id"]
            return redirect(f"{reverse('reports_dashboard')}?{urlencode(redirect_params)}")

        context = {
            "company": company,
            "sheet": sheet,
            "selected_date": filters["selected_date"],
            "generated_at": timezone.localtime(),
        }
        response = render(request, "manufacturing/reports_work_order_sheet.html", context)
        response["Content-Disposition"] = (
            f'inline; filename="work_order_efficiency_wo_{sheet["work_order"].id}.html"'
        )
        return response


class ExportAuditCSVView(ReportsDashboardView):
    def get(self, request):
        if not self._has_report_access(request.user):
            return HttpResponseForbidden("Access Denied")

        company = require_company(request.user)
        if not company:
            return HttpResponseForbidden("No company")

        filters = self._validate_audit_filters(company, self._extract_audit_filters(request))
        rows, _has_more = self._audit_rows(company, filters)

        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = 'attachment; filename="work_order_audit.csv"'
        writer = csv.writer(response)
        writer.writerow(["Time", "User", "Action", "Target", "Event", "Context", "IP"])
        for row in rows:
            writer.writerow(
                [
                    timezone.localtime(row["timestamp"]).strftime("%Y-%m-%d %H:%M:%S"),
                    getattr(row["user"], "username", "System") if row["user"] else "System",
                    row["action"],
                    row["target_label"],
                    row["event_label"],
                    row["summary_text"],
                    row["ip_address"] or "",
                ]
            )
        return response


class ExportReportPDFView(ReportsDashboardView):
    def get(self, request):
        if not self._has_report_access(request.user):
            return HttpResponseForbidden("Access Denied")

        company = require_company(request.user)
        if not company:
            return HttpResponseForbidden("No company")

        section = self._selected_section(request)
        generated_at = timezone.localtime().strftime("%Y-%m-%d %H:%M")

        if section == "audit":
            filters = self._validate_audit_filters(company, self._extract_audit_filters(request))
            rows, has_more = self._audit_rows(company, filters)
            lines = [
                f"Company: {company.name}",
                f"Generated: {generated_at}",
                f"Displayed rows: {len(rows)}{' (capped)' if has_more else ''}",
                "",
            ]
            for row in rows[:20]:
                timestamp = timezone.localtime(row["timestamp"]).strftime("%Y-%m-%d %H:%M")
                user = getattr(row["user"], "username", "System") if row["user"] else "System"
                lines.append(f"{timestamp} | {user} | {row['action']} | {row['target_label']}")
                if row["event_label"]:
                    lines.append(f"  Event: {row['event_label']}")
                if row["summary_text"]:
                    lines.append(f"  {row['summary_text']}")
            return self._simple_pdf_response("work_order_audit.pdf", "Work Order Audit Report", lines)

        if section == "bi":
            data = self._bi_dashboard_data(company)
            lines = [
                f"Company: {company.name}",
                f"Generated: {generated_at}",
                f"Total work orders: {data['total_work_orders']}",
                f"Open work orders: {data['open_work_orders']}",
                f"Completed work orders: {data['completed_work_orders']}",
                f"Planner-closed work orders: {data['closed_work_orders']}",
                f"Late work orders: {data['late_work_orders']}",
                f"Material ready work orders: {data['material_ready_count']}",
                f"Material shortage work orders: {data['material_shortage_count']}",
                f"Schedule adherence: {data['schedule_adherence_pct'] if data['schedule_adherence_pct'] is not None else 'N/A'}%",
                f"Pending approvals: {data['pending_approval_count']}",
                f"Approved output quantity: {data['approved_output_qty']}",
                "",
                "Top products:",
            ]
            for item in data["top_products"]:
                lines.append(f"- {item['label']}: {item['count']} WOs")
            return self._simple_pdf_response("manufacturing_bi_dashboard.pdf", "Manufacturing BI Dashboard", lines)

        filters = self._validate_filters(company, self._extract_filters(request))
        report_data = self._actual_vs_planned_data(company, filters)
        summary = report_data["summary"]
        lines = [
            f"Company: {company.name}",
            f"Generated: {generated_at}",
            f"Date: {filters['selected_date']}",
            f"Visible work orders: {summary['work_order_count']}",
            f"Overall efficiency: {summary['overall_efficiency_pct'] if summary['overall_efficiency_pct'] is not None else 'N/A'}%",
            f"Time efficiency: {summary['time_efficiency_pct'] if summary['time_efficiency_pct'] is not None else 'N/A'}%",
            f"Material efficiency: {summary['material_efficiency_pct'] if summary['material_efficiency_pct'] is not None else 'N/A'}%",
            f"Production efficiency: {summary['production_efficiency_pct'] if summary['production_efficiency_pct'] is not None else 'N/A'}%",
            "",
            "Work orders:",
        ]
        for row in report_data["rows"][:20]:
            lines.append(
                f"WO-{row['wo_id']} | {row['product_name']} | "
                f"order {row['order_qty']} | done {row['actual_output_qty'] if row['actual_output_qty'] is not None else 'N/A'}"
            )
        return self._simple_pdf_response("actual_vs_planned.pdf", "Actual vs Planned Report", lines)
