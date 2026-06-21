from decimal import Decimal, InvalidOperation
from datetime import timedelta
from .models import BillOfMaterial, Product, ProductionLog, SystemSettings, WorkOrder
from .shift_utils import factory_shift_configuration, machine_shift_configuration, summarize_shift_configuration
from .units import UnitService
from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone
from accounts.models import Profile
from manufacturing.access_control import resolve_user_role, worker_eligible_user_q
from manufacturing.services_blueprint.bom_service import (
    BOMService,
    _work_order_db_alias,
    _work_order_family_ids,
    apply_latest_bom_to_work_order,
    clear_work_order_assignment_for_bom_change,
    decide_bom_change_archive_and_replace,
    decide_bom_change_continue_old,
    decide_bom_change_scrap_and_apply,
    flag_bom_change_impact,
    get_apply_latest_bom_eligibility,
    get_latest_active_bom_for_work_order,
    get_workorder_bom_change_payload,
    get_workorder_production_totals,
    resolve_bom_for_work_order,
    work_order_has_started_production,
)

from manufacturing.services_blueprint.work_order_service import (
    WorkOrderLifecycle,
    WorkOrderLifecycleError,
    WorkOrderService,
    get_company_default_operation_flow_mode,
    get_work_order_operation_flow_mode,
    get_workorder_quantity_breakdown,
    normalize_operation_flow_mode,
    request_store_receipt_for_work_order,
)




def get_workorder_reported_quantity_floor(work_order):
    """
    Return the minimum quantity a planner can safely keep on a work order.

    For root work orders that already have routed stage tasks, each stage task can
    report the same physical batch. Summing stage logs would overcount the batch,
    so use the maximum reported quantity across the root and its active child
    tasks. For single work orders / stage tasks, this collapses to the work
    order's own non-rejected and approved quantities.
    """
    if not work_order or not getattr(work_order, "pk", None):
        return {"reported": 0, "approved": 0}

    work_order_ids = [work_order.id]
    if not getattr(work_order, "parent_id", None):
        child_ids = list(
            work_order.sub_tasks.exclude(status__in=["canceled", "archived"]).values_list("id", flat=True)
        )
        work_order_ids.extend(child_ids)

    reported_rows = (
        ProductionLog.objects.filter(work_order_id__in=work_order_ids)
        .exclude(status="rejected")
        .values("work_order_id")
        .annotate(total=Sum("quantity"))
    )
    approved_rows = (
        ProductionLog.objects.filter(work_order_id__in=work_order_ids, status="approved")
        .values("work_order_id")
        .annotate(total=Sum("quantity"))
    )

    max_reported = max((int(row["total"] or 0) for row in reported_rows), default=0)
    max_approved = max((int(row["total"] or 0) for row in approved_rows), default=0)
    return {"reported": max_reported, "approved": max_approved}


def get_workorder_material_readiness_payload(work_order, company=None):
    """Return store-controlled material readiness plus scaled BOM requirements."""
    if not work_order:
        return {
            "status": "not_checked",
            "status_label": "Not Checked",
            "shortage_note": "",
            "available_qty": None,
            "available_percent": None,
            "shortfall_qty": None,
            "expected_delivery_date": "",
            "can_plan": False,
            "planner_blocker": "Store has not confirmed BOM materials.",
            "materials": [],
            "has_bom": False,
        }

    company = company or getattr(work_order, "company", None)
    bom = resolve_bom_for_work_order(work_order, company) if company else getattr(work_order, "bom", None)
    _base_qty, _compensation_qty, adjusted_qty = get_workorder_quantity_breakdown(work_order)
    status = getattr(work_order, "material_readiness_status", None) or "not_checked"
    status_labels = {
        "not_checked": "Waiting for Store Confirmation",
        "ready": "Material OK",
        "partial": "Partially OK",
        "shortage": "Not Available",
    }
    materials = []
    available_qty = getattr(work_order, "material_available_qty", None)
    available_percent = getattr(work_order, "material_available_percent", None)
    if available_percent is None and available_qty is not None and adjusted_qty:
        available_percent = (Decimal(int(available_qty)) * Decimal("100") / Decimal(int(adjusted_qty))).quantize(Decimal("0.01"))
    shortfall_qty = None
    if available_qty is not None:
        shortfall_qty = max(int(adjusted_qty or 0) - int(available_qty or 0), 0)
    planner_blocker = get_material_readiness_planning_blocker(work_order)

    if bom:
        base_quantity = Decimal(str(getattr(bom, "base_quantity", None) or 1))
        if base_quantity <= 0:
            base_quantity = Decimal("1")
        scale = Decimal(str(adjusted_qty or 0)) / base_quantity

        for component in bom.components.select_related("product").order_by("id"):
            component_qty = Decimal(str(component.quantity or 0))
            required_qty = component_qty * scale
            materials.append(
                {
                    "component_id": component.id,
                    "name": component.product.name if component.product_id else (component.material_name or "-"),
                    "unit": component.unit or getattr(component.product, "unit", "") or "pcs",
                    "bom_quantity": float(component_qty),
                    "required_quantity": float(required_qty.quantize(Decimal("0.001"))),
                }
            )

    if status == "ready":
        planner_next_action = "Schedule or release the full work order quantity."
    elif status == "partial":
        planner_next_action = "Split or reduce the work order before scheduling the shortfall quantity."
    elif status == "shortage":
        planner_next_action = "Wait for store delivery update before scheduling."
    else:
        planner_next_action = "Ask store to confirm OK, Partially OK, or Not Available."

    expected_delivery_date = getattr(work_order, "material_expected_delivery_date", None)

    return {
        "status": status,
        "status_label": status_labels.get(status, status.replace("_", " ").title()),
        "shortage_note": getattr(work_order, "material_shortage_note", "") or "",
        "available_qty": int(available_qty) if available_qty is not None else None,
        "available_percent": float(available_percent) if available_percent is not None else None,
        "shortfall_qty": int(shortfall_qty) if shortfall_qty is not None else None,
        "expected_delivery_date": expected_delivery_date.isoformat() if expected_delivery_date else "",
        "can_plan": planner_blocker is None,
        "planner_blocker": planner_blocker or "",
        "planner_next_action": planner_next_action,
        "updated_at": (
            work_order.material_readiness_updated_at.isoformat()
            if getattr(work_order, "material_readiness_updated_at", None)
            else None
        ),
        "updated_by": (
            work_order.material_readiness_updated_by.username
            if getattr(work_order, "material_readiness_updated_by", None)
            else None
        ),
        "materials": materials,
        "has_bom": bool(bom),
        "work_order_quantity": int(adjusted_qty or 0),
    }


def get_material_readiness_source(work_order):
    if not work_order:
        return None
    return work_order.parent if getattr(work_order, "parent_id", None) and getattr(work_order, "parent", None) else work_order


def get_material_readiness_planning_blocker(work_order):
    source = get_material_readiness_source(work_order)
    if not source:
        return "Store has not confirmed BOM materials."

    status = str(getattr(source, "material_readiness_status", "not_checked") or "not_checked").lower()
    note = (getattr(source, "material_shortage_note", "") or "").strip()
    if status == "ready":
        return None
    if status == "partial":
        available_qty = int(getattr(source, "material_available_qty", 0) or 0)
        required_qty = int(getattr(source, "quantity", 0) or 0)
        delivery_date = getattr(source, "material_expected_delivery_date", None)
        delivery_hint = f" Expected delivery: {delivery_date.isoformat()}." if delivery_date else ""
        if available_qty <= 0:
            return (note or "Store marked material partially OK but did not confirm a usable percentage.") + delivery_hint
        if required_qty > available_qty:
            shortfall_qty = required_qty - available_qty
            return (
                f"Store confirmed material for {available_qty} units only. "
                f"Shortfall is {shortfall_qty} units. Reduce or split the WO before planning the shortfall."
                f"{delivery_hint}"
            )
        return None
    if status == "shortage":
        delivery_date = getattr(source, "material_expected_delivery_date", None)
        delivery_hint = f" Expected delivery: {delivery_date.isoformat()}." if delivery_date else ""
        return (note or "Store marked BOM materials as not available.") + delivery_hint
    return "Store has not confirmed BOM materials."


def workorder_has_material_planning_blocker(work_order):
    return bool(get_material_readiness_planning_blocker(work_order))


def workorder_has_material_shortage(work_order):
    source = get_material_readiness_source(work_order)
    if not source:
        return False
    return getattr(source, "material_readiness_status", "not_checked") == "shortage"


def get_workorder_execution_readiness(work_order, *, actor=None, now=None):
    """Return the execution start gate used by worker and supervisor surfaces."""
    from django.utils import timezone

    current_time = now or timezone.now()
    if not work_order:
        return {
            "can_start": False,
            "reason_code": "missing_work_order",
            "reason": "Work order was not found.",
        }

    status = str(getattr(work_order, "status", "") or "").strip().lower()
    if status != "pending":
        return {
            "can_start": False,
            "reason_code": "invalid_status",
            "reason": f"Invalid status: {status or 'blank'}.",
        }

    if not getattr(work_order, "machine_id", None):
        return {
            "can_start": False,
            "reason_code": "machine_not_assigned",
            "reason": "Machine not assigned.",
        }

    machine = getattr(work_order, "machine", None)
    if machine and str(getattr(machine, "status", "") or "").strip().lower() != "operational":
        return {
            "can_start": False,
            "reason_code": "machine_not_operational",
            "reason": "Machine is not operational.",
        }

    if not getattr(work_order, "assigned_worker_id", None):
        return {
            "can_start": False,
            "reason_code": "worker_not_assigned",
            "reason": "Worker not assigned.",
        }

    actor_id = getattr(actor, "id", None)
    if actor_id and getattr(work_order, "assigned_worker_id", None) != actor_id:
        return {
            "can_start": False,
            "reason_code": "worker_not_assigned",
            "reason": "Worker not assigned to this job.",
        }

    scheduled_start = getattr(work_order, "scheduled_start_date", None) or getattr(work_order, "start_date", None)

    source = work_order.parent if getattr(work_order, "parent_id", None) and getattr(work_order, "parent", None) else work_order
    material_blocker = get_material_readiness_planning_blocker(source)
    if material_blocker:
        status = getattr(source, "material_readiness_status", "not_checked") or "not_checked"
        return {
            "can_start": False,
            "reason_code": "material_not_ready" if status != "shortage" else "material_shortage",
            "reason": material_blocker,
            "material_readiness_status": status,
            "material_shortage_note": getattr(source, "material_shortage_note", "") or "",
            "material_available_qty": getattr(source, "material_available_qty", None),
            "material_available_percent": (
                float(source.material_available_percent)
                if getattr(source, "material_available_percent", None) is not None
                else None
            ),
            "material_expected_delivery_date": (
                source.material_expected_delivery_date.isoformat()
                if getattr(source, "material_expected_delivery_date", None)
                else ""
            ),
        }

    if getattr(source, "bom_change_status", "none") == "action_required":
        latest = None
        latest_id = getattr(source, "bom_change_latest_bom_id", None)
        if latest_id:
            latest = BillOfMaterial.objects.using(_work_order_db_alias(source)).filter(id=latest_id).first()
        return {
            "can_start": False,
            "reason_code": "bom_change_action_required",
            "reason": "BOM change action required.",
            "latest_bom_id": latest.id if latest else None,
            "latest_bom_version": latest.version if latest else "",
        }

    return {
            "can_start": True,
            "reason_code": "ready",
            "reason": "Ready to start.",
            "scheduled_start_at": scheduled_start.isoformat() if scheduled_start else None,
            "starts_before_plan": bool(scheduled_start and scheduled_start > current_time),
        }


def get_stage_time_breakdown(work_order):
    """
    Return stage timing metadata for timeline rendering:
    - setup_minutes: setup time for the current stage operation
    - estimated_duration_minutes: setup + run duration scaled by quantity
    """
    bom = getattr(work_order, 'bom', None)
    if not bom:
        return 0, 0

    stage_id = getattr(work_order, 'stage_id', None) or getattr(work_order, 'current_stage_id', None)
    ops = list(bom.operations.all())
    if not ops:
        return 0, 0

    # Keep operation selection stable by order.
    ops.sort(key=lambda x: int(getattr(x, 'order', 0) or 0))
    op = None
    if stage_id:
        for candidate in ops:
            if getattr(candidate, 'stage_id', None) == stage_id:
                op = candidate
                break
    if op is None:
        op = ops[0]

    setup_minutes = int(float(getattr(op, 'setup_time', 0) or 0))
    try:
        estimated_duration = int(round(float(WorkOrderService._compute_operation_duration(op, work_order.quantity))))
    except Exception:
        estimated_duration = 0
    return max(setup_minutes, 0), max(estimated_duration, 0)


