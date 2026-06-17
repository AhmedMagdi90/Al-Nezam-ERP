from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils.text import slugify

from accounts.models import Profile, Role
from django.contrib.auth.models import User
from manufacturing.models import (
    BOMAcceptanceCriteria,
    BOMComponent,
    BOMOperation,
    BillOfMaterial,
    Company,
    Customer,
    Machine,
    MachineFault,
    MaterialUsage,
    Notification,
    Product,
    ProductionLog,
    ProductionStage,
    QualityCheck,
    ShiftAssignment,
    SystemSettings,
    WorkOrder,
    WorkOrderChangeLog,
    WorkOrderStage,
    WorkerCertification,
)
from tenancy.models import Tenant
from tenancy.services import ensure_tenant_schema


class Command(BaseCommand):
    help = (
        "Phase 3 migration: move legacy shared-db company data into isolated tenant DBs. "
        "Supports --dry-run and per-company migration."
    )

    def add_arguments(self, parser):
        parser.add_argument("--company-id", type=int, action="append", dest="company_ids")
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--force", action="store_true")

    def handle(self, *args, **options):
        company_ids = options.get("company_ids") or []
        dry_run = bool(options.get("dry_run"))
        force = bool(options.get("force"))

        companies_qs = Company.objects.using("default").all().order_by("id")
        if company_ids:
            companies_qs = companies_qs.filter(id__in=company_ids)

        companies = list(companies_qs)
        if not companies:
            self.stdout.write("No companies found.")
            return

        self.stdout.write(f"Companies to process: {len(companies)}")
        for company in companies:
            self._migrate_one_company(company, dry_run=dry_run, force=force)

        if dry_run:
            self.stdout.write(self.style.WARNING("Dry-run only: no tenant DB changes were applied."))
        else:
            self.stdout.write(self.style.SUCCESS("Phase 3 migration completed."))

    def _migrate_one_company(self, company: Company, dry_run: bool, force: bool):
        tenant = self._get_or_create_tenant_for_company(company, dry_run=dry_run)

        self.stdout.write(
            f"[Company {company.id}] {company.name} -> tenant code={tenant.code}, alias={tenant.db_alias}"
        )
        if dry_run:
            self._print_counts(company)
            return

        alias = ensure_tenant_schema(tenant)

        # Safety: avoid accidental overwrite unless explicitly forced.
        if not force and Company.objects.using(alias).exclude(id=company.id).exists():
            raise CommandError(
                f"Tenant DB '{alias}' already has different company rows. Use --force to continue."
            )

        with transaction.atomic(using=alias):
            self._copy_accounts(company, alias)
            self._copy_manufacturing(company, alias)

        self.stdout.write(self.style.SUCCESS(f"[Company {company.id}] migrated to {alias}"))

    def _get_or_create_tenant_for_company(self, company: Company, dry_run: bool) -> Tenant:
        # Prefer existing tenant by exact name (legacy heuristic).
        existing = Tenant.objects.using("default").filter(name=company.name, is_active=True).first()
        if existing:
            return existing

        base = slugify(company.name) or f"company-{company.id}"
        code = base
        if Tenant.objects.using("default").filter(code=code).exists():
            code = f"{base}-{company.id}"
            n = 2
            while Tenant.objects.using("default").filter(code=code).exists():
                code = f"{base}-{company.id}-{n}"
                n += 1

        if dry_run:
            return Tenant(name=company.name, code=code, db_alias=f"tenant_{code.replace('-', '_')}", db_name=f"tenant_dbs/{code}.sqlite3")

        return Tenant.objects.using("default").create(
            name=company.name,
            code=code,
            db_alias=f"tenant_{code.replace('-', '_')}",
            db_name=f"tenant_dbs/{code}.sqlite3",
            is_active=True,
        )

    def _print_counts(self, company: Company):
        company_users = User.objects.using("default").filter(profile__company_id=company.id).distinct()
        user_ids = list(company_users.values_list("id", flat=True))
        self.stdout.write(f"  users={len(user_ids)}")
        self.stdout.write(f"  machines={Machine.objects.using('default').filter(company_id=company.id).count()}")
        self.stdout.write(f"  products={Product.objects.using('default').filter(company_id=company.id).count()}")
        self.stdout.write(f"  stages={ProductionStage.objects.using('default').filter(machine__company_id=company.id).count()}")
        self.stdout.write(f"  work_orders={WorkOrder.objects.using('default').filter(company_id=company.id).count()}")

    @staticmethod
    def _upsert_model_list(model, objs, target_db, omit_fields=None):
        omit_fields = set(omit_fields or [])
        pk_name = model._meta.pk.attname
        for obj in objs:
            payload = {}
            for field in model._meta.concrete_fields:
                if field.attname in omit_fields:
                    continue
                payload[field.attname] = getattr(obj, field.attname)

            pk_value = payload.pop(pk_name)
            model.objects.using(target_db).update_or_create(pk=pk_value, defaults=payload)

    def _copy_accounts(self, company: Company, alias: str):
        company_users_qs = User.objects.using("default").filter(profile__company_id=company.id).distinct().order_by("id")
        user_ids = list(company_users_qs.values_list("id", flat=True))

        profiles_qs = Profile.objects.using("default").filter(user_id__in=user_ids).order_by("id")
        role_ids = list(profiles_qs.exclude(role_id__isnull=True).values_list("role_id", flat=True).distinct())
        roles_qs = Role.objects.using("default").filter(id__in=role_ids).order_by("id")

        self._upsert_model_list(Role, list(roles_qs), alias)
        self._upsert_model_list(User, list(company_users_qs), alias)
        self._upsert_model_list(Company, [Company.objects.using("default").get(id=company.id)], alias)
        settings_obj = SystemSettings.objects.using("default").filter(company_id=company.id).first()
        if settings_obj:
            self._upsert_model_list(SystemSettings, [settings_obj], alias)
        self._upsert_model_list(Profile, list(profiles_qs), alias)

    def _copy_manufacturing(self, company: Company, alias: str):
        company_id = company.id
        source = "default"
        company_users_qs = User.objects.using(source).filter(profile__company_id=company_id).distinct()
        company_user_ids = list(company_users_qs.values_list("id", flat=True))

        machines = list(Machine.objects.using(source).filter(company_id=company_id).order_by("id"))
        products = list(Product.objects.using(source).filter(company_id=company_id).order_by("id"))
        customers = list(Customer.objects.using(source).filter(company_id=company_id).order_by("id"))
        stages = list(ProductionStage.objects.using(source).filter(machine__company_id=company_id).order_by("id"))

        boms = list(BillOfMaterial.objects.using(source).filter(product__company_id=company_id).order_by("id"))
        bom_ids = [x.id for x in boms]
        bom_components = list(BOMComponent.objects.using(source).filter(bom_id__in=bom_ids).order_by("id"))
        bom_ops = list(BOMOperation.objects.using(source).filter(bom_id__in=bom_ids).order_by("id"))
        bom_criteria = list(BOMAcceptanceCriteria.objects.using(source).filter(bom_id__in=bom_ids).order_by("id"))

        work_orders = list(WorkOrder.objects.using(source).filter(company_id=company_id).order_by("id"))
        wo_ids = [x.id for x in work_orders]
        wo_stages = list(WorkOrderStage.objects.using(source).filter(work_order_id__in=wo_ids).order_by("id"))
        wo_stage_ids = [x.id for x in wo_stages]
        logs = list(ProductionLog.objects.using(source).filter(work_order_id__in=wo_ids).order_by("id"))
        log_ids = [x.id for x in logs]
        machine_faults = list(MachineFault.objects.using(source).filter(machine__company_id=company_id).order_by("id"))
        qcs = list(QualityCheck.objects.using(source).filter(work_order_id__in=wo_ids).order_by("id"))
        usages = list(MaterialUsage.objects.using(source).filter(production_log_id__in=log_ids).order_by("id"))
        wo_changes = list(WorkOrderChangeLog.objects.using(source).filter(work_order_id__in=wo_ids).order_by("id"))
        certs = list(
            WorkerCertification.objects.using(source)
            .filter(machine__company_id=company_id, worker_id__in=company_user_ids)
            .order_by("id")
        )
        shifts = list(
            ShiftAssignment.objects.using(source)
            .filter(machine__company_id=company_id, worker_id__in=company_user_ids)
            .order_by("id")
        )
        notifs = list(Notification.objects.using(source).filter(recipient_id__in=company_user_ids).order_by("id"))

        self._upsert_model_list(Machine, machines, alias)
        self._upsert_model_list(Product, products, alias)
        self._upsert_model_list(Customer, customers, alias)
        self._upsert_model_list(ProductionStage, stages, alias)

        # Self-FK pass for BillOfMaterial
        self._upsert_model_list(BillOfMaterial, boms, alias, omit_fields={"parent_bom_id"})
        self._upsert_model_list(BillOfMaterial, boms, alias)

        self._upsert_model_list(BOMComponent, bom_components, alias)
        self._upsert_model_list(BOMOperation, bom_ops, alias)
        self._upsert_model_list(BOMAcceptanceCriteria, bom_criteria, alias)

        # Self-FK pass for WorkOrder
        self._upsert_model_list(WorkOrder, work_orders, alias, omit_fields={"parent_id", "source_task_id"})
        self._upsert_model_list(WorkOrder, work_orders, alias)

        self._upsert_model_list(WorkOrderStage, wo_stages, alias)
        self._copy_work_order_stage_dependencies(wo_stages, wo_stage_ids, alias)
        self._upsert_model_list(ProductionLog, logs, alias)
        self._upsert_model_list(MachineFault, machine_faults, alias)
        self._upsert_model_list(QualityCheck, qcs, alias)
        self._upsert_model_list(MaterialUsage, usages, alias)
        self._upsert_model_list(WorkOrderChangeLog, wo_changes, alias)
        self._upsert_model_list(WorkerCertification, certs, alias)
        self._upsert_model_list(ShiftAssignment, shifts, alias)
        self._upsert_model_list(Notification, notifs, alias)

    @staticmethod
    def _copy_work_order_stage_dependencies(source_wo_stages, source_stage_ids, alias):
        src_qs = WorkOrderStage.objects.using("default").filter(id__in=source_stage_ids).order_by("id")
        for src in src_qs:
            target = WorkOrderStage.objects.using(alias).filter(id=src.id).first()
            if not target:
                continue
            dep_ids = list(src.depends_on.using("default").values_list("id", flat=True))
            target.depends_on.set(dep_ids)

