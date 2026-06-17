from decimal import Decimal, InvalidOperation
import logging
import re
import base64
from datetime import datetime, timedelta

from django.conf import settings
from django.db.models import Q, Sum, Avg, Count
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import viewsets, permissions, status, filters
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from manufacturing.bom_attachments import save_bom_attachment
from .models import (
    WorkOrder, Machine, ProductionStage, BillOfMaterial,
    Product, ProductionLog, MachineFault, QualityCheck,
    WorkerCertification, ShiftAssignment, BOMOperationMaterial, Notification
)
from .serializers import (
    WorkOrderSerializer, MachineSerializer, ProductionStageSerializer,
    BillOfMaterialSerializer, ProductSerializer, ProductionLogSerializer,
    MachineFaultSerializer, QualityCheckSerializer,
    WorkerCertificationSerializer, ShiftAssignmentSerializer, WorkerSimpleSerializer
)
from django.contrib.auth.models import User
from .views import get_user_company, user_has_role
from .services import WorkOrderLifecycle, WorkOrderLifecycleError, flag_bom_change_impact
from manufacturing.utils import normalize_operation_time_minutes
from manufacturing.access_control import worker_eligible_user_q
from manufacturing.machine_shift_propagation import (
    machine_department_shift_keys,
    propagate_machine_department_shift_configuration,
)
from manufacturing.security import audit_request_action
from manufacturing.shift_utils import parse_bool

logger = logging.getLogger(__name__)


_BOM_VERSION_STEP = Decimal("0.1")
_OP_PREFIX_RE = re.compile(r"^\s*op\s*\d+\s*:\s*", re.IGNORECASE)


def _parse_bom_version_value(version) -> Decimal | None:
    version_text = str(version or "").strip().lower()
    if version_text.startswith("v"):
        version_text = version_text[1:]
    match = re.search(r"\d+(?:\.\d+)?", version_text)
    if not match:
        return None
    try:
        return Decimal(match.group(0))
    except InvalidOperation:
        return None


def _next_bom_version(product) -> str:
    versions = list(
        BillOfMaterial.objects.filter(product=product)
        .values_list("version", flat=True)
    )
    parsed_versions = [value for value in (_parse_bom_version_value(v) for v in versions) if value is not None]
    if not parsed_versions:
        fallback_major = max(len(versions) + 1, 1)
        return f"v{fallback_major}.0"
    next_value = max(parsed_versions) + _BOM_VERSION_STEP
    return f"v{next_value:.1f}"


def _normalize_operation_stage_name(operation_payload, fallback_index: int) -> str:
    explicit_stage = str(operation_payload.get("stage_name") or "").strip()
    if explicit_stage:
        return explicit_stage

    display_name = str(operation_payload.get("name") or "").strip()
    cleaned_display_name = _OP_PREFIX_RE.sub("", display_name).strip()
    if cleaned_display_name:
        return cleaned_display_name

    machine_type = str(operation_payload.get("type") or "").strip()
    if machine_type:
        return machine_type

    return f"Operation {fallback_index}"


def _company_stage_queryset(company):
    return (
        ProductionStage.objects.filter(
            Q(machine__company=company)
            | Q(bomoperation__bom__product__company=company)
        )
        .distinct()
    )


def _resolve_operation_stage(company, operation_payload, machine, stage_category, index: int):
    from .models import ProductionStage

    company_stages = _company_stage_queryset(company)
    stage_id = operation_payload.get("stage_id")
    if stage_id and str(stage_id).isdigit():
        stage = (
            company_stages.filter(id=stage_id)
            .select_related("machine")
            .first()
        )
        if stage:
            return stage, False

    stage_name = _normalize_operation_stage_name(operation_payload, index + 1)
    stage_candidates = company_stages.filter(name__iexact=stage_name)
    if machine:
        prioritized_stage = (
            stage_candidates.filter(Q(machine=machine) | Q(machine__isnull=True))
            .select_related("machine")
            .first()
        )
        if prioritized_stage:
            return prioritized_stage, False

    if stage_category:
        categorized_stage = (
            stage_candidates.filter(Q(category__iexact=stage_category) | Q(category__isnull=True))
            .select_related("machine")
            .first()
        )
        if categorized_stage:
            return categorized_stage, False

    existing_stage = stage_candidates.select_related("machine").first()
    if existing_stage:
        return existing_stage, False

    created_stage = ProductionStage.objects.create(
        name=stage_name,
        machine=machine,
        category=stage_category,
        order=index + 1,
        is_quality_check=bool(operation_payload.get("quality_check")),
    )
    return created_stage, True

