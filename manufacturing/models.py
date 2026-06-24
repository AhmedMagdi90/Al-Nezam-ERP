from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone
from .managers import TenantManager
from .shift_utils import SHIFT_MODE_CHOICES

OPERATION_FLOW_MODE_CHOICES = [
    ('series', 'Series'),
    ('parallel', 'Parallel'),
]

# 🏢 SaaS: Company Tenant
class Company(models.Model):
    """
    SaaS Tenant: Represents a manufacturing company using the ERP.
    """
    name = models.CharField(max_length=200)
    logo = models.ImageField(upload_to='company_logos/', blank=True, null=True)
    address = models.TextField(blank=True, null=True)
    support_email = models.EmailField(blank=True, null=True)
    
    # Subscription
    PLAN_CHOICES = [
        ('free_trial', 'Free Trial (30 Days)'),
        ('pro', 'Pro Plan'),
        ('enterprise', 'Enterprise'),
    ]
    subscription_plan = models.CharField(max_length=20, choices=PLAN_CHOICES, default='free_trial')
    is_active = models.BooleanField(default=True)
    
    trial_start_date = models.DateTimeField(default=timezone.now)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name



class SystemSettings(models.Model):
    """
    Settings for the Company Tenant (Theme, Language, Holidays).
    """
    company = models.OneToOneField(Company, on_delete=models.CASCADE, related_name='system_settings')
    theme = models.CharField(max_length=20, default='light', choices=[('light', 'Light'), ('dark', 'Dark')])
    language = models.CharField(max_length=10, default='en', choices=[('en', 'English'), ('es', 'Spanish'), ('fr', 'French'), ('ar', 'Arabic')])
    holidays = models.JSONField(default=list, blank=True, help_text="List of holiday dates [{'date': 'YYYY-MM-DD', 'name': 'New Year'}]")
    weekly_holidays = models.JSONField(default=list, blank=True, help_text="List of day indices (0-6) that are recurring holidays (e.g. [4, 5] for Friday, Saturday)")
    department_catalog = models.JSONField(
        default=dict,
        blank=True,
        help_text="Custom departments grouped by app scope e.g. {'planner': ['Machining', 'Stores']}",
    )
    shift_configuration = models.JSONField(
        default=dict, 
        blank=True, 
        help_text="Custom shift timings e.g. {'morning': {'start': '06:00', 'end': '14:00'}}"
    )
    shift_mode = models.CharField(
        max_length=1,
        choices=SHIFT_MODE_CHOICES,
        default='3',
        help_text="How many factory shifts are active per day.",
    )
    translation_overrides = models.JSONField(
        default=dict,
        blank=True,
        help_text="Per-language company translation overrides e.g. {'ar': {'Actual vs Planned': '...'}}",
    )
    
    # Feature Toggles
    auto_assign_workers = models.BooleanField(default=True)
    predictive_maintenance = models.BooleanField(default=False)
    auto_fault_lockdown = models.BooleanField(default=True)
    trouble_ticket_integration = models.BooleanField(default=True)
    qc_auto_release = models.BooleanField(default=False)
    qc_sla_hours = models.PositiveIntegerField(default=8)
    maintenance_auto_close = models.BooleanField(default=False)
    maintenance_sla_hours = models.PositiveIntegerField(default=8)
    default_operation_flow_mode = models.CharField(
        max_length=20,
        choices=OPERATION_FLOW_MODE_CHOICES,
        default='series',
        help_text="Default execution flow for work order stages.",
    )

    def __str__(self):
        return f"Settings for {self.company.name}"


# 🏭 Machine Model
class Machine(models.Model):
    """
    Physical Asset: A machine on the shop floor.
    """
    STATUS_CHOICES = [
        ('operational', 'Operational'),
        ('maintenance', 'Under Maintenance'),
        ('broken', 'Broken'),
        ('inactive', 'Inactive'),
    ]
    company = models.ForeignKey(Company, on_delete=models.CASCADE, null=True, blank=True)
    name = models.CharField(max_length=100)
    code = models.CharField(max_length=50)
    type = models.CharField(max_length=100, blank=True, null=True, help_text="Technical type/class (e.g. 'Lathe')") # Keeping for legacy
    category = models.CharField(max_length=100, blank=True, null=True, help_text="Broad Category matching BOM (e.g. 'CNC', 'Assembly')") # NEW
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='operational')
    is_active = models.BooleanField(default=True)
    image = models.ImageField(upload_to='machines/', blank=True, null=True)
    maintenance_note = models.TextField(blank=True, null=True, help_text="Current maintenance status or issue.")
    hourly_rate = models.DecimalField(max_digits=10, decimal_places=2, default=0.00, help_text="Cost per hour to run this machine")
    use_factory_shifts = models.BooleanField(
        default=True,
        help_text="If enabled, this machine uses the company shift configuration for scheduling availability.",
    )
    shift_configuration = models.JSONField(
        default=dict,
        blank=True,
        help_text="Optional machine-level shift configuration with enabled flags and start/end times.",
    )
    
    # 🔮 Predictive Maintenance Fields
    last_maintenance_date = models.DateTimeField(null=True, blank=True)
    total_runtime_hours = models.DecimalField(max_digits=10, decimal_places=2, default=0, help_text="Total hours run since last maintenance")

    objects = TenantManager()

    @property
    def display_label(self):
        code = str(self.code or "").strip()
        name = str(self.name or "").strip()
        if code and name and code != name:
            return f"{code} - {name}"
        return code or name or f"Machine #{self.pk}"

    def __str__(self):
        return f"{self.display_label} - {self.get_status_display()}"



