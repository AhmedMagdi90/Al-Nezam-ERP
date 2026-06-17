from __future__ import annotations

from django.contrib.auth import get_user_model
from django.db import transaction

from manufacturing.models import (
    BOMAcceptanceCriteria,
    BOMComponent,
    BOMOperation,
    BOMOperationMaterial,
    BillOfMaterial,
    Company,
    Machine,
    Product,
    ProductionStage,
    SystemSettings,
)

from .db import ensure_tenant_database_registered
from .models import Tenant


def _source_company_for_alias(db_alias: str) -> Company | None:
    return Company.objects.using(db_alias).order_by("id").first()


def _target_owner_user(db_alias: str, owner_email: str):
    if not owner_email:
        return None
    user_model = get_user_model()
    return user_model.objects.using(db_alias).filter(username__iexact=owner_email).first()


def copy_environment_setup(source_tenant: Tenant, target_tenant: Tenant) -> dict[str, int]:
    if not source_tenant or not target_tenant:
        raise ValueError("Source and target tenants are required.")
    if source_tenant.id == target_tenant.id:
        raise ValueError("Source and target tenants must be different.")
    if source_tenant.organization_id and target_tenant.organization_id and source_tenant.organization_id != target_tenant.organization_id:
        raise ValueError("Source and target tenants must belong to the same organization.")

    source_alias = ensure_tenant_database_registered(source_tenant)
    target_alias = ensure_tenant_database_registered(target_tenant)

    source_company = _source_company_for_alias(source_alias)
    target_company = _source_company_for_alias(target_alias)
    if not source_company or not target_company:
        raise ValueError("Both source and target tenants must contain a company record.")

    source_settings = SystemSettings.objects.using(source_alias).filter(company=source_company).first()
    if source_settings:
        settings_defaults = {
            field.name: getattr(source_settings, field.name)
            for field in SystemSettings._meta.concrete_fields
            if field.name not in {"id", "company"}
        }
        SystemSettings.objects.using(target_alias).update_or_create(
            company=target_company,
            defaults=settings_defaults,
        )

    target_owner = _target_owner_user(target_alias, target_tenant.owner_email or source_tenant.owner_email or "")

    machine_map: dict[int, Machine] = {}
    product_map: dict[int, Product] = {}
    stage_map: dict[int, ProductionStage] = {}
    bom_map: dict[int, BillOfMaterial] = {}
    component_map: dict[int, BOMComponent] = {}
    operation_map: dict[int, BOMOperation] = {}

    source_machines = list(Machine.objects.using(source_alias).filter(company=source_company).order_by("id"))
    source_products = list(Product.objects.using(source_alias).filter(company=source_company).order_by("id"))
    source_stages = list(ProductionStage.objects.using(source_alias).filter(machine__company=source_company).order_by("id"))
    source_boms = list(BillOfMaterial.objects.using(source_alias).filter(product__company=source_company).order_by("id"))

    with transaction.atomic(using=target_alias):
        for source_machine in source_machines:
            target_machine, _created = Machine.objects.using(target_alias).update_or_create(
                company=target_company,
                code=source_machine.code,
                defaults={
                    "name": source_machine.name,
                    "type": source_machine.type,
                    "category": source_machine.category,
                    "status": source_machine.status,
                    "is_active": source_machine.is_active,
                    "image": source_machine.image,
                    "maintenance_note": source_machine.maintenance_note,
                    "hourly_rate": source_machine.hourly_rate,
                    "use_factory_shifts": source_machine.use_factory_shifts,
                    "shift_configuration": source_machine.shift_configuration,
                    "last_maintenance_date": source_machine.last_maintenance_date,
                    "total_runtime_hours": source_machine.total_runtime_hours,
                },
            )
            machine_map[source_machine.id] = target_machine

        for source_product in source_products:
            target_product, _created = Product.objects.using(target_alias).update_or_create(
                company=target_company,
                name=source_product.name,
                defaults={
                    "description": source_product.description,
                    "unit": source_product.unit,
                    "material_type": source_product.material_type,
                    "image": source_product.image,
                },
            )
            product_map[source_product.id] = target_product

        for source_stage in source_stages:
            target_machine = machine_map.get(source_stage.machine_id)
            target_stage = (
                ProductionStage.objects.using(target_alias)
                .filter(name=source_stage.name, order=source_stage.order, machine=target_machine)
                .first()
            )
            if target_stage is None:
                target_stage = ProductionStage.objects.using(target_alias).create(
                    name=source_stage.name,
                    machine=target_machine,
                    is_quality_check=source_stage.is_quality_check,
                    category=source_stage.category,
                    order=source_stage.order,
                    color=source_stage.color,
                )
            else:
                target_stage.machine = target_machine
                target_stage.is_quality_check = source_stage.is_quality_check
                target_stage.category = source_stage.category
                target_stage.color = source_stage.color
                target_stage.save(using=target_alias, update_fields=["machine", "is_quality_check", "category", "color"])
            stage_map[source_stage.id] = target_stage

        for source_bom in source_boms:
            target_product = product_map.get(source_bom.product_id)
            target_bom, _created = BillOfMaterial.objects.using(target_alias).update_or_create(
                product=target_product,
                version=source_bom.version,
                defaults={
                    "status": "draft",
                    "base_quantity": source_bom.base_quantity,
                    "uom": source_bom.uom,
                    "parent_bom": None,
                    "created_by": target_owner,
                    "notes": source_bom.notes,
                },
            )
            if target_bom.status != "draft":
                target_bom.status = "draft"
                target_bom.save(using=target_alias, update_fields=["status"])
            target_bom.components.using(target_alias).all().delete()
            target_bom.operations.using(target_alias).all().delete()
            target_bom.acceptance_criteria.using(target_alias).all().delete()
            bom_map[source_bom.id] = target_bom

        for source_bom in source_boms:
            target_bom = bom_map[source_bom.id]
            parent_bom = bom_map.get(source_bom.parent_bom_id)
            update_fields = []
            if target_bom.parent_bom_id != getattr(parent_bom, "id", None):
                target_bom.parent_bom = parent_bom
                update_fields.append("parent_bom")
            if update_fields:
                target_bom.save(using=target_alias, update_fields=update_fields)

            source_components = list(BOMComponent.objects.using(source_alias).filter(bom=source_bom).order_by("id"))
            for source_component in source_components:
                target_component = BOMComponent.objects.using(target_alias).create(
                    bom=target_bom,
                    product=product_map.get(source_component.product_id),
                    material_name=source_component.material_name,
                    quantity=source_component.quantity,
                    unit=source_component.unit,
                    cost_per_unit=source_component.cost_per_unit,
                    wastage_quantity=source_component.wastage_quantity,
                    scrap_value_per_unit=source_component.scrap_value_per_unit,
                    scrap_type=source_component.scrap_type,
                    wastage_percent=source_component.wastage_percent,
                    source_type=source_component.source_type,
                    sub_bom=None,
                )
                component_map[source_component.id] = target_component

            for source_component in source_components:
                if source_component.sub_bom_id and source_component.id in component_map and source_component.sub_bom_id in bom_map:
                    target_component = component_map[source_component.id]
                    target_component.sub_bom = bom_map[source_component.sub_bom_id]
                    target_component.save(using=target_alias, update_fields=["sub_bom"])

            source_operations = list(BOMOperation.objects.using(source_alias).filter(bom=source_bom).order_by("id"))
            for source_operation in source_operations:
                target_operation = BOMOperation.objects.using(target_alias).create(
                    bom=target_bom,
                    machine=machine_map.get(source_operation.machine_id),
                    stage=stage_map.get(source_operation.stage_id),
                    order=source_operation.order,
                    setup_time=source_operation.setup_time,
                    run_time=source_operation.run_time,
                    duration_minutes=source_operation.duration_minutes,
                    machine_type=source_operation.machine_type,
                    description=source_operation.description,
                )
                operation_map[source_operation.id] = target_operation

            for source_criteria in BOMAcceptanceCriteria.objects.using(source_alias).filter(bom=source_bom).order_by("id"):
                BOMAcceptanceCriteria.objects.using(target_alias).create(
                    bom=target_bom,
                    parameter=source_criteria.parameter,
                    method=source_criteria.method,
                    criteria_min=source_criteria.criteria_min,
                    criteria_max=source_criteria.criteria_max,
                    pass_fail=source_criteria.pass_fail,
                    target_value=source_criteria.target_value,
                    tolerance=source_criteria.tolerance,
                    is_critical=source_criteria.is_critical,
                )

        source_operation_materials = list(
            BOMOperationMaterial.objects.using(source_alias)
            .filter(operation_id__in=operation_map.keys())
            .order_by("id")
        )
        for source_link in source_operation_materials:
            target_operation = operation_map.get(source_link.operation_id)
            target_component = component_map.get(source_link.component_id)
            if target_operation and target_component:
                BOMOperationMaterial.objects.using(target_alias).get_or_create(
                    operation=target_operation,
                    component=target_component,
                )

        for source_bom in source_boms:
            target_bom = bom_map[source_bom.id]
            target_bom.base_quantity = source_bom.base_quantity
            target_bom.uom = source_bom.uom
            target_bom.notes = source_bom.notes
            target_bom.created_by = target_owner
            target_bom.status = source_bom.status
            target_bom.save(using=target_alias)

    return {
        "machines": len(machine_map),
        "products": len(product_map),
        "stages": len(stage_map),
        "boms": len(bom_map),
        "components": len(component_map),
        "operations": len(operation_map),
    }
