from django.shortcuts import render, redirect
from django.contrib.auth.mixins import LoginRequiredMixin
from django.views import View
from django.http import JsonResponse, HttpResponseForbidden
from django.db.models import Max, Q
import json
import logging

from manufacturing.models import Machine, ProductionStage, BillOfMaterial, Product, WorkOrder, BOMOperation
from manufacturing.machine_shift_propagation import propagate_machine_department_shift_configuration
from manufacturing.security import audit_request_action
from manufacturing.shift_utils import coerce_shift_configuration_payload, summarize_shift_configuration, parse_bool
from manufacturing.utils import normalize_machine_code
from .dashboard import require_company, user_has_role
logger = logging.getLogger(__name__)


class FactorySetupView(LoginRequiredMixin, View):
    """
    Dedicated Factory Setup page for managing machines, stages, and BOMs.
    """
    def get(self, request):
        try:
            if not user_has_role(request.user, "ui.factory_setup.view"):
                return HttpResponseForbidden("🚫 You are not authorized to access factory setup.")
            
            company = require_company(request.user)

            def _safe_list(queryset, label):
                try:
                    return list(queryset)
                except Exception as exc:
                    logger.exception("Factory setup query failed (%s): %s", label, exc)
                    return []

            try:
                stage_ids_from_boms = (
                    BOMOperation.objects.filter(
                        bom__product__company=company,
                        stage_id__isnull=False,
                    )
                    .values_list("stage_id", flat=True)
                    .distinct()
                )
                stages = list(
                    ProductionStage.objects.filter(
                        Q(machine__company=company) | Q(id__in=stage_ids_from_boms)
                    )
                    .select_related("machine")
                    .distinct()
                    .order_by("order", "name")
                )
            except Exception as exc:
                # Safe fallback to avoid page-level 500 in case of schema mismatch
                logger.exception("Failed to load production stages for factory setup: %s", exc)
                try:
                    stages = list(
                        ProductionStage.objects.filter(machine__company=company)
                        .select_related("machine")
                        .only("id", "name", "order", "machine")
                        .order_by("order", "name")
                    )
                except Exception as fallback_exc:
                    logger.exception("Fallback stage loading failed: %s", fallback_exc)
                    stages = []

            machines = _safe_list(
                Machine.objects.filter(company=company).order_by("name"),
                "machines",
            )
            for machine in machines:
                machine.shift_configuration_json = json.dumps(machine.shift_configuration or {})
                machine.working_hours_summary = (
                    "Factory hours"
                    if getattr(machine, "use_factory_shifts", True)
                    else summarize_shift_configuration(machine.shift_configuration or {})
                )
            boms = _safe_list(
                BillOfMaterial.objects.filter(product__company=company)
                .select_related("product")
                .order_by("-created_at")[:10],
                "boms",
            )
            work_orders = _safe_list(
                WorkOrder.objects.filter(company=company, parent__isnull=True)
                .exclude(status='archived')
                .select_related("current_stage")
                .order_by("-id"),
                "work_orders_parent",
            )
            if not work_orders:
                # Backward-compatible fallback for tenants missing self-parent relation.
                work_orders = _safe_list(
                    WorkOrder.objects.filter(company=company)
                    .exclude(status='archived')
                    .select_related("current_stage")
                    .order_by("-id")[:100],
                    "work_orders_fallback",
                )
            active_products = _safe_list(
                Product.objects.filter(company=company),
                "active_products",
            )

            safe_stages = []
            for stage in stages:
                machine_name = "Unassigned"
                machine_category = "-"
                machine_id = getattr(stage, "machine_id", None)
                if machine_id:
                    try:
                        machine_obj = stage.machine
                        if machine_obj:
                            machine_name = machine_obj.display_label or f"Machine #{machine_id}"
                            machine_category = (machine_obj.category or machine_obj.type or "-")
                    except Exception:
                        machine_name = f"Machine #{machine_id}"
                safe_stages.append(
                    {
                        "id": stage.id,
                        "order": stage.order,
                        "name": stage.name,
                        "machine_name": machine_name,
                        "machine_category": machine_category,
                    }
                )

            safe_boms = []
            for bom in boms:
                try:
                    product_name = bom.product.name
                except Exception:
                    product_name = "(Missing Product)"
                try:
                    status_display = bom.get_status_display()
                except Exception:
                    status_display = str(getattr(bom, "status", "draft")).title()
                safe_boms.append(
                    {
                        "id": bom.id,
                        "product_name": product_name,
                        "version": bom.version,
                        "created_at": bom.created_at,
                        "components_count": bom.components.count(),
                        "operations_count": bom.operations.count(),
                        "base_quantity": bom.base_quantity,
                        "status": bom.status,
                        "status_display": status_display,
                    }
                )

            safe_work_orders = []
            for wo in work_orders:
                current_stage_name = "-"
                current_stage_id = getattr(wo, "current_stage_id", None)
                if current_stage_id:
                    try:
                        current_stage = wo.current_stage
                        if current_stage:
                            current_stage_name = current_stage.name or f"Stage #{current_stage_id}"
                    except Exception:
                        current_stage_name = f"Stage #{current_stage_id}"
                created_ts = (
                    getattr(wo, "created_at", None)
                    or getattr(wo, "planner_start_at", None)
                    or getattr(wo, "scheduled_start_date", None)
                    or getattr(wo, "start_date", None)
                    or getattr(wo, "end_date", None)
                )
                safe_work_orders.append(
                    {
                        "id": wo.id,
                        "product_name": getattr(wo, "product_name", "") or "",
                        "created_at": created_ts,
                        "status": getattr(wo, "status", "draft"),
                        "status_display": wo.get_status_display(),
                        "quantity": getattr(wo, "quantity", 0),
                        "current_stage_name": current_stage_name,
                    }
                )

            context = {
                'machines': machines,
                'stages': safe_stages,
                'boms': safe_boms,
                'work_orders': safe_work_orders,
                'active_products': active_products,
                'factory_setup_error': '',
            }
            return render(request, 'manufacturing/factory_setup.html', context)
        except Exception as exc:
            logger.exception("FactorySetupView fatal error: %s", exc)
            fallback_context = {
                'machines': [],
                'stages': [],
                'boms': [],
                'work_orders': [],
                'active_products': [],
                'factory_setup_error': str(exc),
            }
            return render(request, 'manufacturing/factory_setup.html', fallback_context)

