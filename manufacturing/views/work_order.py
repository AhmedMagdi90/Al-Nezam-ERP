from django.views import View
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import JsonResponse
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
import json
from datetime import datetime

from manufacturing.models import WorkOrder, BillOfMaterial, Machine, Customer, WorkOrderChangeLog
from manufacturing.security import audit_request_action
from manufacturing.services import (
    WorkOrderLifecycle,
    WorkOrderLifecycleError,
    apply_latest_bom_to_work_order,
    decide_bom_change_archive_and_replace,
    decide_bom_change_continue_old,
    decide_bom_change_scrap_and_apply,
    get_apply_latest_bom_eligibility,
    get_company_default_operation_flow_mode,
    NotificationService,
    workorder_has_material_shortage,
)
from .dashboard import get_company_stage, require_company, user_has_role

def _parse_client_datetime(value):
    raw = str(value or "").strip()
    if not raw:
        return None
    normalized = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    normalized = normalized.replace("T", " ")
    dt = datetime.fromisoformat(normalized)
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, timezone.get_current_timezone())
    return dt

class WorkOrderCreateAPI(LoginRequiredMixin, View):
    def post(self, request):
        try:
            user = request.user
            if not user_has_role(user, ['planner', 'admin']):
                return JsonResponse({"success": False, "error": "Unauthorized"}, status=403)

            company = require_company(user)
            data = json.loads(request.body)
            default_operation_flow_mode = get_company_default_operation_flow_mode(company)

            # 1. Basic Fields
            bom_id = data.get('bom_id')
            quantity = int(data.get('quantity', 0))
            customer_id = data.get('customer_id')
            due_date_str = data.get('due_date')
            priority = data.get('priority', 'Normal')
            
            # Split Config: { "is_split": true, "stage_id": 1, "splits": [ { "machine_id": 1, "qty": 25 }, ... ] }
            split_config = data.get('split_config', {})
            
            if quantity <= 0:
                return JsonResponse({"success": False, "error": "Quantity must be positive"})

            bom = get_object_or_404(BillOfMaterial, pk=bom_id, product__company=company)
            if bom.status != 'active':
                return JsonResponse({"success": False, "error": "Work Orders can only be created from Active BOMs."}, status=400)
            
            # 🔒 Company Isolation: All machines must belong to the BOM's product's company
            bom_company = bom.product.company
            approve_below_batch = bool(data.get('approve_below_batch'))
            try:
                base_qty = int(bom.base_quantity or 0)
            except Exception:
                base_qty = 0
            if base_qty and quantity < base_qty and not approve_below_batch:
                return JsonResponse({
                    "success": False,
                    "error": f"Quantity ({quantity}) is below batch size ({base_qty}).",
                    "requires_confirm": True,
                    "base_quantity": base_qty
                }, status=400)
            
            customer = None
            if customer_id:
                customer = get_object_or_404(Customer, pk=customer_id, company=company)
            elif data.get('customer_name'):
                # Auto-Create Customer
                new_name = data.get('customer_name').strip()
                if new_name:
                    customer, _ = Customer.objects.get_or_create(
                        company=company,
                        name__iexact=new_name,
                        defaults={'name': new_name}
                    )
            
            due_date = None
            if due_date_str:
                due_date = datetime.fromisoformat(due_date_str.replace('Z', '+00:00'))

            with transaction.atomic():
                # 2. Check Split Logic
                if split_config.get('is_split') and split_config.get('splits'):
                    # Create PARENT Work Order
                    parent_wo = WorkOrder.objects.create(
                        company=company,
                        product_name=bom.product.name,
                        bom=bom,
                        quantity=quantity,
                        customer=customer,
                        status='pending',
                        due_date=due_date,
                        priority=priority,
                        assigned_to=user,
                        is_split=True,
                        operation_flow_mode=default_operation_flow_mode,
                    )
                    
                    stage_id = split_config.get('stage_id')
                    try:
                        target_stage = get_company_stage(company, stage_id) if stage_id else None
                    except Exception:
                        target_stage = None
                    if stage_id and not target_stage:
                        return JsonResponse(
                            {"success": False, "error": "Invalid split stage for this company."},
                            status=400,
                        )

                    # Create CHILD Work Orders
                    for item in split_config.get('splits', []):
                        m_id = item.get('machine_id')
                        qty = int(item.get('qty', 0))
                        
                        if qty > 0:
                            # 🔒 Validate machine belongs to the BOM's product's company
                            machine = get_object_or_404(Machine, pk=m_id, company=bom_company)
                            WorkOrder.objects.create(
                                company=company,
                                product_name=f"{bom.product.name} (Part)",
                                bom=bom,
                                quantity=qty,
                                customer=customer,
                                status='pending',
                                machine=machine,
                                stage=target_stage, # Initial Stage
                                current_stage=target_stage,
                                due_date=due_date,
                                priority=priority,
                                assigned_to=user,
                                parent=parent_wo,
                                operation_flow_mode=default_operation_flow_mode,
                            )
                    audit_request_action(
                        request,
                        'create',
                        target=parent_wo,
                        details={
                            'event': 'work_order_split_created',
                            'work_order_id': parent_wo.id,
                            'quantity': quantity,
                            'child_count': len(split_config.get('splits', [])),
                            'bom_id': bom.id,
                        },
                    )
                    NotificationService.notify_role(
                        company,
                        roles=['store', 'admin'],
                        title="Material check requested",
                        message=f"WO #{parent_wo.id} needs BOM material readiness for {quantity} units.",
                        link="/manufacturing/store/",
                        exclude_user=user,
                    )
                    
                    return JsonResponse({"success": True, "message": "Split Work Order Created", "wo_id": parent_wo.id})

                else:
                    # Single Work Order
                    requested_status = str(data.get('status', 'pending') or 'pending').strip().lower()
                    if requested_status == 'draft':
                        requested_status = 'pending'
                    if requested_status != 'pending':
                        return JsonResponse(
                            {"success": False, "error": "New work orders can only start as pending."},
                            status=400
                        )

                    wo = WorkOrder.objects.create(
                        company=company,
                        product_name=bom.product.name,
                        bom=bom,
                        quantity=quantity,
                        customer=customer,
                        status='pending',
                        machine=None,    # No machine assigned yet
                        start_date=None, # No schedule yet
                        due_date=due_date,
                        priority=priority,
                        assigned_to=user,
                        operation_flow_mode=default_operation_flow_mode,
                    )
                    audit_request_action(
                        request,
                        'create',
                        target=wo,
                        details={
                            'event': 'work_order_created',
                            'status': wo.status,
                            'quantity': quantity,
                            'bom_id': bom.id,
                            'customer_id': customer.id if customer else None,
                        },
                    )
                    NotificationService.notify_role(
                        company,
                        roles=['store', 'admin'],
                        title="Material check requested",
                        message=f"WO #{wo.id} needs BOM material readiness for {quantity} units.",
                        link="/manufacturing/store/",
                        exclude_user=user,
                    )

                    return JsonResponse({
                        "success": True,
                        "message": f"Work Order Created ({wo.status})",
                        "wo_id": wo.id,
                        "status": wo.status,
                    })

        except Exception as e:
            return JsonResponse({"success": False, "error": str(e)}, status=500)

