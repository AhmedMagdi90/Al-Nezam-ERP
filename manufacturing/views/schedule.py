from django.http import JsonResponse
from django.views import View
from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import get_object_or_404
from datetime import datetime, timedelta
from django.utils import timezone
from django.db import transaction
import json

from manufacturing.bom_attachments import serialize_bom_attachment
from manufacturing.models import WorkOrder, Machine, WorkOrderStage, ProductionStage
from manufacturing.security import audit_request_action
from manufacturing.views.dashboard import (
    get_company_stage,
    require_company,
    user_has_role,
)
from manufacturing.utils import calculate_end_date, validate_start_date
from manufacturing.services import (
    get_workorder_quantity_breakdown,
    get_workorder_material_readiness_payload,
    get_workorder_execution_readiness,
    get_workorder_reported_quantity_floor,
    get_workorder_bom_change_payload,
    get_apply_latest_bom_eligibility,
    get_latest_active_bom_for_work_order,
    WorkOrderService,
    WorkOrderCycleService,
    WorkOrderLifecycle,
    WorkOrderLifecycleError,
    resolve_bom_for_work_order,
    get_company_default_operation_flow_mode,
    get_work_order_operation_flow_mode,
    get_material_readiness_planning_blocker,
    workorder_has_material_shortage,
)


