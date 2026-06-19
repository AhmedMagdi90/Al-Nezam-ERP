"""Work order orchestration service extracted from the legacy facade.

The public compatibility import remains ``from manufacturing.services import
WorkOrderService``. This module intentionally preserves the moved code's
behavior and leaves cross-domain collaborators in ``manufacturing.services`` for
later extractions.
"""

from decimal import Decimal, InvalidOperation
from datetime import timedelta

from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone

from accounts.models import Profile
from manufacturing.models import ProductionLog, SystemSettings, WorkOrder
from manufacturing.shift_utils import machine_shift_configuration


class _LegacyNotificationService:
    @staticmethod
    def notify_role(*args, **kwargs):
        from manufacturing.services import NotificationService

        return NotificationService.notify_role(*args, **kwargs)


NotificationService = _LegacyNotificationService


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
        from manufacturing.models import QualityCheck

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
        from manufacturing.models import ProductionLog, QualityCheck

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
        from manufacturing.models import WorkOrder, QualityCheck

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
            from manufacturing.models import ProductionStage
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
        from manufacturing.models import QualityCheck
        return QualityCheck.objects.filter(work_order=stage_work_order, status='new').exists()

    @staticmethod
    def _ensure_qc_record(stage_work_order):
        from manufacturing.models import QualityCheck
        existing = QualityCheck.objects.filter(work_order=stage_work_order, status='new')
        if existing.exists():
            return True
        # Only create a new QC record when there is approved qty that is not yet checked
        from manufacturing.models import WorkOrder
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
        from manufacturing.models import WorkOrder, WorkOrderChangeLog

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
        from manufacturing.models import WorkOrderChangeLog

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
        from manufacturing.models import WorkOrder

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
        from manufacturing.models import Machine

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
        from manufacturing.models import WorkOrder

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
        from manufacturing.models import WorkOrder, Machine

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
        from manufacturing.models import WorkOrder

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

        actual_completion_at = timezone.now()

        if stage_work_order.parent_id:
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
            from manufacturing.models import QualityCheck
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
        from manufacturing.models import WorkOrder

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
            from manufacturing.models import QualityCheck
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
        from manufacturing.models import WorkOrder
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
        from manufacturing.models import WorkOrder

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
        from manufacturing.models import WorkOrder, WorkOrderChangeLog

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
        from manufacturing.models import WorkOrder, WorkOrderChangeLog

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
        from manufacturing.models import WorkOrder
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
        from manufacturing.models import WorkOrder

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
        from manufacturing.models import WorkOrder

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
        from manufacturing.models import WorkOrder
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
        from manufacturing.models import WorkOrder

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
        from manufacturing.models import WorkOrder

        return WorkOrder.objects.filter(parent=parent_wo, stage_id__isnull=False).values(
            'stage_id'
        ).annotate(
            cnt=Count('id')
        ).filter(cnt__gt=1).exists()
