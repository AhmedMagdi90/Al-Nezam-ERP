from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .api_views import (
    WorkOrderViewSet, MachineViewSet, BillOfMaterialViewSet,
    ProductionLogViewSet, QualityCheckViewSet, ProductionStageViewSet,
    ProductViewSet, WorkerCertificationViewSet, ShiftAssignmentViewSet
)

router = DefaultRouter()
router.register(r'workorders', WorkOrderViewSet, basename='workorder')
router.register(r'machines', MachineViewSet, basename='machine')
router.register(r'boms', BillOfMaterialViewSet, basename='bom')
router.register(r'production-logs', ProductionLogViewSet, basename='productionlog')
router.register(r'quality-checks', QualityCheckViewSet, basename='qualitycheck')
router.register(r'stages', ProductionStageViewSet, basename='productionstage')
router.register(r'products', ProductViewSet, basename='product')
router.register(r'certifications', WorkerCertificationViewSet, basename='certification')
router.register(r'shift-assignments', ShiftAssignmentViewSet, basename='shiftassignment')

urlpatterns = [
    path('api/v1/', include(router.urls)),
]
