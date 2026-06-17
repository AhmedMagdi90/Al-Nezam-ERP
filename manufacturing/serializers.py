import json
import json

from rest_framework import serializers
from django.contrib.auth.models import User
from django.core.files.uploadedfile import UploadedFile
from .models import (
    WorkOrder, Machine, ProductionStage, BillOfMaterial, 
    BOMComponent, Product, ProductionLog, MachineFault, 
    QualityCheck, Company, WorkerCertification, ShiftAssignment
)
from .shift_utils import coerce_shift_configuration_payload, parse_bool
from .utils import normalize_machine_code

class CompanySerializer(serializers.ModelSerializer):
    class Meta:
        model = Company
        fields = '__all__'

class ProductSerializer(serializers.ModelSerializer):
    class Meta:
        model = Product
        fields = '__all__'

class MachineSerializer(serializers.ModelSerializer):
    image_url = serializers.SerializerMethodField()

    class Meta:
        model = Machine
        fields = '__all__'

    def to_internal_value(self, data):
        if hasattr(data, "keys"):
            mutable = {key: data.get(key) for key in data.keys()}
        else:
            mutable = dict(data)

        if "name" in mutable:
            mutable["name"] = str(mutable.get("name") or "").strip()
        if "code" in mutable:
            mutable["code"] = normalize_machine_code(mutable.get("code"))
        if "type" in mutable:
            type_value = str(mutable.get("type") or "").strip()
            if self.instance and not type_value:
                mutable.pop("type", None)
            else:
                mutable["type"] = type_value
        if "category" in mutable:
            category_value = str(mutable.get("category") or "").strip()
            if self.instance and not category_value:
                mutable.pop("category", None)
            else:
                mutable["category"] = category_value
        if "image" in mutable and not isinstance(mutable.get("image"), UploadedFile):
            if not mutable.get("image"):
                mutable.pop("image", None)

        if "use_factory_shifts" in mutable:
            mutable["use_factory_shifts"] = parse_bool(mutable.get("use_factory_shifts"), default=True)

        if "shift_configuration" in mutable:
            try:
                mutable["shift_configuration"] = coerce_shift_configuration_payload(
                    mutable.get("shift_configuration"),
                    default_enabled=False,
                )
            except (TypeError, ValueError, json.JSONDecodeError):
                raise serializers.ValidationError({"shift_configuration": "Invalid shift configuration payload."})

        return super().to_internal_value(mutable)

    def validate(self, attrs):
        attrs = super().validate(attrs)
        code = attrs.get("code", getattr(self.instance, "code", ""))
        company = attrs.get("company", getattr(self.instance, "company", None))
        use_factory = attrs.get("use_factory_shifts", getattr(self.instance, "use_factory_shifts", True))
        shift_config = attrs.get("shift_configuration", getattr(self.instance, "shift_configuration", {}))

        if code and company:
            queryset = Machine.objects.filter(company=company, code__iexact=code)
            if self.instance:
                queryset = queryset.exclude(pk=self.instance.pk)
            if queryset.exists():
                raise serializers.ValidationError({"code": "Machine code already exists for this company."})

        if not use_factory and not any(
            bool(entry.get("enabled"))
            for entry in (shift_config or {}).values()
            if isinstance(entry, dict)
        ):
            raise serializers.ValidationError({"shift_configuration": "Enable at least one machine shift."})
        if use_factory:
            attrs["shift_configuration"] = {}
        return attrs

    def get_image_url(self, obj):
        if obj.image:
            try:
                return obj.image.url
            except Exception:
                return ""
        return ""

class ProductionStageSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProductionStage
        fields = '__all__'

class BOMComponentSerializer(serializers.ModelSerializer):
    class Meta:
        model = BOMComponent
        fields = '__all__'

class BillOfMaterialSerializer(serializers.ModelSerializer):
    components = BOMComponentSerializer(many=True, read_only=True)
    total_cost = serializers.ReadOnlyField()
    product_name = serializers.CharField(source='product.name', read_only=True)
    
    class Meta:
        model = BillOfMaterial
        fields = '__all__'

class ProductionLogSerializer(serializers.ModelSerializer):
    worker_name = serializers.CharField(source='worker.username', read_only=True)
    
    class Meta:
        model = ProductionLog
        fields = '__all__'

class QualityCheckSerializer(serializers.ModelSerializer):
    checked_by_name = serializers.CharField(source='checked_by.username', read_only=True)
    
    class Meta:
        model = QualityCheck
        fields = '__all__'

class MachineFaultSerializer(serializers.ModelSerializer):
    reported_by_name = serializers.CharField(source='reported_by.username', read_only=True)
    
    class Meta:
        model = MachineFault
        fields = '__all__'

class WorkOrderSerializer(serializers.ModelSerializer):
    bom_details = BillOfMaterialSerializer(source='bom', read_only=True)
    machine_details = MachineSerializer(source='machine', read_only=True)
    current_stage_details = ProductionStageSerializer(source='current_stage', read_only=True)
    assigned_to_name = serializers.CharField(source='assigned_to.username', read_only=True)
    assigned_worker_name = serializers.CharField(source='assigned_worker.username', read_only=True)
    production_logs = ProductionLogSerializer(many=True, read_only=True)
    quality_checks = QualityCheckSerializer(many=True, read_only=True)
    cycle_state = serializers.SerializerMethodField()
    
    class Meta:
        model = WorkOrder
        fields = '__all__'

    def get_cycle_state(self, obj):
        from .services import WorkOrderCycleService

        return WorkOrderCycleService.describe(obj)


# Worker Assignment Serializers
class WorkerCertificationSerializer(serializers.ModelSerializer):
    worker_name = serializers.CharField(source='worker.username', read_only=True)
    worker_first_name = serializers.CharField(source='worker.first_name', read_only=True)
    worker_last_name = serializers.CharField(source='worker.last_name', read_only=True)
    machine_name = serializers.CharField(source='machine.display_label', read_only=True)
    machine_code = serializers.CharField(source='machine.code', read_only=True)
    
    class Meta:
        model = WorkerCertification
        fields = '__all__'


class ShiftAssignmentSerializer(serializers.ModelSerializer):
    worker_name = serializers.CharField(source='worker.username', read_only=True)
    worker_first_name = serializers.CharField(source='worker.first_name', read_only=True)
    worker_last_name = serializers.CharField(source='worker.last_name', read_only=True)
    machine_name = serializers.CharField(source='machine.display_label', read_only=True)
    machine_code = serializers.CharField(source='machine.code', read_only=True)
    created_by_name = serializers.CharField(source='created_by.username', read_only=True)
    
    class Meta:
        model = ShiftAssignment
        fields = '__all__'


class WorkerSimpleSerializer(serializers.ModelSerializer):
    """Simple worker serializer for dropdowns and listings"""
    full_name = serializers.SerializerMethodField()
    
    class Meta:
        model = User
        fields = ['id', 'username', 'first_name', 'last_name', 'full_name']
    
    def get_full_name(self, obj):
        return f"{obj.first_name} {obj.last_name}".strip() or obj.username