# 🏢 Customer Model
class Customer(models.Model):
    name = models.CharField(max_length=200)
    email = models.EmailField(blank=True, null=True)
    phone = models.CharField(max_length=50, blank=True, null=True)
    company = models.ForeignKey(Company, on_delete=models.CASCADE)
    created_at = models.DateTimeField(auto_now_add=True)
    
    def __str__(self):
        return self.name


# 📦 Product Model
class Product(models.Model):
    """
    Inventory Item: Can be raw material, semi-finished, or finished product.
    """
    MATERIAL_TYPES = [
        ('raw', 'Raw Material'),
        ('semi', 'Semi-Finished'),
        ('finished', 'Finished Product'),
        ('packaging', 'Packaging'),
    ]
    company = models.ForeignKey(Company, on_delete=models.CASCADE, null=True, blank=True)
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True, null=True)
    unit = models.CharField(max_length=20, default="pcs")
    material_type = models.CharField(max_length=20, choices=MATERIAL_TYPES, default='raw')
    image = models.ImageField(upload_to='products/', blank=True, null=True)

    objects = TenantManager()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['company', 'name'], name='uniq_product_company_name')
        ]

    def __str__(self):
        return f"{self.name} ({self.get_material_type_display()})"



class BillOfMaterial(models.Model):
    STATUS_CHOICES = [
        ('draft', 'Draft (Editable)'),
        ('test', 'Test (Simulation Only)'),
        ('active', 'Active (Production Ready)'),
        ('archived', 'Archived (History)'),
    ]
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="boms", null=True, blank=True)
    version = models.CharField(max_length=20, default="v1.0")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='draft')
    
    # 1. Base Quantity & UOM (Mandatory)
    base_quantity = models.DecimalField(max_digits=10, decimal_places=2, default=1.0, help_text="Batch Size for this BOM")
    uom = models.CharField(max_length=10, default="pcs", help_text="Unit of Measure for Base Qty", choices=[
        ('kg', 'KG'), ('gm', 'Gram'), ('m', 'Meter'), ('cm', 'CM'), ('l', 'Liter'), ('pcs', 'Pcs')
    ])
    
    parent_bom = models.ForeignKey("self", null=True, blank=True, on_delete=models.CASCADE, related_name="sub_boms")
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    notes = models.TextField(blank=True, null=True)
    attachment = models.FileField(upload_to='bom_attachments/', blank=True, null=True)
    attachment_name = models.CharField(max_length=255, blank=True, default="")

    def save(self, *args, **kwargs):
        # 3. Immutability: Active BOMs cannot be modified!
        if self.pk:
            old_instance = BillOfMaterial.objects.get(pk=self.pk)
            if old_instance.status == 'active':
                # Allow status change to 'archived', but prevent other modifications
                # If modifying anything else while active -> ERROR
                if self.status == 'active' and (
                    self.base_quantity != old_instance.base_quantity or 
                    self.uom != old_instance.uom or
                    self.product != old_instance.product
                ):
                    raise ValueError("Cannot modify an Active BOM. Create a new version instead.")

        # Enforce Single Active Constraint
        if self.status == 'active' and self.product:
            BillOfMaterial.objects.filter(product=self.product, status='active').exclude(id=self.id).update(status='archived')
            
        super().save(*args, **kwargs)

    @property
    def total_cost(self):
        # We should use a service for this, but keeping a simple Property for generic lists
        # Real calculation happens in services.py
        total = sum([c.total_cost() for c in self.components.all()])
        return round(total, 2)

    def __str__(self):
        return f"BOM {self.version} for {self.product.name} [{self.status}]"


# ⚙️ BOM Components
class BOMComponent(models.Model):
    SCRAP_TYPES = [
        ('irretrievable', 'Irretrievable Loss (Cost Absorbed)'),
        ('sell_as_scrap', 'Sell as Scrap (Recover Value)'),
        ('return_to_stock', 'Return to Stock (Re-use)'),
    ]
    
    bom = models.ForeignKey(BillOfMaterial, on_delete=models.CASCADE, related_name="components")
    product = models.ForeignKey(
        Product,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="bom_components",
        help_text="Master material/product linked to this BOM component.",
    )
    material_name = models.CharField(max_length=100)
    quantity = models.DecimalField(max_digits=10, decimal_places=3, help_text="Gross Quantity Required per Base Qty")
    unit = models.CharField(max_length=10, default="pcs", choices=[
        ('kg', 'KG'), ('gm', 'Gram'), ('m', 'Meter'), ('cm', 'CM'), ('l', 'Liter'), ('pcs', 'Pcs')
    ])
    cost_per_unit = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    
    # Wastage & Scrap
    wastage_quantity = models.DecimalField(max_digits=10, decimal_places=3, default=0, help_text="Quantity that becomes waste")
    scrap_value_per_unit = models.DecimalField(max_digits=10, decimal_places=2, default=0, help_text="Value of waste per unit")
    scrap_type = models.CharField(max_length=20, choices=SCRAP_TYPES, default='sell_as_scrap')

    # Deprecated fields
    wastage_percent = models.FloatField(default=0) 

    source_type = models.CharField(
        max_length=50,
        choices=[
            ('raw', 'Raw Material'),
            ('semi_finished', 'Semi-Finished'),
            ('finished', 'Finished Product')
        ],
        default='raw'
    )
    
    def save(self, *args, **kwargs):
        # Prevent editing components of an Active BOM
        if self.bom.status == 'active':
             raise ValueError("Cannot modify components of an Active BOM. Create a new version.")
        super().save(*args, **kwargs)
        
    def total_cost(self):
        """
        Compute Net Cost based on Scrap Strategy.
        1. Irretrievable: Cost = Gross Qty * Cost/Unit (Wastage cost is absorbed)
        2. Sell/Return: Cost = (Gross * Cost) - (Wastage * ScrapValue)
        """
        gross_cost = self.quantity * self.cost_per_unit
        
        if self.scrap_type == 'irretrievable':
            return round(gross_cost, 2)
        else:
            scrap_recovery = self.wastage_quantity * self.scrap_value_per_unit
            return max(0, round(gross_cost - scrap_recovery, 2))

    # 🧩 NEW: optional link to another BOM (sub-assembly)
    sub_bom = models.ForeignKey(
        "BillOfMaterial",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="used_in_components",
        help_text="If this component is a sub-assembly, link its BOM here."
    )


    def __str__(self):
        return f"{self.material_name} ({self.quantity} {self.unit})"


