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


def resolve_bom_for_work_order(work_order, company):
    if not work_order:
        return None
    if getattr(work_order, "bom_id", None):
        return work_order.bom

    parent = getattr(work_order, "parent", None)
    if getattr(work_order, "parent_id", None) and parent and getattr(parent, "bom_id", None):
        return parent.bom

    product_name = str(
        getattr(parent, "product_name", "") if parent else getattr(work_order, "product_name", "") or ""
    ).strip()
    if not product_name:
        return None

    product = Product.objects.filter(company=company, name__iexact=product_name).first()
    if not product:
        return None

    active_bom = (
        BillOfMaterial.objects.filter(product=product, status="active")
        .order_by("-created_at", "-id")
        .first()
    )
    if active_bom:
        return active_bom

    return (
        BillOfMaterial.objects.filter(product=product)
        .order_by("-created_at", "-id")
        .first()
    )


def get_workorder_quantity_breakdown(work_order):
    """
    Return a stable quantity breakdown for UI/APIs.
    base_quantity + scrap_compensation_qty = current quantity
    """
    current_qty = int(getattr(work_order, 'quantity', 0) or 0)
    compensation_qty = int(getattr(work_order, 'scrap_compensation_qty', 0) or 0)
    base_qty = getattr(work_order, 'base_quantity', None)
    if base_qty is None:
        base_qty = max(current_qty - compensation_qty, 0)
    return int(base_qty), int(compensation_qty), int(current_qty)


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


def request_store_receipt_for_work_order(work_order, actor=None, notify=True):
    if not work_order:
        return False
    target = work_order.parent if getattr(work_order, "parent_id", None) and getattr(work_order, "parent", None) else work_order
    if getattr(target, "store_receipt_status", "not_requested") == "received":
        return False
    if getattr(target, "store_receipt_status", "not_requested") == "pending":
        return False

    target.store_receipt_status = "pending"
    target.store_receipt_requested_at = timezone.now()
    target.save(update_fields=["store_receipt_status", "store_receipt_requested_at"])

    if notify:
        NotificationService.notify_role(
            target.company,
            roles=["store", "admin"],
            title="Finished goods receipt",
            message=f"WO #{target.id} is complete. Confirm received quantity and scrap before planner close.",
            link="/manufacturing/store/",
            exclude_user=actor,
        )
    return True


def get_latest_active_bom_for_work_order(work_order):
    if not work_order or not getattr(work_order, "bom_id", None):
        return None

    db_alias = getattr(getattr(work_order, "_state", None), "db", None) or "default"
    bom_product_id = (
        BillOfMaterial.objects.using(db_alias)
        .filter(id=work_order.bom_id)
        .values_list("product_id", flat=True)
        .first()
    )
    if not bom_product_id:
        return None

    return (
        BillOfMaterial.objects.using(db_alias)
        .filter(product_id=bom_product_id, status="active")
        .exclude(id=work_order.bom_id)
        .order_by("-created_at", "-id")
        .first()
    )


def _work_order_db_alias(work_order):
    return getattr(getattr(work_order, "_state", None), "db", None) or "default"


def _work_order_family_ids(work_order, db_alias=None):
    if not work_order or not getattr(work_order, "pk", None):
        return []
    alias = db_alias or _work_order_db_alias(work_order)
    root_id = getattr(work_order, "parent_id", None) or work_order.id
    ids = [root_id]
    ids.extend(
        WorkOrder.objects.using(alias)
        .filter(parent_id=root_id)
        .exclude(status__in=["canceled", "archived"])
        .values_list("id", flat=True)
    )
    return list(dict.fromkeys(ids))


def get_workorder_production_totals(work_order):
    db_alias = _work_order_db_alias(work_order)
    ids = _work_order_family_ids(work_order, db_alias=db_alias)
    if not ids:
        return {"reported": 0, "approved": 0, "pending": 0}

    reported = (
        ProductionLog.objects.using(db_alias)
        .filter(work_order_id__in=ids)
        .exclude(status="rejected")
        .aggregate(total=Sum("quantity"))
        .get("total")
        or 0
    )
    approved = (
        ProductionLog.objects.using(db_alias)
        .filter(work_order_id__in=ids, status="approved")
        .aggregate(total=Sum("quantity"))
        .get("total")
        or 0
    )
    pending = (
        ProductionLog.objects.using(db_alias)
        .filter(work_order_id__in=ids, status="pending")
        .aggregate(total=Sum("quantity"))
        .get("total")
        or 0
    )
    return {"reported": int(reported), "approved": int(approved), "pending": int(pending)}


def work_order_has_started_production(work_order):
    if not work_order:
        return False
    totals = get_workorder_production_totals(work_order)
    return (
        totals["reported"] > 0
        or bool(getattr(work_order, "worker_start_at", None))
        or str(getattr(work_order, "status", "") or "").lower() == "in_progress"
    )


def get_workorder_bom_change_payload(work_order):
    db_alias = _work_order_db_alias(work_order)
    latest_bom = get_latest_active_bom_for_work_order(work_order)
    impact_bom = None
    impact_bom_id = getattr(work_order, "bom_change_latest_bom_id", None)
    if impact_bom_id:
        impact_bom = BillOfMaterial.objects.using(db_alias).filter(id=impact_bom_id).first()
    impact_bom = impact_bom or latest_bom
    status_value = getattr(work_order, "bom_change_status", "none") or "none"
    totals = get_workorder_production_totals(work_order)
    action_required = status_value == "action_required" and bool(impact_bom)
    return {
        "status": status_value,
        "action_required": action_required,
        "latest_bom_id": impact_bom.id if impact_bom else (latest_bom.id if latest_bom else None),
        "latest_bom_version": impact_bom.version if impact_bom else (latest_bom.version if latest_bom else ""),
        "detected_at": (
            work_order.bom_change_detected_at.isoformat()
            if getattr(work_order, "bom_change_detected_at", None)
            else None
        ),
        "decision_at": (
            work_order.bom_change_decision_at.isoformat()
            if getattr(work_order, "bom_change_decision_at", None)
            else None
        ),
        "decision_note": getattr(work_order, "bom_change_decision_note", "") or "",
        "replacement_wo_id": getattr(work_order, "bom_change_replacement_wo_id", None),
        "scrapped_qty": int(getattr(work_order, "bom_change_scrapped_qty", 0) or 0),
        "reported_qty": totals["reported"],
        "approved_qty": totals["approved"],
        "pending_qty": totals["pending"],
        "has_started": work_order_has_started_production(work_order),
    }


def clear_work_order_assignment_for_bom_change(work_order):
    work_order.machine = None
    work_order.stage = None
    work_order.current_stage = None
    work_order.assigned_worker = None
    work_order.assignment_type = "auto"
    work_order.start_date = None
    work_order.end_date = None
    work_order.scheduled_start_date = None
    work_order.supervisor_start_at = None
    work_order.worker_start_at = None


def flag_bom_change_impact(new_bom, actor=None):
    if not new_bom or not getattr(new_bom, "product_id", None) or new_bom.status != "active":
        return 0

    db_alias = getattr(getattr(new_bom, "_state", None), "db", None) or "default"
    now = timezone.now()
    impacted = (
        WorkOrder.objects.using(db_alias)
        .filter(bom__product_id=new_bom.product_id, parent__isnull=True)
        .exclude(bom_id=new_bom.id)
        .exclude(status__in=["completed", "canceled", "archived"])
    )
    count = 0
    for work_order in impacted:
        previous_status = work_order.bom_change_status
        previous_latest_id = work_order.bom_change_latest_bom_id
        work_order.bom_change_status = "action_required"
        work_order.bom_change_latest_bom = new_bom
        work_order.bom_change_detected_at = now
        work_order.bom_change_decision_at = None
        work_order.bom_change_decision_by = None
        work_order.bom_change_decision_note = ""
        work_order.bom_change_replacement_wo = None
        work_order.bom_change_scrapped_qty = 0
        work_order.save(
            using=db_alias,
            update_fields=[
                "bom_change_status",
                "bom_change_latest_bom",
                "bom_change_detected_at",
                "bom_change_decision_at",
                "bom_change_decision_by",
                "bom_change_decision_note",
                "bom_change_replacement_wo",
                "bom_change_scrapped_qty",
            ],
        )
        if previous_status != "action_required" or previous_latest_id != new_bom.id:
            count += 1
            try:
                from .models import WorkOrderChangeLog

                WorkOrderChangeLog.objects.using(db_alias).create(
                    work_order=work_order,
                    changed_by=actor,
                    action="BOM change action required",
                    field_name="bom_change_status",
                    old_value=previous_status,
                    new_value="action_required",
                    note=f"Latest active BOM is {new_bom.version}; WO keeps {work_order.bom_version or 'old'} until a planner decision.",
                )
            except Exception:
                pass
    return count


def get_apply_latest_bom_eligibility(work_order):
    if not work_order:
        return False, "Work order is required."

    if getattr(work_order, "parent_id", None):
        return False, "Apply the latest BOM from the parent work order, not a stage task."

    if getattr(work_order, "bom_change_status", "") == "action_required" and work_order_has_started_production(work_order):
        return False, "Production already started. Choose a BOM change decision for this work order."

    if getattr(work_order, "status", None) != "pending":
        return False, "Only pending work orders can apply a newer BOM."

    if getattr(work_order, "assigned_worker_id", None):
        return False, "Unassign the worker before applying a newer BOM."

    if getattr(work_order, "released_qty", 0):
        return False, "Work already released to another stage cannot change BOM."

    if work_order.production_logs.exists():
        return False, "Production has already been logged for this work order."

    latest_bom = get_latest_active_bom_for_work_order(work_order)
    if not latest_bom:
        return False, "No newer active BOM version is available for this product."

    return True, ""


def apply_latest_bom_to_work_order(work_order, actor=None):
    eligible, reason = get_apply_latest_bom_eligibility(work_order)
    if not eligible:
        raise ValueError(reason)

    latest_bom = get_latest_active_bom_for_work_order(work_order)
    previous_bom = work_order.bom
    previous_version = work_order.bom_version or (previous_bom.version if previous_bom else "")
    had_route_plan = work_order.sub_tasks.exists() or work_order.stages.exists()

    if had_route_plan:
        work_order.sub_tasks.all().delete()
        work_order.stages.all().delete()
    clear_work_order_assignment_for_bom_change(work_order)

    work_order.bom = latest_bom
    work_order.product_name = latest_bom.product.name if latest_bom.product else work_order.product_name
    work_order.bom_version = latest_bom.version or ""
    work_order.bom_snapshot = WorkOrder.build_bom_snapshot(latest_bom)
    work_order.material_readiness_status = "not_checked"
    work_order.material_shortage_note = ""
    work_order.material_available_qty = None
    work_order.material_available_percent = None
    work_order.material_expected_delivery_date = None
    work_order.material_readiness_updated_at = None
    work_order.material_readiness_updated_by = None
    work_order.bom_change_status = "latest_applied"
    work_order.bom_change_latest_bom = None
    work_order.bom_change_decision_at = timezone.now()
    work_order.bom_change_decision_by = actor
    work_order.bom_change_decision_note = "Latest active BOM applied before production start."
    work_order.save(update_fields=[
        "machine",
        "stage",
        "current_stage",
        "assigned_worker",
        "assignment_type",
        "start_date",
        "end_date",
        "scheduled_start_date",
        "supervisor_start_at",
        "worker_start_at",
        "bom",
        "product_name",
        "bom_version",
        "bom_snapshot",
        "material_readiness_status",
        "material_shortage_note",
        "material_available_qty",
        "material_available_percent",
        "material_expected_delivery_date",
        "material_readiness_updated_at",
        "material_readiness_updated_by",
        "bom_change_status",
        "bom_change_latest_bom",
        "bom_change_decision_at",
        "bom_change_decision_by",
        "bom_change_decision_note",
    ])

    return {
        "previous_bom": previous_bom,
        "previous_version": previous_version,
        "new_bom": latest_bom,
        "new_version": latest_bom.version or "",
        "route_plan_cleared": had_route_plan,
    }


def decide_bom_change_archive_and_replace(work_order, actor=None, note=""):
    db_alias = _work_order_db_alias(work_order)
    latest_bom = None
    if getattr(work_order, "bom_change_latest_bom_id", None):
        latest_bom = BillOfMaterial.objects.using(db_alias).filter(id=work_order.bom_change_latest_bom_id).first()
    latest_bom = latest_bom or get_latest_active_bom_for_work_order(work_order)
    if not latest_bom:
        raise ValueError("No newer active BOM version is available.")
    if getattr(work_order, "bom_change_status", None) != "action_required":
        raise ValueError("This work order does not require a BOM change decision.")

    with transaction.atomic(using=db_alias):
        locked = WorkOrder.objects.using(db_alias).select_for_update().get(id=work_order.id)
        latest_product_name = (
            Product.objects.using(db_alias)
            .filter(id=latest_bom.product_id)
            .values_list("name", flat=True)
            .first()
        )
        replacement = WorkOrder.objects.using(db_alias).create(
            company_id=locked.company_id,
            product_name=latest_product_name or locked.product_name,
            bom_id=latest_bom.id,
            quantity=locked.quantity,
            base_quantity=locked.base_quantity,
            customer_id=locked.customer_id,
            status="pending",
            due_date=locked.due_date,
            priority=locked.priority,
            assigned_to_id=getattr(actor, "id", None) or locked.assigned_to_id,
            operation_flow_mode=locked.operation_flow_mode,
            instructions=locked.instructions,
            order_type=locked.order_type,
        )
        locked.status = "archived"
        locked.end_date = timezone.now()
        locked.bom_change_status = "archived_replaced"
        locked.bom_change_decision_at = timezone.now()
        locked.bom_change_decision_by = actor
        locked.bom_change_decision_note = note or "Archived old BOM work order and created replacement with latest BOM."
        locked.bom_change_replacement_wo = replacement
        locked.save(
            using=db_alias,
            update_fields=[
                "status",
                "end_date",
                "bom_change_status",
                "bom_change_decision_at",
                "bom_change_decision_by",
                "bom_change_decision_note",
                "bom_change_replacement_wo",
            ],
        )
    return replacement


