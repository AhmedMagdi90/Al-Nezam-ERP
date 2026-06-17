from django.test import SimpleTestCase


class PlannerTemplateWiringTests(SimpleTestCase):
    def test_planner_dashboard_exposes_workspace_preserving_reload(self):
        with open('templates/manufacturing/planner_dashboard.html', encoding='utf-8') as handle:
            content = handle.read()

        self.assertIn('reloadPlannerWorkspacePreservingState', content)
        self.assertIn("window.reloadPlannerWorkspacePreservingState = window.reloadPlannerWorkspacePreservingState || function", content)
        self.assertIn("scopeRoot?.dataset?.plannerTenant", content)
        self.assertIn("data-planner-company=\"{{ company.name|default:'Company' }}\"", content)
        self.assertIn("data-planner-tenant=\"{{ request.tenant.code|default:'' }}\"", content)
        self.assertIn('window.__plannerResetWorkspaceState = {{ reset_planner_workspace_state|yesno:"true,false" }};', content)
        self.assertIn("key.indexOf('planner.workspace.state:') === 0", content)

    def test_base_dashboard_exposes_first_workspace_tour(self):
        with open('templates/manufacturing/base_dashboard.html', encoding='utf-8') as handle:
            content = handle.read()

        self.assertIn('{% if first_workspace_tour %}', content)
        self.assertIn('window.__firstWorkspaceTour = true;', content)
        self.assertIn('data-system-tour-target="manufacturing"', content)
        self.assertIn('data-system-tour-target="factory"', content)
        self.assertIn('data-system-tour-target="quality"', content)
        self.assertIn('data-system-tour-target="maintenance"', content)
        self.assertIn('data-system-tour-target="reports"', content)
        self.assertIn('data-system-tour-target="settings"', content)
        self.assertIn('System tour ${index + 1} of ${steps.length}', content)

    def test_planner_command_bar_keeps_tabs_visible_with_compact_schedule_actions(self):
        with open('templates/manufacturing/planner_dashboard.html', encoding='utf-8') as handle:
            content = handle.read()

        self.assertIn('flex: 0 0 auto;', content)
        self.assertIn('min-width: max-content;', content)
        self.assertIn('overflow: visible;', content)
        self.assertIn('flex-wrap: wrap;', content)
        self.assertIn('.planner-command-actions', content)
        self.assertIn('flex: 1 1 100%;', content)
        self.assertIn('w-[168px] shrink-0', content)
        self.assertIn('whitespace-nowrap rounded-xl border border-sky-200', content)
        self.assertIn('whitespace-nowrap rounded-xl border border-slate-200', content)
        self.assertNotIn('data-tab-key="intake"', content)
        self.assertNotIn('<span>{% tenant_trans "Intake" %}</span>', content)
        self.assertIn('ph ph-calendar-dots text-base', content)
        self.assertIn('ph ph-seal-check text-base', content)
        self.assertIn('ph ph-list-checks text-base', content)
        self.assertIn('data-tab-key="pending-wos"', content)
        self.assertIn('<span>{% tenant_trans "Pending WOs" %}</span>', content)
        self.assertNotIn('data-tab-key="analytics"', content)
        self.assertIn("border-slate-200 bg-white text-slate-700 shadow-sm", content)
        self.assertNotIn('{% tenant_trans "Planner Intake" %}', content)

    def test_pending_wos_replaces_analytics_screen(self):
        with open('templates/manufacturing/planner_dashboard.html', encoding='utf-8') as handle:
            content = handle.read()

        self.assertIn("activeScreen: savedState.activeScreen === 'analytics'", content)
        self.assertIn("? 'pendingWos'", content)
        self.assertIn("activeScreen === 'pendingWos'", content)
        self.assertIn('{% tenant_trans "Pending Work Orders" %}', content)
        self.assertIn("pendingWoFilter = 'material'", content)
        self.assertIn("pendingWoFilter = 'ready_plan'", content)
        self.assertIn("pendingWoFilter = 'release'", content)
        self.assertIn("pendingWoFilter = 'approval'", content)
        self.assertIn("pendingWoFilter = 'blocked'", content)
        self.assertIn('pending_wos_queue_counts.ready_plan', content)
        self.assertIn('pending_wos_queue|slice:":40"', content)
        self.assertIn("pendingWoFilter === '{{ row.category }}'", content)
        self.assertNotIn('{% tenant_trans "Planner Analytics" %}', content)
        self.assertNotIn("setScreen('analytics')", content)

    def test_create_work_order_modal_uses_planner_preserving_reload(self):
        with open('templates/manufacturing/partials/create_work_order_modal.html', encoding='utf-8') as handle:
            content = handle.read()

        self.assertIn("typeof window.reloadPlannerWorkspacePreservingState === 'function'", content)
        self.assertIn('window.reloadPlannerWorkspacePreservingState()', content)

    def test_edit_task_drawer_uses_planner_preserving_reload_fallback(self):
        with open('templates/manufacturing/partials/edit_task_drawer.html', encoding='utf-8') as handle:
            content = handle.read()

        self.assertIn('window.reloadPlannerWorkspacePreservingState', content)
        self.assertIn('const timelineManagedSaveTaskChanges = window.saveTaskChanges;', content)
        self.assertIn("if (typeof timelineManagedSaveTaskChanges === 'function') {", content)

    def test_timeline_drawer_shows_cycle_state_next_action(self):
        with open('templates/manufacturing/partials/edit_task_drawer.html', encoding='utf-8') as handle:
            drawer = handle.read()
        with open('static/js/manufacturing/timeline.js', encoding='utf-8') as handle:
            timeline = handle.read()

        self.assertIn('id="drawerCycleState"', drawer)
        self.assertIn('id="drawerCycleLabel"', drawer)
        self.assertIn('id="drawerRouteWorkOrderCode"', drawer)
        self.assertIn('id="drawerRouteProductName"', drawer)
        self.assertIn('window.updateDrawerCycleState = window.updateDrawerCycleState || function', drawer)
        self.assertIn('window.updateDrawerCycleState(wo.cycle_state, wo.status);', drawer)
        self.assertIn('window.applyDrawerClosedState(wo);', drawer)
        self.assertIn('window.updateDrawerIdentity = window.updateDrawerIdentity || function', drawer)
        self.assertIn('drawerCancelButton', drawer)
        self.assertIn('Already Closed', drawer)
        self.assertIn("cancelButton.textContent = closed ? 'Close' : 'Cancel';", drawer)
        self.assertIn("function updateDrawerCycleState(cycleState, fallbackStatus = '')", timeline)
        self.assertIn('function updateDrawerIdentity(wo = window.currentDrawerWO)', timeline)
        self.assertIn("routeCodeEl.textContent = displayId ? `WO #${displayId}` : 'WO';", timeline)
        self.assertIn('routeProductEl.textContent = productName;', timeline)
        self.assertIn('window.updateDrawerCycleState = updateDrawerCycleState;', timeline)
        self.assertIn('window.updateDrawerIdentity = updateDrawerIdentity;', timeline)
        self.assertIn('updateDrawerIdentity(wo);', timeline)
        self.assertIn('updateDrawerCycleState(wo.cycle_state, wo.status);', timeline)
        self.assertIn('function applyDrawerClosedState(wo = window.currentDrawerWO)', timeline)
        self.assertIn('saveButton.textContent = \'Already Closed\'', timeline)
        self.assertIn('This work order is planner closed and cannot be edited.', timeline)
        self.assertIn('cycleState?.next_action', timeline)
        self.assertIn('cycleState?.label', timeline)

    def test_planner_cards_surface_blocker_reasons(self):
        with open('templates/manufacturing/partials/workorders_list.html', encoding='utf-8') as handle:
            planner_list = handle.read()

        self.assertIn('cycleBlockerReason(wo.cycle_state)', planner_list)
        self.assertIn(':title="cycleBlockerReason(wo.cycle_state)"', planner_list)

    def test_planner_close_queue_is_single_detailed_closure_surface(self):
        with open('templates/manufacturing/planner_dashboard.html', encoding='utf-8') as handle:
            planner = handle.read()

        self.assertIn('id="plannerClosureQueue"', planner)
        self.assertIn('Planner Closure Queue', planner)
        self.assertIn('planner-close-card', planner)
        self.assertIn('wo.close_completed_qty|default:wo.quantity', planner)
        self.assertIn('wo.close_final_stage_name|default:"-"', planner)
        self.assertIn('wo.close_customer_name|default:"-"', planner)
        self.assertIn("window.openPlannerActionWorkOrder('{{ wo.id }}')", planner)
        self.assertIn("closePlannerAction('{{ wo.id }}')", planner)
        self.assertIn('window.runPlannerActionButton(this, () => closePlannerAction', planner)
        self.assertIn('Planner close actions are handled in the Planner Closure Queue', planner)
        self.assertNotIn('<h2 class="text-lg font-bold text-slate-800 mb-3">Planner Close Actions</h2>', planner)

    def test_worker_assign_modal_supports_planner_reload_fallback(self):
        with open('templates/manufacturing/modals/worker_assign_modal.html', encoding='utf-8') as handle:
            content = handle.read()

        self.assertIn("typeof reloadPlannerWorkspacePreservingState === 'function'", content)

    def test_timeline_route_planner_keeps_split_available(self):
        with open('static/js/manufacturing/timeline.js', encoding='utf-8') as handle:
            content = handle.read()

        self.assertIn('function getCurrentDrawerSplitContext(wo, stages = []) {', content)
        self.assertIn('window.currentDrawerSplitSourceId = splitContext.sourceId || String(wo.id || \'\');', content)
        self.assertNotIn("if (routePlannerMode) {\n                        hintEl.classList.add('hidden');", content)

    def test_timeline_visual_split_groups_are_machine_specific(self):
        with open('static/js/manufacturing/timeline.js', encoding='utf-8') as handle:
            content = handle.read()

        self.assertIn('function getTimelineVisualTaskGroupKey(task) {', content)
        self.assertIn('return `${baseKey}-machine-${machineKey}`;', content)
        self.assertIn('const groupKey = getTimelineVisualTaskGroupKey(task);', content)
        self.assertIn('function getTimelineSplitVisualMeta(task, splitGroups = new Map()) {', content)
        self.assertIn('split_group_status_summary: getTimelineSplitStatusSummary(group)', content)
        self.assertIn('taskDiv.dataset.timelineSplitGroup', content)
        self.assertIn('ph ph-git-branch', content)
        self.assertIn('segments done', content)

    def test_route_planner_search_uses_full_width_control(self):
        with open('templates/manufacturing/partials/edit_task_drawer.html', encoding='utf-8') as handle:
            content = handle.read()

        self.assertIn('id="drawerRouteSearch"', content)
        self.assertIn('Stage, machine name, machine code, or machine type...', content)
        self.assertIn('pl-24 pr-3', content)
        self.assertIn('xl:grid-cols-[minmax(260px,0.7fr)_minmax(420px,1.3fr)]', content)

    def test_timeline_export_includes_order_and_done_quantity_columns(self):
        with open('static/js/manufacturing/timeline.js', encoding='utf-8') as handle:
            content = handle.read()

        self.assertIn('["Work Order ID", "Product", "Order Qty", "Done Qty", "Status", "Start Date", "End Date", "Assigned Machine", "Assigned Worker", "Assignment Type"]', content)
        self.assertIn("Number(t.progress_stats ? t.progress_stats.target : (t.quantity || 0)) || 0", content)
        self.assertIn("Number(t.progress_stats ? t.progress_stats.actual : (t.finished_qty || 0)) || 0", content)

    def test_timeline_shows_system_expected_progress_marker(self):
        with open('static/js/manufacturing/timeline.js', encoding='utf-8') as handle:
            content = handle.read()
        with open('templates/manufacturing/planner_dashboard.html', encoding='utf-8') as handle:
            planner = handle.read()

        self.assertIn('function getTimelineExpectedProgressPercent(startMs, endMs, status = \'\', nowMs = Date.now())', content)
        self.assertIn('System expected progress:', content)
        self.assertIn('Gap:', content)
        self.assertIn('Behind', content)
        self.assertIn('Overdue', content)
        self.assertIn('Exp ${expectedProgressRounded}% / Act ${Math.round(progress)}%', content)
        self.assertIn('aria-label="System expected progress marker"', content)
        self.assertIn('setupTimelineExpectedProgressTicker()', content)
        self.assertIn("timeline.js' %}?v=272", planner)

    def test_timeline_work_order_tooltip_shows_status_and_planned_dates(self):
        with open('static/js/manufacturing/timeline.js', encoding='utf-8') as handle:
            content = handle.read()
        with open('templates/manufacturing/planner_dashboard.html', encoding='utf-8') as handle:
            planner = handle.read()

        self.assertIn("tooltip.setAttribute('role', 'tooltip')", content)
        self.assertIn('let timelineTooltipTimer = null;', content)
        self.assertIn('function getTimelineTaskStatusLabel(status) {', content)
        self.assertIn('function formatTimelineTooltipDate(value) {', content)
        self.assertIn('timelineTooltipTimer = window.setTimeout(() => {', content)
        self.assertIn('}, 300);', content)
        self.assertIn('Planned Start:', content)
        self.assertIn('Planned End:', content)
        self.assertIn('Quantity:', content)
        self.assertIn('getTimelineStatusBadgeStyles(task.status)', content)
        self.assertIn("taskDiv.setAttribute('tabindex', '0');", content)
        self.assertIn("taskDiv.addEventListener('focus', (e) => showTimelineTooltip(t, e, { anchorEl: taskDiv }));", content)
        self.assertIn("taskDiv.addEventListener('blur', hideTimelineTooltip);", content)
        self.assertIn("timeline.js' %}?v=272", planner)

    def test_timeline_header_promotes_day_week_month_scale_toggles(self):
        with open('templates/manufacturing/partials/timeline_header_bar.html', encoding='utf-8') as handle:
            header = handle.read()
        with open('templates/manufacturing/partials/timeline_component.html', encoding='utf-8') as handle:
            component = handle.read()
        with open('static/js/manufacturing/timeline.js', encoding='utf-8') as handle:
            timeline = handle.read()

        self.assertIn('class="timeline-scale-switch"', header)
        self.assertIn('role="group" aria-label="{% tenant_trans \'Timeline scale\' %}"', header)
        self.assertIn('id="timelineScaleDayToggle"', header)
        self.assertIn("window.setGanttView && window.setGanttView('day')", header)
        self.assertIn('id="timelineScaleWeekToggle"', header)
        self.assertIn("window.setGanttView && window.setGanttView('week')", header)
        self.assertIn('id="timelineScaleMonthToggle"', header)
        self.assertIn("window.setGanttView && window.setGanttView('month')", header)
        self.assertIn('.timeline-scale-switch__button[aria-pressed="true"]', component)
        self.assertIn("{ id: 'timelineScaleDayToggle', mode: 'day'", timeline)
        self.assertIn("persistPlannerWorkspaceStatePatch({", timeline)
        self.assertIn("scale: timelineState.viewMode", timeline)
        self.assertNotIn('<span>Day Scale</span>', header)

    def test_planner_follow_up_queue_surfaces_execution_exceptions(self):
        with open('templates/manufacturing/planner_dashboard.html', encoding='utf-8') as handle:
            planner = handle.read()
        with open('static/js/manufacturing/timeline.js', encoding='utf-8') as handle:
            content = handle.read()

        self.assertIn('id="plannerFollowUpQueue"', planner)
        self.assertIn('{% tenant_trans "Follow-up Queue" %}', planner)
        self.assertIn('id="plannerDispatchReadiness"', planner)
        self.assertIn('{% tenant_trans "Dispatch Readiness" %}', planner)
        self.assertIn('id="plannerDispatchReadinessList" class="mt-3 max-h-[420px] space-y-2 overflow-y-auto overscroll-contain pr-1"', planner)
        self.assertIn('id="plannerFollowUpList" class="mt-3 max-h-[360px] space-y-2 overflow-y-auto overscroll-contain pr-1"', planner)
        self.assertIn('function buildPlannerDispatchReadinessQueue(nowMs = Date.now())', content)
        self.assertIn('function renderPlannerDispatchReadinessQueue()', content)
        self.assertIn('mount.innerHTML = queue.map((item) => {', content)
        self.assertIn('Ready for Supervisor', content)
        self.assertIn('Missing Worker', content)
        self.assertIn('Machine Fault', content)
        self.assertIn('Not Started Yet', content)
        self.assertIn('Already Running', content)
        self.assertIn('window.openWorkOrderModal && window.openWorkOrderModal(${Number(task.id)})', content)
        self.assertIn('function getRouteStageReadiness(stage)', content)
        self.assertIn('function getRoutePlannerBlockingIssues(stages = window.currentDrawerRouteStages)', content)
        self.assertIn('Missing Machine', content)
        self.assertIn('Missing Duration', content)
        self.assertIn('Machine Fault', content)
        self.assertIn('Fix ${blockers.length} Stage Issue', content)
        self.assertIn('function buildPlannerFollowUpQueue(nowMs = Date.now())', content)
        self.assertIn('function renderPlannerFollowUpQueue()', content)
        self.assertIn('window.quickPlanPendingWorkOrder = async function (woId)', content)
        self.assertIn('fn(woId, { forceRoutePlanner: true })', content)
        self.assertIn('function setDrawerPlanningMode(wo, stages, options = {})', content)
        self.assertIn('gap >= 10', content)
        self.assertIn("['production_approval', 'supervisor_dispatch', 'planning', 'machine_unavailable', 'next_stage_release', 'planner_close']", content)
        self.assertIn('window.openWorkOrderModal && window.openWorkOrderModal', content)
        self.assertIn('Owner:', content)

    def test_planner_surfaces_bom_change_alerts(self):
        with open('templates/manufacturing/planner_dashboard.html', encoding='utf-8') as handle:
            planner = handle.read()
        with open('static/js/manufacturing/timeline.js', encoding='utf-8') as handle:
            timeline = handle.read()
        with open('templates/manufacturing/partials/edit_task_drawer.html', encoding='utf-8') as handle:
            drawer = handle.read()
        with open('manufacturing/services.py', encoding='utf-8') as handle:
            services = handle.read()

        self.assertIn('openNotifications()', planner)
        self.assertIn('closeNotifications()', planner)
        self.assertIn('data-planner-notifications-button="true"', planner)
        self.assertIn('planner-notification-icon-btn inline-flex', planner)
        self.assertIn('planner-notification-icon-btn__badge', planner)
        self.assertIn("activeScreen === 'notifications'", planner)
        self.assertIn('{% tenant_trans "Notifications" %}', planner)
        self.assertIn('{% tenant_trans "Planner action center" %}', planner)
        self.assertIn('bom_change_actions_count', planner)
        self.assertIn('system_notifications_count', planner)
        self.assertIn('system_notifications|slice:":8"', planner)
        self.assertIn('{% tenant_trans "System notifications" %}', planner)
        self.assertIn('planner_notifications_count', planner)
        self.assertIn('bom_change_actions|slice:":10"', planner)
        self.assertIn('pending_logs|slice:":8"', planner)
        self.assertIn('qc_pending|slice:":8"', planner)
        self.assertIn('{% tenant_trans "QC pending" %}', planner)
        self.assertIn('planner_actions|slice:":8"', planner)
        self.assertIn('release_ready_tasks|slice:":8"', planner)
        self.assertIn('{% tenant_trans "Open Decision" %}', planner)
        self.assertIn("window.openPlannerActionWorkOrder('{{ wo.id }}')", planner)
        self.assertIn('pending_wos_count|default:0|json_script:"pending-wos-count"', planner)
        self.assertIn('window.openPlannerActionWorkOrder = window.openPlannerActionWorkOrder || function', planner)
        self.assertIn("scope.setScreen('schedule')", planner)
        self.assertIn("window.setTimeout(() => openFn(targetId, options || {}), scheduleOpenDelay)", planner)
        self.assertIn('window.__plannerFallbackClosePlannerAction', planner)
        self.assertIn('window.runPlannerActionButton = window.runPlannerActionButton || async function', planner)
        self.assertIn('window.setPlannerActionButtonBusy', planner)
        self.assertIn('planner-action-btn[aria-busy="true"]', planner)
        self.assertIn('.planner-action-btn:focus-visible', planner)
        self.assertIn('aria-live="polite" class="planner-notification-icon-btn__badge', planner)
        self.assertIn('aria-label="{% tenant_trans \'Open BOM decision for\' %} {{ wo.display_work_order_code }}"', planner)
        self.assertIn('aria-label="{% tenant_trans \'Plan work order\' %} {{ wo.display_work_order_code }}"', planner)
        self.assertIn(':aria-expanded="showQueueRail ? \'true\' : \'false\'"', planner)
        self.assertIn("sessionStorage.setItem('planner-open-wo-after-reload', targetId)", planner)
        self.assertIn("window.notifyPlannerAction('Opening work order action...', 'info')", planner)
        self.assertIn("window.reloadPlannerWorkspacePreservingState({ activeScreen: 'notifications' })", timeline)
        self.assertIn("bom_change_status='action_required'", services)
        self.assertIn('"system_notifications_count": len(system_notifications)', services)
        self.assertIn('"planner_notifications_count": planner_notifications_count', services)
        self.assertIn('"pending_wos_count": pending_wos_qs.count()', services)
        self.assertIn('"pending_wos_queue": pending_wos_queue', services)
        self.assertIn('DashboardService._build_pending_wos_queue', services)
        self.assertNotIn('focusBomChangeQueue()', planner)
        self.assertNotIn('id="plannerBomChangeQueue"', planner)
        self.assertIn("window.reloadPlannerWorkspacePreservingState({", timeline)
        self.assertIn("activeScreen: 'schedule'", timeline)
        self.assertIn("function syncMaterialReadinessToPlannerCaches", timeline)
        self.assertIn("window.syncMaterialReadinessToPlannerCaches", timeline)
        self.assertIn("syncMaterialReadinessToPlannerCaches(data.work_order_id || woId, data.material_readiness)", timeline)
        self.assertIn("function rememberPlannerWorkOrderAfterReload(workOrderId)", timeline)
        self.assertIn("sessionStorage.setItem('planner-open-wo-after-reload', targetId)", timeline)
        self.assertIn("function refreshPlannerQueuesAfterMaterialReadinessUpdate(materialReadiness, workOrderId)", timeline)
        self.assertIn("window.refreshPlannerQueuesAfterMaterialReadinessUpdate", timeline)
        self.assertIn("Material ready. Refreshing planner queue...", timeline)
        self.assertIn("refreshPlannerQueuesAfterMaterialReadinessUpdate(data.material_readiness, data.work_order_id || woId)", timeline)
        self.assertIn("function getMaterialReadinessDecisionCopy(readiness = {}, wo = {})", timeline)
        self.assertIn("Partial material percent saved. Split or reduce before planning.", timeline)
        self.assertIn("payload.available_percent = availablePercent", timeline)
        self.assertIn('id="drawerMaterialAvailablePercent"', drawer)
        self.assertIn('id="drawerMaterialDeliveryDate"', drawer)
        self.assertIn('id="drawerMaterialNextAction"', drawer)
        self.assertIn('data-material-status="partial"', drawer)

    def test_work_order_drawer_hides_timeline_toolbar_layer(self):
        with open('templates/manufacturing/partials/timeline_component.html', encoding='utf-8') as handle:
            timeline_component = handle.read()
        with open('templates/manufacturing/partials/edit_task_drawer.html', encoding='utf-8') as handle:
            drawer = handle.read()
        with open('static/js/manufacturing/timeline.js', encoding='utf-8') as handle:
            timeline = handle.read()
        with open('templates/manufacturing/planner_dashboard.html', encoding='utf-8') as handle:
            planner = handle.read()

        self.assertIn('body.work-order-drawer-open .timeline-header-bar', timeline_component)
        self.assertIn("document.body.classList.add('work-order-drawer-open')", timeline)
        self.assertIn("document.body.classList.remove('work-order-drawer-open')", timeline)
        self.assertIn("document.body.classList.add('work-order-drawer-open')", drawer)
        self.assertIn("document.body.classList.remove('work-order-drawer-open')", drawer)
        self.assertIn("timeline.js' %}?v=272", planner)
        self.assertIn("onclick=\"window.closeEditDrawer && window.closeEditDrawer(); return false;\"", drawer)

    def test_work_order_drawer_does_not_offer_draft_status(self):
        with open('templates/manufacturing/partials/edit_task_drawer.html', encoding='utf-8') as handle:
            drawer = handle.read()

        self.assertNotIn('<option value="draft">Draft</option>', drawer)
        self.assertIn('<option value="pending">Pending</option>', drawer)

    def test_split_child_opens_own_drawer_not_parent_route_panel(self):
        with open('static/js/manufacturing/timeline.js', encoding='utf-8') as handle:
            timeline = handle.read()

        self.assertIn("const isSplitChild = !!String(task?.source_task_id || '').trim();", timeline)
        self.assertIn("task?.parent_id && !isSplitChild", timeline)

    def test_timeline_supports_machine_specific_non_working_overlay_and_shift_badges(self):
        with open('static/js/manufacturing/timeline.js', encoding='utf-8') as handle:
            content = handle.read()

        self.assertIn('function normalizeTimelineShiftConfig(rawConfig) {', content)
        self.assertIn('function getTimelineShiftWindow(shiftFilter = null, rawConfig = null) {', content)
        self.assertIn('const selectedShift = config[selectedKey] || config[requestedKey] || config.morning', content)
        self.assertIn('shift_configuration: normalizeTimelineShiftConfig(machine.shift_configuration || timelineState.shiftConfig)', content)
        self.assertIn('if (isTimelineSlotNonWorking(slot, machine)) {', content)
        self.assertIn("machine.working_hours_summary", content)

    def test_timeline_visual_groups_use_latest_scheduled_end_not_summed_parallel_duration(self):
        with open('static/js/manufacturing/timeline.js', encoding='utf-8') as handle:
            content = handle.read()

        self.assertIn('const scheduledEndMs = ends.length ? Math.max(...ends) : null;', content)
        self.assertIn('const projectedEndMs = (earliestStartMs !== null && totalDurationMinutes > 0)', content)
        self.assertIn('const finalEndMs = scheduledEndMs || projectedEndMs;', content)
        self.assertNotIn('const finalEndMs = Math.max(scheduledEndMs || 0, projectedEndMs || 0) || null;', content)

    def test_timeline_resource_column_splits_machine_code_and_name_for_cleaner_rows(self):
        with open('static/js/manufacturing/timeline.js', encoding='utf-8') as handle:
            content = handle.read()

        self.assertIn('function getTimelineMachinePresentation(machine) {', content)
        self.assertIn('const machineCodeBadgeHtml = !isStageRow && machinePresentation?.showCodeBadge', content)
        self.assertIn('const showHoursBadge = !isStageRow && machineHoursSummary && !isCompact && !isTightResourceColumn;', content)
        self.assertIn("text-gray-800 truncate", content)

    def test_timeline_resource_column_renders_single_machine_status_lamp(self):
        with open('static/js/manufacturing/timeline.js', encoding='utf-8') as handle:
            content = handle.read()

        self.assertIn('function renderTimelineMachineLamps(machine, visibleMachineTasks = [])', content)
        self.assertIn("Operational - no load", content)
        self.assertIn("Operational - with load", content)
        self.assertIn("Fault / unavailable", content)
        self.assertIn("return hasLoad ? 'loaded' : 'idle';", content)
        self.assertIn('aria-label="Machine status lamp"', content)
        self.assertNotIn('aria-label="Machine status lamps"', content)
        self.assertNotIn('lamps.map', content)
        self.assertIn('const machineLampHtml = !isStageRow', content)

    def test_timeline_component_hidden_mode_keeps_canonical_filter_controls(self):
        with open('templates/manufacturing/partials/timeline_component.html', encoding='utf-8') as handle:
            content = handle.read()

        self.assertIn('{% else %}', content)
        self.assertIn('<select id="timelineMachineFilter" class="hidden">', content)
        self.assertIn('<select id="timelineStageFilter" class="hidden">', content)
        self.assertIn('<select id="timelineFilter" class="hidden">', content)

    def test_timeline_restores_invalid_saved_filters_to_all(self):
        with open('static/js/manufacturing/timeline.js', encoding='utf-8') as handle:
            content = handle.read()

        self.assertIn("const requestedMachineActivity = state.machineActivity || 'all';", content)
        self.assertIn("const resolvedMachineActivity = machineSelect ? (machineSelect.value || 'all') : 'all';", content)
        self.assertIn("const requestedStage = state.stage || 'all';", content)
        self.assertIn("const resolvedStage = stageSelect ? (stageSelect.value || 'all') : 'all';", content)
        self.assertIn("persistPlannerWorkspaceStatePatch(normalizedWorkspacePatch);", content)
        self.assertIn("let timelineBlankGridRecoveryAttempted = false;", content)
        self.assertIn("filterState.status = 'all';", content)
        self.assertIn("filterState.stage = 'all';", content)
        self.assertIn("filterState.machineActivity = 'all';", content)
        self.assertIn("filterState.search = '';", content)

    def test_timeline_prefers_template_payload_before_forced_refresh(self):
        with open('static/js/manufacturing/timeline.js', encoding='utf-8') as handle:
            content = handle.read()

        self.assertIn('function loadTimelineTemplateDataIntoCache() {', content)
        self.assertIn('function normalizeTimelineMachinePayload(machine, index = 0) {', content)
        self.assertIn('.map((machine, index) => normalizeTimelineMachinePayload(machine, index))', content)
        self.assertIn('const templateHasRenderableData = loadTimelineTemplateDataIntoCache();', content)
        self.assertIn("Timeline API returned empty payload; preserving existing template data.", content)
        self.assertIn("Timeline render failed; preserving previous timeline rows.", content)
        self.assertIn('function renderTimelineFromCurrentCache() {', content)
        self.assertIn('const shouldShowQueueLane = allowUnassigned && (unassignedTasks.length > 0 || machinesCache.length === 0);', content)
        self.assertIn('renderTimelineFromCurrentCache();', content)
        self.assertIn("console.warn('Timeline secondary renderer failed.', error);", content)
        self.assertIn("const timelineEl = document.querySelector('#customTimeline');", content)
        self.assertIn("const infoEl = timelineEl?.closest('.glass-panel-premium')", content)
        self.assertIn('.filter((range) => range && Number.isFinite(range.start)', content)
        self.assertIn('if (!range || !Number.isFinite(range.start) || !Number.isFinite(range.end)) return false;', content)

    def test_timeline_uses_verbose_duration_formatter(self):
        with open('static/js/manufacturing/timeline.js', encoding='utf-8') as handle:
            content = handle.read()

        self.assertIn('function formatManufacturingDurationFromMinutes(totalMinutes) {', content)
        self.assertIn('window.formatManufacturingDurationFromMinutes = formatManufacturingDurationFromMinutes;', content)
        self.assertIn('Stage Time: ${formatManufacturingDurationFromMinutes(estimatedMinutes)}', content)
        self.assertIn('Setup ${formatManufacturingDurationFromMinutes(setupMinutes)} of ${formatManufacturingDurationFromMinutes(actualDurationMinutes)}', content)

    def test_split_modal_uses_verbose_duration_formatter(self):
        with open('templates/manufacturing/partials/split_work_order_modal.html', encoding='utf-8') as handle:
            content = handle.read()

        self.assertIn("typeof window.formatManufacturingDurationFromMinutes === 'function'", content)
        self.assertIn('displayElem.textContent = formatDuration(totalTime);', content)

    def test_bom_builder_accepts_decimal_cycle_time_and_humanizes_total(self):
        with open('templates/manufacturing/bom_builder.html', encoding='utf-8') as handle:
            content = handle.read()

        self.assertIn('x-text="$store.bom.formatDuration($store.bom.calculateTotalTime())"', content)
        self.assertIn('const runMinutes = this.normalizeOperationTimeToMinutes(op.run_time, op.run_time_unit);', content)
        self.assertIn('x-model="$store.bom.selectedOp.run_time" min="0" step="0.01"', content)
        self.assertIn('x-model="$store.bom.selectedOp.run_time_unit"', content)

    def test_bom_builder_resolves_factory_setup_stage_names_at_save_time(self):
        with open('templates/manufacturing/bom_builder.html', encoding='utf-8') as handle:
            content = handle.read()

        self.assertIn('stageCatalog: {{ stage_catalog_json|safe }}', content)
        self.assertIn('Stages / Resources', content)
        self.assertIn('placeholder="Find stage/type"', content)
        self.assertIn('Drag stages or resources from right', content)
        self.assertIn('@input="$store.bom.syncSelectedOperationStage($event.target.value)"', content)
        self.assertIn('normalizeStageName(value) {', content)
        self.assertIn('const stageName = this.normalizeStageName(op.name || op.stage_name);', content)
        self.assertIn('const matchedStage = this.findStageByName(stageName) || this.findStageById(op.stage_id);', content)
        self.assertIn('default_machine_id: "{{ machine.default_machine_id|default:\'\'|escapejs }}"', content)
        self.assertIn("const normalizedMachineId = isStageSource ? '' : String(machine.machine_id || machine.ref_id || machine.id || '');", content)
        self.assertIn('machine_id: op.machine_id || null', content)

    def test_bom_builder_surfaces_pre_save_readiness_and_stage_impact(self):
        with open('templates/manufacturing/bom_builder.html', encoding='utf-8') as handle:
            content = handle.read()

        self.assertIn('readinessStatusLabel', content)
        self.assertIn('saveImpactSummary', content)
        self.assertIn('validateBomForSave({ includeWarnings: false })', content)
        self.assertIn('selectedOperationStageImpact', content)
        self.assertIn('New Factory Setup stage will be created', content)
        self.assertIn('Saving will create a new draft version; existing work orders keep their BOM snapshot.', content)

    def test_bom_details_use_humanized_duration_filter(self):
        with open('templates/manufacturing/partials/bom_details.html', encoding='utf-8') as handle:
            content = handle.read()

        self.assertIn('{% load duration_tags %}', content)
        self.assertIn('|humanize_duration_minutes', content)

    def test_bom_modals_allow_decimal_operation_times_and_verbose_preview(self):
        with open('templates/manufacturing/bom_builder.html', encoding='utf-8') as handle:
            builder = handle.read()
        with open('templates/manufacturing/partials/create_bom_modal.html', encoding='utf-8') as handle:
            create_modal = handle.read()
        with open('templates/manufacturing/modals/bom_modal.html', encoding='utf-8') as handle:
            legacy_modal = handle.read()

        self.assertIn("x-model=\"$store.bom.selectedOp.setup_time_unit\"", builder)
        self.assertIn("x-model=\"$store.bom.selectedOp.run_time_unit\"", builder)
        self.assertIn('inline-flex max-w-full flex-wrap items-center gap-2', builder)
        self.assertIn('class="w-14 border-0 bg-transparent px-1.5 py-1.5 text-sm font-semibold text-slate-700 focus:ring-0"', builder)
        self.assertIn("normalizeOperationTimeToMinutes(value, unit)", builder)
        self.assertIn("setup_time_unit: this.normalizeOperationTimeUnit(op.setup_time_unit)", builder)
        self.assertIn("run_time_unit: this.normalizeOperationTimeUnit(op.run_time_unit)", builder)
        self.assertIn('function formatBomModalDuration(totalMinutes) {', create_modal)
        self.assertIn('function normalizeBomOperationTimeToMinutes(value, unit) {', create_modal)
        self.assertIn('name="op_setup_unit"', create_modal)
        self.assertIn('name="op_run_unit"', create_modal)
        self.assertIn('setup_time_unit: setupUnitInput ? setupUnitInput.value : \'min\'', create_modal)
        self.assertIn('run_time_unit: runUnitInput ? runUnitInput.value : \'min\'', create_modal)
        self.assertIn('name="op_run" value="5" min="0" step="0.01"', create_modal)
        self.assertIn('function normalizeBomOperationTimeToMinutes(value, unit) {', legacy_modal)
        self.assertIn('name="op_setup_unit"', legacy_modal)
        self.assertIn('name="op_run_unit"', legacy_modal)
        self.assertIn("setup_time_unit: node.querySelector('[name=op_setup_unit]')?.value || 'min'", legacy_modal)
        self.assertIn("run_time_unit: node.querySelector('[name=op_run_unit]')?.value || 'min'", legacy_modal)
        self.assertIn('name="op_run" value="5" min="0" step="0.01"', legacy_modal)
