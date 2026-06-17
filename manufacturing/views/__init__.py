from .dashboard import PlannerDashboardView, SupervisorDashboardView, require_company as get_user_company, user_has_role
from .settings import SettingsDashboardView  # NEW
from .shift_planner import EmployeeShiftPlannerView
from .api import (
    WorkOrderMaterialReadinessAPI, WorkOrderUpdateView, ShopFloorUpdateView, WorkOrderDetailsAPI, 
    WOMaterialsAPI, WOCriteriaAPI, SimulationView, QualityAnalysisView,
    NotificationAPI, NotificationReadView, TimelineDataView, TimelineSnapAPIView, AssignWorkOrderView,
    PlannerUndoRestoreAPI,
    AssignWorkerAPIView, ReportFaultAPIView, WorkOrderLogAPI, MachineLogAPI
)
from .bom import (
    BOMBuilderView, BOMSaveAPI, BOMDetailsView, BOMJsonView, BOMLifecycleView
)
from .material_search import search_materials
from .setup import FactorySetupView, CreateMachineView, CreateStageView, BulkWorkOrderActionView
from .shop_floor import (
    RecordOutputView, ShopFloorKioskView, LogProductionAPI,
    ApproveLogView, ReportFaultAPI, WorkOrderStartDateUpdateView,
    ProductionLogEditView
)
from .maintenance import MaintenanceDashboardView
from .quality import QualityCheckView
from .store import StoreDashboardView, StoreReceiptConfirmAPI
from .onboarding import LandingPageView, RegisterCompanyView, OnboardingDataView, OnboardingUsersView
from .bulk import BulkImportView, DownloadTemplateView, HandleBulkImportView
from .work_order import WorkOrderCreateAPI, WorkOrderSplitAPI, WorkOrderCancelSplitAPI, WorkOrderCombineAPI, WorkOrderRecommendationAPI, WorkOrderCloseAPI, WorkOrderReleaseNextStageAPI, WorkOrderUnscheduleAPI, WorkOrderApplyLatestBOMAPI, WorkOrderBOMChangeDecisionAPI
from .schedule import WorkOrderDetailAPI, ScheduleWorkOrderAPI, AdvancedScheduleAPI
from .worker_assignment import AssignWorkerToWOView, GetAvailableWorkersView

# Reports (Import last to avoid cycles)
from .reports import (
    ExportAuditCSVView,
    ExportProductionCSVView,
    ExportReportPDFView,
    ExportWorkOrderSheetView,
    ReportsDashboardView,
)