class DashboardService:
    @staticmethod
    def _normalize_viewer_role(viewer_role):
        role = str(viewer_role or "").strip().lower()
        return role or "planner"

    @staticmethod
    def _pending_queue_work_order_code(work_order):
        if not work_order:
            return "-"
        return getattr(work_order, "display_work_order_code", None) or f"WO-{getattr(work_order, 'id', '-')}"

    @staticmethod
    def _pending_queue_stage_name(work_order, fallback="-"):
        if not work_order:
            return fallback
        stage = getattr(work_order, "stage", None) or getattr(work_order, "current_stage", None)
        return getattr(stage, "name", None) or getattr(work_order, "current_stage_name", None) or fallback

    @staticmethod
    def _pending_queue_machine_label(work_order, fallback="Unassigned"):
        if not work_order:
            return fallback
        machine = getattr(work_order, "machine", None)
        worker = getattr(work_order, "assigned_worker", None)
        if machine:
            return getattr(machine, "display_label", None) or getattr(machine, "name", None) or fallback
        if worker:
            return worker.get_full_name() or getattr(worker, "username", None) or fallback
        return fallback

    @staticmethod
    def _pending_queue_sort_value(work_order, fallback_id=0):
        if not work_order:
            return (timezone.now(), int(fallback_id or 0))
        return (
            getattr(work_order, "due_date", None)
            or getattr(work_order, "start_date", None)
            or getattr(work_order, "created_at", None)
            or timezone.now(),
            int(getattr(work_order, "id", None) or fallback_id or 0),
        )

    @staticmethod
    def _build_pending_wos_queue(
        *,
        pending_wos,
        material_actions,
        release_ready_tasks,
        pending_logs,
        planner_actions,
        bom_change_actions,
        qc_pending,
    ):
        """
        Normalize planner action sources into one backend-driven Pending WOs queue.
        The legacy context lists remain available for existing sidebars and actions.
        """
        rows = []
        seen = set()

        def add_row(category, source_key, work_order, **payload):
            dedupe_key = (category, source_key)
            if dedupe_key in seen:
                return
            seen.add(dedupe_key)
            sort_at, sort_id = DashboardService._pending_queue_sort_value(work_order, source_key)
            rows.append({
                "category": category,
                "sort_rank": payload.pop("sort_rank"),
                "sort_at": sort_at,
                "sort_id": sort_id,
                "wo_code": payload.pop(
                    "wo_code",
                    DashboardService._pending_queue_work_order_code(work_order),
                ),
                "product_name": payload.pop(
                    "product_name",
                    getattr(work_order, "product_name", None) or "Work Order",
                ),
                "quantity": payload.pop("quantity", getattr(work_order, "quantity", None) or "-"),
                "stage_name": payload.pop(
                    "stage_name",
                    DashboardService._pending_queue_stage_name(work_order),
                ),
                "machine_label": payload.pop(
                    "machine_label",
                    DashboardService._pending_queue_machine_label(work_order),
                ),
                **payload,
            })

        for wo in material_actions:
            material_payload = getattr(wo, "material_payload", {}) or {}
            add_row(
                "material",
                getattr(wo, "id", None),
                wo,
                sort_rank=10,
                status_label=material_payload.get("status_label") or "Material check",
                status_badge_class="bg-sky-50 text-sky-700",
                action_label="Review",
                action_wo_id=getattr(wo, "id", None),
            )

        for wo in pending_wos:
            add_row(
                "ready_plan",
                getattr(wo, "id", None),
                wo,
                sort_rank=20,
                status_label="Ready to Plan",
                status_badge_class="bg-blue-50 text-blue-700",
                action_label="Open Plan",
                action_wo_id=getattr(wo, "id", None),
            )

        for release_meta in release_ready_tasks:
            source_id = release_meta.get("source_id") or release_meta.get("id")
            parent_id = release_meta.get("parent_id") or source_id
            add_row(
                "release",
                source_id,
                None,
                sort_rank=30,
                wo_code=f"WO-{parent_id}",
                product_name=release_meta.get("product_name") or "Work Order",
                quantity=release_meta.get("available_qty") or release_meta.get("quantity") or "-",
                stage_name=release_meta.get("next_stage_name") or "Next Stage",
                status_label="Ready to Release",
                status_badge_class="bg-indigo-50 text-indigo-700",
                machine_label="Assign machine",
                action_label="Assign",
                action_wo_id=release_meta.get("id") or source_id,
            )

        for log in pending_logs:
            work_order = getattr(log, "work_order", None)
            add_row(
                "approval",
                getattr(log, "id", None),
                work_order,
                sort_rank=40,
                wo_code=DashboardService._pending_queue_work_order_code(work_order) if work_order else f"WO-{getattr(log, 'work_order_id', '-')}",
                product_name=getattr(log, "review_product_name", None) or "Production approval",
                quantity=getattr(log, "quantity", None) or "-",
                stage_name=getattr(log, "review_stage_name", None) or "-",
                status_label="Needs Approval",
                status_badge_class="bg-fuchsia-50 text-fuchsia-700",
                machine_label=getattr(log, "review_worker_name", None) or "Worker",
                action_label="Review",
                action_screen="approvals",
                action_wo_id=getattr(log, "work_order_id", None),
            )

        for wo in planner_actions:
            add_row(
                "approval",
                f"planner-{getattr(wo, 'id', None)}",
                wo,
                sort_rank=45,
                quantity=getattr(wo, "close_completed_qty", None) or getattr(wo, "quantity", None) or "-",
                stage_name=getattr(wo, "close_final_stage_name", None) or DashboardService._pending_queue_stage_name(wo),
                status_label="Planner Close",
                status_badge_class="bg-emerald-50 text-emerald-700",
                machine_label=DashboardService._pending_queue_machine_label(wo, "-"),
                action_label="Open",
                action_wo_id=getattr(wo, "id", None),
            )

        for wo in bom_change_actions:
            add_row(
                "blocked",
                f"bom-{getattr(wo, 'id', None)}",
                wo,
                sort_rank=50,
                status_label="BOM Decision",
                status_badge_class="bg-amber-50 text-amber-700",
                machine_label="Planner",
                action_label="Resolve",
                action_wo_id=getattr(wo, "id", None),
            )

        for wo in qc_pending:
            add_row(
                "blocked",
                f"qc-{getattr(wo, 'id', None)}",
                wo,
                sort_rank=55,
                stage_name=DashboardService._pending_queue_stage_name(wo, "QC"),
                status_label="QC Pending",
                status_badge_class="bg-orange-50 text-orange-700",
                machine_label="Quality",
                action_label="Open",
                action_wo_id=getattr(wo, "id", None),
            )

        rows.sort(key=lambda row: (row["sort_rank"], row["sort_at"], row["sort_id"]))
        counts = {
            "all": len(rows),
            "material": sum(1 for row in rows if row["category"] == "material"),
            "ready_plan": sum(1 for row in rows if row["category"] == "ready_plan"),
            "release": sum(1 for row in rows if row["category"] == "release"),
            "approval": sum(1 for row in rows if row["category"] == "approval"),
            "blocked": sum(1 for row in rows if row["category"] == "blocked"),
        }
        return rows, counts

    @staticmethod
    def _build_stage_order_lookup(work_orders):
        """
        Build (bom_id, stage_id) -> operation order for fast stage sequencing.
        """
        from .models import BOMOperation

        bom_ids = set()
        stage_ids = set()
        for wo in work_orders:
            stage_id = getattr(wo, "stage_id", None) or getattr(wo, "current_stage_id", None)
            if stage_id:
                stage_ids.add(stage_id)

            bom_id = None
            if getattr(wo, "parent_id", None):
                parent_obj = getattr(wo, "parent", None)
                bom_id = getattr(parent_obj, "bom_id", None)
            else:
                bom_id = getattr(wo, "bom_id", None)
            if bom_id:
                bom_ids.add(bom_id)

        if not bom_ids or not stage_ids:
            return {}

        lookup = {}
        rows = BOMOperation.objects.filter(
            bom_id__in=bom_ids,
            stage_id__in=stage_ids
        ).values("bom_id", "stage_id", "order")
        for row in rows:
            key = (row["bom_id"], row["stage_id"])
            try:
                order_val = int(row.get("order") or 0)
            except Exception:
                order_val = 0
            if order_val <= 0:
                continue
            prev = lookup.get(key)
            if prev is None or order_val < prev:
                lookup[key] = order_val

        return lookup

    @staticmethod
    def _resolve_stage_rank(work_order, stage_order_lookup):
        import re

        stage_id = getattr(work_order, "stage_id", None) or getattr(work_order, "current_stage_id", None)

        bom_id = None
        if getattr(work_order, "parent_id", None):
            parent_obj = getattr(work_order, "parent", None)
            bom_id = getattr(parent_obj, "bom_id", None)
        else:
            bom_id = getattr(work_order, "bom_id", None)

        if bom_id and stage_id:
            mapped = stage_order_lookup.get((bom_id, stage_id))
            if mapped is not None:
                return int(mapped)

        stage_obj = getattr(work_order, "stage", None) or getattr(work_order, "current_stage", None)
        stage_name = str(getattr(stage_obj, "name", "") or "")

        match = re.search(r"\bop\s*0*(\d+)\b", stage_name, flags=re.IGNORECASE)
        if match:
            try:
                return int(match.group(1))
            except Exception:
                pass

        try:
            stage_order = int(getattr(stage_obj, "order", 0) or 0)
            if stage_order > 0:
                return stage_order
        except Exception:
            pass

        return 999999

    @staticmethod
    def _normalize_department_value(value):
        return " ".join(str(value or "").strip().lower().split())

    @staticmethod
    def _split_department_values(value):
        import re

        if value is None:
            return []

        if isinstance(value, (list, tuple, set)):
            raw_values = value
        else:
            raw_values = re.split(r"[\n;]+", str(value))

        cleaned = []
        seen = set()
        for item in raw_values:
            label = " ".join(str(item or "").strip().split())
            key = label.lower()
            if not label or key in seen:
                continue
            seen.add(key)
            cleaned.append(label)
        return cleaned

    @staticmethod
    def _resolve_viewer_department(viewer):
        departments = DashboardService._resolve_viewer_departments(viewer)
        return next(iter(departments), None) if departments else None

    @staticmethod
    def _resolve_viewer_departments(viewer):
        if not viewer or not getattr(viewer, "is_authenticated", False):
            return set()

        profile = getattr(viewer, "profile", None)
        department = getattr(profile, "department", None) if profile else None
        if not department and getattr(viewer, "id", None):
            try:
                db_alias = getattr(getattr(viewer, "_state", None), "db", None) or "default"
                department = (
                    Profile.objects.using(db_alias)
                    .filter(user_id=viewer.id)
                    .values_list("department", flat=True)
                    .first()
                )
            except Exception:
                department = None

        return {
            normalized
            for normalized in (
                DashboardService._normalize_department_value(item)
                for item in DashboardService._split_department_values(department)
            )
            if normalized
        }

    @staticmethod
    def _resolve_work_order_department_keys(work_order):
        stage_obj = getattr(work_order, "current_stage", None) or getattr(work_order, "stage", None)
        machine = getattr(work_order, "machine", None)

        raw_values = [
            getattr(stage_obj, "category", None),
            getattr(stage_obj, "name", None),
            getattr(machine, "category", None),
            getattr(machine, "type", None),
        ]

        return {
            normalized
            for normalized in (
                DashboardService._normalize_department_value(value)
                for value in raw_values
            )
            if normalized
        }

    @staticmethod
    def _resolve_company_supervisor_departments(viewer):
        if not viewer or not getattr(viewer, "is_authenticated", False):
            return set()

        profile = getattr(viewer, "profile", None)
        company_id = getattr(profile, "company_id", None) if profile else None
        if not company_id and getattr(viewer, "id", None):
            try:
                db_alias = getattr(getattr(viewer, "_state", None), "db", None) or "default"
                profile = (
                    Profile.objects.using(db_alias)
                    .select_related("role")
                    .filter(user_id=viewer.id)
                    .first()
                )
                company_id = getattr(profile, "company_id", None)
            except Exception:
                company_id = None
        if not company_id:
            return set()

        db_alias = getattr(getattr(viewer, "_state", None), "db", None) or "default"
        rows = (
            Profile.objects.using(db_alias)
            .filter(company_id=company_id, role__name="supervisor")
            .values_list("department", flat=True)
        )
        normalized_departments = set()
        for value in rows:
            normalized_departments.update(
                normalized
                for normalized in (
                    DashboardService._normalize_department_value(item)
                    for item in DashboardService._split_department_values(value)
                )
                if normalized
            )
        return normalized_departments

    @staticmethod
    def _should_apply_supervisor_department_filter(viewer):
        return resolve_user_role(viewer) == "supervisor"

    @staticmethod
    def _filter_work_orders_for_viewer(
        work_orders,
        viewer_role="planner",
        shift_config=None,
        viewer=None,
        restrict_to_current_shift=True,
        include_future_pending=False,
    ):
        """
        Planner/Admin:
            - see all reserved stage tasks (full planned day timeline).
        Supervisor/Worker:
            - for each parent WO, show only the earliest non-completed stage lane.
            - future reserved stages stay hidden until previous stage closes.
            - do not trust parent.current_stage for visibility because split/release
              flows can advance the parent pointer before the previous lane is fully done.
        """
        from collections import defaultdict

        role = DashboardService._normalize_viewer_role(viewer_role)
        work_orders = list(work_orders or [])
        if role in {"planner", "admin"}:
            return work_orders

        hidden_statuses = {"canceled", "archived"}
        non_planner_hidden = set()
        terminal_statuses = {"completed", "canceled", "archived"}

        grouped = defaultdict(list)
        for wo in work_orders:
            root_id = wo.parent_id or wo.id
            grouped[root_id].append(wo)

        stage_order_lookup = DashboardService._build_stage_order_lookup(work_orders)
        rank_cache = {}

        def rank_for(wo):
            cached = rank_cache.get(wo.id)
            if cached is not None:
                return cached
            value = DashboardService._resolve_stage_rank(wo, stage_order_lookup)
            rank_cache[wo.id] = value
            return value

        visible = []
        for group in grouped.values():
            has_child_flow = any(wo.parent_id for wo in group)
            if not has_child_flow:
                for wo in group:
                    if wo.status in hidden_statuses or wo.status in non_planner_hidden:
                        continue
                    visible.append(wo)
                continue

            active = [
                wo for wo in group
                if wo.status not in terminal_statuses and wo.status not in non_planner_hidden
            ]
            if not active:
                continue

            flow_mode = get_work_order_operation_flow_mode(active[0])
            if flow_mode == 'parallel':
                visible.extend(active)
            else:
                # Only the earliest open stage is actionable for execution roles.
                current_rank = min(rank_for(wo) for wo in active)
                visible.extend(
                    wo for wo in active
                    if rank_for(wo) == current_rank
                )

        if role == "supervisor":
            visible = DashboardService._filter_supervisor_execution_work_orders(
                visible,
                include_future_pending=include_future_pending,
            )
        elif role == "worker":
            visible = DashboardService._filter_worker_execution_work_orders(
                visible,
                viewer=viewer,
            )

        if role == "supervisor" and restrict_to_current_shift:
            visible = DashboardService._filter_work_orders_to_current_shift(
                visible,
                shift_config=shift_config,
            )

        if role == "supervisor" and DashboardService._should_apply_supervisor_department_filter(viewer):
            viewer_departments = DashboardService._resolve_viewer_departments(viewer)
            if viewer_departments:
                company_departments = DashboardService._resolve_company_supervisor_departments(viewer)
                explicit_company_departments = {
                    department for department in company_departments
                    if department != "production"
                }
                visible = [
                    wo for wo in visible
                    if (
                        viewer_departments & DashboardService._resolve_work_order_department_keys(wo)
                        or (
                            "production" in viewer_departments
                            and not (
                                DashboardService._resolve_work_order_department_keys(wo)
                                & explicit_company_departments
                            )
                        )
                    )
                ]

        if role in {"supervisor", "worker"} and viewer:
            from manufacturing.work_order_visibility import can_user_see_work_order

            visible = [
                wo for wo in visible
                if can_user_see_work_order(viewer, wo, shift_config=shift_config)
            ]

        return visible

    @staticmethod
    def _filter_supervisor_execution_work_orders(work_orders, now=None, include_future_pending=False):
        from django.utils import timezone

        current_time = now or timezone.now()
        visible = []
        for wo in work_orders:
            status_value = str(getattr(wo, 'status', '') or '').strip().lower()
            if status_value == 'in_progress':
                visible.append(wo)
                continue

            if status_value != 'pending':
                visible.append(wo)
                continue

            scheduled_start = getattr(wo, 'scheduled_start_date', None)
            effective_start = scheduled_start or getattr(wo, 'start_date', None)

            if include_future_pending and effective_start:
                visible.append(wo)
                continue

            # Hide only explicitly route-reserved future tasks until their slot opens.
            # Legacy/manual stage tasks may carry `start_date` without a reserved
            # `scheduled_start_date`; once the stage is released, the supervisor
            # should still see those jobs in their department queue.
            if scheduled_start:
                if scheduled_start <= current_time:
                    visible.append(wo)
                continue

            if effective_start:
                visible.append(wo)
                continue

            visible.append(wo)

        return visible

    @staticmethod
    def _filter_execution_work_orders_by_schedule_time(work_orders, now=None):
        # Backward-compatible alias for older callers/tests.
        return DashboardService._filter_supervisor_execution_work_orders(
            work_orders,
            now=now,
        )

    @staticmethod
    def _filter_worker_execution_work_orders(work_orders, viewer=None):
        viewer_id = getattr(viewer, "id", None)
        visible = []
        for wo in work_orders:
            status_value = str(getattr(wo, "status", "") or "").strip().lower()
            if status_value in {"canceled", "archived"}:
                continue
            if viewer_id and getattr(wo, "assigned_worker_id", None) not in {None, viewer_id}:
                continue
            visible.append(wo)
        return visible

    @staticmethod
    def classify_worker_queue_work_orders(work_orders, now=None):
        current_time = now
        active = []
        ready = []
        future_pending = []

        for wo in work_orders:
            status_value = str(getattr(wo, "status", "") or "").strip().lower()
            if status_value in {"canceled", "archived", "completed", "done"}:
                continue

            if status_value == "in_progress":
                active.append(wo)
                continue

            if status_value != "pending":
                wo.execution_readiness = {
                    "can_start": False,
                    "reason_code": "invalid_status",
                    "reason": f"Invalid status: {status_value or 'blank'}.",
                }
                ready.append(wo)
                continue

            readiness = get_workorder_execution_readiness(wo, now=current_time)
            wo.execution_readiness = readiness
            wo.dispatch_block_reason = readiness.get("reason")
            wo.dispatch_block_code = readiness.get("reason_code")
            if readiness.get("can_start"):
                ready.append(wo)
            else:
                future_pending.append(wo)

        return {
            "active": active,
            "ready": ready,
            "future_pending": future_pending,
            "ready_ids": [wo.id for wo in ready if getattr(wo, "id", None)],
        }

    @staticmethod
    def get_current_shift_window(shift_config=None, now=None):
        """Return current shift metadata with exact datetime bounds."""
        from datetime import datetime, timedelta
        from django.utils import timezone

        now = timezone.localtime(now or timezone.now())
        config = factory_shift_configuration(type("ShiftSettings", (), {
            "shift_configuration": shift_config or {},
            "shift_mode": "3",
        })())

        def parse_time(config_key, key, fallback):
            raw_value = str(config.get(config_key, {}).get(key, fallback) or fallback)
            return datetime.strptime(raw_value, "%H:%M").time()

        def build_window(config_key, default_start, default_end):
            start_time = parse_time(config_key, 'start', default_start)
            end_time = parse_time(config_key, 'end', default_end)
            start_dt = now.replace(
                hour=start_time.hour,
                minute=start_time.minute,
                second=0,
                microsecond=0,
            )
            end_dt = now.replace(
                hour=end_time.hour,
                minute=end_time.minute,
                second=0,
                microsecond=0,
            )
            if start_time < end_time:
                if now.time() < start_time:
                    start_dt -= timedelta(days=1)
                    end_dt -= timedelta(days=1)
            else:
                if now.time() < end_time:
                    start_dt -= timedelta(days=1)
                else:
                    end_dt += timedelta(days=1)
            return start_dt, end_dt

        shift_defs = (
            ('day', 'Day Shift', 'morning', '06:00', '14:00'),
            ('middle', 'Middle Shift', 'afternoon', '14:00', '22:00'),
            ('night', 'Night Shift', 'night', '22:00', '06:00'),
        )
        active_shift_defs = [
            item for item in shift_defs
            if config.get(item[2], {}).get('enabled', True)
        ]
        if not active_shift_defs:
            active_shift_defs = [shift_defs[0]]

        for shift_type, shift_name, config_key, default_start, default_end in active_shift_defs:
            shift_start, shift_end = build_window(config_key, default_start, default_end)
            if shift_start <= now < shift_end:
                return {
                    'shift_type': shift_type,
                    'shift_name': shift_name,
                    'start': shift_start,
                    'end': shift_end,
                    'is_active': True,
                }

        next_shift = None
        for shift_type, shift_name, config_key, default_start, default_end in active_shift_defs:
            start_time = parse_time(config_key, 'start', default_start)
            end_time = parse_time(config_key, 'end', default_end)
            next_start = now.replace(
                hour=start_time.hour,
                minute=start_time.minute,
                second=0,
                microsecond=0,
            )
            if next_start <= now:
                next_start += timedelta(days=1)
            next_end = next_start.replace(
                hour=end_time.hour,
                minute=end_time.minute,
                second=0,
                microsecond=0,
            )
            if end_time <= start_time:
                next_end += timedelta(days=1)
            candidate = (next_start, next_end, shift_type, shift_name)
            if next_shift is None or candidate[0] < next_shift[0]:
                next_shift = candidate

        shift_start, shift_end, shift_type, shift_name = next_shift
        return {
            'shift_type': shift_type,
            'shift_name': shift_name,
            'start': shift_start,
            'end': shift_end,
            'is_active': False,
            'next_shift_name': shift_name,
        }

    @staticmethod
    def _filter_work_orders_to_current_shift(work_orders, shift_config=None):
        """Execution views surface current-shift work plus inherited open work."""
        shift_window = DashboardService.get_current_shift_window(shift_config=shift_config)
        if not shift_window.get('is_active', True):
            return []
        shift_start = shift_window['start']
        shift_end = shift_window['end']

        visible = []
        for wo in work_orders:
            wo_start = getattr(wo, 'start_date', None)
            if not wo_start:
                continue
            wo_end = getattr(wo, 'end_date', None) or wo_start
            status_value = str(getattr(wo, 'status', '') or '').strip().lower()
            if status_value in {'pending', 'in_progress'} and wo_start < shift_start:
                visible.append(wo)
                continue
            if wo_start < shift_end and wo_end > shift_start:
                visible.append(wo)
        return visible

    @staticmethod
    def _timeline_display_bounds_for_viewer(work_order, viewer_role="planner", now=None):
        start_at = getattr(work_order, 'start_date', None)
        end_at = getattr(work_order, 'end_date', None)
        inherited = False
        role = DashboardService._normalize_viewer_role(viewer_role)
        status_value = str(getattr(work_order, 'status', '') or '').strip().lower()

        if role in {'supervisor', 'worker'} and status_value in {'pending', 'in_progress'} and start_at:
            current_time = timezone.localtime(now or timezone.now())
            visible_start = current_time.replace(hour=0, minute=0, second=0, microsecond=0)
            visible_end = visible_start + timedelta(days=1)
            local_start = timezone.localtime(start_at)
            local_end = timezone.localtime(end_at) if end_at else None
            if local_start < visible_start and (local_end is None or local_end <= visible_start or status_value == 'in_progress'):
                inherited = True
                duration = (
                    local_end - local_start
                    if local_end and local_end > local_start
                    else timedelta(hours=1)
                )
                start_at = visible_start
                end_at = min(
                    visible_end,
                    max(current_time, visible_start + min(duration, timedelta(hours=4))),
                )

        return start_at, end_at, inherited

    @staticmethod
    def get_timeline_data(company, include_unscheduled=False, viewer_role="planner", viewer=None, status_filter=None):
        """
        Fetch data for Gantt Chart and Supervisor Dashboard.
        Returns:
            - tasks_data: List of metrics for Gantt
            - live_status: Dict of active machine states
            - kpi_data: Dict of header metrics
        """
        from .models import WorkOrder, MachineFault, Machine, ProductionStage, BOMOperation, SystemSettings

        requested_status_filter = str(status_filter or "").strip().lower()
        
        # 1. Fetch Work Orders (Optimized: Current Window Only)
        from django.utils import timezone
        from datetime import timedelta
        from django.db.models import Q, Exists, OuterRef
        
        # Default window: Last 30 days to Next 30 days
        start_range = timezone.now() - timedelta(days=30)
        end_range = timezone.now() + timedelta(days=30)
        
        base_filter = Q(start_date__lte=end_range)
        if include_unscheduled:
            base_filter = base_filter | Q(start_date__isnull=True, status='pending')

        active_child_orders = WorkOrder.objects.filter(parent_id=OuterRef("pk")).exclude(
            status__in=["canceled", "archived"]
        )

        work_orders = WorkOrder.objects.filter(
            company=company
        ).filter(
            base_filter
        ).filter(
            Q(end_date__gte=start_range) | Q(end_date__isnull=True)
        ).annotate(
            has_active_sub_tasks=Exists(active_child_orders)
        ).filter(
            Q(sub_tasks__isnull=True) | Q(parent__isnull=True, has_active_sub_tasks=False)
        ).select_related(
            "machine", "assigned_worker", "current_stage", "stage", "bom", "parent", "parent__bom"
        ).prefetch_related(
            "production_logs", "bom__operations"
        ).order_by("start_date").distinct()

        system_settings = SystemSettings.objects.filter(company=company).first()
        shift_config = factory_shift_configuration(system_settings)
        work_orders = DashboardService._filter_work_orders_for_viewer(
            work_orders,
            viewer_role=viewer_role,
            shift_config=shift_config,
            viewer=viewer,
        )

        # 2. Timeline Data (Gantt) with Progress Stats
        # ========================================
        # REQUIREMENT 2: Add progress_stats for Target vs Actual display
        # ========================================
        tasks_data = []
        for wo in work_orders:
            status_value = str(getattr(wo, 'status', '') or '').strip().lower()
            if status_value == 'archived':
                continue
            if requested_status_filter == 'canceled':
                if status_value != 'canceled':
                    continue
            elif status_value == 'canceled':
                continue
            # Keep unscheduled planner work orders when explicitly requested so
            # the Unassigned / Pending lane can render.
            if (
                getattr(wo, 'status') == 'pending'
                and (not wo.start_date or not wo.machine_id)
                and not include_unscheduled
            ):
                continue
                
            approved_qty = 0
            reported_qty = 0
            for log in wo.production_logs.all():
                if log.status == 'rejected':
                    continue
                reported_qty += log.quantity
                if log.status == 'approved':
                    approved_qty += log.quantity
            
            stage_obj = wo.current_stage or wo.stage
            base_qty, compensation_qty, adjusted_qty = get_workorder_quantity_breakdown(wo)
            setup_minutes, estimated_duration_minutes = get_stage_time_breakdown(wo)
            bom_change = get_workorder_bom_change_payload(wo)
            timeline_start, timeline_end, timeline_inherited = DashboardService._timeline_display_bounds_for_viewer(
                wo,
                viewer_role=viewer_role,
            )
            task = {
                "id": wo.id,
                "machine_id": wo.machine.id if wo.machine else None,
                "product": wo.product_name,
                "start": timeline_start.isoformat() if timeline_start else None,
                "end": timeline_end.isoformat() if timeline_end else None,
                "original_start": wo.start_date.isoformat() if wo.start_date else None,
                "original_end": wo.end_date.isoformat() if wo.end_date else None,
                "timeline_inherited": timeline_inherited,
                "status": wo.status,
                "progress": float(wo.progress),
                "quantity": adjusted_qty,  # Added for easier access
                "base_quantity": base_qty,
                "scrap_compensation_qty": compensation_qty,
                "has_scrap_compensation": compensation_qty > 0,
                "is_scrap_compensation_task": bool(getattr(wo, 'is_scrap_compensation_task', False)),
                "scrap_source_qc_id": getattr(wo, 'scrap_source_quality_check_id', None),
                "assigned_worker_name": wo.assigned_worker.username if wo.assigned_worker else None,
                "assignment_type": wo.assignment_type,
                "parent_id": wo.parent_id,
                "source_task_id": getattr(wo, "source_task_id", None),
                "qc_requirement": getattr(wo, 'qc_requirement', False),
                "stage_id": stage_obj.id if stage_obj else None,
                "stage_name": stage_obj.name if stage_obj else None,
                "setup_minutes": int(setup_minutes),
                "estimated_duration_minutes": int(estimated_duration_minutes),
                "finished_qty": int(reported_qty),
                "approved_qty": int(approved_qty),
                "remaining_qty": max(int(adjusted_qty) - int(reported_qty), 0),
                "bom_change": bom_change,
                "bom_change_status": bom_change["status"],
                "bom_change_action_required": bom_change["action_required"],
                # Progress Stats: Target vs Actual
                "progress_stats": {
                    "target": adjusted_qty,
                    "actual": int(reported_qty),
                    "approved": int(approved_qty)
                },
                "progress_text": f"Qty: {int(reported_qty)} / {adjusted_qty}"
            }
            tasks_data.append(task)
        
        # 3. Active Machine Data (For Polling)
        live_status = {}
        active_wos = WorkOrder.objects.filter(company=company, status='in_progress', sub_tasks__isnull=True).select_related('machine')
        
        # 🔮 Check for High Risk Machines (Predictive Maintenance)
        for machine in Machine.objects.filter(company=company):
            # Flag if runtime > 100 hours (simulation logic)
            # In real system, this comes from total_runtime_hours
            risk = False
            if machine.total_runtime_hours > 100:
                risk = True
                
            # If machine has an active WO, add to live_status
            wo = next((w for w in active_wos if w.machine_id == machine.id), None)
            
            if wo:
                live_status[machine.id] = {
                    'wo_id': wo.id,
                    'product': wo.product_name,
                    'progress': float(wo.progress),
                    'quantity': wo.quantity,
                    'risk': risk # Pass risk flag
                }
            elif risk:
                # Even if idle, show risk
                 live_status[machine.id] = {
                    'wo_id': None,
                    'product': "Idle",
                    'progress': 0,
                    'risk': risk
                 }

        # 4. KPI Data (For Dashboard Header)
        pending_count = WorkOrder.objects.filter(company=company, status='pending').count()
        alerts_count = MachineFault.objects.filter(machine__company=company, status='open').count()
        
        # Calculate Active Lines (Machines with in_progress jobs)
        active_lines_count = active_wos.filter(machine__isnull=False).values('machine').distinct().count()

        system_settings = SystemSettings.objects.filter(company=company).first()

        # 5. All Machines (For Timeline Rows)
        all_machines = []
        for m in Machine.objects.filter(company=company, is_active=True):
            effective_shift_config = machine_shift_configuration(m, system_settings)
            all_machines.append({
                'id': m.id,
                'name': m.name,
                'display_name': m.display_label,
                'code': m.code,
                'type': m.type,
                'category': m.category,
                'status': m.status,
                'use_factory_shifts': bool(getattr(m, 'use_factory_shifts', True)),
                'shift_configuration': effective_shift_config,
                'working_hours_summary': (
                    'Factory hours'
                    if getattr(m, 'use_factory_shifts', True)
                    else summarize_shift_configuration(effective_shift_config)
                ),
                'total_runtime_hours': float(m.total_runtime_hours or 0),
                'image_url': m.image.url if m.image else ''
            })

        # 6. Pending Orders (For Sidebar Refresh)
        pending_orders = WorkOrder.objects.filter(company=company, status='pending').values(
            'id', 'product_name', 'quantity', 'end_date', 'status', 'customer__name'
        ).order_by('id')

        # 7. Stage Catalog (for timeline filters and drawers)
        # Include:
        # - stages attached to company machines
        # - stages used by company BOM operations (covers category-only stages)
        stage_ids_from_boms = (
            BOMOperation.objects.filter(
                bom__product__company=company,
                stage_id__isnull=False,
            )
            .values_list("stage_id", flat=True)
            .distinct()
        )
        stage_rows = (
            ProductionStage.objects.filter(
                Q(machine__company=company) | Q(id__in=stage_ids_from_boms)
            )
            .select_related("machine")
            .distinct()
            .order_by("order", "name", "id")
        )
        stages_data = [
            {
                "id": stage.id,
                "name": stage.name,
                "order": stage.order,
                "default_machine_id": stage.machine_id,
                "machine_type": (
                    stage.category
                    or (stage.machine.category if stage.machine else None)
                    or (stage.machine.type if stage.machine else None)
                    or None
                ),
            }
            for stage in stage_rows
        ]

        weekly_holidays = []
        if system_settings and isinstance(system_settings.weekly_holidays, list):
            weekly_holidays = [int(d) for d in system_settings.weekly_holidays if str(d).isdigit()]

        return {
            "tasks": tasks_data,
            "machines": list(all_machines),
            "stages": stages_data,
            "weekly_holidays": weekly_holidays,
            "pending_orders": list(pending_orders),
            "live_status": live_status,
            "kpi_data": {
                "pending": pending_count,
                "active_lines": active_lines_count,
                "alerts": alerts_count
            }
        }

    @staticmethod
    def get_current_shift_info(shift_config=None):
        """Calculate current shift and remaining time."""
        from datetime import timedelta
        from django.utils import timezone
        now = timezone.localtime()
        shift_window = DashboardService.get_current_shift_window(
            shift_config=shift_config,
            now=now,
        )
        shift_name = shift_window['shift_name']
        shift_end = shift_window['end']
        remaining = (shift_end - now) if shift_window.get('is_active', True) else (shift_window['start'] - now)
        remaining_minutes = int(remaining.total_seconds() / 60)
        hours = remaining_minutes // 60
        minutes = remaining_minutes % 60
        
        return {
            'shift_name': shift_name if shift_window.get('is_active', True) else f"Off Shift · Next {shift_name}",
            'remaining_display': f"{hours:02d}:{minutes:02d}"
        }

    @staticmethod
    def get_dashboard_context(company, viewer_role="planner", viewer=None, status_filter=None):
        """
        Unified context for Planner and Supervisor dashboards.
        """
        from .models import WorkOrder, Machine, ProductionStage, BillOfMaterial, ProductionLog, Product, Customer, SystemSettings, QualityCheck, BOMOperation, Notification
        from django.contrib.auth.models import User
        from django.core.serializers.json import DjangoJSONEncoder
        from django.utils import timezone
        from django.db.models import Sum, Q, Exists, OuterRef
        import json
        
        # Reconcile parent completion/alerts before building dashboard data
        WorkOrderService.sync_parent_completion(company)
        requested_status_filter = str(status_filter or "").strip().lower()

        # Optimized fetching
        machines = list(Machine.objects.filter(company=company))
        active_child_orders = WorkOrder.objects.filter(parent_id=OuterRef("pk")).exclude(
            status__in=["canceled", "archived"]
        )
        work_orders = WorkOrder.objects.filter(company=company).annotate(
            has_active_sub_tasks=Exists(active_child_orders)
        ).filter(
            Q(sub_tasks__isnull=True) | Q(parent__isnull=True, has_active_sub_tasks=False)
        ).select_related(
            "machine",
            "bom__product",
            "bom",
            "parent",
            "parent__bom",
            "assigned_to",
            "assigned_worker",
            "current_stage",
            "stage",
        ).prefetch_related(
            "production_logs",
            "bom__operations"
        ).distinct()

        system_settings = SystemSettings.objects.filter(company=company).first()
        shift_config = factory_shift_configuration(system_settings)

        timeline_work_orders = DashboardService._filter_work_orders_for_viewer(
            work_orders,
            viewer_role=viewer_role,
            shift_config=shift_config,
            viewer=viewer,
        )
        
        # Stats
        active_wos_count = work_orders.filter(status='in_progress').count()
        op_count = sum(1 for m in machines if m.status == 'operational')
        fault_count = sum(1 for m in machines if m.status in ['maintenance', 'breakdown', 'broken', 'faulty'])
        free_count = len(machines) - op_count - fault_count
        pending_tasks_count = work_orders.filter(status='pending').count()
        
        # Utilization KPI
        # Capacity = Total Machines - Broken/Maintenance Machines
        total_capacity_count = len(machines) - fault_count
        active_machine_count = work_orders.filter(status='in_progress', machine__isnull=False).values('machine').distinct().count()
        utilization_rate = int((active_machine_count / total_capacity_count * 100) if total_capacity_count > 0 else 0)
        
        # Timeline Data
        machines_data = []
        for m in machines:
            effective_shift_config = machine_shift_configuration(m, system_settings)
            machines_data.append(
                {
                    "id": m.id,
                    "name": m.name,
                    "display_name": m.display_label,
                    "code": m.code,
                    "status": m.status,
                    "type": m.type,
                    "category": m.category,
                    "use_factory_shifts": bool(getattr(m, "use_factory_shifts", True)),
                    "shift_configuration": effective_shift_config,
                    "working_hours_summary": (
                        "Factory hours"
                        if getattr(m, "use_factory_shifts", True)
                        else summarize_shift_configuration(effective_shift_config)
                    ),
                }
            )

        stage_ids_from_boms = (
            BOMOperation.objects.filter(
                bom__product__company=company,
                stage_id__isnull=False,
            )
            .values_list("stage_id", flat=True)
            .distinct()
        )
        timeline_stages_qs = (
            ProductionStage.objects.filter(
                Q(machine__company=company) | Q(id__in=stage_ids_from_boms)
            )
            .select_related("machine")
            .distinct()
            .order_by("order", "name", "id")
        )
        stages_data = [
            {
                "id": stage.id,
                "name": stage.name,
                "order": stage.order,
                "default_machine_id": stage.machine_id,
                "machine_type": (
                    stage.category
                    or (stage.machine.category if stage.machine else None)
                    or (stage.machine.type if stage.machine else None)
                    or None
                ),
            }
            for stage in timeline_stages_qs
        ]
        
        # Calculate current time for late order detection
        now = timezone.now()
        
        include_unscheduled_in_context = str(viewer_role or '').strip().lower() in ['planner', 'admin']

        tasks_data = []
        for wo in timeline_work_orders:
            status_value = str(getattr(wo, 'status', '') or '').strip().lower()
            if status_value == 'archived':
                continue
            if requested_status_filter == 'canceled':
                if status_value != 'canceled':
                    continue
            elif status_value == 'canceled':
                continue
            # Planners need unscheduled work orders in the template payload so
            # first paint can still show the shared Unassigned / Pending lane.
            if (
                getattr(wo, 'status') == 'pending'
                and (not wo.start_date or not wo.machine_id)
                and not include_unscheduled_in_context
            ):
                continue
                
            approved_qty = 0
            reported_qty = 0
            for log in wo.production_logs.all():
                if log.status == 'rejected':
                    continue
                reported_qty += log.quantity
                if log.status == 'approved':
                    approved_qty += log.quantity
            base_qty, compensation_qty, adjusted_qty = get_workorder_quantity_breakdown(wo)
            setup_minutes, estimated_duration_minutes = get_stage_time_breakdown(wo)
            bom_change = get_workorder_bom_change_payload(wo)
            timeline_start, timeline_end, timeline_inherited = DashboardService._timeline_display_bounds_for_viewer(
                wo,
                viewer_role=viewer_role,
                now=now,
            )

            tasks_data.append({
                "id": wo.id,
                "machine_id": wo.machine.id if wo.machine else None,
                "product": wo.product_name,
                "start": timeline_start.isoformat() if timeline_start else None,
                "end": timeline_end.isoformat() if timeline_end else None,
                "original_start": wo.start_date.isoformat() if wo.start_date else None,
                "original_end": wo.end_date.isoformat() if wo.end_date else None,
                "timeline_inherited": timeline_inherited,
                "status": wo.status,
                "progress": float(wo.progress),
                "quantity": adjusted_qty,
                "base_quantity": base_qty,
                "scrap_compensation_qty": compensation_qty,
                "has_scrap_compensation": compensation_qty > 0,
                "is_scrap_compensation_task": bool(getattr(wo, 'is_scrap_compensation_task', False)),
                "scrap_source_qc_id": getattr(wo, 'scrap_source_quality_check_id', None),
                "assigned_worker_name": wo.assigned_worker.username if wo.assigned_worker else None,
                "assignment_type": wo.assignment_type,
                "parent_id": wo.parent_id,
                "qc_requirement": getattr(wo, 'qc_requirement', False),
                "stage_id": (wo.current_stage.id if wo.current_stage else (wo.stage.id if wo.stage else None)),
                "stage_name": (wo.current_stage.name if wo.current_stage else (wo.stage.name if wo.stage else None)),
                "setup_minutes": int(setup_minutes),
                "estimated_duration_minutes": int(estimated_duration_minutes),
                "progress_stats": {
                    "target": adjusted_qty,
                    "actual": int(reported_qty),
                    "approved": int(approved_qty)
                },
                "progress_text": f"Qty: {int(reported_qty)} / {adjusted_qty}",
                "finished_qty": int(reported_qty),
                "approved_qty": int(approved_qty),
                "remaining_qty": max(int(adjusted_qty) - int(reported_qty), 0),
                "bom_change": bom_change,
                "bom_change_status": bom_change["status"],
                "bom_change_action_required": bom_change["action_required"],
                "image": wo.bom.product.image.url if wo.bom and wo.bom.product and wo.bom.product.image else None,
                "is_late": wo.due_date and wo.due_date < now and wo.status in ['pending', 'in_progress'],
                "cycle_state": WorkOrderCycleService.describe(wo),
            })

        # Planner Intake list should represent root work orders (not stage child tasks).
        intake_orders_qs = WorkOrder.objects.filter(
            company=company,
            parent__isnull=True
        ).exclude(
            status__in=['completed', 'canceled', 'archived']
        ).exclude(
            bom_change_status='action_required'
        ).select_related(
            'machine',
            'bom__product',
            'current_stage',
            'stage'
        ).prefetch_related(
            'sub_tasks__machine'
        ).order_by('-id')

        intake_orders_data = []
        for wo in intake_orders_qs:
            material_readiness = get_workorder_material_readiness_payload(wo, company)
            display_machine_id = wo.machine_id
            display_start = wo.start_date
            display_end = wo.end_date
            display_status = wo.status

            if wo.sub_tasks.exists():
                current_child = wo.sub_tasks.filter(
                    status__in=['in_progress', 'pending']
                ).select_related('machine').order_by('start_date', 'id').first()
                if not current_child:
                    current_child = wo.sub_tasks.select_related('machine').order_by('-id').first()
                if current_child:
                    if not display_machine_id:
                        display_machine_id = current_child.machine_id
                    display_start = current_child.start_date or display_start
                    display_end = current_child.end_date or display_end
                if getattr(wo, 'next_stage_ready', False):
                    display_status = 'in_progress'

            intake_orders_data.append({
                "id": wo.id,
                "machine_id": display_machine_id,
                "product": wo.product_name,
                "start": display_start.isoformat() if display_start else None,
                "end": display_end.isoformat() if display_end else None,
                "status": display_status,
                "progress": float(wo.progress or 0),
                "quantity": int(wo.quantity or 0),
                "assigned_worker_name": wo.assigned_worker.username if wo.assigned_worker else None,
                "parent_id": wo.parent_id,
                "next_stage_ready": bool(getattr(wo, 'next_stage_ready', False)),
                "planner_action_required": bool(getattr(wo, 'planner_action_required', False)),
                "material_readiness_status": material_readiness["status"],
                "material_shortage_note": material_readiness["shortage_note"],
                "material_available_qty": material_readiness["available_qty"],
                "material_readiness": material_readiness,
                "release_source_id": wo.id,
                "available_release_qty": 0,
                "next_stage_name": None,
                "cycle_state": WorkOrderCycleService.describe(wo),
            })
        
        # Logs waiting for supervisor/planner review. Keep the data review-ready so
        # templates do not need to guess which WO/stage/machine fields are active.
        pending_logs_qs = (
            ProductionLog.objects.filter(work_order__company=company, status='pending')
            .select_related(
                'worker',
                'work_order',
                'work_order__machine',
                'work_order__stage',
                'work_order__current_stage',
                'work_order__bom__product',
            )
            .prefetch_related('material_usage')
            .order_by('-created_at')
        )
        if DashboardService._normalize_viewer_role(viewer_role) == "supervisor":
            approval_work_orders = (
                WorkOrder.objects.filter(company=company, production_logs__status='pending')
                .select_related(
                    "machine",
                    "stage",
                    "current_stage",
                    "parent",
                    "parent__current_stage",
                )
                .distinct()
            )
            visible_approval_ids = {
                wo.id
                for wo in DashboardService._filter_work_orders_for_viewer(
                    approval_work_orders,
                    viewer_role=viewer_role,
                    shift_config=shift_config,
                    viewer=viewer,
                    restrict_to_current_shift=False,
                    include_future_pending=True,
                )
            }
            pending_logs_qs = pending_logs_qs.filter(work_order_id__in=visible_approval_ids)

        pending_logs = list(pending_logs_qs)

        def _format_log_qty(value):
            if value is None:
                return "0"
            try:
                decimal_value = Decimal(str(value))
                if decimal_value == decimal_value.to_integral_value():
                    return str(int(decimal_value))
                return format(decimal_value.normalize(), "f")
            except Exception:
                return str(value)

        for log in pending_logs:
            log_wo = log.work_order
            log_stage = getattr(log_wo, 'stage', None) or getattr(log_wo, 'current_stage', None)
            log_machine = getattr(log_wo, 'machine', None)
            approved_qty = log_wo.production_logs.filter(status='approved').aggregate(
                total=Sum('quantity')
            )['total'] or 0
            other_pending_qty = log_wo.production_logs.filter(status='pending').exclude(id=log.id).aggregate(
                total=Sum('quantity')
            )['total'] or 0
            remaining_before_log = max(
                int(getattr(log_wo, 'quantity', 0) or 0) - int(approved_qty) - int(other_pending_qty),
                0,
            )
            remaining_after_log = max(int(remaining_before_log) - int(log.quantity or 0), 0)
            log_product = (
                getattr(log_wo, 'product_name', None)
                or (
                    log_wo.bom.product.name
                    if getattr(log_wo, 'bom_id', None) and getattr(log_wo.bom, 'product_id', None)
                    else "Work Order"
                )
            )
            material_rows = list(log.material_usage.all())
            if material_rows:
                material_summary = ", ".join(
                    f"{usage.material_name} {_format_log_qty(usage.quantity_used)} {usage.unit or ''}".strip()
                    for usage in material_rows
                )
            else:
                material_summary = "No material usage"

            log.review_product_name = log_product
            log.review_stage_name = getattr(log_stage, 'name', None) or "-"
            log.review_machine_label = getattr(log_machine, 'display_label', None) or "Manual"
            log.review_worker_name = (
                log.worker.get_full_name()
                if log.worker and log.worker.get_full_name()
                else (log.worker.username if log.worker else "-")
            )
            log.review_material_summary = material_summary
            log.review_approved_qty = int(approved_qty)
            log.review_remaining_before_log = int(remaining_before_log)
            log.review_remaining_after_log = int(remaining_after_log)
            log.review_has_remaining_after_approval = remaining_after_log > 0

        # Planner actions: completed parent WOs waiting for closure
        planner_actions = list(WorkOrder.objects.filter(
            company=company,
            parent__isnull=True,
            planner_action_required=True,
            closed_by_planner=False
        ).select_related(
            'customer',
            'stage',
            'current_stage',
        ).prefetch_related(
            'sub_tasks',
            'sub_tasks__stage',
            'sub_tasks__current_stage',
        ).order_by('-end_date'))
        for close_wo in planner_actions:
            if getattr(close_wo, 'store_receipt_status', 'not_requested') == 'not_requested':
                request_store_receipt_for_work_order(close_wo, notify=False)
                close_wo.refresh_from_db(fields=['store_receipt_status', 'store_receipt_requested_at'])
            close_wo.close_completed_qty = int(close_wo.quantity or 0) if close_wo.status == 'completed' else 0
            final_stage_task = None
            sub_tasks = list(close_wo.sub_tasks.all())
            if sub_tasks:
                final_stage_task = max(
                    sub_tasks,
                    key=lambda child: (
                        int(getattr(getattr(child, 'stage', None) or getattr(child, 'current_stage', None), 'order', 0) or 0),
                        child.end_date or child.start_date or child.created_at,
                        child.id,
                    )
                )
            final_stage = (
                getattr(final_stage_task, 'stage', None)
                or getattr(final_stage_task, 'current_stage', None)
                if final_stage_task
                else None
            ) or close_wo.stage or close_wo.current_stage
            close_wo.close_final_stage_name = getattr(final_stage, 'name', None) or '-'
            close_wo.close_customer_name = (
                getattr(close_wo.customer, 'name', None)
                or getattr(close_wo.customer, 'customer_name', None)
                or getattr(close_wo, 'customer_name', None)
                or '-'
            )

        bom_change_actions = list(WorkOrder.objects.filter(
            company=company,
            parent__isnull=True,
            bom_change_status='action_required',
        ).exclude(
            status__in=['canceled', 'archived']
        ).select_related(
            'bom',
            'bom_change_latest_bom',
        ).order_by('-bom_change_detected_at', '-id'))
        for bom_wo in bom_change_actions:
            bom_wo.bom_change_payload = get_workorder_bom_change_payload(bom_wo)

        material_actions = list(WorkOrder.objects.filter(
            company=company,
            parent__isnull=True,
            material_readiness_status__in=['not_checked', 'partial', 'shortage'],
        ).exclude(
            status__in=['completed', 'canceled', 'archived'],
        ).exclude(
            bom_change_status='action_required',
        ).select_related(
            'bom',
            'bom__product',
            'customer',
        ).order_by('material_readiness_updated_at', '-id'))
        for material_wo in material_actions:
            material_wo.material_payload = get_workorder_material_readiness_payload(material_wo, company)

        # QC pending (for planner visibility only)
        # Show any stage task that has a NEW QC record,
        # or has approved qty that still needs QC inspection.
        from django.db.models import F, IntegerField, ExpressionWrapper
        from django.db.models.functions import Coalesce
        qc_pending = WorkOrderService.annotate_qc_metrics(WorkOrder.objects.filter(
            company=company,
            qc_requirement=True
        ).exclude(
            status__in=['canceled', 'archived']
        )).filter(
            Q(has_new_qc=True) | Q(approved_qty__gt=F('qc_checked'))
        )

        # Next Stage Ready (planner action queue)
        # Show only tasks that still require planner intervention:
        # - next stage task exists but has no machine assignment, OR
        # - legacy/manual release flow where next stage task does not exist yet.
        release_ready_tasks = []
        release_meta_by_parent = {}
        # Use ALL leaf tasks (including parent WOs without sub-tasks)
        stage_tasks = work_orders
        for wo in stage_tasks:
            if wo.status in ['canceled', 'archived']:
                continue
            bom = wo.parent.bom if wo.parent_id else wo.bom
            if not bom:
                continue
            current_stage_id = wo.stage_id or wo.current_stage_id or (wo.parent.current_stage_id if wo.parent_id else None)
            # Fallback: if current stage isn't set, assume first BOM operation
            if not current_stage_id:
                first_op = bom.operations.select_related('stage').order_by('order').first()
                if first_op and first_op.stage_id:
                    current_stage_id = first_op.stage_id
            if not current_stage_id:
                continue
            next_stage = WorkOrderService.get_next_stage(bom, current_stage_id)
            if not next_stage:
                continue
            parent_wo = wo.parent if wo.parent_id else wo
            existing_next_task = WorkOrder.objects.filter(
                parent=parent_wo,
                stage=next_stage
            ).exclude(
                status__in=['completed', 'canceled', 'archived']
            ).order_by('start_date', 'id').first()
            planner_assignment_required = bool(existing_next_task and not existing_next_task.machine_id)

            qc_required = WorkOrderService._is_qc_required(wo, bom, current_stage_id)
            qc_pending_flag = False
            released_qty = int(getattr(wo, 'released_qty', 0) or 0)
            if qc_required:
                qc_pending_flag = QualityCheck.objects.filter(work_order=wo, status='new').exists()
                qc_good_qty = QualityCheck.objects.filter(
                    work_order=wo,
                    status='processed'
                ).aggregate(total=Sum('good_quantity'))['total'] or 0
                available_qty = max(int(qc_good_qty) - released_qty, 0)
            else:
                approved_qty = wo.production_logs.filter(status='approved').aggregate(
                    total=Sum('quantity')
                )['total'] or 0
                available_qty = max(int(approved_qty) - released_qty, 0)

            stage_complete = str(getattr(wo, 'status', '') or '').lower() == 'completed'
            if (stage_complete or available_qty > 0) and not qc_pending_flag:
                release_meta = {
                    "id": existing_next_task.id if existing_next_task else wo.id,
                    "source_id": wo.id,
                    "parent_id": wo.parent_id or wo.id,
                    "product_name": wo.product_name,
                    "quantity": wo.quantity,
                    "available_qty": int(available_qty),
                    "next_stage_name": next_stage.name if next_stage else "Next Stage",
                    "planner_assignment_required": planner_assignment_required,
                }
                parent_id = release_meta["parent_id"]
                prev_meta = release_meta_by_parent.get(parent_id)
                if prev_meta is None or int(release_meta.get("available_qty") or 0) > int(prev_meta.get("available_qty") or 0):
                    release_meta_by_parent[parent_id] = release_meta
                if planner_assignment_required:
                    release_ready_tasks.append(release_meta)

        # Attach release-source details to intake rows so Start Stage 2 can target
        # the correct stage task (leaf WO) instead of the root parent WO.
        for row in intake_orders_data:
            release_meta = release_meta_by_parent.get(row.get('id'))
            if not release_meta:
                continue
            row['release_source_id'] = release_meta.get('source_id') or release_meta.get('id')
            row['available_release_qty'] = int(release_meta.get('available_qty') or 0)
            row['next_stage_name'] = release_meta.get('next_stage_name')
            if release_meta.get('planner_assignment_required'):
                row['next_stage_ready'] = True

        # Autocomplete Data
        all_materials = Product.objects.filter(company=company).values('name', 'unit', 'material_type')
        pending_wos_qs = work_orders.filter(
            parent__isnull=True
        ).filter(
            Q(status='pending') & Q(start_date__isnull=True)
        ).exclude(
            bom_change_status='action_required'
        ).exclude(
            material_readiness_status__in=['not_checked', 'partial', 'shortage']
        )
        system_notifications = []
        if viewer:
            system_notifications = list(
                Notification.objects.filter(recipient=viewer, is_read=False)
                .order_by('-created_at')[:10]
            )
            for notice in system_notifications:
                if notice.link and "/manufacturing/planner/" in notice.link:
                    notice.link = notice.link.replace("/manufacturing/planner/", "/manufacturing/dashboard/")
        planner_notifications_count = (
            len(system_notifications)
            + len(bom_change_actions)
            + len(material_actions)
            + len(pending_logs)
            + len(planner_actions)
            + len(release_ready_tasks)
            + qc_pending.count()
        )
        pending_wos_queue, pending_wos_queue_counts = DashboardService._build_pending_wos_queue(
            pending_wos=pending_wos_qs,
            material_actions=material_actions,
            release_ready_tasks=release_ready_tasks,
            pending_logs=pending_logs,
            planner_actions=planner_actions,
            bom_change_actions=bom_change_actions,
            qc_pending=qc_pending,
        )

        return {
            "machines": machines,
            "machines_data": machines_data,
            "machines_json": json.dumps(machines_data, cls=DjangoJSONEncoder),
            "tasks_data": tasks_data,
            "tasks_json": json.dumps(tasks_data, cls=DjangoJSONEncoder),
            "stages_data": stages_data,
            "stages_json": json.dumps(stages_data, cls=DjangoJSONEncoder),
            "intake_orders_data": intake_orders_data,
            "intake_orders_json": json.dumps(intake_orders_data, cls=DjangoJSONEncoder),
            "work_orders": work_orders,  # Added for List View
            # Pending WOs: unscheduled work orders awaiting planning/release.
            "pending_wos": pending_wos_qs,
            "pending_wos_count": pending_wos_qs.count(),
            "pending_wos_queue": pending_wos_queue,
            "pending_wos_queue_count": pending_wos_queue_counts["all"],
            "pending_wos_queue_counts": pending_wos_queue_counts,
            "pending_orders": work_orders.filter(status='pending').exclude(bom_change_status='action_required').exclude(material_readiness_status__in=['not_checked', 'partial', 'shortage']).order_by('-start_date'),
            "active_wos_count": active_wos_count,
            "machine_stats": [op_count, fault_count, free_count],
            "total_capacity": total_capacity_count,
            "utilization_rate": utilization_rate,
            "active_lines_count": active_machine_count,
            "pending_tasks_count": pending_tasks_count,
            "shift_info": DashboardService.get_current_shift_info(shift_config),
            "shift_config": shift_config,
            "shift_config_json": json.dumps(shift_config, cls=DjangoJSONEncoder),
            "weekly_holidays": (
                [
                    int(d)
                    for d in (system_settings.weekly_holidays or [])
                    if str(d).isdigit()
                ]
                if system_settings and isinstance(system_settings.weekly_holidays, list)
                else []
            ),
            "pending_logs": pending_logs,
            "pending_logs_count": len(pending_logs),
            "planner_actions": planner_actions,
            "planner_actions_count": len(planner_actions),
            "bom_change_actions": bom_change_actions,
            "bom_change_actions_count": len(bom_change_actions),
            "material_actions": material_actions,
            "material_actions_count": len(material_actions),
            "system_notifications": system_notifications,
            "system_notifications_count": len(system_notifications),
            "planner_notifications_count": planner_notifications_count,
            "qc_pending": qc_pending,
            "qc_pending_count": qc_pending.count(),
            "release_ready_tasks": release_ready_tasks,
            "release_ready_count": len(release_ready_tasks),
            "customers": Customer.objects.filter(company=company).order_by('name'),
            # Supervisor Data
            "unmanned_orders": work_orders.filter(machine__isnull=False, assigned_worker__isnull=True, status='pending').order_by('start_date'),
            "pending_approvals": work_orders.filter(status='completed').order_by('-end_date'), # Assuming completed needs approval
            "workers": User.objects.filter(profile__company=company).filter(worker_eligible_user_q()),
            "hours": list(range(24)),
            "all_materials_json": json.dumps(list(all_materials), cls=DjangoJSONEncoder),
            "stages": timeline_stages_qs,
            "boms": BillOfMaterial.objects.filter(product__company=company).order_by("-id"),
            "oee_percentage": 84.2, # Simulated for now
            "downtime_str": "02:14", # Simulated for now
        }




        return {
            'estimated_cost': round(total_material_cost + operational_cost, 2),
            'material_cost': round(total_material_cost, 2),
            'operational_cost': round(operational_cost, 2),
            'estimated_time_hours': round(total_time_minutes / 60, 1),
            'bottleneck': bottleneck
        }