# ⚙️ BOM Operations (Routing)
class BOMOperation(models.Model):
    bom = models.ForeignKey(BillOfMaterial, on_delete=models.CASCADE, related_name="operations")
    machine = models.ForeignKey("Machine", on_delete=models.SET_NULL, null=True, blank=True)
    stage = models.ForeignKey("ProductionStage", on_delete=models.SET_NULL, null=True, blank=True)
    order = models.PositiveIntegerField(default=1)
    setup_time = models.DecimalField(max_digits=10, decimal_places=4, default=0, help_text="Setup time in minutes (once per batch)")
    run_time = models.DecimalField(max_digits=10, decimal_places=4, default=0, help_text="Run time in minutes per single unit")
    # duration_minutes is deprecated but kept for backward compatibility (can be calculated as setup + run * batch)
    duration_minutes = models.PositiveIntegerField(default=60, help_text="Total estimated duration (deprecated)") 
    
    # 🆕 Generic Assignment Support
    machine_type = models.CharField(max_length=100, blank=True, null=True, help_text="Generic machine requirement (e.g. 'CNC')")
    # 🆕 QC / Instructions Trigger
    description = models.TextField(blank=True, null=True, help_text="Instructions, QC Triggers, or comments for this step.") 
    
    class Meta:
        ordering = ['order']

    def __str__(self):
        return f"Op {self.order} for {self.bom.version}"


class BOMOperationMaterial(models.Model):
    """
    Links a BOM operation to the BOM components consumed at that operation.
    This allows stage/machine-scoped material visibility on the shop floor.
    """
    operation = models.ForeignKey(BOMOperation, on_delete=models.CASCADE, related_name="material_links")
    component = models.ForeignKey(BOMComponent, on_delete=models.CASCADE, related_name="operation_links")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["operation", "component"],
                name="uniq_bom_operation_component",
            )
        ]

    def __str__(self):
        return f"Op#{self.operation_id} uses {self.component.material_name}"





# ✅ BOM Acceptance Criteria (Tab 3)
class BOMAcceptanceCriteria(models.Model):
    bom = models.ForeignKey(BillOfMaterial, on_delete=models.CASCADE, related_name="acceptance_criteria")
    parameter = models.CharField(max_length=200, help_text="e.g. Dimensions, Surface Finish")
    method = models.CharField(max_length=100, help_text="e.g. Visual, Caliper, Gauge")
    criteria_min = models.CharField(max_length=50, blank=True, null=True, help_text="Minimum acceptable value")
    criteria_max = models.CharField(max_length=50, blank=True, null=True, help_text="Maximum acceptable value")
    pass_fail = models.BooleanField(default=False, help_text="If true, this is a simple Pass/Fail check")
    
    # 🆕 New detailed fields for Wizard
    target_value = models.CharField(max_length=100, blank=True, null=True, help_text="Target value (e.g. 10mm)")
    tolerance = models.CharField(max_length=100, blank=True, null=True, help_text="Tolerance (e.g. +/- 0.05mm)")
    is_critical = models.BooleanField(default=False, help_text="If true, failure rejects the whole batch")
    
    def __str__(self):
        return f"{self.parameter} ({self.bom.version})"


# 🧩 Production Stage
class ProductionStage(models.Model):
    name = models.CharField(max_length=100)
    machine = models.ForeignKey(Machine, on_delete=models.SET_NULL, null=True, blank=True, related_name="stages")
    is_quality_check = models.BooleanField(default=False, help_text="Is this a QA stage?")
    category = models.CharField(max_length=100, blank=True, null=True, help_text="Stage category/type (e.g. Sewing)")
    order = models.PositiveIntegerField(default=1)
    color = models.CharField(max_length=20, default="#90CAF9", help_text="HEX color")

    def __str__(self):
        return f"{self.order}. {self.name}"


