from django.urls import path, include
from . import views
from django.conf import settings

urlpatterns = [
    # 🏠 Landing & Onboarding
    path('', views.LandingPageView.as_view(), name='landing_page'),
    path('register/', views.RegisterCompanyView.as_view(), name='register_company'),
    path('onboarding/data/', views.OnboardingDataView.as_view(), name='onboarding_data'),
    path('onboarding/team/', views.OnboardingUsersView.as_view(), name='onboarding_users'),

    # 🖥️ Dashboards
    path('dashboard/', views.PlannerDashboardView.as_view(), name='dashboard'), # Alias
    path('planner/', views.PlannerDashboardView.as_view(), name='planner_dashboard'),
    path('supervisor/', views.SupervisorDashboardView.as_view(), name='supervisor_dashboard'),
    path('manage-production/', views.SupervisorDashboardView.as_view(), name='manage_production'), # Legacy Alias
    
    path('maintenance/', views.MaintenanceDashboardView.as_view(), name='maintenance_dashboard'),
    path('quality-check/', views.QualityCheckView.as_view(), name='quality_check'),
    path('store/', views.StoreDashboardView.as_view(), name='store_dashboard'),
    path('settings/', views.SettingsDashboardView.as_view(), name='settings_dashboard'), # NEW
    path('shift-planner/', views.EmployeeShiftPlannerView.as_view(), name='employee_shift_planner'),
    
    # 👷 Shop Floor & Worker
    path('record-output/', views.RecordOutputView.as_view(), name='record_output'),
    path('shop-floor/', views.ShopFloorKioskView.as_view(), name='shop_floor'),
    
    # 🏭 Factory Setup & Creation (AJAX/Forms)
    path('factory-setup/', views.FactorySetupView.as_view(), name='factory_setup'),
    path('create-workorder/', views.PlannerDashboardView.as_view(), name='create_workorder'), # Handled by Planner POST
    path('create-machine/', views.CreateMachineView.as_view(), name='create_machine'),
    path('create-stage/', views.CreateStageView.as_view(), name='create_stage'),
    
    # 🏗️ BOM Logic
    path('bom-builder/', views.BOMBuilderView.as_view(), name='bom_builder'),
    path('bom-builder/<int:bom_id>/', views.BOMBuilderView.as_view(), name='bom_builder_edit'),
    path('api/bom/save/', views.BOMSaveAPI.as_view(), name='api_save_bom'), 
    path('create-bom/', views.BOMSaveAPI.as_view(), name='create_bom'), # Legacy Redirect?
    path('update-bom-status/', views.BOMLifecycleView.as_view(), name='update_bom_status'),
    path("bom/<int:bom_id>/details/", views.BOMDetailsView.as_view(), name="view_bom_details"),
    path('api/bom/<int:bom_id>/json/', views.BOMJsonView.as_view(), name='get_bom_json'), # NEW
    path('api/materials/search/', views.search_materials, name='search_materials'), # Material autocomplete
    
    # 📡 API Endpoints
    path('api/create-work-order/', views.WorkOrderCreateAPI.as_view(), name='api_create_work_order'),
    path('api/work-order/<int:pk>/split/', views.WorkOrderSplitAPI.as_view(), name='api_split_work_order'),
    path('api/work-order/<int:pk>/cancel-split/', views.WorkOrderCancelSplitAPI.as_view(), name='api_cancel_split_work_order'),
    path('api/work-order/combine/', views.WorkOrderCombineAPI.as_view(), name='api_combine_work_orders'),
    path('api/work-order/<int:pk>/release/', views.WorkOrderReleaseNextStageAPI.as_view(), name='api_release_next_stage'),
    path('api/work-order/bulk-action/', views.BulkWorkOrderActionView.as_view(), name='api_bulk_wo_action'),

    path('api/work-order/recommend/', views.WorkOrderRecommendationAPI.as_view(), name='api_recommend_work_order'),
    path('api/work-order/<int:wo_id>/close/', views.WorkOrderCloseAPI.as_view(), name='api_close_work_order'),
    path('api/work-order/<int:wo_id>/apply-latest-bom/', views.WorkOrderApplyLatestBOMAPI.as_view(), name='api_apply_latest_bom'),
    path('api/work-order/<int:wo_id>/bom-change-decision/', views.WorkOrderBOMChangeDecisionAPI.as_view(), name='api_bom_change_decision'),
    path('api/work-order/<int:wo_id>/material-readiness/', views.WorkOrderMaterialReadinessAPI.as_view(), name='api_work_order_material_readiness'),
    path('api/work-order/<int:wo_id>/store-receipt/', views.StoreReceiptConfirmAPI.as_view(), name='api_store_receipt_confirm'),
    path('api/work-order/<int:wo_id>/', views.WorkOrderDetailsAPI.as_view(), name='get_work_order'),
    path('api/work-order/<int:wo_id>/log/', views.WorkOrderLogAPI.as_view(), name='api_work_order_log'),
    path('api/machine/<int:machine_id>/log/', views.MachineLogAPI.as_view(), name='api_machine_log'),
    path('api/workorder/<int:wo_id>/json/', views.WorkOrderDetailAPI.as_view(), name='api_workorder_detail'),
    path('api/schedule-work-order/<int:wo_id>/', views.ScheduleWorkOrderAPI.as_view(), name='api_schedule_work_order'),
    path('api/schedule-advanced/', views.AdvancedScheduleAPI.as_view(), name='api_schedule_advanced'),
    path('api/work-order/<int:wo_id>/materials/', views.WOMaterialsAPI.as_view(), name='api_wo_materials'),
    path('api/work-order/<int:wo_id>/update/', views.WorkOrderUpdateView.as_view(), name='update_work_order'), # Timeline Drag
    path('api/workorder/<int:wo_id>/update/', views.WorkOrderUpdateView.as_view(), name='update_work_order_alias'), # Alias for Drawer JS
    path('api/workorder/<int:pk>/unschedule/', views.WorkOrderUnscheduleAPI.as_view(), name='api_unschedule_work_order'), # Unschedule Action
    path('api/work-order/<int:wo_id>/shop-update/', views.ShopFloorUpdateView.as_view(), name='api_update_work_order_status'), # Kiosk
    path('api/work-order/<int:wo_id>/criteria/', views.WOCriteriaAPI.as_view(), name='api_wo_criteria'),
    
    path('api/assess-quality/', views.QualityAnalysisView.as_view(), name='analyze_quality_image'),
    path('quality-check/analyze/', views.QualityAnalysisView.as_view(), name='analyze_quality_image_legacy'), # Alias

    path('api/log-production/', views.LogProductionAPI.as_view(), name='log_production'),
    path('approve-log/<int:log_id>/', views.ApproveLogView.as_view(), name='approve_log'),
    path('api/production-log/<int:log_id>/', views.ApproveLogView.as_view(), name='api_production_log_detail'),
    path('api/production-log/<int:log_id>/update/', views.ProductionLogEditView.as_view(), name='api_production_log_update'),
    path('api/work-order/<int:wo_id>/start-date/', views.WorkOrderStartDateUpdateView.as_view(), name='api_update_wo_start_date'),
    path('api/report-fault/', views.ReportFaultAPI.as_view(), name='report_fault_api'),
    
    path('api/timeline/', views.TimelineDataView.as_view(), name='get_timeline_data'),
    path('api/timeline/snap/', views.TimelineSnapAPIView.as_view(), name='api_timeline_snap'),
    path('api/notifications/', views.NotificationAPI.as_view(), name='api_notifications'),
    path('api/notifications/<int:notif_id>/read/', views.NotificationReadView.as_view(), name='api_mark_read'),
    path('api/simulation/', views.SimulationView.as_view(), name='api_simulation'),
    
    path('api/assign-wo/', views.AssignWorkOrderView.as_view(), name='assign_work_order'),
    path('api/planner/undo/', views.PlannerUndoRestoreAPI.as_view(), name='api_planner_undo'),
    path('api/assign-worker/', views.AssignWorkerAPIView.as_view(), name='assign_worker_api'),
    
    # 👷 Worker Assignment (NEW)
    path('api/assign-worker-to-wo/', views.AssignWorkerToWOView.as_view(), name='assign_worker_to_wo'),
    path('api/available-workers/', views.GetAvailableWorkersView.as_view(), name='get_available_workers'),


    # 📤 Bulk Import
    path('bulk-import/', views.BulkImportView.as_view(), name='bulk_import_dashboard'),
    path('bulk-import/upload/', views.HandleBulkImportView.as_view(), name='handle_bulk_import'),
    path('download-template/<str:filename>/', views.DownloadTemplateView.as_view(), name='download_template'),

    # 📊 Reports
    path('reports/', views.ReportsDashboardView.as_view(), name='reports_dashboard'),
    path('reports/export/csv/', views.ExportProductionCSVView.as_view(), name='export_production_csv'),
    path('reports/export/audit.csv/', views.ExportAuditCSVView.as_view(), name='export_audit_csv'),
    path('reports/export/pdf/', views.ExportReportPDFView.as_view(), name='export_report_pdf'),
    path('reports/export/sheet/', views.ExportWorkOrderSheetView.as_view(), name='export_work_order_sheet'),

    # API v1 (Modern ViewSets)
    path('', include('manufacturing.api_urls')),
    
]

if settings.DEBUG:
    urlpatterns += [
        path('debug-wos/', views.debug_dump.DebugWOView.as_view(), name='debug_wos'),
        path('debug-delete-wos/', views.DeleteAllWorkOrdersView.as_view(), name='debug_delete_wos'),
    ]