# Custom permission for company-based access
class IsCompanyMember(permissions.BasePermission):
    def has_permission(self, request, view):
        return request.user and request.user.is_authenticated
    
    def has_object_permission(self, request, view, obj):
        company = get_user_company(request.user)
        if hasattr(obj, 'company'):
            return obj.company == company
        elif hasattr(obj, 'product') and hasattr(obj.product, 'company'):
            return obj.product.company == company
        elif hasattr(obj, 'work_order') and hasattr(obj.work_order, 'company'):
            return obj.work_order.company == company
        return False

class WorkOrderViewSet(viewsets.ModelViewSet):
    serializer_class = WorkOrderSerializer
    permission_classes = [IsAuthenticated, IsCompanyMember]
    
    def get_queryset(self):
        company = get_user_company(self.request.user)
        queryset = WorkOrder.objects.filter(company=company)
        
        # Filtering
        status_filter = self.request.query_params.get('status')
        if status_filter:
            queryset = queryset.filter(status=status_filter)
            
        priority_filter = self.request.query_params.get('priority')
        if priority_filter:
            queryset = queryset.filter(priority=priority_filter)
            
        return queryset.select_related('bom', 'machine', 'current_stage', 'assigned_to')
    
    @action(detail=True, methods=['post'])
    def update_status(self, request, pk=None):
        if not user_has_role(request.user, 'api.work_order.update_status'):
            return Response({'error': 'Unauthorized'}, status=status.HTTP_403_FORBIDDEN)

        work_order = self.get_object()
        new_status = str(request.data.get('status') or '').strip().lower()

        try:
            WorkOrderLifecycle.apply_transition(
                work_order,
                new_status,
                actor=request.user,
            )
        except WorkOrderLifecycleError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response({'status': 'updated', 'new_status': work_order.status})
    
    @action(detail=True, methods=['post'])
    def assign_to_stage(self, request, pk=None):
        if not user_has_role(request.user, 'api.work_order.assign_stage'):
            return Response({'error': 'Unauthorized'}, status=status.HTTP_403_FORBIDDEN)

        work_order = self.get_object()
        stage_id = request.data.get('stage_id')
        
        try:
            stage = ProductionStage.objects.get(id=stage_id)
            company = get_user_company(request.user)
            if stage.machine and stage.machine.company != company:
                return Response({'error': 'Invalid stage for this company'}, status=status.HTTP_400_BAD_REQUEST)
            work_order.current_stage = stage
            work_order.save()
            return Response({'status': 'assigned'})
        except ProductionStage.DoesNotExist:
            return Response({'error': 'Stage not found'}, status=status.HTTP_404_NOT_FOUND)
    
    @action(detail=True, methods=['post'])
    def auto_assign_worker(self, request, pk=None):
        """Automatically assign worker based on shift assignment"""
        if not user_has_role(request.user, 'api.work_order.assign_worker'):
            return Response({'error': 'Unauthorized'}, status=status.HTTP_403_FORBIDDEN)

        work_order = self.get_object()
        
        if not work_order.machine:
            return Response({'error': 'Work order has no machine assigned'}, status=status.HTTP_400_BAD_REQUEST)
        
        if not work_order.start_date:
            return Response({'error': 'Work order has no start date'}, status=status.HTTP_400_BAD_REQUEST)
        
        # Determine which shift this WO falls into
        hour = work_order.start_date.hour
        if 6 <= hour < 14:
            shift_type = 'day'
        elif 14 <= hour < 22:
            shift_type = 'middle'
        else:
            shift_type = 'night'
        
        # Look for shift assignment
        shift_assignment = ShiftAssignment.objects.filter(
            machine=work_order.machine,
            date=work_order.start_date.date(),
            shift_type=shift_type
        ).first()
        
        if shift_assignment:
            work_order.assigned_worker = shift_assignment.worker
            work_order.assignment_type = 'auto'
            if not work_order.supervisor_start_at:
                work_order.supervisor_start_at = timezone.now()
            work_order.save()
            
            return Response({
                'success': True,
                'assigned_worker': shift_assignment.worker.username,
                'assignment_type': 'auto'
            })
        else:
            return Response({
                'success': False,
                'message': f'No worker assigned to {work_order.machine.name} for {shift_type} shift on {work_order.start_date.date()}'
            }, status=status.HTTP_404_NOT_FOUND)
    
    @action(detail=True, methods=['post'])
    def reassign_worker(self, request, pk=None):
        """Manually reassign work order to a different worker"""
        if not user_has_role(request.user, 'api.work_order.assign_worker'):
            return Response({'error': 'Unauthorized'}, status=status.HTTP_403_FORBIDDEN)

        work_order = self.get_object()
        worker_id = request.data.get('worker_id')
        
        if not worker_id:
            return Response({'error': 'worker_id is required'}, status=status.HTTP_400_BAD_REQUEST)
        
        try:
            from django.contrib.auth.models import User
            worker = User.objects.get(id=worker_id)
            
            work_order.assigned_worker = worker
            work_order.assignment_type = 'manual'
            if not work_order.supervisor_start_at:
                work_order.supervisor_start_at = timezone.now()
            work_order.save()
            
            return Response({
                'success': True,
                'assigned_worker': worker.username,
                'assignment_type': 'manual'
            })
            
        except User.DoesNotExist:
            return Response({'error': 'Worker not found'}, status=status.HTTP_404_NOT_FOUND)

    @action(detail=True, methods=['post'])
    def close_order(self, request, pk=None):
        """Close a work order (Planner Action)"""
        if not user_has_role(request.user, 'api.work_order.close'):
            return Response({'error': 'Unauthorized'}, status=status.HTTP_403_FORBIDDEN)

        work_order = self.get_object()
        try:
            WorkOrderLifecycle.close(
                work_order,
                actor=request.user,
                require_ready=True,
            )
        except WorkOrderLifecycleError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response({'success': True})

    @action(detail=True, methods=['get'], url_path='bom-requirements')
    def bom_requirements(self, request, pk=None):
        """
        Calculate expected material usage based on produced quantity.
        Usage: GET /api/v1/workorders/{id}/bom-requirements/?qty=10
        """
        work_order = self.get_object()
        try:
            qty_produced = float(request.query_params.get('qty', 0))
        except ValueError:
            return Response({'error': 'Invalid quantity'}, status=status.HTTP_400_BAD_REQUEST)

        if not work_order.bom:
            return Response({'components': []})

        components_qs = work_order.bom.components.all()
        current_stage_id = work_order.current_stage_id or work_order.stage_id
        current_machine_id = work_order.machine_id
        target_operation = None

        operation_qs = work_order.bom.operations.all()
        if current_stage_id:
            target_operation = operation_qs.filter(stage_id=current_stage_id).order_by('order').first()
        if target_operation is None and current_machine_id:
            target_operation = operation_qs.filter(machine_id=current_machine_id).order_by('order').first()

        # If explicit operation-material mapping exists, constrain to those materials.
        # Legacy BOMs without mapping continue to return all BOM components.
        if target_operation is not None:
            component_ids = list(target_operation.material_links.values_list('component_id', flat=True))
            if component_ids:
                components_qs = components_qs.filter(id__in=component_ids)

        components_data = []
        bom_base_qty = float(work_order.bom.base_quantity or 1) or 1.0
        for comp in components_qs:
            component_qty = float(comp.quantity or 0)
            expected = (component_qty / bom_base_qty) * qty_produced
            
            components_data.append({
                'component_id': comp.id,
                'product_id': comp.product_id,
                'name': comp.material_name,
                'unit': comp.unit,
                'base_quantity': bom_base_qty,
                'component_quantity': component_qty,
                'expected_qty': round(expected, 3),
                'cost_per_unit': float(comp.cost_per_unit)
            })
            
        return Response({'components': components_data})

