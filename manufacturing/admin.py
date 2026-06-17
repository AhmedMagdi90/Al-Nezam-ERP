from django.contrib import admin
from .models import Machine, ProductionStage, WorkOrder, Company, WorkerCertification

@admin.register(Machine)
class MachineAdmin(admin.ModelAdmin):
    list_display = ('name', 'type')
    search_fields = ('name', 'type')


@admin.register(ProductionStage)
class ProductionStageAdmin(admin.ModelAdmin):
    list_display = ('name', 'order', 'color', 'machine')
    list_filter = ('machine',)
    search_fields = ('name',)


@admin.register(WorkOrder)
class WorkOrderAdmin(admin.ModelAdmin):
    list_display = ('product_name', 'machine', 'stage', 'quantity', 'start_date', 'end_date')
    list_filter = ('machine', 'stage')
    search_fields = ('product_name',)


@admin.register(Company)
class CompanyAdmin(admin.ModelAdmin):
    list_display = ('name', 'address', 'support_email')
    search_fields = ('name',)


@admin.register(WorkerCertification)
class WorkerCertificationAdmin(admin.ModelAdmin):
    list_display = ('worker', 'machine', 'certified_date')
    list_filter = ('worker', 'machine')