class LiveCostService:
    @staticmethod
    def calculate_current_burn(work_order):
        """
        💰 Calculate real-time money burn for an active WO.
        Burn = Material Cost (Fixed) + (Machine Rate * Elapsed Time)
        """
        from django.utils import timezone
        
        # 1. Material (Sunk Cost)
        # Assumes WO has a BOM, otherwise 0
        material_cost = Decimal(0)
        if work_order.bom:
            # Simple check: (Progress% * Total Material) or (Total Material committed at start?)
            # Usually material is issued at start. Let's count full material cost.
            unit_mat_cost = BOMService.calculate_cost(work_order.bom)
            material_cost = unit_mat_cost * Decimal(work_order.quantity)
            
        # 2. Machine Burn
        machine_cost = Decimal(0)
        if work_order.status == 'in_progress' and work_order.start_date:
            duration = timezone.now() - work_order.start_date
            hours = Decimal(duration.total_seconds()) / 3600
            
            # Mock Rate
            rate = Decimal(50.00) # Default $50/hr
            machine_cost = hours * rate
            
        elif work_order.status == 'completed' and work_order.start_date and work_order.end_date:
             duration = work_order.end_date - work_order.start_date
             hours = Decimal(duration.total_seconds()) / 3600
             rate = Decimal(50.00)
             machine_cost = hours * rate
             
        return round(material_cost + machine_cost, 2)