class MachineViewSet(viewsets.ModelViewSet):
    serializer_class = MachineSerializer
    permission_classes = [IsAuthenticated, IsCompanyMember]
    pagination_class = None

    def update(self, request, *args, **kwargs):
        self._shift_propagated_count = 0
        response = super().update(request, *args, **kwargs)
        if hasattr(response.data, "__setitem__"):
            response.data["shift_propagated_count"] = getattr(self, "_shift_propagated_count", 0)
        return response

    def perform_update(self, serializer):
        instance = serializer.instance
        tracked_fields = (
            "name",
            "code",
            "type",
            "category",
            "status",
            "is_active",
            "hourly_rate",
            "use_factory_shifts",
            "shift_configuration",
        )
        before = {field: getattr(instance, field, None) for field in tracked_fields}
        shift_fields_present = any(
            field in self.request.data
            for field in ("use_factory_shifts", "shift_configuration")
        )
        previous_keys = machine_department_shift_keys(instance) if shift_fields_present else []
        machine = serializer.save()
        apply_to_category = parse_bool(self.request.data.get("apply_shift_to_category"), default=True)
        if shift_fields_present and apply_to_category:
            self._shift_propagated_count = propagate_machine_department_shift_configuration(
                machine,
                extra_keys=previous_keys,
            )
        changed_fields = [
            field
            for field in tracked_fields
            if before.get(field) != getattr(machine, field, None)
        ]
        if changed_fields or getattr(self, "_shift_propagated_count", 0):
            audit_request_action(
                self.request,
                "update",
                target=machine,
                details={
                    "event": "machine_shift_updated" if shift_fields_present else "machine_updated",
                    "machine_id": machine.id,
                    "machine_code": machine.code,
                    "status": machine.status,
                    "changed_fields": changed_fields,
                    "shift_propagated_count": getattr(self, "_shift_propagated_count", 0),
                },
            )
    
    def get_queryset(self):
        company = get_user_company(self.request.user)
        queryset = Machine.objects.filter(company=company)
        
        status_filter = self.request.query_params.get('status')
        if status_filter:
            queryset = queryset.filter(status=status_filter)
            
        return queryset
    
    @action(detail=True, methods=['get'])
    def utilization(self, request, pk=None):
        machine = self.get_object()
        # Calculate machine utilization for last 30 days
        end_date = datetime.now()
        start_date = end_date - timedelta(days=30)
        
        total_hours = (end_date - start_date).total_seconds() / 3600
        work_hours = WorkOrder.objects.filter(
            machine=machine,
            start_date__gte=start_date,
            start_date__lte=end_date,
            status__in=['in_progress', 'completed']
        ).count() * 8  # Assuming 8 hours per work order
        
        utilization = (work_hours / total_hours * 100) if total_hours > 0 else 0
        
        return Response({
            'utilization_percentage': round(utilization, 2),
            'total_hours': total_hours,
            'work_hours': work_hours
        })
    
    @action(detail=True, methods=['get'])
    def fault_history(self, request, pk=None):
        machine = self.get_object()
        faults = MachineFault.objects.filter(machine=machine).order_by('-created_at')
        serializer = MachineFaultSerializer(faults, many=True)
        return Response(serializer.data)
    
    @action(detail=False, methods=['get'])
    def by_category(self, request):
        """
        Get machines filtered by category/type.
        Usage: GET /api/v1/machines/by_category/?category=CNC
        Returns only machines from user's company that match the category.
        """
        company = get_user_company(request.user)
        category = request.query_params.get('category', '').strip()
        
        # Filter by company and matching category or type
        queryset = Machine.objects.filter(company=company, is_active=True)
        
        if category:
            from django.db.models import Q
            queryset = queryset.filter(
                Q(category__iexact=category) |
                Q(type__iexact=category)
            )
        
        queryset = queryset.order_by('name')
        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)

