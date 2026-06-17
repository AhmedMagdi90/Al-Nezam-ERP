"""Presentation helpers for operational audit events."""


AUDIT_EVENT_LABELS = {
    "bom_change_archive_new": "BOM Change: Archive and Replace",
    "bom_change_continue_old": "BOM Change: Continue Old BOM",
    "bom_change_scrap_apply": "BOM Change: Scrap and Apply",
    "latest_bom_applied": "Latest BOM Applied",
    "machine_created": "Machine Created",
    "machine_updated": "Machine Updated",
    "machine_shift_updated": "Machine Working Hours Updated",
    "material_readiness_updated": "Material Readiness Updated",
    "production_log_approved": "Production Log Approved",
    "production_log_rejected": "Production Log Rejected",
    "production_log_updated": "Production Log Updated",
    "production_logged": "Production Logged",
    "shop_floor_status_updated": "Shop Floor Status Updated",
    "store_receipt_confirmed": "Store Receipt Confirmed",
    "work_order_closed": "Planner Closed Work Order",
    "work_order_created": "Work Order Created",
    "work_order_released_to_next_stage": "Released to Next Stage",
    "work_order_route_scheduled": "Route Planned",
    "work_order_route_status_updated": "Route Status Updated",
    "work_order_scheduled": "Work Order Scheduled",
    "work_order_split": "Work Order Split",
    "work_order_split_created": "Work Order Split Created",
    "work_order_start_date_updated": "Start Date Updated",
    "work_order_unscheduled": "Work Order Unscheduled",
    "work_order_updated_from_timeline": "Timeline Updated Work Order",
    "worker_assigned": "Worker Assigned",
}

STATUS_LABELS = {
    "not_checked": "Waiting for Store Confirmation",
    "ready": "Material OK",
    "partial": "Partially OK",
    "shortage": "Not Available",
    "in_progress": "In Progress",
}


def readable_audit_event(event_name):
    event = str(event_name or "").strip()
    if not event:
        return ""
    return AUDIT_EVENT_LABELS.get(event, event.replace("_", " ").title())


def readable_status(status):
    value = str(status or "").strip()
    if not value:
        return ""
    return STATUS_LABELS.get(value.lower(), value.replace("_", " ").title())


def _has_value(value):
    return value not in (None, "", [])


def _append_field(parts, details, key, label, *, formatter=None):
    value = details.get(key)
    if not _has_value(value):
        return
    if formatter:
        value = formatter(value)
    parts.append(f"{label}: {value}")


def audit_summary_text(details):
    details = details or {}
    parts = []

    previous_bom = details.get("previous_bom_version") or details.get("previous_bom_id")
    new_bom = details.get("new_bom_version") or details.get("new_bom_id") or details.get("latest_bom_id")
    if _has_value(previous_bom) and _has_value(new_bom):
        parts.append(f"BOM: {previous_bom} -> {new_bom}")
    elif _has_value(new_bom):
        parts.append(f"BOM: {new_bom}")

    for key, label in (
        ("work_order_id", "WO"),
        ("source_work_order_id", "From WO"),
        ("target_work_order_id", "To WO"),
        ("replacement_wo_id", "Replacement WO"),
        ("first_stage_work_order_id", "First Stage WO"),
        ("machine_id", "Machine"),
        ("machine_code", "Machine Code"),
        ("target_machine_id", "Target Machine"),
        ("stage_id", "Stage"),
        ("assigned_worker_id", "Worker ID"),
        ("worker_username", "Worker"),
        ("quantity", "Qty"),
        ("available_qty", "Available Qty"),
        ("available_percent", "Available %"),
        ("expected_delivery_date", "Expected Delivery"),
        ("received_qty", "Received Qty"),
        ("scrap_qty", "Scrap Qty"),
        ("scrapped_qty", "Scrapped Qty"),
        ("stage_count", "Stages"),
        ("route_stage_count", "Stages"),
        ("route_task_count", "Route Tasks"),
        ("shift_propagated_count", "Machines Updated"),
        ("operation_flow_mode", "Flow"),
    ):
        _append_field(parts, details, key, label)

    changed_fields = details.get("changed_fields")
    if isinstance(changed_fields, (list, tuple)) and changed_fields:
        parts.append(f"Fields: {', '.join(str(field) for field in changed_fields)}")

    _append_field(parts, details, "status", "Status", formatter=readable_status)

    if details.get("quantity_changed") is True:
        parts.append("Quantity changed during planning")
    if details.get("closed_by_planner") is True:
        parts.append("Closed by planner")

    _append_field(parts, details, "shortage_note", "Material Note")
    _append_field(parts, details, "note", "Note")
    _append_field(parts, details, "notes", "Note")

    return " | ".join(parts)