class QualityService:
    @staticmethod
    def analyze_image(image_file):
        """
        🔮 Simulate AI Visual Inspection.
        Returns: { "has_defect": bool, "defect_type": str, "confidence": float }
        """
        import random
        
        # Simulate processing delay? (Handled by frontend spinner usually, but sleep here if needed)
        # For demo, we just randomize result based on file name or pure random
        
        # Hack: if filename contains "bad", it's a defect. Else 80% chance good.
        is_bad = False
        if "bad" in image_file.name.lower() or "defect" in image_file.name.lower():
            is_bad = True
        else:
            is_bad = random.choice([True, False, False, False, False]) # 20% chance of random defect
            
        if is_bad:
            defect = random.choice(["Surface Crack", "Scratch", "Paint Defect", "Misalignment"])
            confidence = round(random.uniform(0.85, 0.99), 2)
            return {
                "has_defect": True,
                "defect_type": defect,
                "confidence": confidence,
                "message": f"⚠️ Defect Detected: {defect}"
            }
        else:
            confidence = round(random.uniform(0.90, 0.99), 2)
            return {
                "has_defect": False,
                "defect_type": "None",
                "confidence": confidence,
                "message": "✅ Quality OK"
            }


class WorkOrderCycleService:
    """
    Product-facing cycle state for manufacturing work orders.

    This is intentionally separate from WorkOrderLifecycle: lifecycle protects
    legal status transitions, while cycle state explains where the WO sits in
    the operating process and who should act next.
    """

    @staticmethod
    def describe(work_order):
        from .models import QualityCheck

        def payload(step, label, next_action=None, owner_role=None, blocker_reason=None, blocked=False):
            return {
                "step": step,
                "label": label,
                "next_action": next_action,
                "owner_role": owner_role,
                "blocker_reason": blocker_reason,
                "blocked": bool(blocked or blocker_reason),
            }

        if not work_order:
            return payload(
                "unknown",
                "Unknown",
                blocker_reason="Work order is not available.",
                blocked=True,
            )

        status_value = str(getattr(work_order, "status", "") or "").strip().lower()

        def has_sub_tasks():
            sub_tasks = getattr(work_order, "sub_tasks", None)
            return bool(sub_tasks and sub_tasks.exists())

        def assigned_machine_blocker():
            machine = getattr(work_order, "machine", None)
            if not machine:
                return None
            machine_status = str(getattr(machine, "status", "") or "").strip().lower()
            fault_statuses = {"fault", "broken", "breakdown", "maintenance", "down", "offline", "inactive"}
            if getattr(machine, "is_active", True) is False or machine_status in fault_statuses:
                machine_label = getattr(machine, "display_label", None) or getattr(machine, "name", None) or "Assigned machine"
                return f"{machine_label} is unavailable."
            return None

        def is_scheduled():
            return bool(getattr(work_order, "scheduled_start_date", None) or getattr(work_order, "start_date", None))

        if status_value == "archived":
            return payload("archived", "Archived")
        if status_value == "canceled":
            return payload("canceled", "Canceled")
        if getattr(work_order, "closed_by_planner", False):
            return payload("planner_closed", "Planner Closed")

        if getattr(work_order, "parent_id", None):
            route_parent = getattr(work_order, "parent", None)
        else:
            route_parent = work_order

        if getattr(route_parent, "planner_action_required", False) and status_value == "completed":
            if getattr(route_parent, "store_receipt_status", "not_requested") != "received":
                return payload(
                    "store_receipt",
                    "Store Receipt",
                    next_action="Confirm received quantity and scrap",
                    owner_role="store",
                    blocker_reason="Store receipt confirmation is required before planner close.",
                    blocked=True,
                )
            return payload(
                "planner_close",
                "Planner Close",
                next_action="Close completed work order",
                owner_role="planner",
                blocker_reason="Completed work order is waiting for planner close.",
            )

        qc_pending = QualityCheck.objects.filter(work_order=work_order, status="new").exists()
        if qc_pending:
            return payload(
                "quality_inspection",
                "Quality Inspection",
                next_action="Complete and approve quality inspection",
                owner_role="quality",
                blocker_reason="QC is pending before this work can move forward.",
                blocked=True,
            )

        approved_qty = work_order.production_logs.filter(status="approved").aggregate(
            total=Sum("quantity")
        )["total"] or 0
        pending_qty = work_order.production_logs.filter(status="pending").aggregate(
            total=Sum("quantity")
        )["total"] or 0

        if pending_qty:
            return payload(
                "production_approval",
                "Production Approval",
                next_action="Approve or reject pending production output",
                owner_role="supervisor",
                blocker_reason="Production output is waiting for supervisor approval.",
            )

        qc_required = bool(
            getattr(work_order, "qc_requirement", False)
            or (getattr(work_order, "stage", None) and work_order.stage.is_quality_check)
        )
        if qc_required:
            metrics = WorkOrderService.annotate_qc_metrics(
                type(work_order).objects.filter(id=work_order.id)
            ).first()
            qc_checked = int(getattr(metrics, "qc_checked", 0) or 0)
            if int(approved_qty or 0) > qc_checked:
                return payload(
                    "quality_inspection",
                    "Quality Inspection",
                    next_action="Inspect approved production output",
                    owner_role="quality",
                )

        if getattr(route_parent, "next_stage_ready", False):
            return payload(
                "next_stage_release",
                "Next Stage Release",
                next_action="Assign or release the next stage",
                owner_role="planner",
                blocker_reason="Next stage is ready and waiting for planner release.",
            )

        if status_value == "pending":
            if not getattr(work_order, "machine_id", None) and not has_sub_tasks():
                return payload(
                    "planning",
                    "Planning",
                    next_action="Assign machine and schedule work",
                    owner_role="planner",
                    blocker_reason="Machine is not assigned.",
                    blocked=True,
                )
            machine_blocker = assigned_machine_blocker()
            if machine_blocker and not has_sub_tasks():
                return payload(
                    "machine_unavailable",
                    "Machine Unavailable",
                    next_action="Assign another machine or resolve fault",
                    owner_role="planner",
                    blocker_reason=machine_blocker,
                )
            if not is_scheduled() and not has_sub_tasks():
                return payload(
                    "planning",
                    "Planning",
                    next_action="Schedule work order",
                    owner_role="planner",
                    blocker_reason="Start time is not scheduled.",
                )
            if not getattr(work_order, "assigned_worker_id", None) and not has_sub_tasks():
                return payload(
                    "supervisor_dispatch",
                    "Supervisor Dispatch",
                    next_action="Assign worker",
                    owner_role="supervisor",
                    blocker_reason="Worker is not assigned.",
                )
            return payload(
                "ready_for_execution",
                "Ready For Execution",
                next_action="Start production",
                owner_role="worker",
            )

        if status_value == "in_progress":
            machine_blocker = assigned_machine_blocker()
            if machine_blocker and not has_sub_tasks():
                return payload(
                    "machine_unavailable",
                    "Machine Unavailable",
                    next_action="Assign another machine or resolve fault",
                    owner_role="planner",
                    blocker_reason=machine_blocker,
                )
            if not getattr(work_order, "assigned_worker_id", None) and not has_sub_tasks():
                return payload(
                    "supervisor_dispatch",
                    "Supervisor Dispatch",
                    next_action="Assign worker",
                    owner_role="supervisor",
                    blocker_reason="Worker is not assigned.",
                )
            remaining_qty = max(int(getattr(work_order, "quantity", 0) or 0) - int(approved_qty or 0), 0)
            if remaining_qty > 0:
                return payload(
                    "shop_floor_execution",
                    "Shop Floor Execution",
                    next_action="Log production output",
                    owner_role="worker",
                )
            return payload(
                "production_approval",
                "Production Approval",
                next_action="Review production completion",
                owner_role="supervisor",
                blocker_reason="Production completion is waiting for supervisor review.",
            )

        if status_value == "completed":
            if not getattr(work_order, "parent_id", None):
                return payload(
                    "planner_close",
                    "Planner Close",
                    next_action="Close completed work order",
                    owner_role="planner",
                    blocker_reason="Completed work order is waiting for planner close.",
                )
            return payload("stage_completed", "Stage Completed")

        if status_value == "hold":
            return payload(
                "on_hold",
                "On Hold",
                next_action="Review hold reason and release work order",
                owner_role="planner",
                blocker_reason="Work order is on hold.",
            )

        return payload(
            "unknown",
            "Unknown",
            blocker_reason=f"Unsupported work order status '{status_value}'.",
            blocked=True,
        )