class WorkOrderDetailAPI(LoginRequiredMixin, View):
    """Get Work Order details for scheduling modal"""
    def get(self, request, wo_id):
        try:
            if not user_has_role(request.user, ['planner', 'admin']):
                return JsonResponse({"success": False, "error": "Unauthorized"}, status=403)

            company = require_company(request.user)
            wo = get_object_or_404(WorkOrder, pk=wo_id, company=company)
            base_qty, compensation_qty, adjusted_qty = get_workorder_quantity_breakdown(wo)
            quantity_floor = get_workorder_reported_quantity_floor(wo)
            material_source_wo = wo.parent if wo.parent_id and wo.parent else wo
            material_readiness = get_workorder_material_readiness_payload(material_source_wo, company)
            execution_readiness = get_workorder_execution_readiness(wo)
            latest_bom = get_latest_active_bom_for_work_order(wo)
            can_apply_latest_bom, apply_latest_bom_blocker = get_apply_latest_bom_eligibility(wo)
            bom_change = get_workorder_bom_change_payload(wo)
            bom_attachment = serialize_bom_attachment(wo.bom, request)

            active_machine_objects = list(
                Machine.objects.filter(company=company, is_active=True)
                .exclude(status__in=['broken', 'maintenance'])
                .order_by('id')
            )

            def build_machine_payload(machine_obj):
                if not machine_obj:
                    return None
                return {
                    'id': machine_obj.id,
                    'name': machine_obj.name,
                    'display_name': machine_obj.display_label,
                    'code': machine_obj.code,
                    'status': machine_obj.status,
                    'type': machine_obj.type,
                    'category': machine_obj.category,
                }

            def machine_matches_required_type(machine_obj, required_type):
                target = str(required_type or '').strip().lower()
                if not target:
                    return False
                haystack = ' '.join([
                    str(getattr(machine_obj, 'name', '') or ''),
                    str(getattr(machine_obj, 'type', '') or ''),
                    str(getattr(machine_obj, 'category', '') or ''),
                ]).strip().lower()
                if not haystack:
                    return False
                if target in haystack:
                    return True
                hay_tokens = set(haystack.replace('-', ' ').replace('_', ' ').split())
                target_tokens = set(target.replace('-', ' ').replace('_', ' ').split())
                return bool(hay_tokens.intersection(target_tokens))

            def stage_required_machine_type(op):
                if getattr(op, 'machine_type', None):
                    return op.machine_type
                if getattr(op, 'machine', None):
                    return op.machine.category or op.machine.type
                if getattr(op, 'stage', None) and getattr(op.stage, 'machine', None):
                    return op.stage.machine.category or op.stage.machine.type
                if getattr(op, 'stage', None):
                    return op.stage.category or (op.stage.name.split(':')[-1].strip() if op.stage.name else None)
                return None

            def build_stage_candidate_machines(op):
                candidates = []
                seen_machine_ids = set()

                def add_candidate(machine_obj):
                    if not machine_obj:
                        return
                    if getattr(machine_obj, 'company_id', None) != company.id:
                        return
                    if getattr(machine_obj, 'status', None) in ['broken', 'maintenance']:
                        return
                    if not getattr(machine_obj, 'is_active', True):
                        return
                    if machine_obj.id in seen_machine_ids:
                        return
                    payload = build_machine_payload(machine_obj)
                    if payload:
                        candidates.append(payload)
                        seen_machine_ids.add(machine_obj.id)

                add_candidate(getattr(op, 'machine', None))
                add_candidate(getattr(getattr(op, 'stage', None), 'machine', None))

                required_type = stage_required_machine_type(op)
                if required_type:
                    for machine_obj in active_machine_objects:
                        if machine_matches_required_type(machine_obj, required_type):
                            add_candidate(machine_obj)

                return candidates

            suggested_machines = []
            machine_type_needed = None
            all_machines = []
            all_machine_ids = set()
            stages_data = []

            if wo.bom:
                bom_ops = (
                    wo.bom.operations.all()
                    .select_related('machine', 'stage', 'stage__machine')
                    .order_by('order', 'id')
                )
                for op in bom_ops:
                    if not op.stage_id:
                        continue

                    candidate_machines = build_stage_candidate_machines(op)
                    for payload in candidate_machines:
                        machine_id = payload['id']
                        if machine_id not in all_machine_ids:
                            all_machines.append(payload)
                            all_machine_ids.add(machine_id)

                    m_type = stage_required_machine_type(op)
                    stages_data.append({
                        "id": op.stage.id,
                        "name": op.stage.name,
                        "machine_type": m_type,
                        "order": op.order,
                        "setup_time": float(op.setup_time or 0),
                        "run_time": float(op.run_time or 0),
                        "duration_minutes": int(op.duration_minutes or 0),
                        "estimated_duration_minutes": int(round(float(op.setup_time or 0) + (float(op.run_time or 0) * float(adjusted_qty or 0)))) or int(op.duration_minutes or 0),
                        "default_machine_id": op.machine_id or op.stage.machine_id,
                        "candidate_machines": candidate_machines,
                    })

                current_stage_id = wo.current_stage_id or wo.stage_id
                current_stage_entry = next((stage for stage in stages_data if stage["id"] == current_stage_id), None)
                first_stage_entry = stages_data[0] if stages_data else None
                source_stage_entry = current_stage_entry or first_stage_entry
                if source_stage_entry:
                    suggested_machines = list(source_stage_entry.get("candidate_machines") or [])
                    machine_type_needed = source_stage_entry.get("machine_type")

            else:
                fallback_machines = [
                    build_machine_payload(machine_obj)
                    for machine_obj in active_machine_objects
                    if machine_obj
                ]
                suggested_machines = list(fallback_machines)
                all_machines = list(fallback_machines)
                all_machine_ids = {payload['id'] for payload in all_machines}

                fallback_stages = (
                    ProductionStage.objects.filter(machine__company=company)
                    .select_related('machine')
                    .order_by('order', 'name', 'id')
                )
                for stage in fallback_stages:
                    stage_payload = {
                        "id": stage.id,
                        "name": stage.name,
                        "machine_type": (
                            stage.category
                            or (stage.machine.category if stage.machine else None)
                            or (stage.machine.type if stage.machine else None)
                        ),
                        "order": stage.order,
                        "setup_time": 0,
                        "run_time": 0,
                        "duration_minutes": 0,
                        "estimated_duration_minutes": 0,
                        "default_machine_id": stage.machine_id,
                        "candidate_machines": (
                            [build_machine_payload(stage.machine)]
                            if stage.machine_id and stage.machine and stage.machine.company_id == company.id and stage.machine.is_active and stage.machine.status not in ['broken', 'maintenance']
                            else []
                        ),
                    }
                    stages_data.append(stage_payload)

            return JsonResponse({
                "success": True,
                "work_order": {
                    "id": wo.id,
                    "product_name": wo.product_name,
                    "quantity": adjusted_qty,
                    "base_quantity": int(base_qty),
                    "scrap_compensation_qty": int(compensation_qty),
                    "reported_qty": int(quantity_floor["reported"]),
                    "approved_finished_qty": int(quantity_floor["approved"]),
                    "minimum_editable_quantity": int(quantity_floor["reported"]),
                    "has_scrap_compensation": bool(compensation_qty > 0),
                    "is_scrap_compensation_task": bool(getattr(wo, 'is_scrap_compensation_task', False)),
                    "scrap_source_qc_id": getattr(wo, 'scrap_source_quality_check_id', None),
                    "status": wo.status,
                    "due_date": wo.due_date.isoformat() if wo.due_date else None,
                    "priority": wo.priority,
                    "customer": wo.customer.name if wo.customer else None,
                    "customer_id": wo.customer.id if wo.customer else None,
                    "current_stage_id": wo.current_stage.id if wo.current_stage else None,
                    "operation_flow_mode": get_work_order_operation_flow_mode(wo),
                    "company_default_operation_flow_mode": get_company_default_operation_flow_mode(company),
                    "cycle_state": WorkOrderCycleService.describe(wo),
                    "material_readiness_status": material_readiness["status"],
                    "material_shortage_note": material_readiness["shortage_note"],
                    "material_readiness": material_readiness,
                    "execution_readiness": execution_readiness,
                    "bom_id": wo.bom_id,
                    "bom_version": wo.bom_version or (wo.bom.version if wo.bom else ""),
                    "bom_attachment": bom_attachment,
                    "latest_bom_id": latest_bom.id if latest_bom else None,
                    "latest_bom_version": latest_bom.version if latest_bom else "",
                    "can_apply_latest_bom": can_apply_latest_bom,
                    "apply_latest_bom_blocker": apply_latest_bom_blocker,
                    "bom_change": bom_change,
                    "bom_change_status": bom_change["status"],
                    "bom_change_action_required": bom_change["action_required"],
                },
                "suggested_machines": suggested_machines,
                "machine_type_needed": machine_type_needed,
                "stages": stages_data,
                "all_machines": all_machines
            })
        except ValueError as e:
            return JsonResponse({"success": False, "error": str(e)}, status=400)
        except WorkOrderLifecycleError as e:
            return JsonResponse({"success": False, "error": str(e)}, status=400)
        except Exception as e:
            return JsonResponse({"success": False, "error": str(e)}, status=500)