class WorkOrderSplitAPI(LoginRequiredMixin, View):
    def post(self, request, pk):
        try:
            from manufacturing.services import WorkOrderService
            user = request.user
            if not user_has_role(user, ['planner', 'admin']):
                return JsonResponse({"success": False, "error": "Unauthorized"}, status=403)

            company = require_company(user)
            original_wo = get_object_or_404(WorkOrder, pk=pk, company=company)
            if original_wo.sub_tasks.exists():
                candidate = WorkOrderService.get_active_stage_task(original_wo)
                if not candidate:
                    return JsonResponse(
                        {"success": False, "error": "No active stage task is available to split."},
                        status=400,
                    )
                original_wo = candidate
            
            data = json.loads(request.body)
            split_quantity = data.get('split_quantity', 0)
            target_machine_id = data.get('machine_id')
            planned_start = _parse_client_datetime(data.get('planned_start') or data.get('start_date'))
            
            if not target_machine_id:
                return JsonResponse({"success": False, "error": "Target machine is required"}, status=400)
                
            target_machine = get_object_or_404(Machine, pk=target_machine_id, company=company)
            
            # Use the robust service logic
            new_wo = WorkOrderService.split_work_order(
                original_wo,
                split_quantity,
                target_machine,
                user,
                planned_start=planned_start,
            )
            audit_request_action(
                request,
                'create',
                target=new_wo,
                details={
                    'event': 'work_order_split',
                    'source_work_order_id': original_wo.id,
                    'machine_id': target_machine.id,
                    'quantity': new_wo.quantity,
                    'planned_start': planned_start.isoformat() if planned_start else None,
                    'scheduled_start': new_wo.start_date.isoformat() if new_wo.start_date else None,
                },
            )
            
            return JsonResponse({
                "success": True, 
                "message": f"Successfully split {new_wo.quantity} units to {target_machine.display_label}",
                "new_wo_id": new_wo.id,
                "source_wo_id": original_wo.id,
                "start_date": new_wo.start_date.isoformat() if new_wo.start_date else None,
                "end_date": new_wo.end_date.isoformat() if new_wo.end_date else None,
            })

        except ValueError as e:
            return JsonResponse({"success": False, "error": str(e)}, status=400)
        except Exception as e:
            return JsonResponse({"success": False, "error": str(e)}, status=500)