# 🧾 Work Order
class WorkOrder(models.Model):
    STATUS_CHOICES = [
        ('pending', 'Pending (Scheduled)'),
        ('in_progress', 'In Progress'),
        ('completed', 'Completed'),
        ('hold', 'On Hold'),
        ('canceled', 'Canceled'),
        ('archived', 'Archived'),
    ]

    ORDER_TYPE_CHOICES = [
        ('production', 'Production'),
        ('repair', 'Repair'),
    ]

    MATERIAL_READINESS_CHOICES = [
        ('not_checked', 'Not Checked'),
        ('ready', 'Ready'),
        ('partial', 'Partially Ready'),
        ('shortage', 'Shortage'),
    ]

    STORE_RECEIPT_STATUS_CHOICES = [
        ('not_requested', 'Not Requested'),
        ('pending', 'Pending Store Receipt'),
        ('received', 'Received by Store'),
    ]

    BOM_CHANGE_STATUS_CHOICES = [
        ('none', 'No BOM Change'),
        ('action_required', 'Action Required'),
        ('latest_applied', 'Latest BOM Applied'),
        ('archived_replaced', 'Archived and Replaced'),
        ('scrap_applied', 'Scrap Done and Latest BOM Applied'),
        ('ignored', 'Continue Old BOM'),
    ]

    company = models.ForeignKey(Company, on_delete=models.CASCADE, null=True, blank=True)
    product_name = models.CharField(max_length=200)
    bom = models.ForeignKey(BillOfMaterial, on_delete=models.SET_NULL, null=True, blank=True)
    bom_version = models.CharField(
        max_length=20,
        blank=True,
        default="",
        help_text="BOM version captured when this work order was created.",
    )
    bom_snapshot = models.JSONField(
        blank=True,
        default=dict,
        help_text="Immutable BOM structure captured when this work order was created.",
    )
    bom_change_status = models.CharField(
        max_length=32,
        choices=BOM_CHANGE_STATUS_CHOICES,
        default='none',
        help_text="Planner decision state when a newer active BOM affects this work order.",
    )
    bom_change_latest_bom = models.ForeignKey(
        BillOfMaterial,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="impacted_work_orders",
        help_text="Latest active BOM version that triggered the action-required warning.",
    )
    bom_change_detected_at = models.DateTimeField(null=True, blank=True)
    bom_change_decision_at = models.DateTimeField(null=True, blank=True)
    bom_change_decision_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='bom_change_decisions',
    )
    bom_change_decision_note = models.TextField(blank=True, default='')
    bom_change_replacement_wo = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="bom_change_replaced_sources",
        help_text="Replacement work order created from the latest BOM.",
    )
    bom_change_scrapped_qty = models.PositiveIntegerField(
        default=0,
        help_text="Finished/reported quantity treated as scrapped when applying the new BOM.",
    )
    quantity = models.PositiveIntegerField()
    base_quantity = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Original planned quantity before scrap compensation increases."
    )
    scrap_compensation_qty = models.PositiveIntegerField(
        default=0,
        help_text="Total quantity added by planner to compensate scrap losses."
    )
    is_scrap_compensation_task = models.BooleanField(
        default=False,
        help_text="True if this task was created as a scrap compensation replenishment."
    )
    scrap_source_quality_check = models.ForeignKey(
        "QualityCheck",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="scrap_compensation_tasks",
        help_text="Quality check that triggered this scrap compensation task."
    )
    machine = models.ForeignKey(Machine, on_delete=models.SET_NULL, null=True, blank=True, help_text="Optional: Assign to specific machine, or use BOM operations")
    current_stage = models.ForeignKey(ProductionStage, on_delete=models.SET_NULL, null=True, blank=True, related_name="active_work_orders")
    stage = models.ForeignKey(ProductionStage, on_delete=models.SET_NULL, null=True, blank=True, help_text="Initial/planned stage")
    # current_stage tracks where the work order is NOW during multi-stage production
    assigned_to = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    operation_flow_mode = models.CharField(
        max_length=20,
        choices=OPERATION_FLOW_MODE_CHOICES,
        default='series',
        help_text="Whether BOM stages run in series or in parallel for this work order.",
    )
    
    scheduled_start_date = models.DateTimeField(null=True, blank=True, help_text="Planned start time")
    start_date = models.DateTimeField(null=True, blank=True, help_text="Actual start time (set during scheduling)")
    end_date = models.DateTimeField(null=True, blank=True)
    due_date = models.DateTimeField(null=True, blank=True, help_text="Customer deadline for order completion")

    # Step start timestamps (role-specific)
    planner_start_at = models.DateTimeField(null=True, blank=True, help_text="When planner scheduled the WO")
    supervisor_start_at = models.DateTimeField(null=True, blank=True, help_text="When supervisor assigned a worker")
    worker_start_at = models.DateTimeField(null=True, blank=True, help_text="When worker started production")
    quality_start_at = models.DateTimeField(null=True, blank=True, help_text="When quality check started")

    progress = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    released_qty = models.PositiveIntegerField(
        default=0,
        help_text="Quantity released to the next stage from this work order."
    )
    qc_requirement = models.BooleanField(
        default=False,
        help_text="If true, this stage requires QC approval before releasing to the next stage."
    )
    planner_action_required = models.BooleanField(
        default=False,
        help_text="Set true when final stage is approved and planner must close the WO."
    )
    closed_by_planner = models.BooleanField(
        default=False,
        help_text="Planner has already closed this work order."
    )
    next_stage_ready = models.BooleanField(
        default=False,
        help_text="True when current stage is approved/QC complete and planner must start the next stage."
    )
    material_readiness_status = models.CharField(
        max_length=20,
        choices=MATERIAL_READINESS_CHOICES,
        default='not_checked',
        help_text="Planner-controlled manufacturing material readiness gate.",
    )
    material_shortage_note = models.TextField(
        blank=True,
        default='',
        help_text="Store note explaining material availability before production release.",
    )
    material_available_qty = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Available production quantity confirmed by store for partial BOM readiness.",
    )
    material_available_percent = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Material availability percentage confirmed by store.",
    )
    material_expected_delivery_date = models.DateField(
        null=True,
        blank=True,
        help_text="Expected material delivery date for partial or unavailable material.",
    )
    material_readiness_updated_at = models.DateTimeField(null=True, blank=True)
    material_readiness_updated_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='material_readiness_updates',
    )
    store_receipt_status = models.CharField(
        max_length=20,
        choices=STORE_RECEIPT_STATUS_CHOICES,
        default='not_requested',
        help_text="Finished-goods receipt gate before planner can close the WO.",
    )
    store_receipt_requested_at = models.DateTimeField(null=True, blank=True)
    store_receipt_confirmed_at = models.DateTimeField(null=True, blank=True)
    store_receipt_confirmed_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='store_receipt_confirmations',
    )
    store_received_qty = models.PositiveIntegerField(null=True, blank=True)
    store_scrap_qty = models.PositiveIntegerField(null=True, blank=True)
    store_receipt_note = models.TextField(blank=True, default='')
    
    # 🧵 Hierarchy for Multi-Stage WOs
    parent = models.ForeignKey("self", on_delete=models.CASCADE, null=True, blank=True, related_name="sub_tasks")
    is_split = models.BooleanField(default=False, help_text="True if this is a parent WO for a split job")
    source_task = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="released_children",
        help_text="Original stage task that released this task (split/QC tracking)."
    )
    subassembly_parent = models.ForeignKey(
        "self",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="subassembly_work_orders",
        help_text="Parent work order that requires this manufactured sub-assembly.",
    )
    source_bom_component = models.ForeignKey(
        BOMComponent,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="generated_work_orders",
        help_text="BOM component shortage that generated this sub-assembly work order.",
    )
    
    # 👤 Customer Link
    customer = models.ForeignKey(Customer, on_delete=models.SET_NULL, null=True, blank=True)

    PRIORITY_CHOICES = [
        ('Low', 'Low'),
        ('Normal', 'Normal'),
        ('High', 'High'),
        ('Urgent', 'Urgent'),
    ]
    priority = models.CharField(max_length=20, choices=PRIORITY_CHOICES, default='Normal')
    instructions = models.TextField(null=True, blank=True, help_text="Special instructions from the planner")
    
    # 👷 Worker Assignment Fields (Hybrid System)
    assigned_worker = models.ForeignKey(
        User, 
        on_delete=models.SET_NULL, 
        null=True, 
        blank=True,
        related_name='assigned_work_orders',
        limit_choices_to={'profile__role__name': 'worker'},
        help_text="Worker assigned to this WO (auto from shift or manual override)"
    )
    
    ASSIGNMENT_TYPE_CHOICES = [
        ('auto', 'Automatic (from shift)'),
        ('manual', 'Manual Override'),
    ]
    assignment_type = models.CharField(
        max_length=20, 
        choices=ASSIGNMENT_TYPE_CHOICES, 
        default='auto',
        help_text="How worker was assigned"
    )

    order_type = models.CharField(
        max_length=20,
        choices=ORDER_TYPE_CHOICES,
        default='production',
        help_text="Type of order: Production or Repair"
    )

    objects = TenantManager()

    class Meta:
        indexes = [
            models.Index(fields=["company", "start_date"], name="mf_wo_company_start_idx"),
            models.Index(fields=["company", "bom"], name="mf_wo_company_bom_idx"),
            models.Index(fields=["company", "material_readiness_status"], name="mf_wo_mat_ready_idx"),
            models.Index(fields=["company", "bom_change_status"], name="mf_wo_bom_change_idx"),
            models.Index(fields=["company", "store_receipt_status"], name="mf_wo_store_receipt_idx"),
        ]

    def delete(self, *args, **kwargs):
        """
        Custom delete to prevent Ghost Reservations.
        Explicitly clears associated data before deletion.
        """
        # 1. Clear sub-tasks explicitly (redundant if CASCADE is on, but safe)
        if self.sub_tasks.exists():
            self.sub_tasks.all().delete()
            
        # 2. Clear stages (explicitly)
        if self.stages.exists():
            self.stages.all().delete()
            
        # 3. Proceed with deletion
        super().delete(*args, **kwargs)

    @staticmethod
    def build_bom_snapshot(bom):
        if not bom:
            return {}

        return {
            "bom_id": bom.id,
            "version": bom.version,
            "status": bom.status,
            "product_id": bom.product_id,
            "product_name": bom.product.name if bom.product else "",
            "base_quantity": str(bom.base_quantity),
            "uom": bom.uom,
            "attachment": {
                "name": bom.attachment_name or (bom.attachment.name.split("/")[-1] if bom.attachment else ""),
                "path": bom.attachment.name if bom.attachment else "",
            } if bom.attachment else None,
            "components": [
                {
                    "id": component.id,
                    "product_id": component.product_id,
                    "material_name": component.material_name,
                    "quantity": str(component.quantity),
                    "unit": component.unit,
                    "cost_per_unit": str(component.cost_per_unit),
                    "wastage_quantity": str(component.wastage_quantity),
                    "wastage_percent": component.wastage_percent,
                    "scrap_value_per_unit": str(component.scrap_value_per_unit),
                    "scrap_type": component.scrap_type,
                    "source_type": component.source_type,
                    "sub_bom_id": component.sub_bom_id,
                }
                for component in bom.components.all().order_by("id")
            ],
            "operations": [
                {
                    "id": operation.id,
                    "order": operation.order,
                    "stage_id": operation.stage_id,
                    "stage_name": operation.stage.name if operation.stage else "",
                    "machine_id": operation.machine_id,
                    "machine_name": operation.machine.name if operation.machine else "",
                    "machine_type": operation.machine_type,
                    "setup_time": str(operation.setup_time),
                    "run_time": str(operation.run_time),
                    "duration_minutes": operation.duration_minutes,
                    "description": operation.description or "",
                    "material_component_ids": list(
                        operation.material_links.values_list("component_id", flat=True)
                    ),
                }
                for operation in bom.operations.all().order_by("order", "id")
            ],
            "acceptance_criteria": [
                {
                    "id": criteria.id,
                    "parameter": criteria.parameter,
                    "method": criteria.method,
                    "criteria_min": criteria.criteria_min,
                    "criteria_max": criteria.criteria_max,
                    "pass_fail": criteria.pass_fail,
                    "target_value": criteria.target_value,
                    "tolerance": criteria.tolerance,
                    "is_critical": criteria.is_critical,
                }
                for criteria in bom.acceptance_criteria.all().order_by("id")
            ],
        }

    def capture_bom_snapshot(self):
        if not self.bom_id or self.bom_snapshot:
            return

        db_alias = getattr(getattr(self, "_state", None), "db", None) or "default"
        bom = (
            BillOfMaterial.objects.using(db_alias)
            .select_related("product")
            .prefetch_related(
                "components__product",
                "operations__stage",
                "operations__machine",
                "operations__material_links",
                "acceptance_criteria",
            )
            .filter(id=self.bom_id)
            .first()
        )
        if not bom:
            return
        self.bom_version = bom.version or ""
        self.bom_snapshot = self.build_bom_snapshot(bom)

    def save(self, *args, **kwargs):
        self.capture_bom_snapshot()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"WO#{self.id} - {self.product_name} ({self.get_status_display()})"

    @property
    def display_work_order_id(self):
        return self.parent_id or self.id

    @property
    def display_work_order_code(self):
        return f"WO-{self.display_work_order_id}"

    @property
    def suggested_machine_type(self):
        """Returns the machine type for the first stage of the BOM."""
        if self.bom:
            first_op = self.bom.operations.all().order_by('order').first()
            if first_op:
                if first_op.machine_type:
                    return first_op.machine_type
                if first_op.machine:
                    return first_op.machine.type
                elif first_op.stage and first_op.stage.machine:
                    return first_op.stage.machine.type
        return ""


