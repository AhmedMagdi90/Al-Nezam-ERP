"""BOM service extraction.

This module owns BOM resolution, BOM change lifecycle helpers, and the
``BOMService`` implementation. ``manufacturing.services`` remains the public
compatibility facade and re-exports these names for existing callers.
"""

from decimal import Decimal

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from manufacturing.models import BillOfMaterial, Product, ProductionLog, WorkOrder
from manufacturing.units import UnitService

__all__ = [
    "BOMService",
    "resolve_bom_for_work_order",
    "get_latest_active_bom_for_work_order",
    "_work_order_db_alias",
    "_work_order_family_ids",
    "get_workorder_production_totals",
    "work_order_has_started_production",
    "get_workorder_bom_change_payload",
    "clear_work_order_assignment_for_bom_change",
    "flag_bom_change_impact",
    "get_apply_latest_bom_eligibility",
    "apply_latest_bom_to_work_order",
    "decide_bom_change_archive_and_replace",
    "decide_bom_change_scrap_and_apply",
    "decide_bom_change_continue_old",
]


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
                from manufacturing.models import WorkOrderChangeLog

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
        ðŸ”® Simulate a production run.
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