class WorkOrderCancelSplitAPI(LoginRequiredMixin, View):
    def post(self, request, pk):
        try:
            from manufacturing.services import WorkOrderService

            user = request.user
            if not user_has_role(user, ['planner', 'admin']):
                return JsonResponse({"success": False, "error": "Unauthorized"}, status=403)

            company = require_company(user)
            split_wo = get_object_or_404(WorkOrder, pk=pk, company=company)
            try:
                data = json.loads(request.body or "{}")
            except json.JSONDecodeError:
                data = {}

            result = WorkOrderService.cancel_split_work_order(
                split_wo,
                user,
                return_to_wo_id=data.get("return_to_wo_id"),
            )
            canceled_wo = result["canceled_work_order"]
            target_wo = result["target_work_order"]
            returned_qty = result["returned_quantity"]

            audit_request_action(
                request,
                'update',
                target=canceled_wo,
                details={
                    'event': 'work_order_split_canceled',
                    'target_work_order_id': target_wo.id,
                    'returned_quantity': returned_qty,
                },
            )

            return JsonResponse({
                "success": True,
                "message": f"Canceled split WO #{canceled_wo.id} and returned {returned_qty} units to WO #{target_wo.id}",
                "canceled_wo_id": canceled_wo.id,
                "target_wo_id": target_wo.id,
                "returned_quantity": returned_qty,
            })

        except ValueError as e:
            return JsonResponse({"success": False, "error": str(e)}, status=400)
        except Exception as e:
            return JsonResponse({"success": False, "error": str(e)}, status=500)

class WorkOrderCombineAPI(LoginRequiredMixin, View):
    def post(self, request):
        try:
            from manufacturing.services import WorkOrderService

            user = request.user
            if not user_has_role(user, ['planner', 'admin']):
                return JsonResponse({"success": False, "error": "Unauthorized"}, status=403)

            company = require_company(user)
            data = json.loads(request.body or "{}")
            work_order_ids = data.get("work_order_ids") or []
            target_wo_id = data.get("target_wo_id")
            source_wo_id = data.get("source_wo_id")

            if not isinstance(work_order_ids, list):
                return JsonResponse({"success": False, "error": "work_order_ids must be a list"}, status=400)

            if source_wo_id:
                target_wo = get_object_or_404(WorkOrder, pk=source_wo_id, company=company)
                split_children = list(
                    WorkOrder.objects.filter(source_task=target_wo, company=company)
                    .exclude(status__in=['completed', 'done', 'canceled', 'archived'])
                    .order_by('id')
                )
                if not split_children:
                    return JsonResponse({
                        "success": True,
                        "message": f"WO #{target_wo.id} has no active split segments to combine.",
                        "target_wo_id": target_wo.id,
                        "combined_quantity": int(target_wo.quantity or 0),
                        "canceled_work_order_ids": [],
                        "already_combined": True,
                    })
                work_orders = [target_wo, *split_children]
                work_order_ids = [wo.id for wo in work_orders]
            else:
                work_orders = list(WorkOrder.objects.filter(pk__in=work_order_ids, company=company))
                target_wo = None
                if target_wo_id:
                    target_wo = get_object_or_404(WorkOrder, pk=target_wo_id, company=company)

            result = WorkOrderService.combine_work_orders(work_orders, user, target_wo=target_wo)
            target_wo = result["target_work_order"]
            canceled_ids = result["canceled_work_order_ids"]

            audit_request_action(
                request,
                'update',
                target=target_wo,
                details={
                    'event': 'work_orders_combined',
                    'combined_work_order_ids': work_order_ids,
                    'canceled_work_order_ids': canceled_ids,
                    'combined_quantity': result["combined_quantity"],
                },
            )

            return JsonResponse({
                "success": True,
                "message": f"Combined {len(work_order_ids)} work orders into WO #{target_wo.id}",
                "target_wo_id": target_wo.id,
                "combined_quantity": result["combined_quantity"],
                "canceled_work_order_ids": canceled_ids,
            })

        except ValueError as e:
            return JsonResponse({"success": False, "error": str(e)}, status=400)
        except Exception as e:
            return JsonResponse({"success": False, "error": str(e)}, status=500)