def decide_bom_change_scrap_and_apply(work_order, actor=None, note=""):
    db_alias = _work_order_db_alias(work_order)
    latest_bom = None
    if getattr(work_order, "bom_change_latest_bom_id", None):
        latest_bom = BillOfMaterial.objects.using(db_alias).filter(id=work_order.bom_change_latest_bom_id).first()
    latest_bom = latest_bom or get_latest_active_bom_for_work_order(work_order)
    if not latest_bom:
        raise ValueError("No newer active BOM version is available.")
    if getattr(work_order, "bom_change_status", None) != "action_required":
        raise ValueError("This work order does not require a BOM change decision.")

    totals = get_workorder_production_totals(work_order)
    scrapped_qty = int(totals["reported"] or 0)
    with transaction.atomic(using=db_alias):
        locked = WorkOrder.objects.using(db_alias).select_for_update().get(id=work_order.id)
        previous_bom = BillOfMaterial.objects.using(db_alias).filter(id=locked.bom_id).first() if locked.bom_id else None
        previous_version = locked.bom_version or (previous_bom.version if previous_bom else "")
        latest_product_name = (
            Product.objects.using(db_alias)
            .filter(id=latest_bom.product_id)
            .values_list("name", flat=True)
            .first()
        )
        if scrapped_qty > 0:
            locked.base_quantity = locked.base_quantity if locked.base_quantity is not None else int(locked.quantity or 0)
        archived_child_ids = list(
            WorkOrder.objects.using(db_alias)
            .filter(parent_id=locked.id)
            .exclude(status__in=["canceled", "archived"])
            .values_list("id", flat=True)
        )
        if archived_child_ids:
            WorkOrder.objects.using(db_alias).filter(id__in=archived_child_ids).update(
                status="archived",
                end_date=timezone.now(),
                assigned_worker=None,
                assignment_type="auto",
            )
        locked.stages.all().delete()
        locked.bom = latest_bom
        locked.product_name = latest_product_name or locked.product_name
        locked.bom_version = latest_bom.version or ""
        locked.bom_snapshot = WorkOrder.build_bom_snapshot(latest_bom)
        locked.status = "pending"
        clear_work_order_assignment_for_bom_change(locked)
        locked.material_readiness_status = "not_checked"
        locked.material_shortage_note = ""
        locked.material_available_qty = None
        locked.material_available_percent = None
        locked.material_expected_delivery_date = None
        locked.material_readiness_updated_at = None
        locked.material_readiness_updated_by = None
        locked.bom_change_status = "scrap_applied"
        locked.bom_change_latest_bom = None
        locked.bom_change_decision_at = timezone.now()
        locked.bom_change_decision_by = actor
        locked.bom_change_decision_note = note or f"Scrapped reported quantity ({scrapped_qty}) and applied latest BOM."
        locked.bom_change_scrapped_qty = scrapped_qty
        locked.save(
            using=db_alias,
            update_fields=[
                "base_quantity",
                "bom",
                "product_name",
                "bom_version",
                "bom_snapshot",
                "status",
                "machine",
                "stage",
                "current_stage",
                "assigned_worker",
                "assignment_type",
                "start_date",
                "end_date",
                "scheduled_start_date",
                "supervisor_start_at",
                "worker_start_at",
                "material_readiness_status",
                "material_shortage_note",
                "material_available_qty",
                "material_available_percent",
                "material_expected_delivery_date",
                "material_readiness_updated_at",
                "material_readiness_updated_by",
                "bom_change_status",
                "bom_change_latest_bom",
                "bom_change_decision_at",
                "bom_change_decision_by",
                "bom_change_decision_note",
                "bom_change_scrapped_qty",
            ],
        )
    return {
        "previous_bom": previous_bom,
        "previous_version": previous_version,
        "new_bom": latest_bom,
        "new_version": latest_bom.version or "",
        "scrapped_qty": scrapped_qty,
        "archived_child_ids": archived_child_ids,
    }


def decide_bom_change_continue_old(work_order, actor=None, note=""):
    if getattr(work_order, "bom_change_status", None) != "action_required":
        raise ValueError("This work order does not require a BOM change decision.")
    db_alias = _work_order_db_alias(work_order)
    work_order.bom_change_status = "ignored"
    work_order.bom_change_decision_at = timezone.now()
    work_order.bom_change_decision_by = actor
    work_order.bom_change_decision_note = note or "Planner accepted continuing production with the old BOM version."
    work_order.save(
        using=db_alias,
        update_fields=[
            "bom_change_status",
            "bom_change_decision_at",
            "bom_change_decision_by",
            "bom_change_decision_note",
        ],
    )
    return work_order


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


def normalize_operation_flow_mode(value, default='series'):
    normalized = str(value or '').strip().lower()
    if normalized not in {'series', 'parallel'}:
        normalized = default
    return normalized


def get_company_default_operation_flow_mode(company):
    if not company:
        return 'series'
    # Backward-compatible fallback for tenants running older schema/code mixes.
    # If this field is unavailable at runtime, keep planner/supervisor endpoints alive.
    try:
        SystemSettings._meta.get_field('default_operation_flow_mode')
    except Exception:
        return 'series'

    try:
        mode = (
            SystemSettings.objects.filter(company=company)
            .values_list('default_operation_flow_mode', flat=True)
            .first()
        )
    except Exception:
        return 'series'
    return normalize_operation_flow_mode(mode)


def get_work_order_operation_flow_mode(work_order):
    if not work_order:
        return 'series'
    root_work_order = work_order.parent if getattr(work_order, 'parent_id', None) else work_order
    explicit_mode = normalize_operation_flow_mode(
        getattr(root_work_order, 'operation_flow_mode', None),
        default='',
    )
    if explicit_mode:
        return explicit_mode
    company = getattr(root_work_order, 'company', None)
    return get_company_default_operation_flow_mode(company)


class BOMService:
    @staticmethod
    def calculate_cost(bom: BillOfMaterial, visited_ids=None):
        """
        Recursively calculate BOM cost.
        Detects infinite loops.
        """
        if visited_ids is None:
            visited_ids = set()
        
        if bom.id in visited_ids:
            raise RecursionError(f"Circular dependency detected in BOM {bom.id}")
        
        visited_ids.add(bom.id)
        
        total = Decimal(0)
        
        for comp in bom.components.all():
            line_cost = comp.total_cost() # Base logic handling scrap strategies
            
            # If this component is a sub-assembly (Semi-Finished) AND has a linked BOM
            if comp.sub_bom:
                sub_cost = BOMService.calculate_cost(comp.sub_bom, visited_ids.copy()) # Cost for 1 batch of Sub-BOM
                
                # Normalize Units
                # Sub-BOM Base Qty & Unit -> Component Qty & Unit
                # Scaled Cost = (SubCost / SubBaseQty) * ComponentQty (Normalized)
                
                try:
                    # Convert Component Qty to Sub-BOM Unit
                    normalized_qty = UnitService.convert(comp.quantity, comp.unit, comp.sub_bom.uom)
                    
                    if comp.sub_bom.base_quantity > 0:
                        unit_cost_derived = sub_cost / comp.sub_bom.base_quantity
                        line_cost = unit_cost_derived * normalized_qty
                    else:
                        line_cost = sub_cost # Fallback logic
                    
                except ValueError:
                    # Fallback if units incompatible
                    if comp.cost_per_unit > 0:
                        line_cost = comp.quantity * comp.cost_per_unit
                    else:
                        # Log warning instead of crash?
                        line_cost = Decimal(0)

            total += line_cost
            
        return total

    @staticmethod
    def calculate_requirements(bom: BillOfMaterial, target_quantity):
        """
        Scale materials based on Target Quantity.
        Rule: scaled_qty = (target_qty / base_quantity) * line_qty
        """
        target_quantity = Decimal(str(target_quantity)) # Handle float/int input safely
        
        if bom.base_quantity <= 0:
            raise ValueError("BOM Base Quantity must be > 0")

        ratio = target_quantity / bom.base_quantity
        requirements = []

        for comp in bom.components.all():
            required_qty = comp.quantity * ratio
            
            # Also scale wastage
            wastage_qty = comp.wastage_quantity * ratio
            
            requirements.append({
                'material_name': comp.material_name,
                'required_qty': required_qty,
                'wastage_qty': wastage_qty,
                'unit': comp.unit,
                'scrap_type': comp.scrap_type
            })
            
        return requirements

    @staticmethod
    def validate_structure(bom: BillOfMaterial):
        """Check for loops before activating."""
        try:
            BOMService.calculate_cost(bom)
            return True, "Valid"
        except RecursionError as e:
            return False, str(e)

    @staticmethod
    def simulate_run(bom, quantity):
        """
        🔮 Simulate a production run.
        Returns: {
            'estimated_cost': Decimal,
            'estimated_time_hours': float,
            'bottleneck_machine': str
        }
        """
        from datetime import timedelta
        
        # 1. Material Cost
        unit_cost = BOMService.calculate_cost(bom)
        total_material_cost = unit_cost * Decimal(quantity)
        
        # 2. Operational Time & Cost
        total_time_minutes = 0
        max_stage_time = 0
        bottleneck = "None"
        
        # Mock Machine Rates (In real app, add hourly_rate to Machine model)
        MACHINE_RATES = {
            'Default': 50.00, # $50/hr
            'CNC': 120.00,
            'Assembly': 40.00
        }
        
        operational_cost = Decimal(0)
        
        for op in bom.operations.all():
            # Time = (Setup + (CycleTime * Qty)) - Simplification: (Duration * Qty) / BatchSize if batching
            # Let's assume duration is per unit for simplicity in this demo, or fixed block.
            # Usually BOM op duration is for 'Base Qty'.
            
            # Scale time: (TargetQty / BaseQty) * OpDuration
            if bom.base_quantity > 0:
                ratio = Decimal(quantity) / bom.base_quantity
            else:
                ratio = Decimal(quantity) # Safety net
                
            stage_time = float(op.duration_minutes) * float(ratio)
            
            total_time_minutes += stage_time
            
            # Bottleneck Analysis (Longest Stage)
            if stage_time > max_stage_time:
                max_stage_time = stage_time
                bottleneck = op.machine.display_label if op.machine else (op.stage.name if op.stage else "Unknown Phase")
            
            # Machine Cost
            m_type = op.machine.type if op.machine else 'Default'
            rate = Decimal(MACHINE_RATES.get(m_type, MACHINE_RATES['Default']))
            operational_cost += (Decimal(stage_time) / 60) * rate

        return {
            'estimated_cost': round(total_material_cost + operational_cost, 2),
            'material_cost': round(total_material_cost, 2),
            'operational_cost': round(operational_cost, 2),
            'estimated_time_hours': round(total_time_minutes / 60, 1),
            'bottleneck': bottleneck
        }


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


class WorkOrderLifecycleError(ValueError):
    """Raised when a WO lifecycle transition violates strict workflow rules."""


class WorkOrderLifecycle:
    """
    Central lifecycle/state-machine for production work orders.

    Strict workflow:
        pending -> in_progress -> completed

    Close is represented by `closed_by_planner=True` on a completed parent WO.
    """

    STRICT_STATUSES = ("pending", "in_progress", "completed")
    TRANSITIONS = {
        "pending": {"in_progress"},
        "in_progress": {"completed"},
        "completed": set(),
    }
    TRANSITION_OWNERS = {
        "in_progress": {"worker", "supervisor", "admin"},
        "completed": {"supervisor", "admin"},
    }
    CLOSE_OWNERS = {"planner", "admin"}

    @staticmethod
    def _normalize_status(status_value):
        return str(status_value or "").strip().lower()

    @staticmethod
    def _resolve_actor_roles(actor):
        roles = set()
        if not actor:
            return roles

        if getattr(actor, "is_superuser", False):
            roles.add("admin")

        # Fast path: profile attached on user instance.
        profile = getattr(actor, "profile", None)
        role_name = None
        if profile and getattr(profile, "role", None):
            role_name = getattr(profile.role, "name", None)

        # Fallback path: resolve role from DB safely.
        if not role_name and getattr(actor, "id", None):
            try:
                from accounts.models import Profile

                db_alias = getattr(getattr(actor, "_state", None), "db", None) or "default"
                role_name = (
                    Profile.objects.using(db_alias)
                    .filter(user_id=actor.id, role_id__isnull=False)
                    .values_list("role__name", flat=True)
                    .first()
                )
            except Exception:
                role_name = None

        if role_name:
            roles.add(str(role_name).strip().lower())

        return roles

    @staticmethod
    def _assert_actor_role(actor, allowed_roles, action_label, allow_system=False):
        if allow_system and not actor:
            return

        actor_roles = WorkOrderLifecycle._resolve_actor_roles(actor)
        if actor_roles.intersection(set(allowed_roles or [])):
            return

        raise WorkOrderLifecycleError(
            f"Only {', '.join(sorted(allowed_roles))} can {action_label}."
        )

    @staticmethod
    def _has_pending_qc(work_order):
        from .models import QualityCheck

        return QualityCheck.objects.filter(
            Q(work_order=work_order) | Q(work_order__parent=work_order),
            status="new",
        ).exists()

    @staticmethod
    def validate_transition(
        work_order,
        target_status,
        actor=None,
        allow_system=False,
        enforce_guards=True,
        allow_reopen=False,
    ):
        current = WorkOrderLifecycle._normalize_status(getattr(work_order, "status", ""))
        target = WorkOrderLifecycle._normalize_status(target_status)

        if not target:
            raise WorkOrderLifecycleError("Target status is required.")

        if target == current:
            return

        if target not in WorkOrderLifecycle.STRICT_STATUSES:
            raise WorkOrderLifecycleError(
                f"Status '{target}' is outside strict WO lifecycle."
            )
        if current not in WorkOrderLifecycle.STRICT_STATUSES:
            raise WorkOrderLifecycleError(
                f"Cannot transition from '{current}'. Work order is outside strict WO lifecycle."
            )

        allowed_targets = set(WorkOrderLifecycle.TRANSITIONS.get(current, set()))
        if allow_reopen and current == "completed" and target == "in_progress":
            allowed_targets.add("in_progress")

        if target not in allowed_targets:
            raise WorkOrderLifecycleError(
                f"Invalid WO transition: {current} -> {target}."
            )

        allowed_roles = WorkOrderLifecycle.TRANSITION_OWNERS.get(target)
        if allowed_roles:
            WorkOrderLifecycle._assert_actor_role(
                actor,
                allowed_roles,
                f"move WO to '{target}'",
                allow_system=allow_system,
            )

        if not enforce_guards:
            return

        if target == "in_progress":
            if getattr(work_order, "closed_by_planner", False):
                raise WorkOrderLifecycleError("Closed work order cannot be re-opened.")

        if target == "completed":
            if work_order.sub_tasks.exclude(status="completed").exists():
                raise WorkOrderLifecycleError(
                    "Cannot complete WO while child stage tasks are still open."
                )
            if WorkOrderLifecycle._has_pending_qc(work_order):
                raise WorkOrderLifecycleError(
                    "Cannot complete WO while QC is pending."
                )

    @staticmethod
    def apply_transition(
        work_order,
        target_status,
        actor=None,
        allow_system=False,
        enforce_guards=True,
        allow_reopen=False,
        save=True,
        update_fields=None,
    ):
        WorkOrderLifecycle.validate_transition(
            work_order=work_order,
            target_status=target_status,
            actor=actor,
            allow_system=allow_system,
            enforce_guards=enforce_guards,
            allow_reopen=allow_reopen,
        )

        target = WorkOrderLifecycle._normalize_status(target_status)
        if target == work_order.status:
            if save and update_fields:
                work_order.save(update_fields=list(dict.fromkeys(update_fields)))
            return False

        work_order.status = target
        if save:
            fields = ["status"]
            if update_fields:
                for field in update_fields:
                    if field != "status":
                        fields.append(field)
            work_order.save(update_fields=fields)
        return True

    @staticmethod
    def validate_close(work_order, actor=None, allow_system=False, require_ready=True):
        if getattr(work_order, "parent_id", None):
            raise WorkOrderLifecycleError("Only parent work orders can be closed.")

        WorkOrderLifecycle._assert_actor_role(
            actor,
            WorkOrderLifecycle.CLOSE_OWNERS,
            "close work orders",
            allow_system=allow_system,
        )

        status_value = WorkOrderLifecycle._normalize_status(getattr(work_order, "status", ""))
        if status_value != "completed":
            raise WorkOrderLifecycleError(
                "WO must be completed before planner close."
            )

        if work_order.sub_tasks.exclude(status="completed").exists():
            raise WorkOrderLifecycleError(
                "Cannot close WO while child stage tasks are still open."
            )

        if WorkOrderLifecycle._has_pending_qc(work_order):
            raise WorkOrderLifecycleError("Cannot close WO while QC is pending.")

        if require_ready and not getattr(work_order, "planner_action_required", False):
            raise WorkOrderLifecycleError(
                "WO is not ready to close. Final stage not approved yet."
            )

        if getattr(work_order, "store_receipt_status", "not_requested") != "received":
            raise WorkOrderLifecycleError(
                "Store must confirm received quantity and scrap before planner close."
            )

    @staticmethod
    def close(work_order, actor=None, allow_system=False, require_ready=True, save=True):
        WorkOrderLifecycle.validate_close(
            work_order=work_order,
            actor=actor,
            allow_system=allow_system,
            require_ready=require_ready,
        )

        from django.utils import timezone

        close_at = timezone.now()
        changed = False
        update_fields = []
        if getattr(work_order, "planner_action_required", False):
            work_order.planner_action_required = False
            update_fields.append("planner_action_required")
            changed = True
        if not getattr(work_order, "closed_by_planner", False):
            work_order.closed_by_planner = True
            update_fields.append("closed_by_planner")
            changed = True
        if WorkOrderLifecycle._normalize_status(getattr(work_order, "status", "")) != "completed":
            work_order.status = "completed"
            update_fields.append("status")
            changed = True
        if not getattr(work_order, "end_date", None) or work_order.end_date > close_at:
            work_order.end_date = close_at
            update_fields.append("end_date")
            changed = True

        if save and changed:
            work_order.save(update_fields=list(dict.fromkeys(update_fields)))
            work_order.sub_tasks.filter(
                status="completed",
                end_date__gt=close_at,
            ).update(end_date=close_at)
        return changed


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


