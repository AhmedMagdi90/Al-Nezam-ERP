from decimal import Decimal, ROUND_CEILING

from django.db import transaction

from manufacturing.models import BillOfMaterial, WorkOrder


def _to_decimal(value, default="0"):
    if value is None or value == "":
        return Decimal(default)
    return Decimal(str(value))


def required_component_quantity(component, parent_quantity):
    bom = component.bom
    base_quantity = _to_decimal(getattr(bom, "base_quantity", None), "1")
    if base_quantity <= 0:
        raise ValueError("BOM base quantity must be greater than zero.")
    return (_to_decimal(component.quantity) * _to_decimal(parent_quantity)) / base_quantity


def ceil_work_order_quantity(value):
    value = _to_decimal(value)
    if value <= 0:
        return 0
    return int(value.to_integral_value(rounding=ROUND_CEILING))


def estimate_store_received_stock(product, company):
    """
    Current ERP has no stock ledger. Treat store-received completed WOs as the
    only reliable finished-goods stock signal until inventory is formalized.
    """
    if not product or not company:
        return Decimal("0")

    total = Decimal("0")
    for work_order in (
        WorkOrder.objects.filter(
            company=company,
            bom__product=product,
            status="completed",
            store_receipt_status="received",
        )
        .only("quantity", "store_received_qty")
        .order_by("id")
    ):
        total += _to_decimal(work_order.store_received_qty or work_order.quantity)
    return total


def iter_manufactured_subassembly_requirements(parent_work_order):
    bom = getattr(parent_work_order, "bom", None)
    company = getattr(parent_work_order, "company", None)
    if not bom or not company:
        return

    components = (
        bom.components.select_related("product", "sub_bom", "sub_bom__product")
        .filter(sub_bom__isnull=False, sub_bom__status="active")
        .order_by("id")
    )
    for component in components:
        sub_bom = component.sub_bom
        product = sub_bom.product or component.product
        if not product or product.company_id != company.id:
            continue

        required_qty = required_component_quantity(component, parent_work_order.quantity)
        available_qty = estimate_store_received_stock(product, company)
        shortage_qty = required_qty - available_qty
        if shortage_qty <= 0:
            continue

        yield {
            "component": component,
            "sub_bom": sub_bom,
            "product": product,
            "required_qty": required_qty,
            "available_qty": available_qty,
            "shortage_qty": shortage_qty,
            "work_order_qty": ceil_work_order_quantity(shortage_qty),
        }


@transaction.atomic
def create_subassembly_work_orders_for_shortages(parent_work_order, actor=None):
    created = []
    for requirement in iter_manufactured_subassembly_requirements(parent_work_order):
        quantity = requirement["work_order_qty"]
        if quantity <= 0:
            continue

        child = WorkOrder.objects.create(
            company=parent_work_order.company,
            product_name=requirement["product"].name,
            bom=requirement["sub_bom"],
            quantity=quantity,
            customer=parent_work_order.customer,
            status="pending",
            due_date=parent_work_order.due_date,
            priority=parent_work_order.priority,
            assigned_to=actor or parent_work_order.assigned_to,
            operation_flow_mode=parent_work_order.operation_flow_mode,
            subassembly_parent=parent_work_order,
            source_bom_component=requirement["component"],
        )
        created.append(child)

    if created:
        child_refs = ", ".join(f"WO #{child.id}" for child in created)
        existing_note = (parent_work_order.material_shortage_note or "").strip()
        dependency_note = f"Sub-assembly work orders created: {child_refs}."
        parent_work_order.material_readiness_status = "shortage"
        parent_work_order.material_shortage_note = (
            f"{existing_note}\n{dependency_note}".strip() if existing_note else dependency_note
        )
        parent_work_order.save(
            update_fields=["material_readiness_status", "material_shortage_note"]
        )

    return created


def resolve_active_sub_bom_for_component(parent_bom, component_product):
    if not parent_bom or not component_product:
        return None
    if parent_bom.product_id and component_product.id == parent_bom.product_id:
        return None

    return (
        BillOfMaterial.objects.filter(
            product=component_product,
            product__company=parent_bom.product.company,
            status="active",
        )
        .exclude(id=parent_bom.id)
        .order_by("-created_at", "-id")
        .first()
    )