class WorkOrderReleaseNextStageAPI(LoginRequiredMixin, View):
    def post(self, request, pk):
        try:
            from manufacturing.services import WorkOrderService
            user = request.user
            if not user_has_role(user, ['planner', 'admin', 'supervisor']):
                return JsonResponse({"success": False, "error": "Unauthorized"}, status=403)

            company = require_company(user)
            original_wo = get_object_or_404(WorkOrder, pk=pk, company=company)

            data = json.loads(request.body)
            release_quantity = int(data.get('release_quantity', 0))
            target_machine_id = data.get('machine_id')
            if workorder_has_material_shortage(original_wo) and not bool(data.get('material_shortage_acknowledged')):
                source_wo = original_wo.parent if original_wo.parent_id and original_wo.parent else original_wo
                return JsonResponse(
                    {
                        "success": False,
                        "error": "Material shortage is marked for this work order. Confirm before releasing to the next stage.",
                        "requires_material_shortage_confirm": True,
                        "material_shortage_note": source_wo.material_shortage_note or "",
                        "material_available_percent": (
                            float(source_wo.material_available_percent)
                            if source_wo.material_available_percent is not None
                            else None
                        ),
                        "material_expected_delivery_date": (
                            source_wo.material_expected_delivery_date.isoformat()
                            if source_wo.material_expected_delivery_date
                            else ""
                        ),
                    },
                    status=409,
                )

            target_machine = None
            if target_machine_id:
                target_machine = get_object_or_404(Machine, pk=target_machine_id, company=company)

            new_wo = WorkOrderService.release_to_next_stage(
                original_wo,
                release_quantity,
                user,
                target_machine
            )
            audit_request_action(
                request,
                'create',
                target=new_wo,
                details={
                    'event': 'work_order_released_to_next_stage',
                    'source_work_order_id': original_wo.id,
                    'machine_id': target_machine.id if target_machine else None,
                    'quantity': release_quantity,
                },
            )

            return JsonResponse({
                "success": True,
                "message": f"Released {release_quantity} units to next stage",
                "new_wo_id": new_wo.id
            })
        except Exception as e:
            return JsonResponse({"success": False, "error": str(e)}, status=500)

class WorkOrderRecommendationAPI(LoginRequiredMixin, View):
    """
    🔗 Returns a system recommendation for machine and slot.
    """
    def get(self, request):
        from manufacturing.services import WorkOrderService
        if not user_has_role(request.user, ['planner', 'admin', 'supervisor']):
            return JsonResponse({"success": False, "error": "Unauthorized"}, status=403)

        bom_id = request.GET.get('bom_id')
        qty = int(request.GET.get('quantity', 1))
        
        bom = get_object_or_404(BillOfMaterial, pk=bom_id, product__company=require_company(request.user))
        if bom.status != 'active':
            return JsonResponse({"success": False, "error": "BOM is not active."}, status=400)
        rec = WorkOrderService.get_recommendation(bom, qty)
        
        if rec:
            return JsonResponse({
                "success": True,
                "recommendation": {
                    "machine_id": rec['machine'].id,
                    "machine_name": rec['machine'].display_label,
                    "start": rec['start'].isoformat(),
                    "end": rec['end'].isoformat(),
                    "duration": rec['duration']
                }
            })
        return JsonResponse({"success": False, "error": "No recommendation available"})