class CreateMachineView(LoginRequiredMixin, View):
    def post(self, request):
        if not user_has_role(request.user, "ui.factory_setup.manage"):
             return JsonResponse({"success": False, "error": "Unauthorized."}, status=403)
        
        try:
            company = require_company(request.user)
            name = (request.POST.get("name") or "").strip()
            code = normalize_machine_code(request.POST.get("code"))
            machine_type = (request.POST.get("type") or "").strip()
            category = (request.POST.get("category") or "").strip()
            status = request.POST.get("status", "operational")
            if isinstance(status, str):
                status = status.strip().lower()
            status_aliases = {
                "active": "operational",
                "running": "operational",
                "down": "broken",
            }
            status = status_aliases.get(status, status)
            if status not in {"operational", "maintenance", "broken", "inactive"}:
                status = "operational"
            is_active = status != "inactive"
            image = request.FILES.get("image")
            use_factory_shifts = parse_bool(request.POST.get("use_factory_shifts"), default=True)
            apply_shift_to_category = parse_bool(request.POST.get("apply_shift_to_category"), default=True)
            try:
                shift_configuration = coerce_shift_configuration_payload(
                    request.POST.get("shift_configuration"),
                    default_enabled=False,
                )
            except (TypeError, ValueError, json.JSONDecodeError):
                return JsonResponse({"success": False, "error": "Invalid machine shift configuration."}, status=400)

            if not name or not code:
                return JsonResponse(
                    {"success": False, "error": "Machine name and machine code are required."},
                    status=400,
                )
            if Machine.objects.filter(company=company, code__iexact=code).exists():
                 return JsonResponse(
                     {"success": False, "error": "Machine code already exists for this company."},
                     status=400,
                 )
            if not use_factory_shifts and not any(
                bool(entry.get("enabled"))
                for entry in shift_configuration.values()
                if isinstance(entry, dict)
            ):
                return JsonResponse({"success": False, "error": "Enable at least one machine shift."}, status=400)

            if not category:
                category = machine_type
            if not machine_type:
                machine_type = category

            machine = Machine.objects.create(
                company=company,
                name=name,
                code=code,
                type=machine_type,
                category=category,
                status=status,
                is_active=is_active,
                image=image,
                use_factory_shifts=use_factory_shifts,
                shift_configuration=({} if use_factory_shifts else shift_configuration),
            )
            propagated_count = 0
            if not use_factory_shifts and apply_shift_to_category:
                propagated_count = propagate_machine_department_shift_configuration(machine)
            audit_request_action(
                request,
                "create",
                target=machine,
                details={
                    "event": "machine_created",
                    "machine_id": machine.id,
                    "machine_code": machine.code,
                    "status": machine.status,
                    "shift_propagated_count": propagated_count,
                },
            )
            message = "Created successfully"
            if propagated_count:
                message = f"Created successfully. Working hours applied to {propagated_count} matching machine(s)."
            return JsonResponse({
                "success": True,
                "id": machine.id,
                "message": message,
                "shift_propagated_count": propagated_count,
            })
        except Exception as e:
            return JsonResponse({"success": False, "error": str(e)}, status=500)