class WorkOrderService:
    @staticmethod
    def annotate_qc_metrics(queryset):
        """
        Annotate approved/QC quantities without join multiplication.
        """
        from django.db.models import (
            Sum,
            F,
            IntegerField,
            ExpressionWrapper,
            OuterRef,
            Subquery,
            Exists,
        )
        from django.db.models.functions import Coalesce
        from .models import ProductionLog, QualityCheck

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

    @staticmethod
    def sync_parent_completion(company):
        """
        Ensure parent WOs reflect child completion and raise planner action flags.
        This reconciles edge cases where stage approvals happened without triggering
        create_next_stage_task (e.g., legacy logs).
        """
        from django.utils import timezone
        from .models import WorkOrder, QualityCheck

        parents = list(WorkOrder.objects.filter(
            company=company,
            sub_tasks__isnull=False
        ).distinct())

        # Run a few passes so nested parents can propagate completion upward.
        changed = True
        passes = 0
        while changed and passes < 5:
            changed = False
            passes += 1
            for parent in parents:
                if parent.status in ['canceled', 'archived']:
                    continue
                if parent.status not in WorkOrderLifecycle.STRICT_STATUSES:
                    continue

                # Block completion if QC is still pending for any child
                if QualityCheck.objects.filter(work_order__parent=parent, status='new').exists():
                    if parent.planner_action_required:
                        parent.planner_action_required = False
                        parent.save(update_fields=['planner_action_required'])
                    continue

                # If any child is not completed, parent cannot be completed.
                if parent.sub_tasks.exclude(status='completed').exists():
                    continue

                # If BOM has more stages beyond current, do not complete parent yet
                if (
                    get_work_order_operation_flow_mode(parent) != 'parallel'
                    and parent.bom
                    and parent.bom.operations.exists()
                ):
                    stage_id_for_flow = parent.current_stage_id or parent.stage_id
                    if not stage_id_for_flow:
                        ops = list(parent.bom.operations.exclude(stage_id__isnull=True).values('stage_id', 'order'))
                        if ops:
                            order_map = {}
                            for op in ops:
                                if op['stage_id'] not in order_map:
                                    order_map[op['stage_id']] = op['order']
                            completed_stage_ids = list(
                                parent.sub_tasks.filter(status='completed', stage_id__isnull=False)
                                .values_list('stage_id', flat=True)
                            )
                            if completed_stage_ids:
                                stage_id_for_flow = max(
                                    completed_stage_ids,
                                    key=lambda sid: order_map.get(sid, 0)
                                )
                    if stage_id_for_flow:
                        next_stage = WorkOrderService.get_next_stage(parent.bom, stage_id_for_flow)
                        if next_stage:
                            if parent.planner_action_required:
                                parent.planner_action_required = False
                                parent.save(update_fields=['planner_action_required'])
                            continue

                is_new_completion = parent.status != 'completed'
                should_notify = parent.parent_id is None and is_new_completion

                if is_new_completion:
                    current_status = WorkOrderLifecycle._normalize_status(parent.status)
                    try:
                        if current_status == 'pending':
                            WorkOrderLifecycle.apply_transition(
                                parent,
                                'in_progress',
                                actor=None,
                                allow_system=True,
                                save=False
                            )
                            changed = True
                            current_status = WorkOrderLifecycle._normalize_status(parent.status)

                        if current_status == 'in_progress':
                            WorkOrderLifecycle.apply_transition(
                                parent,
                                'completed',
                                actor=None,
                                allow_system=True,
                                save=False
                            )
                            changed = True
                    except WorkOrderLifecycleError:
                        continue
                if parent.progress != 100:
                    parent.progress = 100
                    changed = True
                completed_child_end = parent.sub_tasks.filter(
                    status='completed',
                    end_date__isnull=False,
                ).order_by('-end_date').values_list('end_date', flat=True).first()
                actual_completion_at = completed_child_end or timezone.now()
                if not parent.end_date or parent.end_date != actual_completion_at:
                    parent.end_date = actual_completion_at
                    changed = True
                # Only raise planner action on fresh completion; never re-open after planner closes
                if parent.parent_id is None and is_new_completion and not getattr(parent, 'closed_by_planner', False):
                    parent.planner_action_required = True
                    changed = True

                parent.save(update_fields=['status', 'progress', 'end_date', 'planner_action_required'])
                if parent.parent_id is None and getattr(parent, 'planner_action_required', False):
                    request_store_receipt_for_work_order(parent)

                if should_notify:
                    NotificationService.notify_role(
                        parent.company,
                        roles=['planner', 'admin'],
                        title="WO completed",
                        message=f"WO #{parent.id} final stage approved. Planner action required: close the order.",
                        link=f"/manufacturing/dashboard/?wo={parent.id}"
                    )
    @staticmethod
    def _ordered_bom_stages(bom):
        if not bom:
            return []
        stages = []
        seen = set()
        for op in bom.operations.select_related('stage').order_by('order'):
            if not op.stage_id or op.stage_id in seen:
                continue
            seen.add(op.stage_id)
            stages.append(op.stage)
        return stages

    @staticmethod
    def _fallback_next_stage(parent, bom, current_stage_id):
        """
        Fallback for cases where the task stage is not a BOM stage ID.
        Prevents prematurely completing the parent when BOM still has pending stages.
        """
        ordered_stages = WorkOrderService._ordered_bom_stages(bom)
        if not ordered_stages:
            return None

        ordered_ids = [s.id for s in ordered_stages]
        if current_stage_id in ordered_ids:
            return None

        completed_stage_ids = set(
            parent.sub_tasks.filter(status='completed', stage_id__isnull=False).values_list('stage_id', flat=True)
        )
        for stage in ordered_stages:
            if stage.id not in completed_stage_ids:
                return stage
        return None

    @staticmethod
    def get_next_stage(bom, current_stage_id):
        if not bom:
            return None

        if not current_stage_id:
            return None

        ordered_stages = WorkOrderService._ordered_bom_stages(bom)
        if not ordered_stages:
            return None

        found_index = None
        for idx, stage in enumerate(ordered_stages):
            if stage.id == current_stage_id:
                found_index = idx
                break

        if found_index is None:
            return None
        if found_index + 1 < len(ordered_stages):
            return ordered_stages[found_index + 1]
        return None

    @staticmethod
    def _get_stage_from_ids(stage_work_order, current_stage_id):
        stage = None
        if stage_work_order.stage_id:
            stage = stage_work_order.stage
        elif current_stage_id:
            from .models import ProductionStage
            stage = ProductionStage.objects.filter(id=current_stage_id).first()
        return stage

    @staticmethod
    def _is_qc_required(stage_work_order, bom=None, current_stage_id=None):
        if getattr(stage_work_order, 'qc_requirement', False):
            return True
        stage = WorkOrderService._get_stage_from_ids(stage_work_order, current_stage_id)
        if stage and getattr(stage, 'is_quality_check', False):
            return True
        return False

    @staticmethod
    def _get_scrap_source_qc_id(stage_work_order, max_hops=12):
        if not stage_work_order:
            return None

        # First prefer the direct source on this task, then its source-task chain.
        current = stage_work_order
        visited = set()
        hops = 0
        while current is not None and hops < max_hops:
            current_id = getattr(current, 'id', None)
            if current_id in visited:
                break
            visited.add(current_id)
            qc_id = getattr(current, 'scrap_source_quality_check_id', None)
            if qc_id:
                return int(qc_id)
            current = getattr(current, 'source_task', None)
            hops += 1

        # Fallback to parent chain for legacy rows where source_task wasn't set.
        current = getattr(stage_work_order, 'parent', None)
        visited = set()
        hops = 0
        while current is not None and hops < max_hops:
            current_id = getattr(current, 'id', None)
            if current_id in visited:
                break
            visited.add(current_id)
            qc_id = getattr(current, 'scrap_source_quality_check_id', None)
            if qc_id:
                return int(qc_id)
            current = getattr(current, 'parent', None)
            hops += 1
        return None

    @staticmethod
    def _is_scrap_compensation_flow(stage_work_order, max_hops=12):
        if not stage_work_order:
            return False
        if bool(getattr(stage_work_order, 'is_scrap_compensation_task', False)):
            return True
        if getattr(stage_work_order, 'scrap_source_quality_check_id', None):
            return True

        # Legacy fallback: many older rows are labeled but flag wasn't propagated.
        name = (getattr(stage_work_order, 'product_name', '') or '').lower()
        if 'scrap compensation' in name:
            return True

        # Source-task chain.
        current = getattr(stage_work_order, 'source_task', None)
        visited = set()
        hops = 0
        while current is not None and hops < max_hops:
            current_id = getattr(current, 'id', None)
            if current_id in visited:
                break
            visited.add(current_id)
            if bool(getattr(current, 'is_scrap_compensation_task', False)):
                return True
            if getattr(current, 'scrap_source_quality_check_id', None):
                return True
            current_name = (getattr(current, 'product_name', '') or '').lower()
            if 'scrap compensation' in current_name:
                return True
            current = getattr(current, 'source_task', None)
            hops += 1

        # Parent chain.
        current = getattr(stage_work_order, 'parent', None)
        visited = set()
        hops = 0
        while current is not None and hops < max_hops:
            current_id = getattr(current, 'id', None)
            if current_id in visited:
                break
            visited.add(current_id)
            if bool(getattr(current, 'is_scrap_compensation_task', False)):
                return True
            if getattr(current, 'scrap_source_quality_check_id', None):
                return True
            current_name = (getattr(current, 'product_name', '') or '').lower()
            if 'scrap compensation' in current_name:
                return True
            current = getattr(current, 'parent', None)
            hops += 1

        return False

    @staticmethod
    def _has_pending_qc(stage_work_order):
        from .models import QualityCheck
        return QualityCheck.objects.filter(work_order=stage_work_order, status='new').exists()

    @staticmethod
    def _ensure_qc_record(stage_work_order):
        from .models import QualityCheck
        existing = QualityCheck.objects.filter(work_order=stage_work_order, status='new')
        if existing.exists():
            return True
        # Only create a new QC record when there is approved qty that is not yet checked
        from .models import WorkOrder
        metrics = WorkOrderService.annotate_qc_metrics(
            WorkOrder.objects.filter(id=stage_work_order.id)
        ).first()
        approved_qty = int(getattr(metrics, 'approved_qty', 0) or 0)
        qc_checked = int(getattr(metrics, 'qc_checked', 0) or 0)
        if approved_qty <= qc_checked:
            return False
        QualityCheck.objects.create(work_order=stage_work_order, status='new')
        if not stage_work_order.quality_start_at:
            from django.utils import timezone
            stage_work_order.quality_start_at = timezone.now()
            stage_work_order.save(update_fields=['quality_start_at'])
        return True

    @staticmethod
    def compensate_scrap_from_quality_check(quality_check, compensation_qty, user):
        """
        Planner action:
        Increase WO target quantity by scrap quantity after QC,
        and create a replenishment stage task when QC belongs to a stage child WO.
        """
        from django.db import transaction
        from django.utils import timezone
        from .models import WorkOrder, WorkOrderChangeLog

        try:
            compensation_qty = int(compensation_qty)
        except Exception:
            raise ValueError("Compensation quantity must be a number.")

        if compensation_qty <= 0:
            raise ValueError("Compensation quantity must be positive.")

        stage_wo = quality_check.work_order
        if quality_check.status != 'processed':
            raise ValueError("Quality check must be approved before scrap compensation.")
        parent_wo = stage_wo.parent if stage_wo.parent_id else stage_wo
        if not parent_wo:
            raise ValueError("Could not determine target work order.")

        if parent_wo.status in ['canceled', 'archived']:
            raise ValueError("Cannot compensate scrap on an inactive work order.")

        if getattr(parent_wo, 'closed_by_planner', False):
            raise ValueError("Work order is already closed by planner.")

        faulty_qty = int(getattr(quality_check, 'faulty_quantity', 0) or 0)
        already_compensated = int(getattr(quality_check, 'scrap_compensated_qty', 0) or 0)
        remaining_scrap = max(faulty_qty - already_compensated, 0)
        if remaining_scrap <= 0:
            raise ValueError("No uncompensated scrap remaining for this quality check.")
        if compensation_qty > remaining_scrap:
            raise ValueError(f"Compensation exceeds available scrap ({remaining_scrap}).")

        stage_obj = stage_wo.stage or stage_wo.current_stage or parent_wo.current_stage or parent_wo.stage
        old_parent_qty = int(parent_wo.quantity or 0)
        old_parent_comp = int(getattr(parent_wo, 'scrap_compensation_qty', 0) or 0)

        with transaction.atomic():
            if parent_wo.base_quantity is None:
                parent_wo.base_quantity = max(old_parent_qty - old_parent_comp, 0)

            parent_update_fields = ['base_quantity', 'quantity', 'scrap_compensation_qty']
            parent_wo.quantity = old_parent_qty + compensation_qty
            parent_wo.scrap_compensation_qty = old_parent_comp + compensation_qty

            # If final-stage QC already marked parent complete, reopen it for replenishment flow.
            if parent_wo.status == 'completed':
                WorkOrderLifecycle.apply_transition(
                    parent_wo,
                    'in_progress',
                    actor=None,
                    allow_system=True,
                    allow_reopen=True,
                    save=False
                )
                parent_update_fields.append('status')
            if getattr(parent_wo, 'planner_action_required', False):
                parent_wo.planner_action_required = False
                parent_update_fields.append('planner_action_required')
            if parent_wo.end_date:
                parent_wo.end_date = None
                parent_update_fields.append('end_date')
            parent_wo.save(update_fields=parent_update_fields)

            compensation_task = None
            # For stage child WOs, create a replenishment task on the same stage.
            if stage_wo.parent_id:
                stage_name = stage_obj.name if stage_obj else "Current Stage"
                compensation_task = WorkOrder.objects.create(
                    parent=parent_wo,
                    company=parent_wo.company,
                    product_name=f"{parent_wo.product_name} - {stage_name} (Scrap Compensation)",
                    bom=parent_wo.bom,
                    quantity=compensation_qty,
                    customer=parent_wo.customer,
                    status='pending',
                    stage=stage_obj,
                    current_stage=stage_obj,
                    machine=None,
                    assigned_to=parent_wo.assigned_to or user,
                    priority=parent_wo.priority,
                    order_type=parent_wo.order_type,
                    instructions=(
                        f"{parent_wo.instructions or ''}\n"
                        f"[SCRAP COMPENSATION] QC #{quality_check.id} +{compensation_qty}"
                    ).strip(),
                    operation_flow_mode=get_work_order_operation_flow_mode(parent_wo),
                    qc_requirement=bool(getattr(stage_wo, 'qc_requirement', False)),
                    source_task=stage_wo,
                    is_scrap_compensation_task=True,
                    scrap_source_quality_check=quality_check
                )

            quality_check.scrap_compensated_qty = already_compensated + compensation_qty
            quality_check.scrap_compensated_by = user
            quality_check.scrap_compensated_at = timezone.now()
            quality_check.save(update_fields=[
                'scrap_compensated_qty',
                'scrap_compensated_by',
                'scrap_compensated_at'
            ])

            WorkOrderChangeLog.objects.create(
                work_order=parent_wo,
                changed_by=user,
                action="Scrap compensation applied",
                field_name="quantity",
                old_value=str(old_parent_qty),
                new_value=str(parent_wo.quantity),
                note=f"QC #{quality_check.id}: +{compensation_qty} due to scrap."
            )

            if compensation_task:
                WorkOrderChangeLog.objects.create(
                    work_order=compensation_task,
                    changed_by=user,
                    action="Compensation task created",
                    note=f"Created from QC #{quality_check.id} scrap compensation."
                )

        return {
            "parent_work_order": parent_wo,
            "compensation_task": compensation_task,
            "applied_qty": compensation_qty,
            "remaining_scrap": max(remaining_scrap - compensation_qty, 0),
        }

    @staticmethod
    def accept_scrap_from_quality_check(quality_check, accepted_qty, user):
        """
        Planner action:
        Accept scrap loss from QC without creating compensation work.
        This reduces the target WO quantity and marks the accepted scrap as resolved.
        """
        from django.db import transaction
        from django.utils import timezone
        from .models import WorkOrderChangeLog

        try:
            accepted_qty = int(accepted_qty)
        except Exception:
            raise ValueError("Accepted scrap quantity must be a number.")

        if accepted_qty <= 0:
            raise ValueError("Accepted scrap quantity must be positive.")

        stage_wo = quality_check.work_order
        if quality_check.status != 'processed':
            raise ValueError("Quality check must be approved before accepting scrap.")
        parent_wo = stage_wo.parent if stage_wo.parent_id else stage_wo
        if not parent_wo:
            raise ValueError("Could not determine target work order.")

        if parent_wo.status in ['canceled', 'archived']:
            raise ValueError("Cannot accept scrap on an inactive work order.")

        if getattr(parent_wo, 'closed_by_planner', False):
            raise ValueError("Work order is already closed by planner.")

        faulty_qty = int(getattr(quality_check, 'faulty_quantity', 0) or 0)
        already_compensated = int(getattr(quality_check, 'scrap_compensated_qty', 0) or 0)
        remaining_scrap = max(faulty_qty - already_compensated, 0)
        if remaining_scrap <= 0:
            raise ValueError("No uncompensated scrap remaining for this quality check.")
        if accepted_qty > remaining_scrap:
            raise ValueError(f"Accepted scrap exceeds available scrap ({remaining_scrap}).")

        old_parent_qty = int(parent_wo.quantity or 0)
        base_qty, compensation_qty, _ = get_workorder_quantity_breakdown(parent_wo)
        if accepted_qty > old_parent_qty:
            raise ValueError("Accepted scrap exceeds current work order quantity.")

        new_base_qty = max(int(base_qty) - accepted_qty, 0)
        new_parent_qty = new_base_qty + int(compensation_qty)

        with transaction.atomic():
            parent_wo.base_quantity = new_base_qty
            parent_wo.quantity = new_parent_qty
            parent_wo.save(update_fields=['base_quantity', 'quantity'])

            quality_check.scrap_compensated_qty = already_compensated + accepted_qty
            quality_check.scrap_compensated_by = user
            quality_check.scrap_compensated_at = timezone.now()
            quality_check.save(update_fields=[
                'scrap_compensated_qty',
                'scrap_compensated_by',
                'scrap_compensated_at'
            ])

            WorkOrderChangeLog.objects.create(
                work_order=parent_wo,
                changed_by=user,
                action="Scrap accepted",
                field_name="quantity",
                old_value=str(old_parent_qty),
                new_value=str(new_parent_qty),
                note=f"QC #{quality_check.id}: accepted {accepted_qty} scrap units."
            )

        return {
            "parent_work_order": parent_wo,
            "applied_qty": accepted_qty,
            "remaining_scrap": max(remaining_scrap - accepted_qty, 0),
        }

    @staticmethod
    def _compute_operation_duration(op, quantity):
        try:
            qty_val = float(quantity)
        except Exception:
            qty_val = 0
        setup = float(getattr(op, 'setup_time', 0) or 0)
        run = float(getattr(op, 'run_time', 0) or 0)
        duration = setup + (run * qty_val)
        if duration <= 0:
            duration = float(getattr(op, 'duration_minutes', 60) or 60)
        return duration

    @staticmethod
    def _operation_for_work_order(work_order):
        bom = getattr(work_order, 'bom', None)
        if not bom:
            parent = getattr(work_order, 'parent', None)
            bom = getattr(parent, 'bom', None) if parent else None
        if not bom:
            return None

        stage_id = (
            getattr(work_order, 'stage_id', None)
            or getattr(work_order, 'current_stage_id', None)
        )
        operations = bom.operations.select_related('stage', 'machine').order_by('order', 'id')
        if stage_id:
            operation = operations.filter(stage_id=stage_id).first()
            if operation:
                return operation
        return operations.first()

    @staticmethod
    def calculate_work_order_duration_minutes(work_order, quantity=None):
        operation = WorkOrderService._operation_for_work_order(work_order)
        if operation:
            return max(
                1,
                int(round(WorkOrderService._compute_operation_duration(
                    operation,
                    quantity if quantity is not None else getattr(work_order, 'quantity', 0),
                ))),
            )

        if getattr(work_order, 'start_date', None) and getattr(work_order, 'end_date', None):
            seconds = (work_order.end_date - work_order.start_date).total_seconds()
            if seconds > 0:
                return max(1, int(round(seconds / 60)))
        return 60

    @staticmethod
    def replan_after_quantity_change(work_order):
        """
        Recalculate schedule dates after a planner changes quantity.
        Returns True when schedule fields or routed child tasks were changed.
        """
        from datetime import timedelta
        from .models import WorkOrder

        if not work_order:
            return False

        changed = False
        if not getattr(work_order, 'parent_id', None) and work_order.sub_tasks.exists():
            child_tasks = list(
                WorkOrder.objects.filter(parent=work_order)
                .exclude(status__in=['canceled', 'archived'])
                .order_by('id')
            )
            for child in child_tasks:
                if child.quantity != work_order.quantity:
                    child.quantity = work_order.quantity
                    child.save(update_fields=['quantity'])
                    changed = True
            if not WorkOrderService.has_parallel_stage_tasks(work_order):
                WorkOrderService.reschedule_subtasks_from_anchor(
                    work_order,
                    recalculate_durations=True,
                )
                changed = True
            return changed

        if getattr(work_order, 'parent_id', None):
            if work_order.parent and work_order.parent.quantity != work_order.quantity:
                work_order.parent.quantity = work_order.quantity
                work_order.parent.save(update_fields=['quantity'])
                changed = True
            if work_order.parent and not WorkOrderService.has_parallel_stage_tasks(work_order.parent):
                WorkOrderService.reschedule_subtasks_from_anchor(
                    work_order.parent,
                    anchor_task=work_order,
                    recalculate_durations=True,
                )
                changed = True
            return changed

        if not work_order.start_date:
            return changed

        duration_minutes = WorkOrderService.calculate_work_order_duration_minutes(work_order, work_order.quantity)
        if work_order.machine and work_order.status != 'in_progress':
            start, end = WorkOrderService.find_next_available_slot(
                work_order.machine,
                duration_minutes,
                work_order.start_date,
                exclude_wo_id=work_order.id,
            )
        else:
            start = work_order.start_date
            end = start + timedelta(minutes=duration_minutes)

        update_fields = []
        if work_order.start_date != start:
            work_order.start_date = start
            update_fields.append('start_date')
        if getattr(work_order, 'scheduled_start_date', None) != start:
            work_order.scheduled_start_date = start
            update_fields.append('scheduled_start_date')
        if work_order.end_date != end:
            work_order.end_date = end
            update_fields.append('end_date')

        if update_fields:
            work_order.save(update_fields=update_fields)
            changed = True
        return changed

    @staticmethod
    def _candidate_machines_for_operation(operation, company, preferred_machine=None):
        from .models import Machine

        operational_qs = Machine.objects.filter(
            company=company,
            status='operational',
            is_active=True,
        ).order_by('id')

        candidates = []
        seen_ids = set()

        def add_candidate(machine):
            if not machine or getattr(machine, 'company_id', None) != getattr(company, 'id', None):
                return
            if getattr(machine, 'status', None) != 'operational' or not getattr(machine, 'is_active', True):
                return
            if machine.id in seen_ids:
                return
            seen_ids.add(machine.id)
            candidates.append(machine)

        add_candidate(preferred_machine)
        add_candidate(getattr(operation, 'machine', None))

        stage_obj = getattr(operation, 'stage', None)
        add_candidate(getattr(stage_obj, 'machine', None))

        hints = []
        machine_type_hint = str(getattr(operation, 'machine_type', '') or '').strip()
        stage_category = str(getattr(stage_obj, 'category', '') or '').strip()
        stage_name = str(getattr(stage_obj, 'name', '') or '').strip()
        if machine_type_hint:
            hints.append(machine_type_hint)
        if stage_category:
            hints.append(stage_category)
        if stage_name:
            hints.append(stage_name)

        query = Q()
        for hint in hints:
            query |= Q(type__iexact=hint) | Q(category__iexact=hint)

        if query:
            for machine in operational_qs.filter(query):
                add_candidate(machine)

        if not candidates:
            for machine in operational_qs:
                add_candidate(machine)

        return candidates

    @staticmethod
    def _pick_best_machine_slot(candidates, duration_minutes, start_after, exclude_wo_id=None):
        best_machine = None
        best_start = None
        best_end = None
        for machine in candidates:
            slot_start, slot_end = WorkOrderService.find_next_available_slot(
                machine,
                duration_minutes,
                start_after,
                exclude_wo_id=exclude_wo_id,
            )
            if best_start is None or slot_start < best_start:
                best_machine = machine
                best_start = slot_start
                best_end = slot_end
        return best_machine, best_start, best_end

    @staticmethod
    def get_active_stage_tasks(parent_wo):
        from .models import WorkOrder

        qs = WorkOrder.objects.filter(parent=parent_wo).exclude(
            status__in=['completed', 'canceled', 'archived']
        ).order_by('start_date', 'id')

        if get_work_order_operation_flow_mode(parent_wo) == 'parallel':
            return qs

        current_stage_id = getattr(parent_wo, 'current_stage_id', None)
        if current_stage_id:
            stage_qs = qs.filter(stage_id=current_stage_id)
            if stage_qs.exists():
                return stage_qs

        in_progress_qs = qs.filter(status='in_progress')
        if in_progress_qs.exists():
            return in_progress_qs

        first_task = qs.first()
        if not first_task:
            return qs
        if first_task.stage_id:
            same_stage_qs = qs.filter(stage_id=first_task.stage_id)
            if same_stage_qs.exists():
                return same_stage_qs
        return qs.filter(id=first_task.id)

    @staticmethod
    def get_active_stage_task(parent_wo):
        return WorkOrderService.get_active_stage_tasks(parent_wo).first()

    @staticmethod
    def schedule_full_route(
        parent_wo,
        first_stage,
        first_machine,
        first_start,
        actor=None,
        company=None,
        stage_machine_ids=None,
        stage_assignment_specs=None,
        operation_flow_mode='series',
    ):
        from collections import defaultdict
        from datetime import datetime
        from django.utils import timezone
        from .models import WorkOrder, Machine

        company = company or parent_wo.company
        flow_mode = normalize_operation_flow_mode(
            operation_flow_mode or getattr(parent_wo, 'operation_flow_mode', None) or get_company_default_operation_flow_mode(company)
        )
        if first_start is None:
            raise ValueError("A start time is required for the first stage.")
        bom = getattr(parent_wo, 'bom', None)
        if not bom or not bom.operations.exists():
            raise ValueError("Work order BOM does not define production stages.")

        operations = [
            op for op in bom.operations.select_related('stage', 'machine', 'stage__machine').order_by('order', 'id')
            if op.stage_id
        ]
        if not operations:
            raise ValueError("Work order BOM does not define schedulable stages.")

        anchor_stage_id = getattr(first_stage, 'id', None)
        if not anchor_stage_id:
            anchor_stage_id = operations[0].stage_id
            first_stage = operations[0].stage

        anchor_index = next(
            (index for index, op in enumerate(operations) if op.stage_id == anchor_stage_id),
            None,
        )
        if anchor_index is None:
            raise ValueError("Selected stage is not part of the BOM routing.")

        normalized_stage_machines = {}
        normalized_stage_assignments = {}
        requested_machine_ids = []

        def parse_stage_start(raw_value):
            raw = str(raw_value or '').strip()
            if not raw:
                return None
            normalized = raw
            if normalized.endswith('Z'):
                normalized = normalized[:-1] + '+00:00'
            normalized = normalized.replace('T', ' ')
            try:
                parsed = datetime.fromisoformat(normalized)
            except ValueError:
                raise ValueError("Invalid route stage start time.")
            if timezone.is_naive(parsed):
                parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
            return parsed

        for raw_stage_id, raw_assignment in (stage_assignment_specs or {}).items():
            if raw_stage_id in [None, '']:
                continue
            try:
                stage_id = int(raw_stage_id)
            except (TypeError, ValueError):
                raise ValueError("Invalid route stage selection.")

            assignment = raw_assignment if isinstance(raw_assignment, dict) else {}
            selection_mode = str(assignment.get('selection_mode') or '').strip().lower() or 'manual'
            if selection_mode not in ['manual', 'recommended', 'auto']:
                selection_mode = 'manual'
            requested_stage_start = parse_stage_start(assignment.get('start_date'))

            machine_id = assignment.get('machine_id')
            normalized_machine_id = None
            if machine_id not in [None, '']:
                try:
                    normalized_machine_id = int(machine_id)
                    requested_machine_ids.append(normalized_machine_id)
                except (TypeError, ValueError):
                    raise ValueError("Invalid route machine selection.")

            normalized_stage_assignments[stage_id] = {
                'machine_id': normalized_machine_id,
                'selection_mode': selection_mode,
                'start_date': requested_stage_start,
            }

        for raw_stage_id, raw_machine_id in (stage_machine_ids or {}).items():
            if raw_stage_id in [None, ''] or raw_machine_id in [None, '']:
                continue
            try:
                stage_id = int(raw_stage_id)
                machine_id = int(raw_machine_id)
                normalized_stage_machines[stage_id] = machine_id
                requested_machine_ids.append(machine_id)
                normalized_stage_assignments.setdefault(stage_id, {
                    'machine_id': machine_id,
                    'selection_mode': 'manual',
                })
            except (TypeError, ValueError):
                raise ValueError("Invalid route machine selection.")

        selected_machines = {
            machine.id: machine
            for machine in Machine.objects.filter(company=company, id__in=requested_machine_ids)
        }
        if len(selected_machines) != len(set(requested_machine_ids)):
            raise ValueError("One or more selected machines do not belong to this company.")
        for machine in selected_machines.values():
            if machine.status != 'operational' or not machine.is_active:
                raise ValueError(f"Machine '{machine.display_label}' is not available for planning.")

        open_children = list(
            WorkOrder.objects.filter(parent=parent_wo).exclude(
                status__in=['canceled', 'archived']
            ).order_by('id')
        )
        non_plannable = [
            child for child in open_children
            if child.status != 'pending'
        ]
        if non_plannable:
            raise ValueError("Cannot auto-plan all stages after execution has started.")

        existing_by_stage = defaultdict(list)
        for child in open_children:
            existing_by_stage[getattr(child, 'stage_id', None)].append(child)

        planned_tasks = []
        previous_end = first_start
        route_end = first_start
        first_task = None
        blocked_by_missing_machine = False
        now = timezone.now()

        for index, operation in enumerate(operations[anchor_index:]):
            duration_minutes = max(
                1,
                int(round(WorkOrderService._compute_operation_duration(operation, parent_wo.quantity))),
            )
            overall_index = anchor_index + index
            previous_operation = operations[overall_index - 1] if overall_index > 0 else None
            stage_name = getattr(operation.stage, 'name', f"Stage {operation.order}")

            stage_children = existing_by_stage.get(operation.stage_id, [])
            child = stage_children.pop(0) if stage_children else None
            exclude_ids = [parent_wo.id]
            if child and child.id:
                exclude_ids.append(child.id)

            stage_assignment = normalized_stage_assignments.get(operation.stage_id)
            has_explicit_assignment = stage_assignment is not None
            if not has_explicit_assignment:
                stage_assignment = {}
            stage_machine_id = stage_assignment.get('machine_id')
            if stage_machine_id is None:
                stage_machine_id = normalized_stage_machines.get(operation.stage_id)
            stage_machine = selected_machines.get(stage_machine_id)
            stage_selection_mode = str(stage_assignment.get('selection_mode') or '').strip().lower()
            if stage_selection_mode not in ['manual', 'recommended', 'auto']:
                stage_selection_mode = 'manual' if stage_machine else 'auto'
            if index == 0 and not stage_assignment and first_machine and not stage_machine and stage_selection_mode == 'auto':
                stage_selection_mode = 'manual'
            assigned_machine = stage_machine or (first_machine if index == 0 else None)
            slot_start = None
            slot_end = None
            requested_start = first_start if flow_mode == 'parallel' else previous_end
            requested_stage_start = stage_assignment.get('start_date') if stage_assignment else None
            if requested_stage_start:
                if flow_mode == 'series' and requested_start and requested_stage_start < requested_start:
                    previous_stage_name = getattr(getattr(previous_operation, 'stage', None), 'name', None) or "the previous stage"
                    raise ValueError(f"{stage_name} cannot start before {previous_stage_name} finishes.")
                requested_start = requested_stage_start
            if not has_explicit_assignment:
                if index == 0 and not assigned_machine:
                    candidates = WorkOrderService._candidate_machines_for_operation(
                        operation,
                        company,
                    )
                    assigned_machine, slot_start, slot_end = WorkOrderService._pick_best_machine_slot(
                        candidates,
                        duration_minutes,
                        requested_start,
                        exclude_wo_id=exclude_ids,
                    )
                    if not assigned_machine:
                        raise ValueError(f"No operational machine available for {stage_name}.")
                elif assigned_machine and (flow_mode == 'parallel' or not blocked_by_missing_machine):
                    slot_start, slot_end = WorkOrderService.find_next_available_slot(
                        assigned_machine,
                        duration_minutes,
                        requested_start,
                        exclude_wo_id=exclude_ids,
                    )
                    if index == 0 and first_machine and abs((slot_start - first_start).total_seconds()) > 60:
                        suggestion = slot_start.strftime('%Y-%m-%d %H:%M')
                        raise ValueError(
                            f"Machine occupied at this time. First available slot: {suggestion}"
                        )
                elif flow_mode == 'parallel' or not blocked_by_missing_machine:
                    candidates = WorkOrderService._candidate_machines_for_operation(
                        operation,
                        company,
                    )
                    assigned_machine, slot_start, slot_end = WorkOrderService._pick_best_machine_slot(
                        candidates,
                        duration_minutes,
                        requested_start,
                        exclude_wo_id=exclude_ids,
                    )
                    if not assigned_machine:
                        raise ValueError(f"No operational machine available for {stage_name}.")
            else:
                if index == 0 and not assigned_machine:
                    raise ValueError("Assign a machine to the first stage before planning this work order.")

                can_schedule_now = bool(assigned_machine) and (flow_mode == 'parallel' or not blocked_by_missing_machine)

                if can_schedule_now and stage_selection_mode == 'recommended':
                    candidates = WorkOrderService._candidate_machines_for_operation(
                        operation,
                        company,
                        preferred_machine=assigned_machine,
                    )
                    assigned_machine, slot_start, slot_end = WorkOrderService._pick_best_machine_slot(
                        candidates,
                        duration_minutes,
                        requested_start,
                        exclude_wo_id=exclude_ids,
                    )
                    if not assigned_machine:
                        raise ValueError(f"No operational machine available for {stage_name}.")
                elif can_schedule_now:
                    slot_start, slot_end = WorkOrderService.find_next_available_slot(
                        assigned_machine,
                        duration_minutes,
                        requested_start,
                        exclude_wo_id=exclude_ids,
                    )
                    if index == 0 and stage_selection_mode == 'manual' and abs((slot_start - first_start).total_seconds()) > 60:
                        suggestion = slot_start.strftime('%Y-%m-%d %H:%M')
                        raise ValueError(
                            f"Machine occupied at this time. First available slot: {suggestion}"
                        )

            if requested_stage_start and slot_start and abs((slot_start - requested_stage_start).total_seconds()) > 60:
                suggestion = slot_start.strftime('%Y-%m-%d %H:%M')
                if assigned_machine:
                    raise ValueError(
                        f"{stage_name} is not available at the requested time. First available slot: {suggestion}"
                    )
                raise ValueError(
                    f"No operational machine available for {stage_name} at the requested time. Earliest slot: {suggestion}"
                )

            if flow_mode == 'series' and not assigned_machine:
                blocked_by_missing_machine = True

            if not child:
                child = WorkOrder(parent=parent_wo, company=company)

            child.product_name = f"{parent_wo.product_name} - {operation.stage.name}"
            child.bom = parent_wo.bom
            child.quantity = parent_wo.quantity
            child.customer = parent_wo.customer
            child.status = 'pending'
            child.machine = assigned_machine
            child.stage = operation.stage
            child.current_stage = operation.stage
            child.assigned_to = parent_wo.assigned_to or actor
            child.assigned_worker = None
            child.assignment_type = 'auto'
            child.priority = parent_wo.priority
            child.order_type = parent_wo.order_type
            child.operation_flow_mode = flow_mode
            child.instructions = parent_wo.instructions
            child.material_readiness_status = parent_wo.material_readiness_status
            child.material_shortage_note = parent_wo.material_shortage_note
            child.material_available_qty = parent_wo.material_available_qty
            child.material_available_percent = parent_wo.material_available_percent
            child.material_expected_delivery_date = parent_wo.material_expected_delivery_date
            child.material_readiness_updated_at = parent_wo.material_readiness_updated_at
            child.material_readiness_updated_by = parent_wo.material_readiness_updated_by
            child.qc_requirement = getattr(operation.stage, 'is_quality_check', False)
            child.start_date = slot_start
            child.scheduled_start_date = slot_start
            child.end_date = slot_end
            if not child.planner_start_at:
                child.planner_start_at = now
            child.save()

            planned_tasks.append(child)
            if flow_mode == 'series' and slot_end:
                previous_end = slot_end
            if slot_end and (route_end is None or slot_end > route_end):
                route_end = slot_end
            if first_task is None:
                first_task = child

        if not first_task:
            raise ValueError("No stage tasks were scheduled.")

        if parent_wo.current_stage_id != first_task.stage_id:
            parent_wo.current_stage = first_task.stage
        if parent_wo.status != 'pending':
            raise ValueError(f"Cannot schedule work order in status '{parent_wo.status}'.")

        parent_wo.machine = None
        parent_wo.assigned_worker = None
        parent_wo.assignment_type = 'auto'
        parent_wo.next_stage_ready = False
        parent_wo.operation_flow_mode = flow_mode
        if not parent_wo.planner_start_at:
            parent_wo.planner_start_at = now
        parent_wo.save(update_fields=[
            'current_stage',
            'status',
            'machine',
            'assigned_worker',
            'assignment_type',
            'next_stage_ready',
            'operation_flow_mode',
            'planner_start_at',
        ])

        return {
            'parent_work_order': parent_wo,
            'first_task': first_task,
            'tasks': planned_tasks,
            'final_end': route_end,
        }

    @staticmethod
    def create_next_stage_task(stage_work_order, created_by=None, auto_create=True):
        from django.utils import timezone
        from .models import WorkOrder

        parent = stage_work_order.parent if stage_work_order.parent_id else stage_work_order
        flow_mode = get_work_order_operation_flow_mode(parent)
        bom = parent.bom or stage_work_order.bom
        current_stage_id = (
            stage_work_order.stage_id
            or stage_work_order.current_stage_id
            or parent.current_stage_id
        )

        if not bom:
            return None

        if not current_stage_id:
            first_op = bom.operations.select_related('stage').order_by('order').first()
            if first_op and first_op.stage_id:
                current_stage_id = first_op.stage_id
            else:
                return None

        if stage_work_order.parent_id:
            actual_completion_at = timezone.now()
            if stage_work_order.status != 'completed':
                WorkOrderLifecycle.apply_transition(
                    stage_work_order,
                    'completed',
                    actor=None,
                    allow_system=True,
                    save=False
                )
            stage_work_order.progress = 100
            if not stage_work_order.end_date or stage_work_order.end_date > actual_completion_at:
                stage_work_order.end_date = actual_completion_at
            update_fields = ['status', 'progress', 'end_date']
            if WorkOrderService._is_qc_required(stage_work_order, bom, current_stage_id) and not stage_work_order.qc_requirement:
                stage_work_order.qc_requirement = True
                update_fields.append('qc_requirement')
            stage_work_order.save(update_fields=update_fields)

            stage_check_id = stage_work_order.stage_id or current_stage_id
            if stage_check_id:
                remaining_in_stage = WorkOrder.objects.filter(
                    parent=parent,
                    stage_id=stage_check_id
                ).exclude(status='completed')
                qc_required = WorkOrderService._is_qc_required(stage_work_order, bom, current_stage_id)
                if qc_required:
                    qc_pending = WorkOrderService._ensure_qc_record(stage_work_order)
                    if qc_pending:
                        return {
                            "qc_pending": True,
                            "stage_pending": remaining_in_stage.exists()
                        }
                if remaining_in_stage.exists():
                    return {"stage_pending": True}

        # QC gate for non-split tasks before advancing
        if WorkOrderService._is_qc_required(stage_work_order, bom, current_stage_id):
            qc_pending = WorkOrderService._ensure_qc_record(stage_work_order)
            if qc_pending:
                return {"qc_pending": True}

        if flow_mode == 'parallel' and stage_work_order.parent_id:
            remaining_open_children = WorkOrder.objects.filter(
                parent=parent
            ).exclude(
                status__in=['completed', 'canceled', 'archived']
            )
            if remaining_open_children.exists():
                return {"stage_pending": True, "parallel_mode": True}

            if parent.status != 'completed':
                WorkOrderLifecycle.apply_transition(
                    parent,
                    'completed',
                    actor=None,
                    allow_system=True,
                    save=False
                )
                parent.progress = 100
                completed_child_end = parent.sub_tasks.filter(
                    status='completed',
                    end_date__isnull=False,
                ).order_by('-end_date').values_list('end_date', flat=True).first()
                parent_completion_at = completed_child_end or actual_completion_at
                if not parent.end_date or parent.end_date != parent_completion_at:
                    parent.end_date = parent_completion_at
                if not getattr(parent, 'closed_by_planner', False):
                    parent.planner_action_required = True
                if getattr(parent, 'next_stage_ready', False):
                    parent.next_stage_ready = False
                parent.save(update_fields=['status', 'progress', 'end_date', 'planner_action_required', 'next_stage_ready'])
                if getattr(parent, 'planner_action_required', False):
                    request_store_receipt_for_work_order(parent)
            return {"parent_completed": True, "parallel_mode": True}

        next_stage = WorkOrderService.get_next_stage(bom, current_stage_id)
        if not next_stage:
            fallback_stage = WorkOrderService._fallback_next_stage(parent, bom, current_stage_id)
            if fallback_stage and fallback_stage.id != current_stage_id:
                next_stage = fallback_stage
        if not next_stage:
            if parent.status != 'completed':
                WorkOrderLifecycle.apply_transition(
                    parent,
                    'completed',
                    actor=None,
                    allow_system=True,
                    save=False
                )
                parent.progress = 100
                completed_child_end = parent.sub_tasks.filter(
                    status='completed',
                    end_date__isnull=False,
                ).order_by('-end_date').values_list('end_date', flat=True).first()
                parent_completion_at = completed_child_end or actual_completion_at
                if not parent.end_date or parent.end_date != parent_completion_at:
                    parent.end_date = parent_completion_at
                if not getattr(parent, 'closed_by_planner', False):
                    parent.planner_action_required = True
                if getattr(parent, 'next_stage_ready', False):
                    parent.next_stage_ready = False
                parent.save(update_fields=['status', 'progress', 'end_date', 'planner_action_required', 'next_stage_ready'])
                if getattr(parent, 'planner_action_required', False):
                    request_store_receipt_for_work_order(parent)
            return {"parent_completed": True}

        existing_next_task = WorkOrder.objects.filter(
            parent=parent,
            stage=next_stage
        ).exclude(
            status__in=['completed', 'canceled', 'archived']
        ).order_by('start_date', 'id').first()

        if existing_next_task:
            next_task_update_fields = []
            if (
                existing_next_task.machine_id
                and existing_next_task.start_date
                and not existing_next_task.scheduled_start_date
            ):
                existing_next_task.scheduled_start_date = existing_next_task.start_date
                next_task_update_fields.append('scheduled_start_date')
            if next_task_update_fields:
                existing_next_task.save(update_fields=next_task_update_fields)

            parent_dirty = False
            planner_assignment_required = not bool(existing_next_task.machine_id)
            if parent.current_stage_id != next_stage.id:
                parent.current_stage = next_stage
                parent_dirty = True
            if parent.status == 'pending':
                WorkOrderLifecycle.apply_transition(
                    parent,
                    'in_progress',
                    actor=None,
                    allow_system=True,
                    save=False
                )
                parent_dirty = True
            if parent.status == 'completed' and not getattr(parent, 'closed_by_planner', False):
                WorkOrderLifecycle.apply_transition(
                    parent,
                    'in_progress',
                    actor=None,
                    allow_system=True,
                    allow_reopen=True,
                    save=False
                )
                parent_dirty = True
            if parent.planner_action_required:
                parent.planner_action_required = False
                parent_dirty = True
            if bool(getattr(parent, 'next_stage_ready', False)) != planner_assignment_required:
                parent.next_stage_ready = planner_assignment_required
                parent_dirty = True
            if parent.machine_id:
                parent.machine = None
                parent_dirty = True
            if parent.assigned_worker_id:
                parent.assigned_worker = None
                parent.assignment_type = 'auto'
                parent_dirty = True
            if not parent.parent_id:
                parent.start_date = None
                parent.end_date = None
                parent.scheduled_start_date = None
                parent.progress = 0
                parent_dirty = True
            if parent_dirty:
                parent.save(update_fields=[
                    'current_stage',
                    'status',
                    'machine',
                    'assigned_worker',
                    'assignment_type',
                    'start_date',
                    'end_date',
                    'scheduled_start_date',
                    'progress',
                    'planner_action_required',
                    'next_stage_ready'
                ])
            return {
                "next_stage": next_stage,
                "next_task": existing_next_task,
                "created": False,
                "activated_existing": not planner_assignment_required,
                "planner_assignment_required": planner_assignment_required,
            }

        # Manual Start: do not auto-create next stage task
        if not auto_create:
            if not getattr(parent, 'next_stage_ready', False):
                parent.next_stage_ready = True
                parent.save(update_fields=['next_stage_ready'])
            return {"next_stage": next_stage, "created": False, "manual_start": True}

        existing_qty = WorkOrder.objects.filter(parent=parent, stage=next_stage).exclude(
            status__in=['canceled', 'archived']
        ).aggregate(
            total=Sum('quantity')
        )['total'] or 0

        qc_required = WorkOrderService._is_qc_required(stage_work_order, bom, current_stage_id)
        if qc_required:
            from .models import QualityCheck
            qc_good_qty = QualityCheck.objects.filter(
                work_order=stage_work_order,
                status='processed'
            ).aggregate(total=Sum('good_quantity'))['total'] or 0
            released_qty = int(getattr(stage_work_order, 'released_qty', 0) or 0)
            available_qty = max(int(qc_good_qty) - released_qty, 0)
            remaining_qty = max(int(available_qty) - int(existing_qty), 0)
        else:
            remaining_qty = max(int(parent.quantity) - int(existing_qty), 0)

        next_task = None
        created = False
        is_scrap_comp_flow = WorkOrderService._is_scrap_compensation_flow(stage_work_order)
        scrap_source_qc_id = WorkOrderService._get_scrap_source_qc_id(stage_work_order)
        if remaining_qty > 0:
            next_task = WorkOrder.objects.create(
                parent=parent,
                company=parent.company,
                product_name=f"{parent.product_name} - {next_stage.name}",
                bom=parent.bom,
                quantity=remaining_qty,
                customer=parent.customer,
                status='pending',
                stage=next_stage,
                current_stage=next_stage,
                assigned_to=parent.assigned_to or created_by,
                priority=parent.priority,
                order_type=parent.order_type,
                operation_flow_mode=get_work_order_operation_flow_mode(parent),
                instructions=parent.instructions,
                material_readiness_status=parent.material_readiness_status,
                material_shortage_note=parent.material_shortage_note,
                material_available_qty=parent.material_available_qty,
                material_available_percent=parent.material_available_percent,
                material_expected_delivery_date=parent.material_expected_delivery_date,
                material_readiness_updated_at=parent.material_readiness_updated_at,
                material_readiness_updated_by=parent.material_readiness_updated_by,
                qc_requirement=getattr(next_stage, 'is_quality_check', False),
                source_task=stage_work_order if stage_work_order.parent_id else None,
                is_scrap_compensation_task=is_scrap_comp_flow,
                scrap_source_quality_check_id=scrap_source_qc_id,
            )
            created = True
            if qc_required:
                stage_work_order.released_qty = int(getattr(stage_work_order, 'released_qty', 0) or 0) + int(remaining_qty)
                stage_work_order.save(update_fields=['released_qty'])

        parent_dirty = False
        if parent.current_stage_id != next_stage.id:
            parent.current_stage = next_stage
            parent_dirty = True
        if parent.status == 'pending':
            WorkOrderLifecycle.apply_transition(
                parent,
                'in_progress',
                actor=None,
                allow_system=True,
                save=False
            )
            parent_dirty = True
        if parent.status == 'completed' and not getattr(parent, 'closed_by_planner', False):
            WorkOrderLifecycle.apply_transition(
                parent,
                'in_progress',
                actor=None,
                allow_system=True,
                allow_reopen=True,
                save=False
            )
            parent_dirty = True
        if parent.planner_action_required:
            parent.planner_action_required = False
            parent_dirty = True
        next_stage_requires_planner = bool(next_task and not getattr(next_task, 'machine_id', None))
        if bool(getattr(parent, 'next_stage_ready', False)) != next_stage_requires_planner:
            parent.next_stage_ready = next_stage_requires_planner
            parent_dirty = True
        if parent.machine_id:
            parent.machine = None
            parent_dirty = True
        if parent.assigned_worker_id:
            parent.assigned_worker = None
            parent.assignment_type = 'auto'
            parent_dirty = True
        if not parent.parent_id:
            parent.start_date = None
            parent.end_date = None
            parent.scheduled_start_date = None
            parent.progress = 0
            parent_dirty = True
        if parent_dirty:
            parent.save(update_fields=[
                'current_stage',
                'status',
                'machine',
                'assigned_worker',
                'assignment_type',
                'start_date',
                'end_date',
                'scheduled_start_date',
                'progress',
                'planner_action_required',
                'next_stage_ready'
            ])

        return {"next_stage": next_stage, "next_task": next_task, "created": created}

    @staticmethod
    def release_to_next_stage(stage_work_order, release_quantity, user, target_machine=None):
        """
        Release a partial lot to the next stage without waiting for full stage completion.
        Uses produced (approved + pending) output, gated by QC if required.
        """
        from .models import WorkOrder

        if stage_work_order.sub_tasks.exists():
            raise ValueError("Cannot release from a parent work order. Select a stage task.")

        if stage_work_order.status in ['canceled', 'archived']:
            raise ValueError("Cannot release from an inactive work order.")

        try:
            release_quantity = int(release_quantity)
        except Exception:
            raise ValueError("Release quantity must be a number.")

        if release_quantity <= 0:
            raise ValueError("Release quantity must be positive.")

        parent = stage_work_order.parent if stage_work_order.parent_id else stage_work_order
        bom = parent.bom or stage_work_order.bom
        current_stage_id = (
            stage_work_order.stage_id
            or stage_work_order.current_stage_id
            or parent.current_stage_id
        )

        if not bom or not current_stage_id:
            raise ValueError("Cannot determine current stage.")

        next_stage = WorkOrderService.get_next_stage(bom, current_stage_id)
        if not next_stage:
            raise ValueError("No next stage available.")

        approved_qty = stage_work_order.production_logs.filter(status='approved').aggregate(
            total=Sum('quantity')
        )['total'] or 0
        pending_qty = stage_work_order.production_logs.filter(status='pending').aggregate(
            total=Sum('quantity')
        )['total'] or 0
        released_qty = int(getattr(stage_work_order, 'released_qty', 0) or 0)

        qc_required = WorkOrderService._is_qc_required(stage_work_order, bom, current_stage_id)
        if qc_required:
            if WorkOrderService._has_pending_qc(stage_work_order):
                raise ValueError("QC is pending. Complete quality checks before releasing.")
            from .models import QualityCheck
            qc_good_qty = QualityCheck.objects.filter(
                work_order=stage_work_order,
                status='processed'
            ).aggregate(total=Sum('good_quantity'))['total'] or 0
            available_qty = max(int(qc_good_qty) - released_qty, 0)
        else:
            # Only approved output can move to the next stage
            available_qty = max(int(approved_qty) - released_qty, 0)

        if release_quantity > available_qty:
            raise ValueError("Release quantity exceeds available output to transfer.")

        is_scrap_comp_flow = WorkOrderService._is_scrap_compensation_flow(stage_work_order)
        scrap_source_qc_id = WorkOrderService._get_scrap_source_qc_id(stage_work_order)
        new_task = WorkOrder.objects.create(
            parent=parent,
            company=parent.company,
            product_name=f"{parent.product_name} - {next_stage.name}",
            bom=parent.bom,
            quantity=release_quantity,
            customer=parent.customer,
            status='pending',
            stage=next_stage,
            current_stage=next_stage,
            assigned_to=parent.assigned_to or user,
            priority=parent.priority,
            order_type=parent.order_type,
            operation_flow_mode=get_work_order_operation_flow_mode(parent),
            instructions=parent.instructions,
            qc_requirement=getattr(next_stage, 'is_quality_check', False),
            source_task=stage_work_order,
            material_readiness_status=parent.material_readiness_status,
            material_shortage_note=parent.material_shortage_note,
            material_available_qty=parent.material_available_qty,
            material_available_percent=parent.material_available_percent,
            material_expected_delivery_date=parent.material_expected_delivery_date,
            material_readiness_updated_at=parent.material_readiness_updated_at,
            material_readiness_updated_by=parent.material_readiness_updated_by,
            is_scrap_compensation_task=is_scrap_comp_flow,
            scrap_source_quality_check_id=scrap_source_qc_id,
        )

        if target_machine:
            new_task.machine = target_machine
            new_task.save(update_fields=['machine'])

        stage_work_order.released_qty = released_qty + release_quantity
        stage_work_order.save(update_fields=['released_qty'])

        parent_dirty = False
        if parent.current_stage_id != next_stage.id:
            parent.current_stage = next_stage
            parent_dirty = True
        if parent.status == 'pending':
            WorkOrderLifecycle.apply_transition(
                parent,
                'in_progress',
                actor=None,
                allow_system=True,
                save=False
            )
            parent_dirty = True
        if parent.status == 'completed' and not getattr(parent, 'closed_by_planner', False):
            WorkOrderLifecycle.apply_transition(
                parent,
                'in_progress',
                actor=None,
                allow_system=True,
                allow_reopen=True,
                save=False
            )
            parent_dirty = True
        if parent.planner_action_required:
            parent.planner_action_required = False
            parent_dirty = True
        if getattr(parent, 'next_stage_ready', False):
            parent.next_stage_ready = False
            parent_dirty = True
        if parent.machine_id:
            parent.machine = None
            parent_dirty = True
        if parent.assigned_worker_id:
            parent.assigned_worker = None
            parent.assignment_type = 'auto'
            parent_dirty = True
        if not parent.parent_id:
            parent.start_date = None
            parent.end_date = None
            parent.scheduled_start_date = None
            parent.progress = 0
            parent_dirty = True
        if parent_dirty:
            parent.save(update_fields=[
                'current_stage',
                'status',
                'machine',
                'assigned_worker',
                'assignment_type',
                'start_date',
                'end_date',
                'scheduled_start_date',
                'progress',
                'planner_action_required',
                'next_stage_ready'
            ])

        return new_task

    @staticmethod
    def get_recommendation(bom, quantity, start_after=None):
        """
        🔮 Recommendation Engine: Suggest the best machine and slot for a job.
        """
        from django.utils import timezone
        if not start_after: start_after = timezone.now()
        
        # If BOM has operations, check the first one
        if bom.operations.exists():
            op = bom.operations.all().order_by('order').first()
            duration = WorkOrderService._compute_operation_duration(op, quantity)
            machine = WorkOrderService.find_first_available_machine(
                op.stage,
                bom.product.company,
                start_after,
                duration,
                machine_type_hint=getattr(op, 'machine_type', None),
                preferred_machine=getattr(op, 'machine', None),
            )
            start, end = WorkOrderService.find_next_available_slot(
                machine, duration, start_after
            )
            return {
                "machine": machine,
                "start": start,
                "end": end,
                "duration": duration
            }
        return None

    @staticmethod
    def split_work_order(original_wo, split_quantity, target_machine, user, planned_start=None, actual_finish_at=None):
        """
        ⚡ Robust Split Logic:
        - Allow splitting remaining quantity to another machine.
        - Prevent splitting beyond remaining qty or completed orders.
        - Preserve production logs and progress integrity.
        """
        from .models import WorkOrder
        from django.db.models import Sum
        from django.utils import timezone
        from datetime import timedelta
        
        if original_wo.sub_tasks.exists():
            raise ValueError("Cannot split a parent work order. Split a stage task instead.")

        if original_wo.status in ['completed', 'canceled', 'archived']:
            raise ValueError("Cannot split a completed or inactive work order.")

        try:
            split_decimal = Decimal(str(split_quantity).strip())
        except (InvalidOperation, ValueError):
            raise ValueError("Split quantity must be a number.")
        if split_decimal != split_decimal.to_integral_value():
            raise ValueError("Split quantity must be a whole number.")
        split_quantity = int(split_decimal)

        if split_quantity <= 0:
            raise ValueError("Invalid split quantity.")

        approved_qty = original_wo.production_logs.filter(status='approved').aggregate(
            total=Sum('quantity')
        )['total'] or 0

        remaining_qty = max(original_wo.quantity - approved_qty, 0)
        if split_quantity > remaining_qty:
            raise ValueError("Split quantity exceeds remaining quantity.")

        new_original_qty = original_wo.quantity - split_quantity
        if new_original_qty < 0:
            raise ValueError("Split quantity exceeds remaining quantity.")
        close_original_after_split = new_original_qty == 0

        old_total = original_wo.quantity
        original_start = original_wo.start_date
        original_end = original_wo.end_date
        actual_finish_at = actual_finish_at or None
        if actual_finish_at and original_start and actual_finish_at < original_start:
            actual_finish_at = original_start
        original_duration_seconds = (
            (original_end - original_start).total_seconds()
            if original_start and original_end and original_end > original_start
            else None
        )

        # 1. Shrink Original
        original_wo.quantity = new_original_qty
        original_update_fields = ['quantity', 'end_date', 'progress']
        if WorkOrderService._sync_base_quantity_to_current_quantity(original_wo):
            original_update_fields.append('base_quantity')
        
        # 2. Recalculate Timing for Original (Scale proportionally)
        if original_start and original_duration_seconds is not None:
            new_duration_seconds = (original_duration_seconds * original_wo.quantity) / max(old_total, 1)
            original_wo.end_date = original_start + timedelta(seconds=new_duration_seconds)
        if actual_finish_at and (not original_wo.end_date or original_wo.end_date > actual_finish_at):
            original_wo.end_date = actual_finish_at
        
        if original_wo.quantity > 0:
            original_wo.progress = min(100, (approved_qty / original_wo.quantity) * 100)
        else:
            original_wo.progress = 100

        original_wo.save(update_fields=original_update_fields)
        WorkOrderService._sync_parent_quantity_from_stage_task(original_wo)

        # 3. Create Split WO
        is_scrap_comp_flow = WorkOrderService._is_scrap_compensation_flow(original_wo)
        scrap_source_qc_id = WorkOrderService._get_scrap_source_qc_id(original_wo)
        new_wo = WorkOrder.objects.create(
            company=original_wo.company,
            product_name=original_wo.product_name,
            bom=original_wo.bom,
            quantity=split_quantity,
            base_quantity=split_quantity,
            customer=original_wo.customer,
            status='pending',
            machine=target_machine,
            stage=original_wo.stage,
            current_stage=original_wo.current_stage,
            assigned_to=user,
            parent=original_wo.parent,
            priority=original_wo.priority,
            operation_flow_mode=get_work_order_operation_flow_mode(original_wo),
            qc_requirement=getattr(original_wo, 'qc_requirement', False),
            material_readiness_status=original_wo.material_readiness_status,
            material_shortage_note=original_wo.material_shortage_note,
            material_available_qty=original_wo.material_available_qty,
            material_available_percent=original_wo.material_available_percent,
            material_expected_delivery_date=original_wo.material_expected_delivery_date,
            material_readiness_updated_at=original_wo.material_readiness_updated_at,
            material_readiness_updated_by=original_wo.material_readiness_updated_by,
            is_scrap_compensation_task=is_scrap_comp_flow,
            scrap_source_quality_check_id=scrap_source_qc_id,
            assigned_worker=None,
            assignment_type='auto',
            source_task=original_wo,
        )
        
        # Find Slot for New WO
        start_after = planned_start or actual_finish_at or timezone.now()
        # Estimate duration for new part
        duration_mins = 60  # Default
        if original_duration_seconds is not None and old_total:
            # Use the pre-split scheduled duration. The source order has already
            # been shortened, so reading its current end date here double-scales.
            total_minutes = original_duration_seconds / 60
            per_unit = total_minutes / max(old_total, 1)
            duration_mins = max(1, int(round(per_unit * split_quantity)))
        elif original_wo.bom:
            # Fallback: scale from first operation duration
            op = original_wo.bom.operations.all().order_by('order').first()
            if op:
                duration_mins = max(1, int(round(WorkOrderService._compute_operation_duration(op, split_quantity))))
        
        s, e = WorkOrderService.find_next_available_slot(target_machine, int(duration_mins), start_after)
        new_wo.start_date = s
        new_wo.scheduled_start_date = s
        new_wo.end_date = e
        new_wo.save()

        NotificationService.notify_role(
            original_wo.company,
            roles=['supervisor', 'admin'],
            title="Split work order needs assignment",
            message=(
                f"WO #{original_wo.parent_id or original_wo.id} was split. "
                f"{split_quantity} units are waiting on {target_machine.display_label} for worker assignment."
            ),
            link="/manufacturing/supervisor/?tab=assignments",
            exclude_user=user,
        )

        # 4. If original is now complete due to split, close it safely.
        original_fully_covered_by_approved = approved_qty >= original_wo.quantity
        if (close_original_after_split or original_fully_covered_by_approved) and original_wo.status in ['pending', 'in_progress']:
            if original_wo.status == 'pending':
                WorkOrderLifecycle.apply_transition(
                    original_wo,
                    'in_progress',
                    actor=None,
                    allow_system=True,
                    save=False
                )
            WorkOrderLifecycle.apply_transition(
                original_wo,
                'completed',
                actor=None,
                allow_system=True,
                save=False
            )
            completed_at = timezone.now()
            if not original_wo.end_date or original_wo.end_date > completed_at:
                original_wo.end_date = completed_at
            original_wo.save(update_fields=['status', 'end_date'])
            if original_fully_covered_by_approved and approved_qty > 0:
                WorkOrderService.create_next_stage_task(original_wo, user, auto_create=False)

        return new_wo

    @staticmethod
    def _approved_quantity_for_work_order(work_order):
        from django.db.models import Sum

        return work_order.production_logs.filter(status='approved').aggregate(
            total=Sum('quantity')
        )['total'] or 0

    @staticmethod
    def _rescale_work_order_duration(work_order, old_quantity, new_quantity):
        from datetime import timedelta

        if not work_order.start_date or not work_order.end_date:
            return False
        if work_order.end_date <= work_order.start_date or old_quantity <= 0:
            return False

        current_seconds = (work_order.end_date - work_order.start_date).total_seconds()
        new_seconds = max(60, (current_seconds * new_quantity) / max(old_quantity, 1))
        work_order.end_date = work_order.start_date + timedelta(seconds=new_seconds)
        return True

    @staticmethod
    def _recalculate_work_order_progress(work_order, approved_qty=None):
        if approved_qty is None:
            approved_qty = WorkOrderService._approved_quantity_for_work_order(work_order)

        quantity = int(work_order.quantity or 0)
        if quantity <= 0:
            work_order.progress = 100
            return

        work_order.progress = min(100, (approved_qty / quantity) * 100)

    @staticmethod
    def _sync_base_quantity_to_current_quantity(work_order):
        if getattr(work_order, "base_quantity", None) is None:
            return False

        compensation_qty = int(getattr(work_order, "scrap_compensation_qty", 0) or 0)
        work_order.base_quantity = max(int(getattr(work_order, "quantity", 0) or 0) - compensation_qty, 0)
        return True

    @staticmethod
    def _sync_parent_quantity_from_stage_task(stage_task):
        parent = getattr(stage_task, "parent", None)
        if not parent:
            return False

        parent.quantity = int(getattr(stage_task, "quantity", 0) or 0)
        update_fields = ["quantity"]
        if WorkOrderService._sync_base_quantity_to_current_quantity(parent):
            update_fields.append("base_quantity")
        parent.save(update_fields=update_fields)
        return True

    @staticmethod
    def _route_stage_order_map(parent):
        bom = getattr(parent, "bom", None)
        if not bom:
            return {}
        return {
            operation.stage_id: operation.order
            for operation in bom.operations.filter(stage_id__isnull=False).order_by("order", "id")
        }

    @staticmethod
    def _downstream_main_branch_tasks(stage_task):
        parent = getattr(stage_task, "parent", None)
        if not parent:
            return []

        stage_order = WorkOrderService._route_stage_order_map(parent)
        source_order = stage_order.get(stage_task.stage_id or stage_task.current_stage_id)
        if source_order is None:
            return []

        return [
            task for task in parent.sub_tasks.exclude(status__in=["canceled", "archived"]).order_by("id")
            if task.id != stage_task.id
            and stage_order.get(task.stage_id or task.current_stage_id, -1) > source_order
            and (not task.source_task_id or task.source_task_id == stage_task.id)
        ]

    @staticmethod
    def _sync_downstream_main_branch_after_split(stage_task):
        downstream_tasks = WorkOrderService._downstream_main_branch_tasks(stage_task)
        target_qty = int(getattr(stage_task, "quantity", 0) or 0)

        blocked = [
            task for task in downstream_tasks
            if task.production_logs.exists() or task.status not in ["pending"]
        ]
        if blocked:
            raise ValueError("Cannot split because downstream route stages already started.")

        for task in downstream_tasks:
            old_qty = int(task.quantity or 0)
            task.quantity = target_qty
            update_fields = ["quantity", "source_task"]
            if WorkOrderService._sync_base_quantity_to_current_quantity(task):
                update_fields.append("base_quantity")
            if not task.source_task_id:
                task.source_task = stage_task
            if WorkOrderService._rescale_work_order_duration(task, old_qty, target_qty):
                update_fields.append("end_date")
            task.save(update_fields=list(dict.fromkeys(update_fields)))

        return downstream_tasks

    @staticmethod
    def _is_terminal_work_order(work_order):
        return str(getattr(work_order, "status", "") or "").strip().lower() in {
            "completed",
            "canceled",
            "archived",
        }

    @staticmethod
    def _assert_leaf_work_order(work_order, action_label):
        if work_order.sub_tasks.exists():
            raise ValueError(f"Cannot {action_label} a parent work order. Select a stage task.")

    @staticmethod
    def _split_return_candidate(cancelled_wo, explicit_target_id=None):
        from .models import WorkOrder

        compatible_filters = {
            "company_id": cancelled_wo.company_id,
            "bom_id": cancelled_wo.bom_id,
            "stage_id": cancelled_wo.stage_id,
            "current_stage_id": cancelled_wo.current_stage_id,
            "parent_id": cancelled_wo.parent_id,
            "product_name": cancelled_wo.product_name,
            "customer_id": cancelled_wo.customer_id,
            "operation_flow_mode": cancelled_wo.operation_flow_mode,
        }

        if explicit_target_id:
            target = WorkOrder.objects.select_for_update().filter(
                id=explicit_target_id,
                **compatible_filters,
            ).first()
            if not target:
                raise ValueError("Return target is not compatible with this split work order.")
            return target

        if cancelled_wo.source_task_id:
            source = WorkOrder.objects.select_for_update().filter(
                id=cancelled_wo.source_task_id,
                company_id=cancelled_wo.company_id,
            ).first()
            if source and not WorkOrderService._is_terminal_work_order(source):
                return source

        return (
            WorkOrder.objects.select_for_update()
            .filter(**compatible_filters)
            .exclude(id=cancelled_wo.id)
            .exclude(status__in=['completed', 'canceled', 'archived'])
            .order_by('id')
            .first()
        )

    @staticmethod
    def cancel_split_work_order(split_wo, user, return_to_wo_id=None):
        """
        Cancel an unstarted split child and return its quantity to a compatible
        source/sibling work order. Existing production logs are not moved.
        """
        from django.db import transaction
        from django.utils import timezone
        from .models import WorkOrder, WorkOrderChangeLog

        db_alias = split_wo._state.db or "default"

        with transaction.atomic(using=db_alias):
            split_wo = WorkOrder.objects.using(db_alias).select_for_update().get(pk=split_wo.pk)
            WorkOrderService._assert_leaf_work_order(split_wo, "cancel split on")

            if WorkOrderService._is_terminal_work_order(split_wo):
                raise ValueError("Cannot cancel a completed or inactive split work order.")

            if split_wo.production_logs.exists():
                raise ValueError("Cannot cancel split work order after production logs exist.")

            if not (split_wo.source_task_id or split_wo.parent_id):
                raise ValueError("This work order is not linked to a split group.")

            target = WorkOrderService._split_return_candidate(split_wo, return_to_wo_id)
            if not target:
                raise ValueError("No compatible active work order is available to receive the split quantity.")
            if target.id == split_wo.id:
                raise ValueError("Split quantity cannot be returned to the same work order.")
            if WorkOrderService._is_terminal_work_order(target):
                raise ValueError("Return target is completed or inactive.")
            if getattr(target, "closed_by_planner", False):
                raise ValueError("Return target is already closed by planner.")

            returned_qty = int(split_wo.quantity or 0)
            old_target_qty = int(target.quantity or 0)
            new_target_qty = old_target_qty + returned_qty

            target.quantity = new_target_qty
            target_fields = ['quantity', 'progress']
            if WorkOrderService._sync_base_quantity_to_current_quantity(target):
                target_fields.append('base_quantity')
            if WorkOrderService._rescale_work_order_duration(target, old_target_qty, new_target_qty):
                target_fields.append('end_date')
            WorkOrderService._recalculate_work_order_progress(target)
            target.save(update_fields=target_fields)
            WorkOrderService._sync_parent_quantity_from_stage_task(target)

            old_status = split_wo.status
            split_wo.status = 'canceled'
            split_wo.end_date = timezone.now()
            split_wo.assigned_worker = None
            split_wo.assignment_type = 'auto'
            split_wo.save(update_fields=['status', 'end_date', 'assigned_worker', 'assignment_type'])

            WorkOrderChangeLog.objects.using(db_alias).create(
                work_order=target,
                changed_by=user,
                action="Split quantity returned",
                field_name="quantity",
                old_value=str(old_target_qty),
                new_value=str(new_target_qty),
                note=f"Returned {returned_qty} units from canceled split WO #{split_wo.id}.",
            )
            WorkOrderChangeLog.objects.using(db_alias).create(
                work_order=split_wo,
                changed_by=user,
                action="Split canceled",
                field_name="status",
                old_value=old_status,
                new_value="canceled",
                note=f"Returned {returned_qty} units to WO #{target.id}.",
            )

        return {
            "canceled_work_order": split_wo,
            "target_work_order": target,
            "returned_quantity": returned_qty,
        }

    @staticmethod
    def _combine_compatibility_key(work_order):
        return (
            work_order.company_id,
            work_order.bom_id,
            work_order.stage_id,
            work_order.current_stage_id,
            work_order.parent_id,
            work_order.product_name,
            work_order.customer_id,
            work_order.order_type,
            work_order.operation_flow_mode,
            bool(work_order.qc_requirement),
        )

    @staticmethod
    def combine_work_orders(work_orders, user, target_wo=None):
        """
        Merge compatible active stage work orders into one target WO. Secondary
        records are canceled for auditability; their logs are not moved.
        """
        from django.db import transaction
        from django.utils import timezone
        from .models import WorkOrder, WorkOrderChangeLog

        ids = [int(wo.id) for wo in work_orders if getattr(wo, "id", None)]
        ids = list(dict.fromkeys(ids))
        if len(ids) < 2:
            raise ValueError("At least two work orders are required to combine.")

        target_id = getattr(target_wo, "id", None)
        if target_id and target_id not in ids:
            raise ValueError("Target work order must be included in the combine set.")

        db_alias = (
            getattr(getattr(target_wo, "_state", None), "db", None)
            or next((getattr(getattr(wo, "_state", None), "db", None) for wo in work_orders if getattr(getattr(wo, "_state", None), "db", None)), None)
            or "default"
        )

        with transaction.atomic(using=db_alias):
            locked = list(
                WorkOrder.objects.using(db_alias).select_for_update()
                .filter(id__in=ids)
                .order_by('id')
            )
            if len(locked) != len(ids):
                raise ValueError("One or more work orders could not be found.")

            companies = {wo.company_id for wo in locked}
            if len(companies) != 1:
                raise ValueError("Cannot combine work orders across companies.")

            for wo in locked:
                WorkOrderService._assert_leaf_work_order(wo, "combine")
                if WorkOrderService._is_terminal_work_order(wo):
                    raise ValueError("Cannot combine completed or inactive work orders.")
                if getattr(wo, "closed_by_planner", False):
                    raise ValueError("Cannot combine a work order already closed by planner.")

            compatibility_key = WorkOrderService._combine_compatibility_key(locked[0])
            if any(WorkOrderService._combine_compatibility_key(wo) != compatibility_key for wo in locked[1:]):
                raise ValueError("Work orders are not compatible for combine.")

            target = next((wo for wo in locked if wo.id == target_id), locked[0])
            sources = [wo for wo in locked if wo.id != target.id]
            blocked_with_logs = [wo.id for wo in sources if wo.production_logs.exists()]
            if blocked_with_logs:
                raise ValueError(
                    "Cannot combine source work orders after production logs exist: "
                    + ", ".join(str(wo_id) for wo_id in blocked_with_logs)
                )

            old_target_qty = int(target.quantity or 0)
            combined_qty = sum(int(wo.quantity or 0) for wo in locked)
            target.quantity = combined_qty
            target_fields = ['quantity', 'progress']
            if WorkOrderService._sync_base_quantity_to_current_quantity(target):
                target_fields.append('base_quantity')
            if WorkOrderService._rescale_work_order_duration(target, old_target_qty, combined_qty):
                target_fields.append('end_date')
            WorkOrderService._recalculate_work_order_progress(target)
            target.save(update_fields=target_fields)
            WorkOrderService._sync_parent_quantity_from_stage_task(target)

            canceled_ids = []
            for source in sources:
                old_status = source.status
                source.status = 'canceled'
                source.end_date = timezone.now()
                source.assigned_worker = None
                source.assignment_type = 'auto'
                source.save(update_fields=['status', 'end_date', 'assigned_worker', 'assignment_type'])
                canceled_ids.append(source.id)
                WorkOrderChangeLog.objects.using(db_alias).create(
                    work_order=source,
                    changed_by=user,
                    action="Combined into work order",
                    field_name="status",
                    old_value=old_status,
                    new_value="canceled",
                    note=f"Combined into WO #{target.id}; quantity retained in audit record.",
                )

            WorkOrderChangeLog.objects.using(db_alias).create(
                work_order=target,
                changed_by=user,
                action="Work orders combined",
                field_name="quantity",
                old_value=str(old_target_qty),
                new_value=str(combined_qty),
                note=f"Combined source WOs: {', '.join(str(wo_id) for wo_id in canceled_ids)}.",
            )

        return {
            "target_work_order": target,
            "combined_quantity": combined_qty,
            "canceled_work_order_ids": canceled_ids,
        }

    @staticmethod
    def find_next_available_slot(machine, duration_minutes, start_after, exclude_wo_id=None):
        """
        Find the first available machine slot while respecting machine-specific
        shift availability.
        """
        from .models import WorkOrder
        from datetime import datetime, timedelta
        from django.utils import timezone

        def _machine_windows(start_dt, days_ahead=30):
            if not machine:
                return []
            local_start = timezone.localtime(start_dt)
            tz = local_start.tzinfo
            config = machine_shift_configuration(
                machine,
                getattr(getattr(machine, "company", None), "system_settings", None),
            )
            anchor_date = local_start.date() - timedelta(days=1)
            windows = []
            for offset in range(days_ahead + 2):
                base_date = anchor_date + timedelta(days=offset)
                for shift_key in ("morning", "afternoon", "night"):
                    entry = config.get(shift_key, {})
                    if not entry.get("enabled"):
                        continue
                    start_time = datetime.strptime(entry["start"], "%H:%M").time()
                    end_time = datetime.strptime(entry["end"], "%H:%M").time()
                    window_start = timezone.make_aware(datetime.combine(base_date, start_time), tz)
                    window_end = timezone.make_aware(datetime.combine(base_date, end_time), tz)
                    if end_time <= start_time:
                        window_end += timedelta(days=1)
                    windows.append((window_start, window_end))
            return sorted(windows, key=lambda item: item[0])

        def _align_to_machine_window(dt):
            for window_start, window_end in _machine_windows(dt):
                if window_start <= dt < window_end:
                    return dt
                if dt < window_start:
                    return window_start
            return dt

        def _machine_end_for_duration(start_dt, total_minutes):
            remaining = max(int(total_minutes or 0), 1)
            current = start_dt
            for window_start, window_end in _machine_windows(start_dt):
                if current >= window_end:
                    continue
                if current < window_start:
                    current = window_start
                available_minutes = int((window_end - current).total_seconds() // 60)
                if available_minutes <= 0:
                    continue
                consumed = min(remaining, available_minutes)
                current += timedelta(minutes=consumed)
                remaining -= consumed
                if remaining <= 0:
                    return current
            return current + timedelta(minutes=remaining)

        duration_minutes = max(int(duration_minutes or 0), 1)
        potential_start = _align_to_machine_window(start_after)

        query = WorkOrder.objects.filter(
            machine=machine,
            end_date__gte=potential_start,
            start_date__isnull=False,
        ).exclude(status__in=["canceled", "completed", "archived"])

        if exclude_wo_id:
            if isinstance(exclude_wo_id, (list, tuple, set)):
                query = query.exclude(id__in=list(exclude_wo_id))
            else:
                query = query.exclude(id=exclude_wo_id)

        existing_bookings = list(query.order_by("start_date"))

        while True:
            potential_start = _align_to_machine_window(potential_start)
            potential_end = _machine_end_for_duration(potential_start, duration_minutes)
            conflict = next(
                (
                    booking
                    for booking in existing_bookings
                    if booking.start_date < potential_end and booking.end_date > potential_start
                ),
                None,
            )
            if not conflict:
                return potential_start, potential_end
            potential_start = max(conflict.end_date, potential_start)

    @staticmethod
    def _ceil_datetime_to_snap(dt, snap_minutes):
        from datetime import timedelta

        if not dt:
            return dt
        try:
            snap_minutes = max(int(snap_minutes or 0), 1)
        except Exception:
            return dt

        base_dt = dt.replace(second=0, microsecond=0)
        minute_remainder = base_dt.minute % snap_minutes
        shift_minutes = (snap_minutes - minute_remainder) % snap_minutes

        if dt.second or dt.microsecond:
            if shift_minutes == 0:
                shift_minutes = snap_minutes
        elif minute_remainder == 0:
            return base_dt

        return base_dt + timedelta(minutes=shift_minutes)

    @staticmethod
    def _find_snapped_available_slot(machine, duration_minutes, requested_start, snap_minutes, exclude_wo_id=None):
        slot_start = requested_start
        slot_end = requested_start

        for _ in range(12):
            slot_start, slot_end = WorkOrderService.find_next_available_slot(
                machine,
                duration_minutes,
                slot_start,
                exclude_wo_id=exclude_wo_id,
            )
            snapped_start = WorkOrderService._ceil_datetime_to_snap(slot_start, snap_minutes)
            if snapped_start == slot_start:
                return slot_start, slot_end
            slot_start = snapped_start

        return slot_start, slot_end

    @staticmethod
    def _shift_start_past_fixed_bookings(machine, requested_start, duration_minutes, exclude_wo_id=None, snap_minutes=5):
        from datetime import timedelta
        from .models import WorkOrder

        needed_duration = timedelta(minutes=max(int(duration_minutes or 0), 1))
        potential_start = requested_start

        query = WorkOrder.objects.filter(
            machine=machine,
            status='in_progress',
            start_date__isnull=False,
            end_date__isnull=False,
        )
        if exclude_wo_id:
            if isinstance(exclude_wo_id, (list, tuple, set)):
                query = query.exclude(id__in=list(exclude_wo_id))
            else:
                query = query.exclude(id=exclude_wo_id)

        while True:
            potential_end = potential_start + needed_duration
            conflict = (
                query.filter(start_date__lt=potential_end, end_date__gt=potential_start)
                .order_by('start_date')
                .first()
            )
            if not conflict:
                return potential_start
            potential_start = WorkOrderService._ceil_datetime_to_snap(conflict.end_date, snap_minutes)

    @staticmethod
    def snap_scheduled_work_orders(company, snap_minutes):
        from .models import WorkOrder

        try:
            snap_minutes = max(int(snap_minutes or 0), 1)
        except Exception:
            raise ValueError("Snap minutes must be a positive number.")

        scheduled_task_ids = list(
            WorkOrder.objects.filter(
                company=company,
                machine__isnull=False,
                start_date__isnull=False,
                end_date__isnull=False,
                sub_tasks__isnull=True,
            )
            .exclude(status__in=['in_progress', 'completed', 'canceled', 'archived'])
            .order_by('start_date', 'id')
            .values_list('id', flat=True)
        )

        processed_ids = set()
        machine_busy_until = {}
        changed_ids = []

        for task_id in scheduled_task_ids:
            if task_id in processed_ids:
                continue

            work_order = (
                WorkOrder.objects.select_related('parent', 'machine', 'bom', 'stage', 'current_stage')
                .filter(id=task_id, company=company)
                .first()
            )
            if not work_order:
                continue

            status_value = str(getattr(work_order, 'status', '') or '').strip().lower()
            if status_value in {'in_progress', 'completed', 'done', 'canceled', 'archived'}:
                continue
            if not work_order.machine_id or not work_order.start_date or not work_order.end_date:
                continue

            duration_minutes = max(
                int(round((work_order.end_date - work_order.start_date).total_seconds() / 60)),
                1,
            )
            machine_floor = machine_busy_until.get(work_order.machine_id)
            requested_start = machine_floor or work_order.start_date

            exclude_ids = [work_order.id]
            if work_order.parent_id:
                exclude_ids.append(work_order.parent_id)

            if work_order.parent_id and normalize_operation_flow_mode(get_work_order_operation_flow_mode(work_order.parent)) == 'series':
                ordered_subtasks = WorkOrderService._ordered_subtasks_for_parent(work_order.parent)
                current_index = next((idx for idx, task in enumerate(ordered_subtasks) if task.id == work_order.id), None)
                if current_index is not None and current_index > 0:
                    previous_task = ordered_subtasks[current_index - 1]
                    previous_end = previous_task.end_date or previous_task.start_date
                    if previous_end and (not requested_start or previous_end > requested_start):
                        requested_start = previous_end

            requested_start = WorkOrderService._ceil_datetime_to_snap(requested_start, snap_minutes)

            snapped_start = WorkOrderService._shift_start_past_fixed_bookings(
                work_order.machine,
                requested_start,
                duration_minutes,
                exclude_wo_id=exclude_ids,
                snap_minutes=snap_minutes,
            )
            snapped_end = snapped_start + (work_order.end_date - work_order.start_date)

            update_fields = []
            if work_order.start_date != snapped_start:
                work_order.start_date = snapped_start
                update_fields.append('start_date')
            if work_order.end_date != snapped_end:
                work_order.end_date = snapped_end
                update_fields.append('end_date')
            if getattr(work_order, 'scheduled_start_date', None) != snapped_start:
                work_order.scheduled_start_date = snapped_start
                update_fields.append('scheduled_start_date')
            if update_fields:
                work_order.save(update_fields=update_fields)
                if work_order.id not in changed_ids:
                    changed_ids.append(work_order.id)

            processed_ids.add(work_order.id)
            if work_order.machine_id and work_order.end_date:
                previous_busy = machine_busy_until.get(work_order.machine_id)
                if previous_busy is None or work_order.end_date > previous_busy:
                    machine_busy_until[work_order.machine_id] = work_order.end_date

        return {
            "snap_minutes": snap_minutes,
            "changed_count": len(changed_ids),
            "changed_ids": changed_ids,
        }

    @staticmethod
    def find_first_available_machine(stage, company, start_after, duration_minutes, machine_type_hint=None, preferred_machine=None):
        """
        Find the first available machine for a stage.
        Priority: 
        1. Machine linked to the stage (if stage has a machine)
        2. Operational machines with least workload
        3. Any operational machine
        Returns: Machine object or None
        """
        class StageOperationHint:
            def __init__(self, stage_obj, machine_type_value, preferred_machine_obj):
                self.stage = stage_obj
                self.machine_type = machine_type_value
                self.machine = preferred_machine_obj

        hint = StageOperationHint(stage, machine_type_hint, preferred_machine)
        candidate_machines = WorkOrderService._candidate_machines_for_operation(
            hint,
            company,
            preferred_machine=preferred_machine,
        )
        best_machine, _, _ = WorkOrderService._pick_best_machine_slot(
            candidate_machines,
            duration_minutes,
            start_after,
        )
        return best_machine

    @staticmethod
    def create_subtasks(parent_wo, user, company=None):
        """
        Explode a Work Order into sub-tasks based on BOM Operations.
        ⚡ Uses Finite Capacity Scheduling to prevent overlaps.
        🔄 Auto-assigns machines when BOM operations don't specify machines.
        """
        from .models import WorkOrder
        from datetime import timedelta
        
        if not parent_wo.bom or not parent_wo.bom.operations.exists():
            return
            
        operations = parent_wo.bom.operations.all().order_by('order')
        
        # Start looking for slots from the parent WO start date
        search_start_time = parent_wo.start_date
        
        # Dependent tasks must happen sequentially
        # Task B starts only after Task A finishes
        previous_task_end = search_start_time
        
        # Use company from parent_wo if not provided
        subtask_company = company or parent_wo.company
        
        for i, op in enumerate(operations):
            duration_minutes = WorkOrderService._compute_operation_duration(op, parent_wo.quantity)
            
            # 🎯 Auto-assign machine if not specified in BOM operation
            assigned_machine = op.machine
            if not assigned_machine and op.stage:
                # Try to find best available machine for this stage
                assigned_machine = WorkOrderService.find_first_available_machine(
                    op.stage,
                    subtask_company,
                    previous_task_end,
                    duration_minutes,
                    machine_type_hint=getattr(op, 'machine_type', None),
                    preferred_machine=getattr(op, 'machine', None),
                )
            
            # 🧠 Smart Scheduling Logic
            if assigned_machine:
                # Find space on the assigned machine
                # It must start 'previous_task_end' at the earliest (sequential dependency)
                actual_start, actual_end = WorkOrderService.find_next_available_slot(
                    assigned_machine, 
                    duration_minutes, 
                    previous_task_end
                )
            else:
                # If no machine (e.g. manual work), just schedule sequentially
                actual_start = previous_task_end
                actual_end = actual_start + timedelta(minutes=duration_minutes)
            
            # Create Sub-Task
            WorkOrder.objects.create(
                parent=parent_wo,
                product_name=f"{parent_wo.product_name} - {op.stage.name if op.stage else 'Op ' + str(op.order)}",
                bom=parent_wo.bom,
                quantity=parent_wo.quantity,
                machine=assigned_machine,  # Use auto-assigned machine if BOM didn't specify
                stage=op.stage,
                assigned_to=user,
                status='pending',
                start_date=actual_start,
                end_date=actual_end,
                progress=0,
                company=subtask_company,  # Ensure company is set
                operation_flow_mode=get_work_order_operation_flow_mode(parent_wo),
                qc_requirement=getattr(op.stage, 'is_quality_check', False)
            )
            
            # Update sequence pointer
            previous_task_end = actual_end

        # 4. Clear Machine from Parent (It is now a Container)
        # This prevents the Parent WO from appearing on the Timeline alongside its children
        parent_wo.machine = None
        if parent_wo.status == 'pending':
            WorkOrderLifecycle.apply_transition(
                parent_wo,
                'in_progress',
                actor=None,
                allow_system=True,
                save=False
            )
        parent_wo.save(update_fields=['machine', 'status'])

    @staticmethod
    def reschedule_subtasks(parent_wo):
        """
        Reschedule all subtasks for a parent work order.
        This is called when a planner changes machine assignments.
        Maintains sequential dependency but recalculates all schedules.
        """
        WorkOrderService.reschedule_subtasks_from_anchor(parent_wo)

    @staticmethod
    def _ordered_subtasks_for_parent(parent_wo):
        from .models import WorkOrder

        subtasks = list(
            WorkOrder.objects.filter(parent=parent_wo)
            .exclude(status__in=['canceled', 'archived'])
            .select_related('stage', 'machine')
            .order_by('id')
        )
        if not subtasks or not getattr(parent_wo, 'bom_id', None):
            return subtasks

        stage_order_lookup = {}
        for fallback_index, op in enumerate(
            parent_wo.bom.operations.exclude(stage_id__isnull=True).order_by('order', 'id'),
            start=1,
        ):
            stage_order_lookup.setdefault(op.stage_id, int(op.order or fallback_index))

        return sorted(
            subtasks,
            key=lambda task: (
                stage_order_lookup.get(task.stage_id, 10**6),
                task.id,
            ),
        )

    @staticmethod
    def validate_series_stage_move(parent_wo, anchor_task, requested_start):
        if not parent_wo or not anchor_task or not requested_start:
            return
        if normalize_operation_flow_mode(get_work_order_operation_flow_mode(parent_wo)) == 'parallel':
            return

        subtasks = WorkOrderService._ordered_subtasks_for_parent(parent_wo)
        anchor_index = next((idx for idx, task in enumerate(subtasks) if task.id == anchor_task.id), None)
        if anchor_index is None or anchor_index == 0:
            return

        previous_task = subtasks[anchor_index - 1]
        previous_end = previous_task.end_date or previous_task.start_date
        if not previous_end:
            return
        if requested_start >= previous_end:
            return

        current_stage_name = getattr(getattr(anchor_task, 'stage', None), 'name', None) or f"WO #{anchor_task.id}"
        previous_stage_name = getattr(getattr(previous_task, 'stage', None), 'name', None) or f"WO #{previous_task.id}"
        raise ValueError(
            f"{current_stage_name} cannot start before {previous_stage_name} finishes."
        )

    @staticmethod
    def reschedule_subtasks_from_anchor(parent_wo, anchor_task=None, recalculate_durations=False):
        """
        Rebuild sequential timing from a moved/edited stage onward.
        Earlier stages stay fixed; downstream series stages follow the anchor.
        """
        from datetime import timedelta
        from django.utils import timezone

        if not parent_wo.bom:
            return
        if normalize_operation_flow_mode(get_work_order_operation_flow_mode(parent_wo)) == 'parallel':
            return

        subtasks = WorkOrderService._ordered_subtasks_for_parent(parent_wo)
        if not subtasks:
            return

        route_exclude_ids = [parent_wo.id] + [subtask.id for subtask in subtasks if subtask.id]

        operations_by_stage = {}
        for op in parent_wo.bom.operations.exclude(stage_id__isnull=True).order_by('order', 'id'):
            operations_by_stage.setdefault(op.stage_id, op)

        anchor_index = 0
        requested_anchor_start = None
        if anchor_task is not None:
            for idx, subtask in enumerate(subtasks):
                if subtask.id == anchor_task.id:
                    anchor_index = idx
                    requested_anchor_start = anchor_task.start_date
                    break

        if anchor_index > 0:
            previous_task_end = (
                subtasks[anchor_index - 1].end_date
                or subtasks[anchor_index - 1].start_date
                or timezone.now()
            )
        else:
            previous_task_end = parent_wo.start_date or subtasks[0].start_date or timezone.now()

        for idx in range(anchor_index, len(subtasks)):
            subtask = subtasks[idx]
            duration_minutes = 60
            operation = operations_by_stage.get(subtask.stage_id)
            if operation:
                try:
                    duration_minutes = max(
                        1,
                        int(round(WorkOrderService._compute_operation_duration(operation, parent_wo.quantity))),
                    )
                except Exception:
                    duration_minutes = max(int(getattr(operation, 'duration_minutes', 60) or 60), 1)

            if not recalculate_durations and subtask.end_date and subtask.start_date:
                existing_duration = subtask.end_date - subtask.start_date
                if isinstance(existing_duration, timedelta):
                    duration_minutes = max(int(existing_duration.total_seconds() / 60), 1)

            requested_start = previous_task_end
            if idx == anchor_index and requested_anchor_start:
                requested_start = max(requested_anchor_start, previous_task_end)

            if idx == anchor_index and anchor_task is not None:
                actual_start = requested_start
                explicit_end = getattr(anchor_task, 'end_date', None)
                if not recalculate_durations and explicit_end and explicit_end >= actual_start:
                    actual_end = explicit_end
                else:
                    actual_end = actual_start + timedelta(minutes=duration_minutes)
            elif not subtask.machine:
                actual_start = requested_start
                actual_end = requested_start + timedelta(minutes=duration_minutes)
            else:
                actual_start, actual_end = WorkOrderService.find_next_available_slot(
                    subtask.machine,
                    duration_minutes,
                    requested_start,
                    exclude_wo_id=route_exclude_ids,
                )

            update_fields = []
            if subtask.start_date != actual_start:
                subtask.start_date = actual_start
                update_fields.append('start_date')
            if subtask.end_date != actual_end:
                subtask.end_date = actual_end
                update_fields.append('end_date')
            if subtask.scheduled_start_date != actual_start:
                subtask.scheduled_start_date = actual_start
                update_fields.append('scheduled_start_date')
            if update_fields:
                subtask.save(update_fields=update_fields)

            previous_task_end = actual_end

    @staticmethod
    def has_parallel_stage_tasks(parent_wo):
        """
        Returns True if a parent has multiple tasks for the same stage (parallel splits).
        Used to avoid sequential rescheduling on parallel stages.
        """
        from django.db.models import Count
        from .models import WorkOrder

        return WorkOrder.objects.filter(parent=parent_wo, stage_id__isnull=False).values(
            'stage_id'
        ).annotate(
            cnt=Count('id')
        ).filter(cnt__gt=1).exists()


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