class NotificationService:
    @staticmethod
    def notify_users(users, title, message, link=None):
        from .models import Notification

        for user in users:
            Notification.objects.create(
                recipient=user,
                title=title,
                message=message,
                link=link
            )

    @staticmethod
    def notify_role(company, roles, title, message, link=None, exclude_user=None):
        if not company:
            return

        from django.contrib.auth.models import User

        roles = [r.lower() for r in roles]
        recipients = User.objects.filter(
            profile__company=company,
            profile__role__name__in=roles
        ).distinct()

        if exclude_user:
            recipients = recipients.exclude(id=exclude_user.id)

        NotificationService.notify_users(recipients, title, message, link)

    @staticmethod
    def notify_supervisors_for_log(log):
        work_order = log.work_order
        if log.completion_requested:
            title = "Completion requested"
            message = f"WO #{work_order.id} marked complete by {log.worker.username}. Review and approve."
        else:
            title = "Production log pending approval"
            message = f"WO #{work_order.id} has a new production log from {log.worker.username}."
        NotificationService.notify_role(
            work_order.company,
            roles=['supervisor', 'admin'],
            title=title,
            message=message,
            link="/manufacturing/supervisor/"
        )

    @staticmethod
    def notify_planner_for_approval(log, stage_result=None):
        work_order = log.work_order
        parent_wo = work_order.parent if work_order.parent_id else work_order
        title = "Production log approved"
        stage_name = work_order.stage.name if work_order.stage else None

        planner_link = f"/manufacturing/dashboard/?wo={parent_wo.id}"

        if stage_result and stage_result.get('next_stage'):
            if stage_result.get('planner_assignment_required') or stage_result.get('manual_start'):
                message = (
                    f"WO #{parent_wo.id} stage '{stage_name or 'current'}' approved. "
                    f"Planner action required: assign a machine for '{stage_result['next_stage'].name}'."
                )
            elif stage_result.get('created'):
                message = (
                    f"WO #{parent_wo.id} stage '{stage_name or 'current'}' approved. "
                    f"Next stage '{stage_result['next_stage'].name}' is pending scheduling."
                )
            else:
                message = (
                    f"WO #{parent_wo.id} stage '{stage_name or 'current'}' approved. "
                    f"Next stage '{stage_result['next_stage'].name}' is ready for review."
                )
        elif stage_result and stage_result.get('qc_pending'):
            message = (
                f"WO #{parent_wo.id} stage '{stage_name or 'current'}' approved. "
                "Quality check is pending before releasing the next stage."
            )
        elif stage_result and stage_result.get('stage_pending'):
            message = (
                f"WO #{parent_wo.id} stage '{stage_name or 'current'}' partially approved. "
                "Remaining split tasks are still in progress."
            )
        elif stage_result and stage_result.get('parent_completed'):
            message = (
                f"WO #{parent_wo.id} final stage '{stage_name or 'current'}' approved. "
                "Planner action required: close the work order."
            )
        elif parent_wo.status == 'completed':
            message = f"WO #{parent_wo.id} is completed. Review and close the order."
        else:
            message = f"WO #{parent_wo.id} log approved. Review next stage assignment."

        if work_order.assigned_to:
            NotificationService.notify_users(
                [work_order.assigned_to],
                title=title,
                message=message,
                link=planner_link
            )
            return

        NotificationService.notify_role(
            work_order.company,
            roles=['planner', 'admin'],
            title=title,
            message=message,
            link=planner_link
        )