class CreateStageView(LoginRequiredMixin, View):
    def post(self, request):
        if not user_has_role(request.user, "ui.factory_setup.manage"):
             return JsonResponse({"success": False, "error": "Unauthorized."}, status=403)
        try:
            company = require_company(request.user)
            name = (request.POST.get("name") or "").strip()
            category = request.POST.get("category", "").strip()

            if not name:
                return JsonResponse({"success": False, "error": "Stage name is required."}, status=400)
            if not category:
                return JsonResponse({"success": False, "error": "Stage category is required."}, status=400)

            bom_stage_ids = (
                BOMOperation.objects
                .filter(bom__product__company=company, stage_id__isnull=False)
                .values_list("stage_id", flat=True)
                .distinct()
            )
            machine = (
                Machine.objects
                .filter(company=company)
                .filter(Q(category__iexact=category) | Q(type__iexact=category))
                .order_by("name")
                .first()
            )
            if not machine:
                machine = Machine.objects.filter(company=company).order_by("name").first()
            if not machine:
                return JsonResponse({"success": False, "error": "Create at least one machine before adding a stage."}, status=400)

            existing_stage = (
                ProductionStage.objects
                .filter(Q(machine__company=company) | Q(id__in=bom_stage_ids))
                .filter(name__iexact=name)
                .select_related("machine")
                .first()
            )
            if existing_stage:
                update_fields = []
                if category and not existing_stage.category:
                    existing_stage.category = category
                    update_fields.append("category")
                if machine and not existing_stage.machine_id:
                    existing_stage.machine = machine
                    update_fields.append("machine")
                if update_fields:
                    existing_stage.save(update_fields=update_fields)
                return JsonResponse({
                    "success": True,
                    "id": existing_stage.id,
                    "message": "Stage already exists and is ready for BOM routing.",
                })

            next_order = (
                ProductionStage.objects
                .filter(Q(machine__company=company) | Q(id__in=bom_stage_ids))
                .aggregate(max_order=Max("order"))
                .get("max_order") or 0
            ) + 1
            stage = ProductionStage.objects.create(
                name=name,
                machine=machine,
                order=next_order,
                category=category
            )
            return JsonResponse({"success": True, "id": stage.id, "message": "Stage created"})
        except Exception as e:
             return JsonResponse({"success": False, "error": str(e)}, status=500)


class BulkWorkOrderActionView(LoginRequiredMixin, View):
    def post(self, request):
        if not user_has_role(request.user, "ui.factory_setup.manage"):
             return JsonResponse({"success": False, "error": "Unauthorized."}, status=403)
        
        try:
            company = require_company(request.user)
            import json
            data = json.loads(request.body)
            action = data.get('action')
            wo_ids = data.get('ids', [])
            
            if not wo_ids:
                return JsonResponse({"success": False, "error": "No items selected"}, status=400)

            qs = WorkOrder.objects.filter(company=company, id__in=wo_ids)
            count = qs.count()

            if action == 'delete':
                qs.delete()
                message = f"Deleted {count} work orders."
            elif action == 'set_archived':
                qs.update(status='archived')
                message = f"Archived {count} work orders."
            elif action == 'set_pending':
                qs.update(status='pending')
                message = f"Set {count} work orders to pending."
            else:
                return JsonResponse({"success": False, "error": "Invalid action"}, status=400)
            
            return JsonResponse({"success": True, "message": message})
        except Exception as e:
            return JsonResponse({"success": False, "error": str(e)}, status=500)