# 🎯 Work Order Stage (Multi-Stage Scheduling)
class WorkOrderStage(models.Model):
    """
    Represents a single stage/operation in a multi-stage work order.
    Allows parallel processing and complex routing.
    """
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('scheduled', 'Scheduled'),
        ('in_progress', 'In Progress'),
        ('completed', 'Completed'),
        ('blocked', 'Blocked'),
    ]
    
    work_order = models.ForeignKey(WorkOrder, on_delete=models.CASCADE, related_name='stages')
    stage = models.ForeignKey(ProductionStage, on_delete=models.PROTECT, help_text="BOM operation/stage")
    machine = models.ForeignKey(Machine, on_delete=models.SET_NULL, null=True, blank=True)
    
    sequence_order = models.IntegerField(default=0, help_text="Order in the production flow")
    depends_on = models.ManyToManyField('self', symmetrical=False, blank=True, help_text="Stages that must complete before this one")
    
    start_date = models.DateTimeField(null=True, blank=True)
    end_date = models.DateTimeField(null=True, blank=True)
    
    quantity = models.IntegerField(help_text="Quantity to produce in this stage (for split production)")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    
    notes = models.TextField(blank=True, null=True)
    
    class Meta:
        ordering = ['sequence_order']
    
    def __str__(self):
        return f"WO#{self.work_order.id} - Stage {self.sequence_order}: {self.stage.name}"


