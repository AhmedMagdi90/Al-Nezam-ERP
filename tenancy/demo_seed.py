from __future__ import annotations

from dataclasses import dataclass

from django.contrib.auth.models import User

from accounts.models import Profile, Role
from manufacturing.models import (
    BillOfMaterial,
    BOMOperation,
    Company,
    Machine,
    Product,
    ProductionStage,
    WorkOrder,
)


DEMO_LOGIN_PASSWORD = "DemoPass123!"


@dataclass(frozen=True)
class DemoUserSpec:
    role: str
    first_name: str
    last_name: str
    username: str
    app_scope: str
    department: str


DEMO_USER_SPECS = (
    DemoUserSpec("planner", "Demo", "Planner", "planner", "planner", "Planning"),
    DemoUserSpec("supervisor", "Demo", "Supervisor", "supervisor", "planner", "Production"),
    DemoUserSpec("worker", "Demo", "Worker", "worker", "planner", "Production"),
    DemoUserSpec("quality", "Demo", "Quality", "quality", "quality", "Quality"),
    DemoUserSpec("maintenance", "Demo", "Maintenance", "maintenance", "maintenance", "Maintenance"),
)


DEMO_MACHINE_SPECS = (
    {"code": "M-001", "name": "CNC Machine 01", "type": "CNC", "category": "CNC"},
    {"code": "M-002", "name": "Assembly Machine 01", "type": "Assembly", "category": "Assembly"},
    {"code": "M-003", "name": "Mixing Machine 01", "type": "Mix", "category": "Mix"},
    {"code": "M-004", "name": "Filling Machine 01", "type": "Fill", "category": "Fill"},
    {"code": "M-005", "name": "Packing Machine 01", "type": "Pack", "category": "Pack"},
)


def _demo_email(company: Company, username: str) -> str:
    company_slug = company.name.lower().replace(" ", "-")
    return f"{username}@{company_slug}.demo.local"


def get_demo_login_accounts(company_name: str) -> dict[str, str]:
    company = Company(name=company_name)
    accounts = {"owner": ""}
    for spec in DEMO_USER_SPECS:
        accounts[spec.role] = _demo_email(company, spec.username)
    return accounts


def _ensure_demo_user(db_alias: str, company: Company, spec: DemoUserSpec, password: str):
    role, _ = Role.objects.using(db_alias).get_or_create(name=spec.role)
    email = _demo_email(company, spec.username)
    user, created = User.objects.using(db_alias).get_or_create(
        username=email,
        defaults={
            "email": email,
            "first_name": spec.first_name,
            "last_name": spec.last_name,
        },
    )
    if created:
        user.set_password(password)
        user.save(using=db_alias, update_fields=["password"])
    else:
        update_fields = []
        if user.email != email:
            user.email = email
            update_fields.append("email")
        if user.first_name != spec.first_name:
            user.first_name = spec.first_name
            update_fields.append("first_name")
        if user.last_name != spec.last_name:
            user.last_name = spec.last_name
            update_fields.append("last_name")
        if update_fields:
            user.save(using=db_alias, update_fields=update_fields)

    Profile.objects.using(db_alias).update_or_create(
        user_id=user.id,
        defaults={
            "company_id": company.id,
            "role_id": role.id,
            "app_scope": spec.app_scope,
            "department": spec.department,
        },
    )
    return user


def seed_demo_tenant_package(db_alias: str, company: Company, owner_user: User, password: str = DEMO_LOGIN_PASSWORD) -> dict:
    """
    Seed a predictable demo package so trial/demo tenants are immediately usable.
    The package is idempotent and safe to call more than once.
    """
    # Ensure all application roles exist inside the tenant DB.
    admin_role, _ = Role.objects.using(db_alias).get_or_create(name="admin")
    Profile.objects.using(db_alias).update_or_create(
        user_id=owner_user.id,
        defaults={
            "company_id": company.id,
            "role_id": admin_role.id,
            "app_scope": "planner",
            "department": "Management",
        },
    )

    seeded_users = {
        "owner": owner_user,
    }
    for spec in DEMO_USER_SPECS:
        seeded_users[spec.role] = _ensure_demo_user(db_alias, company, spec, password)

    machines = {}
    for machine_spec in DEMO_MACHINE_SPECS:
        machine, _ = Machine.objects.using(db_alias).update_or_create(
            company=company,
            code=machine_spec["code"],
            defaults={
                "name": machine_spec["name"],
                "type": machine_spec["type"],
                "category": machine_spec["category"],
                "status": "operational",
                "is_active": True,
            },
        )
        machines[machine_spec["category"]] = machine

    stages = {}
    for order, (category, machine) in enumerate(machines.items(), start=1):
        stage, _ = ProductionStage.objects.using(db_alias).update_or_create(
            name=f"Op {order * 10:02d}: {category}",
            machine=machine,
            defaults={
                "category": category,
                "order": order,
            },
        )
        stages[category] = stage

    planner_user = seeded_users["planner"]
    demo_products = (
        {
            "name": "Demo Bottle",
            "ops": ("Mix", "Fill", "Pack"),
            "quantities": (240, 120),
        },
        {
            "name": "Demo Housing",
            "ops": ("CNC", "Assembly", "Pack"),
            "quantities": (60,),
        },
    )

    seeded_work_orders = []
    for product_spec in demo_products:
        product, _ = Product.objects.using(db_alias).get_or_create(
            company=company,
            name=product_spec["name"],
            defaults={
                "unit": "pcs",
                "material_type": "finished",
            },
        )
        bom, _ = BillOfMaterial.objects.using(db_alias).update_or_create(
            product=product,
            version="demo-v1",
            defaults={
                "status": "active",
                "base_quantity": 100,
                "uom": "pcs",
                "created_by_id": planner_user.id,
                "notes": "Auto-seeded demo BOM",
            },
        )
        existing_ops = set(BOMOperation.objects.using(db_alias).filter(bom=bom).values_list("order", flat=True))
        for index, category in enumerate(product_spec["ops"], start=1):
            op_order = index * 10
            if op_order in existing_ops:
                continue
            BOMOperation.objects.using(db_alias).create(
                bom=bom,
                machine=machines[category],
                stage=stages[category],
                order=op_order,
                machine_type=category,
                duration_minutes=60,
                setup_time=15,
                run_time=0.5,
                description=f"Demo step for {category}",
            )

        quantities = list(product_spec["quantities"])
        for sequence, qty in enumerate(quantities, start=1):
            work_order, _ = WorkOrder.objects.using(db_alias).get_or_create(
                company=company,
                product_name=product.name,
                bom=bom,
                quantity=qty,
                current_stage=stages[product_spec["ops"][0]],
                defaults={
                    "machine": machines[product_spec["ops"][0]],
                    "stage": stages[product_spec["ops"][0]],
                    "status": "pending" if sequence == 1 else "draft",
                    "assignment_type": "auto",
                },
            )
            seeded_work_orders.append(work_order.id)

    return {
        "users": {
            role: user.username for role, user in seeded_users.items()
        },
        "machine_codes": sorted(m.code for m in machines.values()),
        "work_order_ids": seeded_work_orders,
    }