class ScheduleWorkOrderAPI(LoginRequiredMixin, View):
    """Schedule a pending work order to a machine with start date"""
    def post(self, request, wo_id):
        try:
            company = require_company(request.user)
            if not user_has_role(request.user, ['planner', 'admin']):
                return JsonResponse({"success": False, "error": "Unauthorized"}, status=403)

            # Allow scheduling pending orders. Terminal status
            # changes are handled separately below and can be applied from active
            # routed states such as in_progress/hold.
            wo = get_object_or_404(WorkOrder, pk=wo_id, company=company)
            inferred_bom = resolve_bom_for_work_order(wo, company)
            if inferred_bom and not wo.bom_id:
                wo.bom = inferred_bom
                wo.save(update_fields=["bom"])

            data = json.loads(request.body)
            start_date_str = data.get('start_date')
            machine_id = data.get('machine_id')
            stage_id = data.get('stage_id')
            requested_quantity = data.get('quantity')
            requested_status = str(data.get('status') or '').strip().lower()
            terminal_statuses = {'completed', 'canceled', 'archived'}
            valid_statuses = {choice[0] for choice in WorkOrder.STATUS_CHOICES}
            if requested_status and requested_status not in valid_statuses:
                return JsonResponse({"success": False, "error": f"Invalid work order status '{requested_status}'"}, status=400)
            if requested_status == 'draft':
                requested_status = 'pending'
            if requested_status not in terminal_statuses and wo.status != 'pending':
                return JsonResponse({"success": False, "error": f"Cannot schedule work order in status '{wo.status}'"})
            operation_flow_mode = (data.get('operation_flow_mode') or getattr(wo, 'operation_flow_mode', None) or get_company_default_operation_flow_mode(company)).strip().lower()
            if operation_flow_mode not in {'series', 'parallel'}:
                operation_flow_mode = get_company_default_operation_flow_mode(company)
            raw_route_assignments = data.get('route_assignments') or []
            shortage_acknowledged = bool(data.get('material_shortage_acknowledged'))
            quantity_changed = False
            if requested_quantity not in [None, ""]:
                try:
                    requested_quantity = int(requested_quantity)
                except (TypeError, ValueError):
                    return JsonResponse({"success": False, "error": "Invalid quantity"}, status=400)
                if requested_quantity <= 0:
                    return JsonResponse({"success": False, "error": "Quantity must be greater than zero."}, status=400)
                quantity_floor = get_workorder_reported_quantity_floor(wo)
                if requested_quantity < int(quantity_floor["reported"]):
                    return JsonResponse(
                        {
                            "success": False,
                            "error": f"Quantity cannot be below already reported output ({int(quantity_floor['reported'])}).",
                        },
                        status=400,
                    )
                if requested_quantity != wo.quantity:
                    wo.quantity = requested_quantity
                    if wo.base_quantity is not None:
                        wo.base_quantity = max(
                            requested_quantity - int(getattr(wo, "scrap_compensation_qty", 0) or 0),
                            0,
                        )
                    wo.save(update_fields=["quantity", "base_quantity"] if wo.base_quantity is not None else ["quantity"])
                    quantity_changed = True

            material_blocker = get_material_readiness_planning_blocker(wo)
            if material_blocker:
                source_wo = wo.parent if wo.parent_id and wo.parent else wo
                return JsonResponse(
                    {
                        "success": False,
                        "error": material_blocker,
                        "requires_store_material_action": True,
                        "material_readiness_status": source_wo.material_readiness_status,
                        "material_shortage_note": source_wo.material_shortage_note or '',
                        "material_available_qty": source_wo.material_available_qty,
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
            route_assignment_map = {}
            route_assignment_specs = {}
            if isinstance(raw_route_assignments, list):
                for entry in raw_route_assignments:
                    if not isinstance(entry, dict):
                        continue
                    stage_key = entry.get('stage_id')
                    machine_key = entry.get('machine_id')
                    selection_mode = str(entry.get('selection_mode') or '').strip().lower() or 'manual'
                    if stage_key in [None, '']:
                        continue
                    route_assignment_specs[str(stage_key)] = {
                        'machine_id': str(machine_key or ''),
                        'selection_mode': selection_mode,
                        'start_date': entry.get('start_date') or '',
                    }
                    if machine_key in [None, '']:
                        continue
                    route_assignment_map[str(stage_key)] = str(machine_key)
            has_route_plan_payload = bool(route_assignment_specs)

            if requested_status in terminal_statuses:
                with transaction.atomic():
                    effective_end = timezone.now()
                    route_tasks = list(
                        WorkOrder.objects.filter(company=company, parent=wo)
                        .exclude(status='archived')
                        .order_by('id')
                    )

                    for route_task in route_tasks:
                        update_fields = []
                        if route_task.status != requested_status:
                            route_task.status = requested_status
                            update_fields.append('status')
                        if requested_status in {'canceled', 'archived'} and route_task.assigned_worker_id:
                            route_task.assigned_worker = None
                            route_task.assignment_type = 'auto'
                            update_fields.extend(['assigned_worker', 'assignment_type'])
                        if not route_task.end_date:
                            route_task.end_date = effective_end
                            update_fields.append('end_date')
                        if update_fields:
                            route_task.save(update_fields=list(dict.fromkeys(update_fields)))

                    root_update_fields = []
                    if wo.status != requested_status:
                        wo.status = requested_status
                        root_update_fields.append('status')
                    if requested_status in {'canceled', 'archived'} and wo.assigned_worker_id:
                        wo.assigned_worker = None
                        wo.assignment_type = 'auto'
                        root_update_fields.extend(['assigned_worker', 'assignment_type'])
                    if not wo.end_date:
                        wo.end_date = effective_end
                        root_update_fields.append('end_date')
                    if root_update_fields:
                        wo.save(update_fields=list(dict.fromkeys(root_update_fields)))

                audit_request_action(
                    request,
                    'update',
                    target=wo,
                    details={
                        'event': 'work_order_route_status_updated',
                        'status': requested_status,
                        'route_task_count': len(route_tasks),
                    },
                )
                return JsonResponse({
                    "success": True,
                    "message": f"Work Order #{wo.id} moved to {requested_status.replace('_', ' ')}.",
                    "wo_id": wo.id,
                    "status": requested_status,
                    "route_task_count": len(route_tasks),
                })

            if not start_date_str:
                return JsonResponse({"success": False, "error": "Start date is required"})

            if not stage_id:
                if wo.stage_id:
                    stage_id = wo.stage_id
                elif wo.bom:
                    first_op = wo.bom.operations.select_related('stage').order_by('order').first()
                    if first_op and first_op.stage_id:
                        stage_id = first_op.stage_id

            target_wo = wo
            stage = None
            route_plan_requested = False
            if stage_id:
                stage = get_company_stage(company, stage_id)
                if not stage:
                    return JsonResponse({"success": False, "error": "Invalid stage for this company."}, status=400)

                if target_wo.parent_id:
                    if target_wo.stage_id and target_wo.stage_id != stage.id:
                        return JsonResponse({"success": False, "error": "Stage does not match this work order."}, status=400)
                elif target_wo.bom and target_wo.bom.operations.exists() and has_route_plan_payload:
                    route_plan_requested = True
                elif not target_wo.stage_id and target_wo.bom and target_wo.bom.operations.exists():
                    route_plan_requested = True
                elif not target_wo.stage_id:
                    subtask = WorkOrder.objects.filter(company=company, parent=wo, stage=stage).first()
                    if not subtask:
                        subtask = WorkOrder.objects.create(
                            parent=wo,
                            company=company,
                            product_name=f"{wo.product_name} - {stage.name}",
                            bom=wo.bom,
                            quantity=wo.quantity,
                            customer=wo.customer,
                            status='pending',
                            stage=stage,
                            current_stage=stage,
                            assigned_to=wo.assigned_to or request.user,
                            priority=wo.priority,
                            order_type=wo.order_type,
                            instructions=wo.instructions
                        )
                    target_wo = subtask

                    if wo.current_stage_id != stage.id:
                        wo.current_stage = stage
                    if wo.machine_id:
                        wo.machine = None
                    if wo.assigned_worker_id:
                        wo.assigned_worker = None
                        wo.assignment_type = 'auto'
                    wo.save(update_fields=[
                        'current_stage',
                        'status',
                        'machine',
                        'assigned_worker',
                        'assignment_type'
                    ])
                else:
                    target_wo = wo
                    if target_wo.stage_id != stage.id:
                        return JsonResponse({"success": False, "error": "Stage does not match this work order."}, status=400)

                if not route_plan_requested and not target_wo.stage_id:
                    target_wo.stage = stage
                if not route_plan_requested and not target_wo.current_stage_id:
                    target_wo.current_stage = stage

            if not route_plan_requested and target_wo.status != 'pending':
                return JsonResponse({"success": False, "error": f"Cannot schedule work order in status '{target_wo.status}'"})

            machine = None
            if machine_id not in [None, '']:
                machine = get_object_or_404(Machine, pk=machine_id, company=company)
            start_date = datetime.fromisoformat(start_date_str.replace('Z', '+00:00'))
            if timezone.is_naive(start_date):
                start_date = timezone.make_aware(start_date)
            
            # Validation: Cannot start on a holiday
            if not validate_start_date(start_date, company):
                return JsonResponse({"success": False, "error": "Cannot schedule starting on a holiday."})

            if route_plan_requested:
                with transaction.atomic():
                    route_result = WorkOrderService.schedule_full_route(
                        wo,
                        stage,
                        machine,
                        start_date,
                        actor=request.user,
                        company=company,
                        stage_machine_ids=route_assignment_map,
                        stage_assignment_specs=route_assignment_specs,
                        operation_flow_mode=operation_flow_mode,
                    )
                    if requested_status and requested_status != 'pending':
                        route_status_fields = ['status']
                        if requested_status == 'in_progress':
                            route_status_fields.append('start_date')
                        elif requested_status in {'completed', 'canceled', 'archived'}:
                            route_status_fields.append('end_date')

                        for route_task in route_result['tasks']:
                            route_task.status = requested_status
                            if requested_status == 'in_progress' and not route_task.start_date:
                                route_task.start_date = start_date
                            elif requested_status in {'completed', 'canceled', 'archived'} and not route_task.end_date:
                                route_task.end_date = route_result['final_end']
                            route_task.save(update_fields=list(dict.fromkeys(route_status_fields)))

                        root_update_fields = ['status']
                        wo.status = requested_status
                        if requested_status == 'in_progress' and not wo.start_date:
                            wo.start_date = start_date
                            root_update_fields.append('start_date')
                        elif requested_status in {'completed', 'canceled', 'archived'} and not wo.end_date:
                            wo.end_date = route_result['final_end']
                            root_update_fields.append('end_date')
                        wo.save(update_fields=list(dict.fromkeys(root_update_fields)))
                first_task = route_result['first_task']
                audit_request_action(
                    request,
                    'update',
                    target=wo,
                    details={
                        'event': 'work_order_route_scheduled',
                        'stage_count': len(route_result['tasks']),
                        'first_stage_work_order_id': first_task.id,
                        'start_date': start_date.isoformat(),
                        'end_date': route_result['final_end'].isoformat(),
                        'operation_flow_mode': operation_flow_mode,
                        'quantity': wo.quantity,
                        'quantity_changed': bool(quantity_changed),
                    },
                )
                return JsonResponse({
                    "success": True,
                    "message": f"Work Order #{wo.id} scheduled across {len(route_result['tasks'])} stages",
                    "wo_id": wo.id,
                    "quantity": wo.quantity,
                    "quantity_changed": bool(quantity_changed),
                    "first_stage_wo_id": first_task.id,
                    "tasks": [
                        {
                            "id": task.id,
                            "stage_id": task.stage_id,
                            "quantity": task.quantity,
                        }
                        for task in route_result['tasks']
                    ],
                    "end_date": route_result['final_end'].isoformat(),
                })
                

            if not machine:
                return JsonResponse({"success": False, "error": "Machine is required"})

            # Calculate duration and end_date
            duration_hours = 8  # Default 8 hours
            if target_wo.bom and target_wo.bom.operations.exists():
                total_minutes = 0
                if stage:
                    op = target_wo.bom.operations.filter(stage=stage).order_by('order').first()
                    if op:
                        setup = float(op.setup_time or 0)
                        run_per_unit = float(op.run_time or 0)
                        total_minutes = setup + (run_per_unit * target_wo.quantity)
                        if total_minutes <= 0:
                            total_minutes = op.duration_minutes or 60
                else:
                    for op in target_wo.bom.operations.all():
                        setup = float(op.setup_time or 0)
                        run_per_unit = float(op.run_time or 0)
                        total_minutes += setup + (run_per_unit * target_wo.quantity)
                duration_hours = max(1, total_minutes / 60)
            
            end_date = calculate_end_date(start_date, duration_hours, company)
            
            # Conflict Check: Does this machine have an overlapping task?
            # Check for any WO on this machine where (StartA <= EndB) and (EndA >= StartB)
            conflicts = WorkOrder.objects.filter(
                company=company,
                machine=machine,
                status__in=['pending', 'in_progress'],
                start_date__lt=end_date,
                end_date__gt=start_date
            ).exclude(id=target_wo.id)
            
            if conflicts.exists():
                conflict_wo = conflicts.first()
                return JsonResponse({
                    "success": False, 
                    "error": f"Conflict! Machine '{machine.display_label}' is busy with Order #{conflict_wo.id} from {conflict_wo.start_date.strftime('%H:%M')} to {conflict_wo.end_date.strftime('%H:%M')}."
                })

            # Update work order
            target_wo.start_date = start_date
            target_wo.scheduled_start_date = start_date
            target_wo.end_date = end_date
            target_wo.machine = machine
            target_wo.operation_flow_mode = operation_flow_mode
            WorkOrderLifecycle.apply_transition(
                target_wo,
                'pending',
                actor=request.user,
                save=False,
                enforce_guards=False
            )
            if not target_wo.planner_start_at:
                target_wo.planner_start_at = timezone.now()
            target_wo.save()
            audit_request_action(
                request,
                'update',
                target=target_wo,
                details={
                    'event': 'work_order_scheduled',
                    'machine_id': machine.id,
                    'stage_id': stage.id if stage else None,
                    'start_date': start_date.isoformat(),
                    'end_date': end_date.isoformat(),
                    'operation_flow_mode': operation_flow_mode,
                },
            )
            
            return JsonResponse({
                "success": True,
                "message": f"Work Order #{target_wo.id} scheduled to {machine.display_label}",
                "wo_id": target_wo.id,
                "end_date": end_date.isoformat()
            })

        except ValueError as e:
            return JsonResponse({"success": False, "error": str(e)}, status=400)
        except WorkOrderLifecycleError as e:
            return JsonResponse({"success": False, "error": str(e)}, status=400)
        except Exception as e:
            return JsonResponse({"success": False, "error": str(e)}, status=500)


class AdvancedScheduleAPI(LoginRequiredMixin, View):
    """Create multi-stage schedule for a work order"""
    def post(self, request):
        try:
            if not user_has_role(request.user, ['planner', 'admin']):
                return JsonResponse({"success": False, "error": "Unauthorized"}, status=403)

            company = require_company(request.user)
            data = json.loads(request.body)
            
            wo_id = data.get('work_order_id')
            stages_data = data.get('stages', [])
            
            if not stages_data:
                return JsonResponse({"success": False, "error": "No stages provided"})
            
            wo = get_object_or_404(WorkOrder, pk=wo_id, company=company, status='pending')
            
            with transaction.atomic():
                # Create WorkOrderStage for each stage
                created_stages = {}
                
                for idx, stage_info in enumerate(stages_data):
                    stage_id = stage_info['stage_id']
                    machine_id = stage_info['machine_id']
                    quantity = stage_info.get('quantity', wo.quantity)
                    start_date_str = stage_info['start_date']
                    duration_hours = stage_info.get('duration_hours', 4)
                    
                    start_date = datetime.fromisoformat(start_date_str.replace('Z', '+00:00'))
                    if timezone.is_naive(start_date):
                        start_date = timezone.make_aware(start_date)
                    
                    if not validate_start_date(start_date, company):
                         return JsonResponse({"success": False, "error": f"Stage {stage_id} starts on a holiday."})

                    end_date = calculate_end_date(start_date, duration_hours, company)
                    
                    machine = get_object_or_404(Machine, pk=machine_id, company=company)
                    stage = get_company_stage(company, stage_id)
                    if not stage:
                        return JsonResponse({"success": False, "error": "Invalid stage for this company."}, status=400)
                    
                    wo_stage = WorkOrderStage.objects.create(
                        work_order=wo,
                        stage=stage,
                        machine=machine,
                        quantity=quantity,
                        start_date=start_date,
                        end_date=end_date,
                        status='scheduled',
                        sequence_order=idx
                    )
                    
                    created_stages[stage_id] = wo_stage
                
                # Set dependencies
                for stage_info in stages_data:
                    depends_on_id = stage_info.get('depends_on_id')
                    if depends_on_id:
                        current_stage = created_stages[stage_info['stage_id']]
                        dep_stage = created_stages.get(depends_on_id)
                        if dep_stage:
                            current_stage.depends_on.add(dep_stage)
                
                # Update main WO status and dates
                all_start_dates = [s.start_date for s in created_stages.values()]
                all_end_dates = [s.end_date for s in created_stages.values()]
                
                wo.start_date = min(all_start_dates)
                wo.end_date = max(all_end_dates)
                if not wo.planner_start_at:
                    wo.planner_start_at = timezone.now()
                wo.save()
            
            return JsonResponse({
                'success': True, 
                'message': f'Advanced schedule created with {len(created_stages)} stages',
                'wo_id': wo.id
            })
            
        except ValueError as e:
            return JsonResponse({'success': False, 'error': str(e)}, status=400)
        except WorkOrderLifecycleError as e:
            return JsonResponse({'success': False, 'error': str(e)}, status=400)
        except Exception as e:
            return JsonResponse({'success': False, 'error': str(e)}, status=500)