# 🔧 Machine Fault / Maintenance Log
class MachineFault(models.Model):
    STATUS_CHOICES = [
        ('open', 'Open'),
        ('assigned', 'Assigned'),
        ('in_progress', 'In Progress'),
        ('completed', 'Completed'),
        ('approved', 'Approved'),
        ('resolved', 'Resolved'),
    ]
    PRIORITY_CHOICES = [
        ('low', 'Low'),
        ('normal', 'Normal'),
        ('high', 'High'),
        ('urgent', 'Urgent'),
    ]
    machine = models.ForeignKey(Machine, on_delete=models.CASCADE, related_name="faults")
    reported_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    description = models.TextField()
    priority = models.CharField(max_length=20, choices=PRIORITY_CHOICES, default='normal')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='open')
    assigned_supervisor = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="maintenance_supervisor_assignments")
    assigned_worker = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="maintenance_worker_assignments")
    completed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="maintenance_completed")
    completed_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="maintenance_approved")
    approved_at = models.DateTimeField(null=True, blank=True)
    override_notes = models.TextField(blank=True, null=True)
    resolution_notes = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"Fault on {self.machine.code}: {self.status}"


# ✅ Quality Check (End of Line)
class QualityCheck(models.Model):
    STATUS_CHOICES = [
        ('new', 'New'),
        ('processed', 'Processed'),
    ]

    work_order = models.ForeignKey(WorkOrder, on_delete=models.CASCADE, related_name="quality_checks")
    checked_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    assigned_supervisor = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="qc_supervisor_assignments")
    assigned_worker = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="qc_worker_assignments")
    completed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="qc_completed")
    completed_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="qc_approved")
    approved_at = models.DateTimeField(null=True, blank=True)
    override_notes = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    
    good_quantity = models.PositiveIntegerField(default=0)
    repair_quantity = models.PositiveIntegerField(default=0)
    faulty_quantity = models.PositiveIntegerField(default=0)
    notes = models.TextField(blank=True, null=True)

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='new')
    generated_wo = models.ForeignKey(WorkOrder, on_delete=models.SET_NULL, null=True, blank=True, related_name="source_quality_check")
    scrap_compensated_qty = models.PositiveIntegerField(
        default=0,
        help_text="How many scrapped units were already added back to WO target by planner."
    )
    scrap_compensated_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="qc_scrap_compensations",
        help_text="Last planner/admin who applied scrap compensation."
    )
    scrap_compensated_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When scrap compensation was last applied."
    )

    def __str__(self):
        return f"QC for WO#{self.work_order.id}"