class BillOfMaterialViewSet(viewsets.ModelViewSet):
    serializer_class = BillOfMaterialSerializer
    permission_classes = [IsAuthenticated, IsCompanyMember]
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['product__name', 'version', 'status']
    ordering_fields = ['created_at', 'updated_at']
    ordering = ['-created_at'] # Show newest first
    
    def get_queryset(self):
        company = get_user_company(self.request.user)
        qs = BillOfMaterial.objects.filter(product__company=company).select_related('product')
        status_filter = self.request.query_params.get('status')
        active_only = self.request.query_params.get('active_only')
        if status_filter:
            qs = qs.filter(status=status_filter)
        elif active_only in ['1', 'true', 'True']:
            qs = qs.filter(status='active')
        return qs

    def perform_update(self, serializer):
        previous_status = getattr(serializer.instance, "status", None)
        bom = serializer.save()
        if previous_status != "active" and bom.status == "active":
            flag_bom_change_impact(bom, actor=self.request.user)
    
    @action(detail=True, methods=['post'])
    def calculate_cost(self, request, pk=None):
        bom = self.get_object()
        from .services import BOMService
        
        try:
            cost = BOMService.calculate_cost(bom)
            return Response({'total_cost': float(cost)})
        except ValueError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
    
    @action(detail=False, methods=['post'])
    def create_full_bom(self, request):
        """
        Create a full BOM with Product, Components, Operations, and Quality Checks within a transaction.
        Supports updating existing Draft BOMs if bom_id is provided.
        """
        if not user_has_role(request.user, 'api.bom.manage'):
            return Response({'error': 'Unauthorized'}, status=status.HTTP_403_FORBIDDEN)

        data = request.data
        bom_id = data.get('bom_id')
        
        product_name = data.get('product')
        batch_size = data.get('batch')
        batch_type = (data.get('batch_type') or data.get('uom') or 'pcs').strip().lower()
        components = data.get('materials', [])
        operations = data.get('operations', [])
        attachment_data = data.get('attachment_data')
        attachment_name = data.get('attachment_name')
        logger.debug(
            "create_full_bom requested user_id=%s bom_id=%s materials=%s operations=%s",
            getattr(request.user, 'id', None),
            bom_id,
            len(components),
            len(operations),
        )

        quality_checks = data.get('qualityChecks', []) 

        valid_uoms = {choice[0] for choice in BillOfMaterial._meta.get_field('uom').choices}

        if not product_name:
            return Response({'error': 'Product name is required'}, status=status.HTTP_400_BAD_REQUEST)
        if batch_type not in valid_uoms:
            return Response({'error': 'Invalid batch type'}, status=status.HTTP_400_BAD_REQUEST)

        company = get_user_company(request.user)

        from django.db import transaction
        try:
            with transaction.atomic():
                # 1. Create or Get Product
                product = Product.objects.filter(
                    company=company,
                    name__iexact=product_name
                ).first()
                if not product:
                    product = Product.objects.create(
                        name=product_name,
                        company=company,
                        description=f'Product {product_name}'
                    )

                # 2. Create or Update BillOfMaterial
                bom = None
                version_created_from = None
                if bom_id:
                    try:
                        existing_bom = BillOfMaterial.objects.get(pk=bom_id, product__company=company)
                        bom_is_used = WorkOrder.objects.filter(bom=existing_bom).exists()
                        # If existing is unused DRAFT, update it. If active/used, create a new version.
                        if existing_bom.status == 'draft' and not bom_is_used:
                            bom = existing_bom
                            bom.base_quantity = batch_size or 1
                            bom.uom = batch_type
                            # We will clear components/ops for full replace
                        else:
                            # Active or already referenced by a WO, so fall through to create new version logic.
                            version_created_from = existing_bom.id
                            pass 
                    except BillOfMaterial.DoesNotExist:
                        pass # proceed to create new

                if not bom:
                     # Check if an active BOM exists for this product to determining version
                    existing_active = BillOfMaterial.objects.filter(product=product, status='active').first()

                    if existing_active:
                        new_version = _next_bom_version(product)
                    else:
                        new_version = "v1.0"
                    
                    bom = BillOfMaterial.objects.create(
                        product=product,
                        base_quantity=batch_size or 100,
                        uom=batch_type,
                        version=new_version,
                        status='draft',
                        created_by=request.user
                    )
                else:
                    # Updating Existing Draft -> Wipe Content for clean save
                    bom.save() # Update fields like base_quantity
                    bom.components.all().delete()
                    bom.operations.all().delete()
                    # bom.acceptance_criteria.all().delete() # If managing criteria too

                if attachment_data:
                    header, encoded = attachment_data.split(";base64,", 1)
                    content_type = header.replace("data:", "", 1)
                    save_bom_attachment(
                        bom,
                        base64.b64decode(encoded),
                        file_name=attachment_name,
                        content_type=content_type,
                    )
                
                # 3. Create Components
                from .models import BOMComponent
                components_by_client_id = {}

                def _to_float(value, default=0.0):
                    try:
                        if value is None or value == '':
                            return float(default)
                        return float(value)
                    except (TypeError, ValueError):
                        return float(default)

                for idx, comp in enumerate(components, start=1):
                     material_name = (comp.get('name') or '').strip()
                     product = None

                     if not material_name:
                         return Response(
                             {'error': f'Material row {idx} is missing a material name.'},
                             status=status.HTTP_400_BAD_REQUEST
                         )

                     # Check if material exists as a Product, if not create it so it appears in search next time
                     if material_name:
                         product = Product.objects.filter(
                             company=company,
                             name__iexact=material_name
                         ).first()
                         if not product:
                             product = Product.objects.create(
                                 company=company,
                                 name=material_name,
                                 description='Material automatically created from BOM',
                                 unit=comp.get('unit', 'pcs'),
                                 material_type='raw'
                             )

                     qty_value = _to_float(comp.get('qty'), 0)
                     if qty_value <= 0:
                        return Response(
                            {'error': f"Material '{material_name}' must have a quantity greater than 0."},
                            status=status.HTTP_400_BAD_REQUEST
                        )
                     wastage_mode = (comp.get('wastage_mode') or 'percent').lower()
                     raw_wastage = _to_float(comp.get('wastage'), 0)
                     wastage_percent = _to_float(comp.get('wastage_percent'), 0)
                     wastage_quantity = _to_float(comp.get('wastage_qty'), 0)

                     if wastage_mode == 'qty':
                        if wastage_quantity == 0:
                            wastage_quantity = raw_wastage
                        if wastage_percent == 0 and qty_value > 0:
                            wastage_percent = (wastage_quantity / qty_value) * 100
                     else:
                        if wastage_percent == 0:
                            wastage_percent = raw_wastage
                        if wastage_quantity == 0 and qty_value > 0:
                            wastage_quantity = (qty_value * wastage_percent) / 100

                     created_component = BOMComponent.objects.create(
                        bom=bom,
                        product=product,
                        material_name=material_name,
                        quantity=qty_value,
                        unit=comp.get('unit', 'pcs'),
                        cost_per_unit=comp.get('cost', 0),
                        wastage_quantity=wastage_quantity,
                        wastage_percent=wastage_percent,
                        scrap_value_per_unit=comp.get('scrap', 0)
                     )
                     client_id = str(comp.get('client_id') or '').strip()
                     if client_id:
                        components_by_client_id[client_id] = created_component
                     components_by_client_id[f"cmp-{created_component.id}"] = created_component

                # 4. Create Operations (Routing)
                from .models import BOMOperation

                for index, op in enumerate(operations):
                    machine_id = op.get('machine_id')
                    stage_id = op.get('stage_id')
                    machine_type_str = op.get('type')

                    machine = None
                    if machine_id and str(machine_id).isdigit():
                        try:
                            machine = Machine.objects.get(id=machine_id, company=company)
                        except Machine.DoesNotExist:
                            logger.warning(
                                "create_full_bom machine lookup miss user_id=%s machine_id=%s company_id=%s",
                                getattr(request.user, 'id', None),
                                machine_id,
                                getattr(company, 'id', None),
                            )

                    stage_category = (
                        machine_type_str
                        or (machine.category if machine else None)
                        or (machine.type if machine else None)
                    )
                    requires_qc = bool(op.get('quality_check'))
                    stage, _ = _resolve_operation_stage(
                        company=company,
                        operation_payload=op,
                        machine=machine,
                        stage_category=stage_category,
                        index=index,
                    )
                    if stage and not stage_category:
                        stage_category = (
                            stage.category
                            or (stage.machine.category if stage.machine else None)
                            or (stage.machine.type if stage.machine else None)
                        )
                    stage_name = stage.name

                    update_fields = []
                    if machine and not stage.machine_id:
                        stage.machine = machine
                        update_fields.append('machine')
                    if stage_category and not stage.category:
                        stage.category = stage_category
                        update_fields.append('category')
                    if requires_qc != bool(getattr(stage, 'is_quality_check', False)):
                        stage.is_quality_check = requires_qc
                        update_fields.append('is_quality_check')
                    if update_fields:
                        stage.save(update_fields=update_fields)

                    created_operation = BOMOperation.objects.create(
                        bom=bom,
                        machine=machine,
                        machine_type=machine_type_str or stage_category,
                        stage=stage,
                        order=(index + 1) * 10,
                        setup_time=normalize_operation_time_minutes(
                            op.get('setup_time', 0),
                            op.get('setup_time_unit') or op.get('setup_unit') or 'min',
                        ),
                        run_time=normalize_operation_time_minutes(
                            op.get('run_time', 0),
                            op.get('run_time_unit') or op.get('run_unit') or 'min',
                        ),
                        description=op.get('description') or f"Operation {stage_name}"
                    )

                    material_refs = op.get('material_client_ids') or op.get('material_refs') or []
                    if isinstance(material_refs, str):
                        material_refs = [material_refs]

                    linked_component_ids = set()
                    for ref in material_refs:
                        key = str(ref or '').strip()
                        if not key:
                            continue
                        component = components_by_client_id.get(key)
                        if component is None and key.isdigit():
                            component = components_by_client_id.get(f"cmp-{key}")
                        if component is None or component.id in linked_component_ids:
                            continue
                        linked_component_ids.add(component.id)
                        BOMOperationMaterial.objects.create(
                            operation=created_operation,
                            component=component,
                        )

                    # 5. Handle Quality Checks LINKED to this operation
                    # The frontend stores checks on the operation object?
                    # "op.quality_check" boolean and maybe a list of checks if we inspect the frontend store.
                    # The store has 'selectedOpChecks'. Ideally this data is passed inside 'operations' list.
                    # If not, we might lose it. I'll assume standard payload structure for now.

                # 6. Global Quality Checks (Tab 2)
                from .models import BOMAcceptanceCriteria
                for check in quality_checks:
                     BOMAcceptanceCriteria.objects.create(
                        bom=bom,
                        parameter=check.get('name', 'Check'),
                        method=check.get('type', 'Visual'),
                        criteria_min=check.get('criteria', ''),
                        pass_fail=(check.get('type') == 'pass_fail')
                     )

                if version_created_from:
                    Notification.objects.create(
                        recipient=request.user,
                        title="BOM version created",
                        message=(
                            f"{bom.product.name} was saved as {bom.version}. "
                            "Existing work orders keep their original BOM snapshot."
                        ),
                        link=f"/manufacturing/bom-builder/{bom.id}/",
                    )

        except Exception as e:
            logger.exception(
                "create_full_bom failed user_id=%s company_id=%s bom_id=%s",
                getattr(request.user, 'id', None),
                getattr(company, 'id', None) if company else None,
                bom_id,
            )
            payload = {'error': 'Failed to save BOM. Please review your data and try again.'}
            if settings.DEBUG:
                payload['detail'] = str(e)
            return Response(payload, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        return Response({
            'success': True,
            'product_id': product.id,
            'bom_id': bom.id,
            'bom_status': bom.status,
            'bom_version': bom.version,
            'version_created': bool(version_created_from),
            'previous_bom_id': version_created_from,
            'message': (
                'A new BOM version was created. Existing work orders keep their original BOM snapshot.'
                if version_created_from
                else 'BOM configuration saved successfully.'
            ),
        })

class ProductionLogViewSet(viewsets.ModelViewSet):
    serializer_class = ProductionLogSerializer
    permission_classes = [IsAuthenticated, IsCompanyMember]
    
    def get_queryset(self):
        company = get_user_company(self.request.user)
        return ProductionLog.objects.filter(work_order__company=company).select_related('work_order', 'worker')
    
    @action(detail=False, methods=['get'])
    def my_logs(self, request):
        """Get logs for current user"""
        logs = self.get_queryset().filter(worker=request.user)
        serializer = self.get_serializer(logs, many=True)
        return Response(serializer.data)
    
    @action(detail=False, methods=['post'])
    def approve_multiple(self, request):
        """Approve multiple logs at once"""
        if not user_has_role(request.user, 'api.production_log.approve'):
            return Response({'error': 'Unauthorized'}, status=status.HTTP_403_FORBIDDEN)

        log_ids = request.data.get('log_ids', [])
        if not log_ids:
            return Response({'error': 'No log IDs provided'}, status=status.HTTP_400_BAD_REQUEST)

        from .services import ProductionLogService

        logs = list(self.get_queryset().filter(id__in=log_ids, status='pending'))
        approved_count = 0
        for log in logs:
            ProductionLogService.approve_log(log, request.user)
            approved_count += 1

        return Response({'approved_count': approved_count})

class QualityCheckViewSet(viewsets.ModelViewSet):
    serializer_class = QualityCheckSerializer
    permission_classes = [IsAuthenticated, IsCompanyMember]
    
    def get_queryset(self):
        company = get_user_company(self.request.user)
        return QualityCheck.objects.filter(work_order__company=company).select_related('work_order', 'checked_by')
    
    @action(detail=False, methods=['get'])
    def quality_metrics(self, request):
        """Get quality metrics for the company"""
        company = get_user_company(self.request.user)
        end_date = datetime.now()
        start_date = end_date - timedelta(days=30)
        
        checks = QualityCheck.objects.filter(
            work_order__company=company,
            created_at__gte=start_date,
            created_at__lte=end_date
        )
        
        total_checked = checks.count()
        if total_checked == 0:
            return Response({
                'total_checks': 0,
                'good_quantity': 0,
                'faulty_quantity': 0,
                'repair_quantity': 0,
                'quality_rate': 0
            })
        
        good_qty = checks.aggregate(Sum('good_quantity'))['good_quantity__sum'] or 0
        faulty_qty = checks.aggregate(Sum('faulty_quantity'))['faulty_quantity__sum'] or 0
        repair_qty = checks.aggregate(Sum('repair_quantity'))['repair_quantity__sum'] or 0
        total_qty = good_qty + faulty_qty + repair_qty
        
        quality_rate = (good_qty / total_qty * 100) if total_qty > 0 else 0
        
        return Response({
            'total_checks': total_checked,
            'good_quantity': good_qty,
            'faulty_quantity': faulty_qty,
            'repair_quantity': repair_qty,
            'quality_rate': round(quality_rate, 2)
        })

class ProductionStageViewSet(viewsets.ModelViewSet):
    serializer_class = ProductionStageSerializer
    permission_classes = [IsAuthenticated, IsCompanyMember]
    
    def get_queryset(self):
        company = get_user_company(self.request.user)
        return (
            ProductionStage.objects.filter(
                Q(machine__company=company) |
                Q(bomoperation__bom__product__company=company)
            )
            .select_related('machine')
            .distinct()
            .order_by('order', 'name')
        )

class ProductViewSet(viewsets.ModelViewSet):
    serializer_class = ProductSerializer
    permission_classes = [IsAuthenticated, IsCompanyMember]
    
    def get_queryset(self):
        company = get_user_company(self.request.user)
        return Product.objects.filter(company=company)


# Worker Assignment ViewSets
class WorkerCertificationViewSet(viewsets.ModelViewSet):
    serializer_class = WorkerCertificationSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = None
    
    def get_queryset(self):
        company = get_user_company(self.request.user)
        queryset = WorkerCertification.objects.filter(
            machine__company=company
        ).select_related('worker', 'machine')
        
        # Filter by worker
        worker_id = self.request.query_params.get('worker_id')
        if worker_id:
            queryset = queryset.filter(worker_id=worker_id)
        
        # Filter by machine
        machine_id = self.request.query_params.get('machine_id')
        if machine_id:
            queryset = queryset.filter(machine_id=machine_id)
        
        return queryset
    
    @action(detail=False, methods=['get'], url_path='certified-workers/(?P<machine_id>[^/.]+)')
    def certified_workers(self, request, machine_id=None):
        """Get all workers available for a specific machine (certification disabled)"""
        company = get_user_company(request.user)
        workers = User.objects.filter(
            profile__company=company,
        ).filter(
            worker_eligible_user_q()
        ).select_related('profile')
        serializer = WorkerSimpleSerializer(workers, many=True)
        return Response(serializer.data)

    @action(detail=False, methods=['get'])
    def all_workers(self, request):
        """Get all workers in the company"""
        company = get_user_company(request.user)
        workers = User.objects.filter(
            profile__company=company
        ).filter(
            worker_eligible_user_q()
        ).select_related('profile')
        serializer = WorkerSimpleSerializer(workers, many=True)
        return Response(serializer.data)
    
    @action(detail=False, methods=['post'])
    def bulk_create(self, request):
        """Bulk create certifications for a worker"""
        worker_id = request.data.get('worker_id')
        machine_ids = request.data.get('machine_ids', [])
        skill_level = request.data.get('skill_level', 'basic')
        
        created_certs = []
        for machine_id in machine_ids:
            cert, created = WorkerCertification.objects.get_or_create(
                worker_id=worker_id,
                machine_id=machine_id,
                defaults={'skill_level': skill_level}
            )
            if created:
                created_certs.append(cert)
        
        serializer = self.get_serializer(created_certs, many=True)
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class ShiftAssignmentViewSet(viewsets.ModelViewSet):
    serializer_class = ShiftAssignmentSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = None
    
    def get_queryset(self):
        company = get_user_company(self.request.user)
        queryset = ShiftAssignment.objects.filter(
            machine__company=company
        ).select_related('worker', 'machine', 'created_by')
        
        # Filter by date
        date_filter = self.request.query_params.get('date')
        if date_filter:
            queryset = queryset.filter(date=date_filter)
        
        # Filter by shift
        shift_filter = self.request.query_params.get('shift')
        if shift_filter:
            queryset = queryset.filter(shift_type=shift_filter)
        
        # Filter by worker
        worker_id = self.request.query_params.get('worker_id')
        if worker_id:
            queryset = queryset.filter(worker_id=worker_id)
        
        return queryset
    
    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)
    
    @action(detail=False, methods=['post'])
    def bulk_assign(self, request):
        """Bulk assign workers to machines for a shift"""
        shift_type = request.data.get('shift_type')
        date = request.data.get('date')
        assignments = request.data.get('assignments', [])  # [{"worker_id": X, "machine_id": Y}, ...]
        
        if not shift_type or not date:
            return Response(
                {'error': 'shift_type and date are required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        company = get_user_company(request.user)
        created_assignments = []
        errors = []
        
        # UI is machine-centric. We should probably clear existing assignments for these machines 
        # for this specific date/shift or update them.
        # But wait, unique_together is ['worker', 'shift_type', 'date'].
        # This means a worker can only be on one machine per shift. Good.
        
        # Extract machine IDs being updated to handle unassignments if needed
        # (Though current UI logic sends the full list of machines)
        
        for assignment in assignments:
            worker_id = assignment.get('worker_id')
            machine_id = assignment.get('machine_id')
            
            if not machine_id:
                continue
                
            try:
                if worker_id:
                    # Update or create assignment for this machine
                    # Since unique_together is worker-based, we should check if this machine already has someone else
                    # and if this worker already is somewhere else.
                    
                    # 1. Remove any other worker from this machine for this shift
                    ShiftAssignment.objects.filter(
                        machine_id=machine_id,
                        date=date,
                        shift_type=shift_type
                    ).exclude(worker_id=worker_id).delete()
                    
                    # 2. Assign worker to machine
                    shift_assignment, created = ShiftAssignment.objects.update_or_create(
                        worker_id=worker_id,
                        shift_type=shift_type,
                        date=date,
                        defaults={
                            'machine_id': machine_id,
                            'created_by': request.user
                        }
                    )
                    created_assignments.append(shift_assignment)
                else:
                    # Unassign machine: remove any assignment for this machine on this shift
                    ShiftAssignment.objects.filter(
                        machine_id=machine_id,
                        date=date,
                        shift_type=shift_type
                    ).delete()
                    
            except Exception as e:
                errors.append(f"Error with machine {machine_id}: {str(e)}")
        
        serializer = self.get_serializer(created_assignments, many=True)
        response_data = {
            'success': True,
            'created': len(created_assignments),
            'assignments': serializer.data
        }
        if errors:
            response_data['errors'] = errors
        
        return Response(response_data, status=status.HTTP_201_CREATED)
    
    @action(detail=False, methods=['get'])
    def current_shift(self, request):
        """Get shift assignments for the current shift"""
        from datetime import datetime
        now = datetime.now()
        hour = now.hour
        
        # Determine current shift
        if 6 <= hour < 14:
            shift_type = 'day'
        elif 14 <= hour < 22:
            shift_type = 'middle'
        else:
            shift_type = 'night'
        
        today = now.date()
        assignments = self.get_queryset().filter(
            date=today,
            shift_type=shift_type
        )
        
        serializer = self.get_serializer(assignments, many=True)
        return Response({
            'shift_type': shift_type,
            'date': today,
            'assignments': serializer.data
        })