class ProductionLogService:
    @staticmethod
    def _is_privileged_user(user, db_alias="default"):
        if user.is_superuser:
            return True
        user_id = getattr(user, "id", None)
        if not user_id:
            return False

        from accounts.models import Profile

        profile = (
            Profile.objects.using(db_alias)
            .select_related("role")
            .filter(user_id=user_id)
            .first()
        )
        if not profile or not profile.role:
            return False
        return profile.role.name.lower() in ['admin', 'supervisor']

    @staticmethod
    def create_log(work_order, worker, quantity, note=None, shift=None, materials=None, completion_requested=False):
        try:
            quantity_val = int(quantity)
        except Exception:
            raise ValueError("Quantity must be a number.")

        if quantity_val <= 0:
            raise ValueError("Quantity must be positive.")

        if not work_order.company:
            raise ValueError("Work order is missing company.")

        db_alias = getattr(work_order._state, "db", None) or "default"
        worker_id = getattr(worker, "id", None)
        if not worker_id:
            raise PermissionError("Worker account is invalid.")

        if not worker.is_superuser:
            from accounts.models import Profile

            worker_profile = (
                Profile.objects.using(db_alias)
                .filter(user_id=worker_id)
                .values("company_id")
                .first()
            )
            if not worker_profile or worker_profile.get("company_id") != work_order.company_id:
                raise PermissionError("Worker does not belong to this company.")

        if work_order.status not in ['pending', 'in_progress']:
            raise ValueError("Work order is not active.")

        if not work_order.assigned_worker_id:
            if not ProductionLogService._is_privileged_user(worker, db_alias=db_alias):
                raise PermissionError("Work order has no worker assigned.")
        elif work_order.assigned_worker_id != worker.id:
            if not ProductionLogService._is_privileged_user(worker, db_alias=db_alias):
                raise PermissionError("Worker is not assigned to this work order.")

        from django.utils import timezone
        from django.db import transaction
        from .models import ProductionLog, MaterialUsage, Product

        valid_shifts = {choice[0] for choice in ProductionLog.SHIFT_CHOICES}
        if shift and shift not in valid_shifts:
            raise ValueError("Invalid shift.")

        if isinstance(completion_requested, str):
            completion_requested = completion_requested.strip().lower() in ['1', 'true', 'yes', 'on']
        else:
            completion_requested = bool(completion_requested)

        materials = materials if isinstance(materials, list) else []

        def _parse_material_quantity(raw_value):
            try:
                qty_value = Decimal(str(raw_value))
            except Exception:
                return None
            return qty_value if qty_value > 0 else None

        def _clean_material_text(raw_value, *, max_length=None):
            text = str(raw_value or '').strip()
            if max_length is not None:
                return text[:max_length]
            return text

        with transaction.atomic(using=db_alias):
            locked_wo = type(work_order).objects.using(db_alias).select_for_update().get(id=work_order.id)

            if locked_wo.status == 'pending':
                now = timezone.now()
                WorkOrderLifecycle.apply_transition(
                    locked_wo,
                    'in_progress',
                    actor=worker,
                    save=False
                )
                if locked_wo.start_date and not locked_wo.scheduled_start_date:
                    locked_wo.scheduled_start_date = locked_wo.start_date
                locked_wo.start_date = now
                if not locked_wo.worker_start_at:
                    locked_wo.worker_start_at = now
                locked_wo.save(update_fields=['status', 'scheduled_start_date', 'start_date', 'worker_start_at'])

            approved_qty = locked_wo.production_logs.filter(status='approved').aggregate(
                total=Sum('quantity')
            )['total'] or 0
            pending_qty = locked_wo.production_logs.filter(status='pending').aggregate(
                total=Sum('quantity')
            )['total'] or 0
            remaining_qty = max(int(locked_wo.quantity) - int(approved_qty) - int(pending_qty), 0)
            if quantity_val > remaining_qty:
                raise ValueError(
                    f"Quantity exceeds remaining work ({remaining_qty}). "
                    f"WO target is {locked_wo.quantity}, already reported {int(approved_qty) + int(pending_qty)}."
                )

            bom_components = {}
            if locked_wo.bom_id:
                bom_components = {
                    component.id: component
                    for component in locked_wo.bom.components.select_related('product').all()
                }

            material_records = []
            for item in materials:
                if not isinstance(item, dict):
                    continue

                qty_val = _parse_material_quantity(item.get('quantity'))
                planned_qty = (
                    _parse_material_quantity(item.get('planned_quantity'))
                    or _parse_material_quantity(item.get('expected_qty'))
                )
                if qty_val is None and planned_qty is None:
                    continue
                if qty_val is None:
                    qty_val = Decimal("0")

                component = None
                component_id = item.get('component_id')
                try:
                    if component_id not in (None, ''):
                        component = bom_components.get(int(component_id))
                except (TypeError, ValueError):
                    component = None

                product = None
                product_id = item.get('product_id')
                try:
                    if product_id not in (None, ''):
                        product = Product.objects.using(db_alias).filter(
                            id=int(product_id),
                            company=locked_wo.company,
                        ).first()
                except (TypeError, ValueError):
                    product = None

                if component and component.product_id:
                    product = component.product

                material_name = _clean_material_text(item.get('material_name'), max_length=100)
                unit = _clean_material_text(item.get('unit'), max_length=10)

                if component:
                    if not material_name:
                        material_name = _clean_material_text(component.material_name, max_length=100)
                    if not unit:
                        unit = _clean_material_text(component.unit, max_length=10)
                    if planned_qty is None:
                        base_qty = Decimal(str(getattr(locked_wo.bom, "base_quantity", None) or 1))
                        if base_qty <= 0:
                            base_qty = Decimal("1")
                        planned_qty = (
                            Decimal(str(component.quantity or 0))
                            / base_qty
                            * Decimal(str(quantity_val))
                        ).quantize(Decimal("0.001"))

                if product:
                    if not material_name:
                        material_name = _clean_material_text(product.name, max_length=100)
                    if not unit:
                        unit = _clean_material_text(product.unit, max_length=10)

                if not material_name:
                    continue

                material_records.append({
                    'product': product,
                    'material_name': material_name,
                    'planned_quantity': planned_qty,
                    'quantity_used': qty_val,
                    'unit': unit or 'pcs',
                })

            if locked_wo.bom and locked_wo.bom.components.exists():
                if not material_records:
                    raise ValueError("Material usage is required for this work order.")

            log = ProductionLog.objects.using(db_alias).create(
                work_order=locked_wo,
                worker_id=worker_id,
                quantity=quantity_val,
                status='pending',
                note=note or '',
                shift=shift or ProductionLog.SHIFT_CHOICES[0][0],
                completion_requested=completion_requested
            )

            for record in material_records:
                MaterialUsage.objects.using(db_alias).create(
                    production_log=log,
                    product=record['product'],
                    material_name=record['material_name'],
                    planned_quantity=record['planned_quantity'],
                    quantity_used=record['quantity_used'],
                    unit=record['unit']
                )

        NotificationService.notify_supervisors_for_log(log)
        return log

    @staticmethod
    def approve_log(log, reviewer):
        if log.status == 'approved':
            return log

        from django.utils import timezone

        log.status = 'approved'
        log.reviewed_by = reviewer
        log.reviewed_at = timezone.now()
        log.save()

        work_order = log.work_order
        actual_finish_at = log.reviewed_at or timezone.now()
        is_comp_task = WorkOrderService._is_scrap_compensation_flow(work_order)
        # Ensure QC record exists for partial approvals when QC is required
        bom_for_qc = work_order.parent.bom if work_order.parent_id else work_order.bom
        qc_required = WorkOrderService._is_qc_required(
            work_order,
            bom_for_qc,
            work_order.stage_id or work_order.current_stage_id
        )
        if qc_required:
            if not getattr(work_order, 'qc_requirement', False):
                work_order.qc_requirement = True
                work_order.save(update_fields=['qc_requirement'])
            WorkOrderService._ensure_qc_record(work_order)
        stage_result = None
        if work_order.status not in ['canceled', 'archived']:
            total_qty = work_order.production_logs.filter(status='approved').aggregate(
                Sum('quantity')
            )['quantity__sum'] or 0
            remaining_qty = max(int(work_order.quantity) - int(total_qty), 0)

            if log.completion_requested:
                # If worker completed their assignment but quantity remains,
                # split remaining qty into a new WO for supervisor assignment.
                if remaining_qty > 0:
                    if is_comp_task:
                        stage_result = {"stage_pending": True}
                    else:
                        split_created = False
                        target_machine = (
                            work_order.machine
                            or (work_order.stage.machine if work_order.stage else None)
                            or (work_order.current_stage.machine if work_order.current_stage else None)
                        )
                        if target_machine:
                            try:
                                WorkOrderService.split_work_order(
                                    work_order,
                                    remaining_qty,
                                    target_machine,
                                    reviewer,
                                    planned_start=actual_finish_at,
                                    actual_finish_at=actual_finish_at,
                                )
                                split_created = True
                            except Exception:
                                split_created = False
                        if split_created:
                            stage_result = WorkOrderService.create_next_stage_task(work_order, reviewer, auto_create=False)
                        else:
                            # Do not advance if we could not split the remaining quantity.
                            # This prevents premature completion of multi-stage work orders.
                            stage_result = {"stage_pending": True}
                            work_order.refresh_from_db()
                else:
                    if not work_order.end_date or work_order.end_date > actual_finish_at:
                        work_order.end_date = actual_finish_at
                        work_order.save(update_fields=['end_date'])
                    stage_result = WorkOrderService.create_next_stage_task(work_order, reviewer, auto_create=False)
            else:
                # If a worker submits partial output and there are no other pending logs,
                # split the remaining qty into a new WO so supervisor can assign it.
                pending_logs_exist = work_order.production_logs.filter(status='pending').exists()
                if remaining_qty > 0 and not pending_logs_exist and not work_order.sub_tasks.exists():
                    if is_comp_task:
                        stage_result = {"stage_pending": True}
                        work_order.refresh_from_db()
                        NotificationService.notify_planner_for_approval(log, stage_result)
                        return log
                    target_machine = (
                        work_order.machine
                        or (work_order.stage.machine if work_order.stage else None)
                        or (work_order.current_stage.machine if work_order.current_stage else None)
                    )
                    if target_machine:
                        try:
                            WorkOrderService.split_work_order(
                                work_order,
                                remaining_qty,
                                target_machine,
                                reviewer,
                                planned_start=actual_finish_at,
                                actual_finish_at=actual_finish_at,
                            )
                            stage_result = {"stage_pending": True}
                        except Exception:
                            pass
                if stage_result is None:
                    if total_qty >= work_order.quantity and not work_order.sub_tasks.exists():
                        if not work_order.end_date or work_order.end_date > actual_finish_at:
                            work_order.end_date = actual_finish_at
                            work_order.save(update_fields=['end_date'])
                        stage_result = WorkOrderService.create_next_stage_task(work_order, reviewer, auto_create=False)
                    else:
                        work_order.refresh_from_db()
        else:
            work_order.refresh_from_db()

        NotificationService.notify_planner_for_approval(log, stage_result)
        return log

    @staticmethod
    def reject_log(log, reviewer, reason=None):
        if log.status == 'rejected':
            return log

        from django.utils import timezone

        rejection_reason = str(reason or '').strip()
        log.status = 'rejected'
        log.reviewed_by = reviewer
        log.reviewed_at = timezone.now()
        if rejection_reason:
            existing_note = (log.note or '').strip()
            review_note = f"Rejection reason: {rejection_reason}"
            log.note = f"{existing_note}\n{review_note}".strip() if existing_note else review_note
        log.save()
        return log