# 📝 Production Log (Worker Output)
class ProductionLog(models.Model):
    STATUS_CHOICES = [
        ('pending', 'Pending Approval'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected'),
    ]
    SHIFT_CHOICES = [
        ('morning', 'Morning'),
        ('evening', 'Evening'),
        ('night', 'Night'),
    ]

    work_order = models.ForeignKey(WorkOrder, on_delete=models.CASCADE, related_name="production_logs")
    worker = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    quantity = models.PositiveIntegerField()
    shift = models.CharField(max_length=20, choices=SHIFT_CHOICES, default='morning')
    date = models.DateField(auto_now_add=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    note = models.TextField(blank=True, null=True)
    completion_requested = models.BooleanField(default=False, help_text="Worker requested completion")

    created_at = models.DateTimeField(auto_now_add=True)
    reviewed_by = models.ForeignKey(User, related_name="reviewed_logs", on_delete=models.SET_NULL, null=True, blank=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["work_order", "date"], name="mf_log_wo_date_idx"),
            models.Index(fields=["work_order", "created_at"], name="mf_log_wo_created_idx"),
        ]

    def __str__(self):
        return f"Log #{self.id} - {self.worker.username} ({self.quantity} qty)"


# 📝 Work Order Change Log (Supervisor/Planner edits)
class WorkOrderChangeLog(models.Model):
    work_order = models.ForeignKey(WorkOrder, on_delete=models.CASCADE, related_name="change_logs")
    changed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    action = models.CharField(max_length=100, default="Work order updated")
    field_name = models.CharField(max_length=100, null=True, blank=True)
    old_value = models.CharField(max_length=255, null=True, blank=True)
    new_value = models.CharField(max_length=255, null=True, blank=True)
    note = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"WO#{self.work_order_id} - {self.action}"


# 📉 Material Usage Log (Actual vs BOM)
class MaterialUsage(models.Model):
    production_log = models.ForeignKey(ProductionLog, on_delete=models.CASCADE, related_name="material_usage")
    product = models.ForeignKey(Product, on_delete=models.SET_NULL, null=True, blank=True)
    material_name = models.CharField(max_length=100)
    planned_quantity = models.DecimalField(max_digits=10, decimal_places=3, null=True, blank=True)
    quantity_used = models.DecimalField(max_digits=10, decimal_places=3)
    unit = models.CharField(max_length=10, default="pcs")

    class Meta:
        indexes = [
            models.Index(fields=["production_log", "product"], name="mf_usage_log_prod_idx"),
        ]
    
    def __str__(self):
        return f"{self.material_name}: {self.quantity_used} {self.unit}"


# 👷 Worker Certification Model
class WorkerCertification(models.Model):
    """
    Tracks which workers are certified to operate which machines.
    Enables skill-based work assignment and compliance tracking.
    """
    SKILL_LEVELS = [
        ('basic', 'Basic'),
        ('intermediate', 'Intermediate'),
        ('expert', 'Expert'),
    ]
    
    worker = models.ForeignKey(
        User, 
        on_delete=models.CASCADE, 
        related_name='certifications',
        limit_choices_to={'profile__role__name': 'worker'}
    )
    machine = models.ForeignKey(Machine, on_delete=models.CASCADE, related_name='certified_workers')
    skill_level = models.CharField(max_length=20, choices=SKILL_LEVELS, default='basic')
    certified_date = models.DateField(auto_now_add=True)
    notes = models.TextField(blank=True, null=True, help_text="Certification notes, training records")
    
    class Meta:
        unique_together = [['worker', 'machine']]
        verbose_name = "Worker Certification"
        verbose_name_plural = "Worker Certifications"
    
    def __str__(self):
        return f"{self.worker.username} → {self.machine.name} ({self.get_skill_level_display()})"


# 📅 Shift Assignment Model
class ShiftAssignment(models.Model):
    """
    Assigns workers to machines for specific shifts.
    Supports hybrid worker assignment system with shift-based defaults.
    """
    SHIFT_CHOICES = [
        ('day', 'Day Shift (6AM-2PM)'),
        ('middle', 'Middle Shift (2PM-10PM)'),
        ('night', 'Night Shift (10PM-6AM)'),
    ]
    
    worker = models.ForeignKey(
        User, 
        on_delete=models.CASCADE, 
        related_name='shift_assignments',
        limit_choices_to={'profile__role__name': 'worker'}
    )
    machine = models.ForeignKey(Machine, on_delete=models.CASCADE, related_name='shift_assignments')
    shift_type = models.CharField(max_length=20, choices=SHIFT_CHOICES)
    date = models.DateField(help_text="Which day this assignment applies to")
    
    created_by = models.ForeignKey(
        User, 
        on_delete=models.SET_NULL, 
        null=True,
        related_name='shift_assignments_created',
        limit_choices_to={'profile__role__name__in': ['supervisor', 'planner']}
    )
    created_at = models.DateTimeField(auto_now_add=True)
    notes = models.TextField(blank=True, null=True)
    
    class Meta:
        unique_together = [['worker', 'shift_type', 'date']]
        ordering = ['date', 'shift_type', 'machine']
        verbose_name = "Shift Assignment"
        verbose_name_plural = "Shift Assignments"
    
    def __str__(self):
        return f"{self.worker.username} → {self.machine.name} ({self.get_shift_type_display()}) on {self.date}"
    
    def save(self, *args, **kwargs):
        """Allow any worker to be assigned to any machine."""
        super().save(*args, **kwargs)



# 📡 Signals
class EmployeeShiftChangeLog(models.Model):
    """
    Audit log for employee shift planning changes.
    Keeps planned schedule edits traceable without changing production logs.
    """
    ACTION_CHOICES = [
        ("assign", "Assign shift"),
        ("swap", "Swap shift"),
        ("clear", "Clear planned shift"),
    ]

    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="employee_shift_change_logs")
    employee = models.ForeignKey(User, on_delete=models.CASCADE, related_name="employee_shift_change_logs")
    changed_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="employee_shift_changes_made",
    )
    action = models.CharField(max_length=20, choices=ACTION_CHOICES)
    previous_shift = models.CharField(max_length=30, blank=True, null=True)
    new_shift = models.CharField(max_length=30, blank=True, null=True)
    previous_planned_shift = models.CharField(max_length=30, blank=True, null=True)
    previous_planned_shift_start_date = models.DateField(blank=True, null=True)
    effective_start_date = models.DateField(blank=True, null=True)
    note = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["company", "employee", "created_at"], name="mf_emp_shift_log_idx"),
        ]

    def __str__(self):
        return f"{self.employee_id} {self.action} {self.new_shift or '-'} from {self.effective_start_date or '-'}"