class WorkOrderCloseAPI(LoginRequiredMixin, View):
    def post(self, request, wo_id):
        try:
            user = request.user
            if not user_has_role(user, ['planner', 'admin']):
                return JsonResponse({"success": False, "error": "Unauthorized"}, status=403)

            company = require_company(user)
            wo = get_object_or_404(WorkOrder, pk=wo_id, company=company)
            try:
                WorkOrderLifecycle.close(
                    wo,
                    actor=user,
                    require_ready=True,
                )
            except WorkOrderLifecycleError as exc:
                return JsonResponse({"success": False, "error": str(exc)}, status=400)
            audit_request_action(
                request,
                'update',
                target=wo,
                details={
                    'event': 'work_order_closed',
                    'status': wo.status,
                    'closed_by_planner': bool(getattr(wo, 'closed_by_planner', False)),
                },
            )

            return JsonResponse({"success": True, "message": f"WO #{wo.id} closed."})
        except Exception as e:
            return JsonResponse({"success": False, "error": str(e)}, status=500)


class WorkOrderApplyLatestBOMAPI(LoginRequiredMixin, View):
    def post(self, request, wo_id):
        try:
            user = request.user
            if not user_has_role(user, ['planner', 'admin']):
                return JsonResponse({"success": False, "error": "Unauthorized"}, status=403)

            company = require_company(user)
            wo = (
                WorkOrder.objects
                .select_related("bom", "bom__product", "parent")
                .prefetch_related("production_logs", "sub_tasks")
                .get(pk=wo_id, company=company)
            )

            eligible, reason = get_apply_latest_bom_eligibility(wo)
            if not eligible:
                return JsonResponse({"success": False, "error": reason}, status=400)

            result = apply_latest_bom_to_work_order(wo, actor=user)
            WorkOrderChangeLog.objects.create(
                work_order=wo,
                changed_by=user,
                action="BOM version applied",
                field_name="bom",
                old_value=result["previous_version"] or str(getattr(result["previous_bom"], "id", "")),
                new_value=result["new_version"],
                note="Planner applied latest active BOM version to eligible work order.",
            )
            audit_request_action(
                request,
                'update',
                target=wo,
                details={
                    'event': 'latest_bom_applied',
                    'previous_bom_id': result["previous_bom"].id if result["previous_bom"] else None,
                    'previous_bom_version': result["previous_version"],
                    'new_bom_id': result["new_bom"].id,
                    'new_bom_version': result["new_version"],
                },
            )

            return JsonResponse({
                "success": True,
                "message": (
                    f"WO #{wo.id} now uses BOM {result['new_version']}. "
                    + ("Old route plan was cleared. Replan the route." if result.get("route_plan_cleared") else "")
                ).strip(),
                "wo_id": wo.id,
                "bom_id": result["new_bom"].id,
                "bom_version": result["new_version"],
                "previous_bom_id": result["previous_bom"].id if result["previous_bom"] else None,
                "previous_bom_version": result["previous_version"],
                "route_plan_cleared": bool(result.get("route_plan_cleared")),
            })
        except WorkOrder.DoesNotExist:
            return JsonResponse({"success": False, "error": "Work order not found"}, status=404)
        except ValueError as exc:
            return JsonResponse({"success": False, "error": str(exc)}, status=400)
        except Exception as e:
            return JsonResponse({"success": False, "error": str(e)}, status=500)