from django.db.models.signals import post_save
from django.dispatch import receiver
from django.db.models import Max, Sum

@receiver(post_save, sender=ProductionLog)
def update_work_order_progress(sender, instance, **kwargs):
    """Update WO progress for approved logs without forcing staged/QC completion."""
    if instance.status != 'approved':
        return

    wo = instance.work_order
    total_qty = wo.production_logs.filter(status='approved').aggregate(Sum('quantity'))['quantity__sum'] or 0
    update_fields = []

    if wo.quantity > 0:
        wo.progress = min(100, (total_qty / wo.quantity) * 100)
        update_fields.append('progress')

    completion_approved = wo.production_logs.filter(
        status='approved',
        completion_requested=True
    ).exists()
    if completion_approved and total_qty >= wo.quantity:
        # Completion for staged/QC flows must be handled by WorkOrderService
        # to avoid skipping QC gates or next-stage manual start.
        has_staged_routing = bool(
            wo.parent_id or
            (wo.bom_id and wo.bom.operations.exclude(stage_id__isnull=True).exists())
        )
        stage_requires_qc = bool(
            getattr(wo, 'qc_requirement', False) or
            (wo.stage_id and getattr(wo.stage, 'is_quality_check', False)) or
            (wo.current_stage_id and getattr(wo.current_stage, 'is_quality_check', False))
        )
        has_pending_qc = QualityCheck.objects.filter(work_order=wo, status='new').exists()

        if not has_staged_routing and not stage_requires_qc and not has_pending_qc:
            if wo.status != 'completed':
                try:
                    from .services import WorkOrderLifecycle, WorkOrderLifecycleError

                    WorkOrderLifecycle.apply_transition(
                        wo,
                        'completed',
                        actor=None,
                        allow_system=True,
                        save=False
                    )
                    update_fields.append('status')
                except WorkOrderLifecycleError:
                    pass
            actual_completion_at = wo.production_logs.filter(
                status='approved',
                completion_requested=True,
            ).aggregate(actual_at=Max('reviewed_at'))['actual_at'] or timezone.now()
            if not wo.end_date or wo.end_date > actual_completion_at:
                wo.end_date = actual_completion_at
                update_fields.append('end_date')

    if update_fields:
        wo.save(update_fields=update_fields)


# 🔔 Notifications System
class Notification(models.Model):
    recipient = models.ForeignKey(User, on_delete=models.CASCADE, related_name="notifications")
    title = models.CharField(max_length=100)
    message = models.TextField()
    link = models.CharField(max_length=200, blank=True, null=True)
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.title} -> {self.recipient.username}"


from .security import AuditLog, SecurityEvent  # Register security/audit models with the app