class WorkOrderBOMChangeDecisionAPI(LoginRequiredMixin, View):
    def post(self, request, wo_id):
        try:
            user = request.user
            if not user_has_role(user, ['planner', 'admin']):
                return JsonResponse({"success": False, "error": "Unauthorized"}, status=403)

            company = require_company(user)
            wo = (
                WorkOrder.objects
                .select_related("bom", "bom__product", "bom_change_latest_bom", "bom_change_latest_bom__product", "parent")
                .prefetch_related("production_logs", "sub_tasks")
                .get(pk=wo_id, company=company)
            )
            data = json.loads(request.body or "{}")
            decision = str(data.get("decision") or "").strip().lower()
            note = str(data.get("note") or "").strip()

            if decision == "archive_new":
                replacement = decide_bom_change_archive_and_replace(wo, actor=user, note=note)
                WorkOrderChangeLog.objects.create(
                    work_order=wo,
                    changed_by=user,
                    action="BOM change decision",
                    field_name="bom_change_status",
                    old_value="action_required",
                    new_value="archived_replaced",
                    note=note or f"Archived and created replacement WO #{replacement.id}.",
                )
                audit_request_action(
                    request,
                    'update',
                    target=wo,
                    details={
                        'event': 'bom_change_archive_new',
                        'replacement_wo_id': replacement.id,
                        'latest_bom_id': replacement.bom_id,
                    },
                )
                return JsonResponse({
                    "success": True,
                    "message": f"WO #{wo.id} archived. Replacement WO #{replacement.id} created with latest BOM.",
                    "replacement_wo_id": replacement.id,
                })

            if decision == "scrap_apply":
                result = decide_bom_change_scrap_and_apply(wo, actor=user, note=note)
                WorkOrderChangeLog.objects.create(
                    work_order=wo,
                    changed_by=user,
                    action="BOM change decision",
                    field_name="bom",
                    old_value=result["previous_version"],
                    new_value=result["new_version"],
                    note=note or f"Scrapped reported quantity ({result['scrapped_qty']}) and applied latest BOM.",
                )
                audit_request_action(
                    request,
                    'update',
                    target=wo,
                    details={
                        'event': 'bom_change_scrap_apply',
                        'scrapped_qty': result["scrapped_qty"],
                        'previous_bom_id': result["previous_bom"].id if result["previous_bom"] else None,
                        'new_bom_id': result["new_bom"].id,
                    },
                )
                return JsonResponse({
                    "success": True,
                    "message": (
                        f"WO #{wo.id} now uses BOM {result['new_version']}. "
                        f"Scrapped qty: {result['scrapped_qty']}. "
                        "Old route tasks were archived; replan the route."
                    ),
                    "scrapped_qty": result["scrapped_qty"],
                    "bom_id": result["new_bom"].id,
                    "bom_version": result["new_version"],
                    "archived_child_ids": result.get("archived_child_ids", []),
                })

            if decision == "continue_old":
                decide_bom_change_continue_old(wo, actor=user, note=note)
                WorkOrderChangeLog.objects.create(
                    work_order=wo,
                    changed_by=user,
                    action="BOM change decision",
                    field_name="bom_change_status",
                    old_value="action_required",
                    new_value="ignored",
                    note=note or "Planner chose to continue this WO with the old BOM.",
                )
                audit_request_action(
                    request,
                    'update',
                    target=wo,
                    details={
                        'event': 'bom_change_continue_old',
                        'current_bom_id': wo.bom_id,
                        'latest_bom_id': wo.bom_change_latest_bom_id,
                    },
                )
                return JsonResponse({
                    "success": True,
                    "message": f"WO #{wo.id} will continue with BOM {wo.bom_version or 'old version'}.",
                })

            return JsonResponse({"success": False, "error": "Invalid BOM change decision."}, status=400)
        except WorkOrder.DoesNotExist:
            return JsonResponse({"success": False, "error": "Work order not found"}, status=404)
        except ValueError as exc:
            return JsonResponse({"success": False, "error": str(exc)}, status=400)
        except Exception as e:
            return JsonResponse({"success": False, "error": str(e)}, status=500)

class WorkOrderUnscheduleAPI(LoginRequiredMixin, View):
    def post(self, request, pk):
        try:
            user = request.user
            if not user_has_role(user, ['planner', 'admin']):
                return JsonResponse({"success": False, "error": "Unauthorized"}, status=403)

            company = require_company(user)
            wo = get_object_or_404(WorkOrder, pk=pk, company=company)

            if wo.status != 'pending':
                return JsonResponse(
                    {"success": False, "error": f"Only pending orders can be unscheduled (current: {wo.status})."},
                    status=400
                )

            # Check if production has started
            if wo.production_logs.exclude(status='rejected').exists():
                 return JsonResponse({"success": False, "error": "Cannot unschedule. Production has already started."})

            wo.start_date = None
            wo.end_date = None
            wo.save(update_fields=['start_date', 'end_date'])
            audit_request_action(
                request,
                'update',
                target=wo,
                details={
                    'event': 'work_order_unscheduled',
                    'start_date': None,
                    'end_date': None,
                },
            )

            return JsonResponse({"success": True, "message": f"WO #{wo.id} unscheduled."})
        except Exception as e:
            return JsonResponse({"success": False, "error": str(e)}, status=500)
