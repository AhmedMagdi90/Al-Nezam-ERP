/**
 * Kemet ERP - Custom CSS Grid Timeline
 * Replaces Vis.js for precise control over Layout (CSS Grid) and Layers (Z-Index).
 */

let currentViewMode = '24h'; // or '8h'
let machinesCache = [];
let tasksCache = [];
let stagesCache = [];
let timelineFetchAlertShown = false;
let timelineLoadedStatusFilter = 'default';
const TIMELINE_EDIT_MODE_STORAGE_KEY = 'planner.timeline.edit_mode';
const TIMELINE_SNAP_STORAGE_KEY = 'planner.timeline.snap_minutes';
const TIMELINE_SNAP_ENABLED_STORAGE_KEY = 'planner.timeline.snap_enabled';
const TIMELINE_MAXIMIZED_STORAGE_KEY = 'planner.timeline.maximized';
const TIMELINE_OPTIONS_MINIMIZED_STORAGE_KEY = 'planner.timeline.options_minimized';
const TIMELINE_ROW_DENSITY_STORAGE_KEY = 'planner.timeline.row_density';
const TIMELINE_SHOW_PROGRESS_STORAGE_KEY = 'planner.timeline.show_progress';
const TIMELINE_SHOW_ASSIGNEE_STORAGE_KEY = 'planner.timeline.show_assignee';
const TIMELINE_SHOW_NON_WORKING_STORAGE_KEY = 'planner.timeline.show_non_working';
const TIMELINE_MACHINE_COLUMN_WIDTH_STORAGE_KEY = 'planner.timeline.machine_column_width';
let plannerWorkspaceStateRestorePending = false;
let plannerWorkspaceStateRestoreInProgress = false;
let timelineEditModeEnabled = true;
let timelineSnapMinutesOverride = 'auto';
let timelineSnapEnabled = true;
let timelineMaximized = false;
let timelineOptionsMinimized = false;
let timelineOverflowMenuOpen = false;
let timelineShowProgress = true;
let timelineShowAssignee = true;
let timelineShowNonWorking = true;
let timelineMachineColumnWidthOverride = null;
let suppressTimelineSearchAutoScroll = false;
let timelineBlankGridRecoveryAttempted = false;
let timelineExpectedProgressTimer = null;
let timelineTooltipTimer = null;

function safeParseJSON(raw) {
    if (raw === null || raw === undefined) return null;
    if (typeof raw !== 'string') return raw;
    const text = raw.trim();
    if (!text) return null;
    try {
        return JSON.parse(text);
    } catch (e) {
        try {
            const inner = JSON.parse(text);
            if (typeof inner === 'string') {
                return JSON.parse(inner);
            }
            return inner;
        } catch (err) {
            return null;
        }
    }
}

function normalizeSortToken(value) {
    return String(value || '').toLowerCase().trim();
}

function sortMachinesForTimeline(list) {
    if (!Array.isArray(list)) return [];
    return list.filter((item) => item && typeof item === 'object').slice().sort((a, b) => {
        const catA = normalizeSortToken(a.category || '');
        const catB = normalizeSortToken(b.category || '');
        if (catA !== catB) return catA.localeCompare(catB);
        const typeA = normalizeSortToken(a.type || '');
        const typeB = normalizeSortToken(b.type || '');
        if (typeA !== typeB) return typeA.localeCompare(typeB);
        const nameA = normalizeSortToken(a.name || '');
        const nameB = normalizeSortToken(b.name || '');
        if (nameA !== nameB) return nameA.localeCompare(nameB);
        return String(a.id || '').localeCompare(String(b.id || ''));
    });
}

function getCompensationQty(task) {
    return Number(task && task.scrap_compensation_qty ? task.scrap_compensation_qty : 0) || 0;
}

function getBaseQty(task) {
    const qty = Number(task && task.quantity ? task.quantity : 0) || 0;
    const compensationQty = getCompensationQty(task);
    const explicitBase = Number(task && task.base_quantity !== undefined && task.base_quantity !== null ? task.base_quantity : NaN);
    if (Number.isFinite(explicitBase)) return explicitBase;
    return Math.max(qty - compensationQty, 0);
}

function formatQuantityBreakdown(task) {
    const qty = Number(task && task.quantity ? task.quantity : 0) || 0;
    const compensationQty = getCompensationQty(task);
    if (compensationQty <= 0) return String(qty);
    const baseQty = getBaseQty(task);
    return `${qty} (Base ${baseQty} + Scrap ${compensationQty})`;
}

function formatManufacturingDurationFromSeconds(totalSeconds) {
    const units = [
        ['month', 30 * 24 * 60 * 60],
        ['week', 7 * 24 * 60 * 60],
        ['day', 24 * 60 * 60],
        ['hour', 60 * 60],
        ['minute', 60],
        ['second', 1],
    ];
    let remaining = Math.max(0, Math.round(Number(totalSeconds) || 0));
    if (remaining <= 0) return '0 seconds';

    const parts = [];
    units.forEach(([label, unitSeconds]) => {
        if (remaining < unitSeconds) return;
        const count = Math.floor(remaining / unitSeconds);
        remaining %= unitSeconds;
        parts.push(`${count} ${count === 1 ? label : `${label}s`}`);
    });
    return parts.length ? parts.join(' ') : '0 seconds';
}

function formatManufacturingDurationFromMinutes(totalMinutes) {
    return formatManufacturingDurationFromSeconds((Number(totalMinutes) || 0) * 60);
}

window.formatManufacturingDurationFromMinutes = formatManufacturingDurationFromMinutes;

// --- Drag & Drop Handler ---
window.currentlyDraggingAllowedTypes = [];
window.currentTimelineDragPayload = null;

window.handleSidebarDragStart = function (e, id, name, allowedTypesStr) {
    if (!e.dataTransfer) return;

    const allowedTypes = allowedTypesStr ? allowedTypesStr.split(',').filter(Boolean) : [];
    window.currentlyDraggingAllowedTypes = allowedTypes; // Store globally for render checks

    const payload = {
        id: id,
        dragType: 'queue-item',
        content: name,
        allowedTypes: allowedTypes
    };
    window.currentTimelineDragPayload = payload;
    e.dataTransfer.setData('text/plain', JSON.stringify(payload));
    e.dataTransfer.effectAllowed = 'copyMove';
    e.dataTransfer.dropEffect = 'copy';

    // Add visual cue to invalid rows
    highlightValidMachines(allowedTypes);
};

window.handleDragEnd = function () {
    // Reset visual cues
    document.querySelectorAll('.machine-row').forEach(row => {
        row.classList.remove('opacity-40', 'grayscale');
    });
    window.currentlyDraggingAllowedTypes = [];
    window.currentTimelineDragPayload = null;
};

function highlightValidMachines(allowedTypes) {
    if (!allowedTypes || allowedTypes.length === 0) return;

    // Iterate all machine rows in DOM
    document.querySelectorAll('.machine-row').forEach(row => {
        const type = row.dataset.machineType || "General";
        // If machine type is NOT in allowed list, fade it out
        if (!allowedTypes.includes(type) && !allowedTypes.includes('General')) {
            row.classList.add('opacity-40', 'grayscale');
        }
    });
}

function normalizeSnapOverride(value) {
    const raw = String(value ?? '').trim().toLowerCase();
    if (!raw || raw === 'auto') return 'auto';
    const parsed = Number(raw);
    if (!Number.isFinite(parsed) || parsed <= 0) return 'auto';
    if (![5, 15, 30, 60].includes(parsed)) return 'auto';
    return String(parsed);
}

function getTimelineWorkspaceScope() {
    const root = document.querySelector('[x-data="plannerWorkspace"], [data-planner-dashboard="true"][x-data]');
    if (!root || !Array.isArray(root._x_dataStack) || !root._x_dataStack.length) return null;
    return root._x_dataStack[0];
}

function hasWorkspaceProperty(propName) {
    const scope = getTimelineWorkspaceScope();
    return !!(scope && Object.prototype.hasOwnProperty.call(scope, propName));
}

function getQueueRailState() {
    const scope = getTimelineWorkspaceScope();
    if (!scope) return null;
    if (typeof scope.showQueueRail === 'boolean') return scope.showQueueRail;
    return null;
}

function setWorkspaceProperty(propName, value) {
    const scope = getTimelineWorkspaceScope();
    if (!scope || !Object.prototype.hasOwnProperty.call(scope, propName)) return false;
    scope[propName] = value;
    return true;
}

function getAvailableTimelinePrimaryViews() {
    const available = ['gantt'];
    if (document.getElementById('plannerViewKanban')) available.push('kanban');
    if (document.getElementById('plannerViewList')) available.push('list');
    if (document.getElementById('plannerViewCalendar')) available.push('calendar');
    return available;
}

function normalizeTimelinePrimaryView(view) {
    const requested = String(view || '').trim().toLowerCase();
    const allowed = getAvailableTimelinePrimaryViews();
    if (allowed.includes(requested)) return requested;
    return 'gantt';
}

function getTimelinePrimaryViewState() {
    const scope = getTimelineWorkspaceScope();
    if (scope) {
        if (typeof scope.currentView === 'string') return scope.currentView.toLowerCase();
        if (typeof scope.scheduleView === 'string') return scope.scheduleView.toLowerCase();
    }

    if (document.getElementById('plannerViewGantt') && !document.getElementById('plannerViewGantt').classList.contains('hidden')) return 'gantt';
    if (document.getElementById('plannerViewKanban') && !document.getElementById('plannerViewKanban').classList.contains('hidden')) return 'kanban';
    if (document.getElementById('plannerViewList') && !document.getElementById('plannerViewList').classList.contains('hidden')) return 'list';
    if (document.getElementById('plannerViewCalendar') && !document.getElementById('plannerViewCalendar').classList.contains('hidden')) return 'calendar';
    return 'gantt';
}

function getPersistedPlannerWorkspaceState() {
    if (typeof window.loadPlannerWorkspaceState !== 'function') return null;
    return window.loadPlannerWorkspaceState();
}

function persistPlannerWorkspaceStatePatch(partialState = {}) {
    if (plannerWorkspaceStateRestoreInProgress) return getPersistedPlannerWorkspaceState();
    if (typeof window.persistPlannerWorkspaceState !== 'function') return null;
    return window.persistPlannerWorkspaceState(partialState);
}

function normalizeTimelineScaleToken(value) {
    const token = String(value || '').trim().toLowerCase();
    return ['day', 'week', 'month'].includes(token) ? token : 'day';
}

function normalizeTimelineShiftToken(value) {
    const token = String(value || '').trim().toLowerCase();
    return ['all', 'day', 'middle', 'night'].includes(token) ? token : 'all';
}

function formatTimelineDateInputValue(date) {
    const target = date instanceof Date ? new Date(date) : new Date(date || Date.now());
    if (Number.isNaN(target.getTime())) return '';
    target.setHours(0, 0, 0, 0);
    const yyyy = target.getFullYear();
    const mm = String(target.getMonth() + 1).padStart(2, '0');
    const dd = String(target.getDate()).padStart(2, '0');
    return `${yyyy}-${mm}-${dd}`;
}

function getTimelineIsoWeekStart(inputDate) {
    const base = new Date(inputDate || new Date());
    if (Number.isNaN(base.getTime())) return null;
    base.setHours(0, 0, 0, 0);
    const mondayOffset = (base.getDay() + 6) % 7; // Monday=0 ... Sunday=6
    base.setDate(base.getDate() - mondayOffset);
    base.setHours(0, 0, 0, 0);
    return base;
}

function formatTimelineWeekInputValue(inputDate) {
    const monday = getTimelineIsoWeekStart(inputDate);
    if (!monday) return '';
    const thursday = new Date(monday);
    thursday.setDate(monday.getDate() + 3);
    const isoYear = thursday.getFullYear();
    const jan4 = new Date(isoYear, 0, 4);
    const firstIsoWeekMonday = getTimelineIsoWeekStart(jan4);
    const diffDays = Math.round((monday.getTime() - firstIsoWeekMonday.getTime()) / 86400000);
    const isoWeek = Math.floor(diffDays / 7) + 1;
    return `${isoYear}-W${String(Math.max(isoWeek, 1)).padStart(2, '0')}`;
}

function formatTimelineMonthInputValue(inputDate) {
    const base = inputDate instanceof Date ? new Date(inputDate) : new Date(inputDate || Date.now());
    if (Number.isNaN(base.getTime())) return '';
    const yyyy = base.getFullYear();
    const mm = String(base.getMonth() + 1).padStart(2, '0');
    return `${yyyy}-${mm}`;
}

function formatTimelineVisibleRangeLabel(inputDate, mode = timelineState.viewMode) {
    const scale = normalizeTimelineScaleToken(mode);
    const base = inputDate instanceof Date ? new Date(inputDate) : new Date(inputDate || Date.now());
    if (Number.isNaN(base.getTime())) return '';
    base.setHours(0, 0, 0, 0);

    if (scale === 'week') {
        const monday = getTimelineIsoWeekStart(base);
        const sunday = new Date(monday);
        sunday.setDate(monday.getDate() + 6);
        return `${formatShortDate(monday)} - ${formatShortDate(sunday)}`;
    }

    if (scale === 'month') {
        return base.toLocaleDateString(undefined, { month: 'long', year: 'numeric' });
    }

    return base.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
}

function parseTimelineWeekInputValue(value) {
    const match = String(value || '').trim().match(/^(\d{4})-W(\d{2})$/i);
    if (!match) return null;
    const isoYear = Number(match[1]);
    const isoWeek = Number(match[2]);
    if (!Number.isInteger(isoYear) || !Number.isInteger(isoWeek) || isoWeek < 1 || isoWeek > 53) {
        return null;
    }
    const jan4 = new Date(isoYear, 0, 4);
    const firstIsoWeekMonday = getTimelineIsoWeekStart(jan4);
    const monday = new Date(firstIsoWeekMonday);
    monday.setDate(firstIsoWeekMonday.getDate() + ((isoWeek - 1) * 7));
    monday.setHours(0, 0, 0, 0);
    return monday;
}

function parseTimelineMonthInputValue(value) {
    const match = String(value || '').trim().match(/^(\d{4})-(\d{2})$/);
    if (!match) return null;
    const year = Number(match[1]);
    const month = Number(match[2]);
    if (!Number.isInteger(year) || !Number.isInteger(month) || month < 1 || month > 12) {
        return null;
    }
    const parsed = new Date(year, month - 1, 1);
    parsed.setHours(0, 0, 0, 0);
    return parsed;
}

function getSavedPlannerWorkspaceDate() {
    const state = getPersistedPlannerWorkspaceState();
    if (!state || !state.date) return null;
    const match = String(state.date).match(/^(\d{4})-(\d{2})-(\d{2})$/);
    if (!match) return null;
    const parsed = new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]));
    if (!Number.isFinite(parsed.getTime()) || parsed.getFullYear() < 2020) return null;
    parsed.setHours(0, 0, 0, 0);
    return parsed;
}

function restorePlannerWorkspaceScrollIfNeeded() {
    if (!plannerWorkspaceStateRestorePending) return;
    const state = getPersistedPlannerWorkspaceState();
    if (!state) {
        plannerWorkspaceStateRestorePending = false;
        return;
    }

    const scrollWrapper = document.getElementById('timelineScrollWrapper');
    if (scrollWrapper) {
        scrollWrapper.scrollLeft = Math.max(0, Number(state.timelineScrollLeft) || 0);
        scrollWrapper.scrollTop = Math.max(0, Number(state.timelineScrollTop) || 0);
    }

    const mainContent = document.getElementById('main-content');
    if (mainContent) {
        mainContent.scrollTop = Math.max(0, Number(state.mainScrollTop) || 0);
    }

    plannerWorkspaceStateRestorePending = false;
}

function applyPlannerWorkspaceInputsFromState() {
    const state = getPersistedPlannerWorkspaceState();
    if (!state) return false;

    let normalizedWorkspacePatch = null;

    plannerWorkspaceStateRestoreInProgress = true;
    try {
        timelineState.viewMode = normalizeTimelineScaleToken(state.scale || timelineState.viewMode);
        timelineState.shiftFilter = normalizeTimelineShiftToken(state.shift || timelineState.shiftFilter);

        const datePicker = document.getElementById('timelineDate');
        const stageSelect = document.getElementById('timelineStageFilter');
        const machineSelect = document.getElementById('timelineMachineFilter');
        const statusSelect = document.getElementById('timelineFilter');
        const globalSearch = document.getElementById('globalSmartSearch');
        const smartSearch = document.getElementById('timelineSmartSearch');
        const localSearch = document.getElementById('timelineSearch');

        if (datePicker && state.date) {
            datePicker.value = state.date;
        }
        const weekPicker = document.getElementById('timelineWeekPicker');
        const savedDate = getSavedPlannerWorkspaceDate();
        if (weekPicker && savedDate) {
            weekPicker.value = formatTimelineWeekInputValue(savedDate);
        }
        const monthPicker = document.getElementById('timelineMonthPicker');
        if (monthPicker && savedDate) {
            monthPicker.value = formatTimelineMonthInputValue(savedDate);
        }
        const requestedMachineActivity = state.machineActivity || 'all';
        if (machineSelect) {
            machineSelect.value = Array.from(machineSelect.options || []).some((opt) => opt.value === requestedMachineActivity)
                ? requestedMachineActivity
                : 'all';
        }
        const resolvedMachineActivity = machineSelect ? (machineSelect.value || 'all') : 'all';

        const requestedStage = state.stage || 'all';
        if (stageSelect) {
            stageSelect.value = Array.from(stageSelect.options || []).some((opt) => opt.value === requestedStage)
                ? requestedStage
                : 'all';
        }
        const resolvedStage = stageSelect ? (stageSelect.value || 'all') : 'all';

        const requestedStatus = normalizeTimelineStatusFilterToken(state.status || 'all');
        if (statusSelect) {
            statusSelect.value = Array.from(statusSelect.options || []).some((opt) => opt.value === requestedStatus)
                ? requestedStatus
                : 'all';
        }
        const normalizedStatus = normalizeTimelineStatusFilterToken(statusSelect ? (statusSelect.value || 'all') : requestedStatus);

        const searchValue = String(state.search || '');
        if (globalSearch) globalSearch.value = searchValue;
        if (smartSearch) smartSearch.value = searchValue;
        if (localSearch) localSearch.value = searchValue;

        filterState.status = normalizedStatus;
        filterState.stage = resolvedStage;
        filterState.machineActivity = resolvedMachineActivity;
        filterState.search = searchValue.toLowerCase();

        normalizedWorkspacePatch = {
            status: normalizedStatus,
            stage: resolvedStage,
            machineActivity: resolvedMachineActivity,
            search: searchValue,
        };

        if (savedDate) {
            timelineState.startDate = savedDate;
            calculateEndDate();
        }

        if (Number(state.timelineScrollLeft) || Number(state.timelineScrollTop) || Number(state.mainScrollTop)) {
            plannerWorkspaceStateRestorePending = true;
            window.hasAutoScrolled = true;
        }
    } finally {
        plannerWorkspaceStateRestoreInProgress = false;
    }

    applyTimelineControlUI();
    if (normalizedWorkspacePatch) {
        persistPlannerWorkspaceStatePatch(normalizedWorkspacePatch);
    }
    return true;
}

window.restorePlannerWorkspaceRuntimeState = function () {
    const restored = applyPlannerWorkspaceInputsFromState();
    if (!restored) return false;
    const state = getPersistedPlannerWorkspaceState() || {};

    window.requestAnimationFrame(() => {
        window.requestAnimationFrame(() => {
            if (document.getElementById('customTimeline')) {
                renderTimeline();
            }
            if (state.search && typeof window.filterTimeline === 'function') {
                suppressTimelineSearchAutoScroll = true;
                window.filterTimeline();
                suppressTimelineSearchAutoScroll = false;
            }
            restorePlannerWorkspaceScrollIfNeeded();
            if (typeof window.syncPlannerFilterDrawer === 'function') {
                window.syncPlannerFilterDrawer();
            }
        });
    });

    return true;
};

function getTimelineMaximizedTopOffset() {
    const mainContent = document.getElementById('main-content');
    if (mainContent) {
        const rect = mainContent.getBoundingClientRect();
        if (Number.isFinite(rect.top)) {
            return Math.max(Math.round(rect.top + 8), 8);
        }
    }

    const appTopNav = document.getElementById('app-top-nav');
    if (appTopNav) {
        const rect = appTopNav.getBoundingClientRect();
        if (Number.isFinite(rect.bottom)) {
            return Math.max(Math.round(rect.bottom + 8), 8);
        }
    }

    return 12;
}

function setTimelineOverflowMenu(open) {
    timelineOverflowMenuOpen = !!open;
    const panel = document.getElementById('timelineOverflowPanel');
    const toggle = document.getElementById('timelineOverflowToggle');
    if (panel) {
        panel.classList.toggle('hidden', !timelineOverflowMenuOpen);
    }
    if (toggle) {
        toggle.setAttribute('aria-expanded', timelineOverflowMenuOpen ? 'true' : 'false');
    }
}

function applyTimelineControlUI() {
    const editBtn = document.getElementById('timelineEditModeToggle');
    const editIcon = document.getElementById('timelineEditModeIcon');
    if (editBtn) {
        editBtn.setAttribute('aria-pressed', timelineEditModeEnabled ? 'true' : 'false');
        if (timelineEditModeEnabled) {
            editBtn.classList.add('text-emerald-700', 'border-emerald-200', 'bg-emerald-50');
            editBtn.classList.remove('text-slate-600', 'border-slate-200', 'bg-white');
            editBtn.title = 'Edit mode enabled';
        } else {
            editBtn.classList.remove('text-emerald-700', 'border-emerald-200', 'bg-emerald-50');
            editBtn.classList.add('text-slate-600', 'border-slate-200', 'bg-white');
            editBtn.title = 'Edit mode disabled';
        }
    }
    if (editIcon) {
        editIcon.className = timelineEditModeEnabled ? 'ph ph-lock-open text-lg' : 'ph ph-lock text-lg';
    }

    const snapSelect = document.getElementById('timelineSnapMinutes');
    if (snapSelect) {
        snapSelect.value = normalizeSnapOverride(timelineSnapMinutesOverride);
        snapSelect.disabled = !timelineSnapEnabled;
        snapSelect.classList.toggle('opacity-50', !timelineSnapEnabled);
        snapSelect.classList.toggle('cursor-not-allowed', !timelineSnapEnabled);
    }

    const snapBtn = document.getElementById('timelineSnapToggle');
    if (snapBtn) {
        snapBtn.setAttribute('aria-pressed', timelineSnapEnabled ? 'true' : 'false');
        snapBtn.classList.remove('text-blue-700', 'border-blue-200', 'bg-blue-50', 'text-slate-600', 'border-slate-200', 'bg-white');
        if (timelineSnapEnabled) {
            snapBtn.classList.add('text-blue-700', 'border-blue-200', 'bg-blue-50');
            snapBtn.title = 'Snap now';
        } else {
            snapBtn.classList.add('text-slate-600', 'border-slate-200', 'bg-white');
            snapBtn.title = 'Enable snap';
        }
    }

    const primaryViewSelect = document.getElementById('plannerPrimaryView');
    if (primaryViewSelect) {
        const activeView = normalizeTimelinePrimaryView(getTimelinePrimaryViewState());
        const availableViews = getAvailableTimelinePrimaryViews();
        Array.from(primaryViewSelect.options || []).forEach((opt) => {
            opt.disabled = !availableViews.includes(String(opt.value || '').toLowerCase());
        });
        if (Array.from(primaryViewSelect.options || []).some((opt) => String(opt.value || '').toLowerCase() === activeView)) {
            primaryViewSelect.value = activeView;
        }
    }

    const rail = document.getElementById('timelineControlRail');
    if (rail) {
        rail.classList.remove('timeline-controls-minimized');
    }

    const dateField = document.getElementById('timelineDateField');
    const weekField = document.getElementById('timelineWeekField');
    const monthField = document.getElementById('timelineMonthField');
    const prevButton = document.getElementById('timelineWindowPrevButton');
    const nextButton = document.getElementById('timelineWindowNextButton');
    const isWeekMode = timelineState.viewMode === 'week';
    const isMonthMode = timelineState.viewMode === 'month';
    const isDayMode = !isWeekMode && !isMonthMode;
    if (dateField) {
        dateField.classList.toggle('hidden', !isDayMode);
    }
    if (weekField) {
        weekField.classList.toggle('hidden', !isWeekMode);
    }
    if (monthField) {
        monthField.classList.toggle('hidden', !isMonthMode);
    }
    const unitLabel = isMonthMode ? 'month' : (isWeekMode ? 'week' : 'day');
    if (prevButton) {
        prevButton.title = `Previous ${unitLabel}`;
        prevButton.setAttribute('aria-label', `Previous ${unitLabel}`);
    }
    if (nextButton) {
        nextButton.title = `Next ${unitLabel}`;
        nextButton.setAttribute('aria-label', `Next ${unitLabel}`);
    }

    const datePicker = document.getElementById('timelineDate');
    if (datePicker) {
        datePicker.value = formatTimelineDateInputValue(timelineState.startDate);
    }
    const weekPicker = document.getElementById('timelineWeekPicker');
    if (weekPicker) {
        weekPicker.value = formatTimelineWeekInputValue(timelineState.startDate);
    }
    const monthPicker = document.getElementById('timelineMonthPicker');
    if (monthPicker) {
        monthPicker.value = formatTimelineMonthInputValue(timelineState.startDate);
    }

    const visibleRangeLabel = document.getElementById('timelineVisibleRangeLabel');
    if (visibleRangeLabel) {
        visibleRangeLabel.textContent = formatTimelineVisibleRangeLabel(timelineState.startDate, timelineState.viewMode);
    }

    [
        { id: 'timelineScaleDayToggle', mode: 'day', title: 'Switch to day scale' },
        { id: 'timelineScaleWeekToggle', mode: 'week', title: 'Switch to week scale' },
        { id: 'timelineScaleMonthToggle', mode: 'month', title: 'Switch to month scale' },
    ].forEach(({ id, mode, title }) => {
        const button = document.getElementById(id);
        if (!button) return;
        const active = timelineState.viewMode === mode;
        button.setAttribute('aria-pressed', active ? 'true' : 'false');
        button.classList.toggle('text-blue-700', active);
        button.classList.toggle('border-blue-200', active);
        button.classList.toggle('bg-blue-50', active);
        button.classList.toggle('text-slate-600', !active);
        button.classList.toggle('border-slate-200', !active);
        button.classList.toggle('bg-white', !active);
        button.title = active ? `${title} (active)` : title;
    });

    const root = document.getElementById('timelineRoot');
    if (root) {
        root.classList.toggle('timeline-maximized', !!timelineMaximized);
        if (timelineMaximized) {
            root.style.setProperty('--timeline-max-top', `${getTimelineMaximizedTopOffset()}px`);
        } else {
            root.style.removeProperty('--timeline-max-top');
        }
    }

    const plannerShell = document.getElementById('plannerViewContainer');
    if (plannerShell) {
        plannerShell.classList.toggle('timeline-shell-maximized', !!timelineMaximized);
        if (timelineMaximized) {
            plannerShell.style.setProperty('--timeline-max-top', `${getTimelineMaximizedTopOffset()}px`);
        } else {
            plannerShell.style.removeProperty('--timeline-max-top');
        }
    }

    if (document.body) {
        document.body.classList.toggle('timeline-maximized-app', !!timelineMaximized);
        if (timelineMaximized) {
            if (document.body.dataset.timelineOverflowBeforeMaximize === undefined) {
                document.body.dataset.timelineOverflowBeforeMaximize = document.body.style.overflow || '';
            }
            document.body.style.overflow = 'hidden';
        } else if (document.body.dataset.timelineOverflowBeforeMaximize !== undefined) {
            document.body.style.overflow = document.body.dataset.timelineOverflowBeforeMaximize;
            delete document.body.dataset.timelineOverflowBeforeMaximize;
        }
    }

    const maxBtn = document.getElementById('timelineMaximizeToggle');
    const maxIcon = document.getElementById('timelineMaximizeIcon');
    if (maxBtn) {
        maxBtn.setAttribute('aria-pressed', timelineMaximized ? 'true' : 'false');
        if (timelineMaximized) {
            maxBtn.classList.add('text-emerald-700', 'border-emerald-200', 'bg-emerald-50');
            maxBtn.classList.remove('text-slate-600', 'border-slate-200', 'bg-white');
            maxBtn.title = 'Minimize timeline';
        } else {
            maxBtn.classList.remove('text-emerald-700', 'border-emerald-200', 'bg-emerald-50');
            maxBtn.classList.add('text-slate-600', 'border-slate-200', 'bg-white');
            maxBtn.title = 'Maximize timeline';
        }
    }
    if (maxIcon) {
        maxIcon.className = timelineMaximized ? 'ph ph-arrows-in text-lg' : 'ph ph-arrows-out text-lg';
    }

    const queueBtn = document.getElementById('timelineQueueRailToggle');
    const queueIcon = document.getElementById('timelineQueueRailIcon');
    const queueOverflowToggle = document.getElementById('timelineOverflowQueueToggle');
    const compactOverflowToggle = document.getElementById('timelineOverflowCompactToggle');
    const progressOverflowToggle = document.getElementById('timelineOverflowProgressToggle');
    const assigneeOverflowToggle = document.getElementById('timelineOverflowAssigneeToggle');
    const nonWorkingOverflowToggle = document.getElementById('timelineOverflowNonWorkingToggle');
    if (queueBtn) {
        const queueSupported = hasWorkspaceProperty('showQueueRail') || hasWorkspaceProperty('toggleQueueRail');
        const queueOpen = getQueueRailState();
        queueBtn.disabled = !queueSupported;
        queueBtn.setAttribute('aria-pressed', queueOpen === false ? 'false' : 'true');
        queueBtn.classList.toggle('opacity-50', !queueSupported);
        queueBtn.classList.toggle('cursor-not-allowed', !queueSupported);
        queueBtn.classList.remove('text-blue-700', 'border-blue-200', 'bg-blue-50', 'text-slate-700', 'border-slate-300');
        if (!queueSupported) {
            queueBtn.title = 'Queue panel is not available in this screen';
            queueBtn.classList.add('text-slate-400');
        } else if (queueOpen === false) {
            queueBtn.classList.remove('text-slate-400');
            queueBtn.title = 'Show queue panel';
            queueBtn.classList.add('text-slate-700', 'border-slate-300');
        } else {
            queueBtn.classList.remove('text-slate-400');
            queueBtn.title = 'Hide queue panel';
            queueBtn.classList.add('text-blue-700', 'border-blue-200', 'bg-blue-50');
        }
    }
    if (queueIcon) {
        queueIcon.className = 'ph ph-sidebar-simple text-base';
    }
    if (queueOverflowToggle) {
        const queueSupported = hasWorkspaceProperty('showQueueRail') || hasWorkspaceProperty('toggleQueueRail');
        const queueOpen = getQueueRailState();
        queueOverflowToggle.disabled = !queueSupported;
        queueOverflowToggle.classList.toggle('opacity-50', !queueSupported);
        queueOverflowToggle.classList.toggle('cursor-not-allowed', !queueSupported);
        if (!queueSupported) {
            queueOverflowToggle.textContent = 'Pending Queue Unavailable';
        } else if (queueOpen === false) {
            queueOverflowToggle.textContent = 'Show Pending Queue';
        } else {
            queueOverflowToggle.textContent = 'Hide Pending Queue';
        }
    }

    if (compactOverflowToggle) {
        compactOverflowToggle.textContent = timelineState.rowDensity === 'compact' ? 'Comfortable Density' : 'Compact Mode';
    }

    if (progressOverflowToggle) {
        progressOverflowToggle.textContent = timelineShowProgress ? 'Hide Progress' : 'Show Progress';
    }

    if (assigneeOverflowToggle) {
        assigneeOverflowToggle.textContent = timelineShowAssignee ? 'Hide Assignee' : 'Show Assignee';
    }

    if (nonWorkingOverflowToggle) {
        nonWorkingOverflowToggle.textContent = timelineShowNonWorking ? 'Hide Non-working Time' : 'Show Non-working Time';
    }
}

function initializeTimelineInteractionControls() {
    const role = String(getUserRole() || '').toLowerCase();
    const roleCanEdit = ['planner', 'admin', 'supervisor'].includes(role);
    const plannerState = getPersistedPlannerWorkspaceState();

    timelineEditModeEnabled = roleCanEdit;
    timelineSnapMinutesOverride = 'auto';
    timelineSnapEnabled = true;
    timelineMaximized = false;
    timelineOptionsMinimized = false;

    if (plannerState) {
        timelineState.viewMode = normalizeTimelineScaleToken(plannerState.scale || timelineState.viewMode);
        timelineState.shiftFilter = normalizeTimelineShiftToken(plannerState.shift || timelineState.shiftFilter);
    }

    try {
        const savedEdit = localStorage.getItem(TIMELINE_EDIT_MODE_STORAGE_KEY);
        if (savedEdit === '1' || savedEdit === '0') {
            timelineEditModeEnabled = (savedEdit === '1') && roleCanEdit;
        }
    } catch (e) {
        // Ignore localStorage issues.
    }

    try {
        const savedSnap = localStorage.getItem(TIMELINE_SNAP_STORAGE_KEY);
        if (savedSnap) timelineSnapMinutesOverride = normalizeSnapOverride(savedSnap);
    } catch (e) {
        // Ignore localStorage issues.
    }

    try {
        const savedSnapEnabled = localStorage.getItem(TIMELINE_SNAP_ENABLED_STORAGE_KEY);
        if (savedSnapEnabled === '1' || savedSnapEnabled === '0') {
            timelineSnapEnabled = savedSnapEnabled === '1';
        }
    } catch (e) {
        // Ignore localStorage issues.
    }

    try {
        const savedMaximized = localStorage.getItem(TIMELINE_MAXIMIZED_STORAGE_KEY);
        if (savedMaximized === '1' || savedMaximized === '0') {
            timelineMaximized = savedMaximized === '1';
        }
    } catch (e) {
        // Ignore localStorage issues.
    }

    try {
        const savedDensity = localStorage.getItem(TIMELINE_ROW_DENSITY_STORAGE_KEY);
        if (savedDensity === 'compact' || savedDensity === 'comfort') {
            timelineState.rowDensity = savedDensity;
            timelineState.rowHeight = savedDensity === 'compact' ? 64 : 80;
        }
    } catch (e) {
        // Ignore localStorage issues.
    }

    try {
        const savedProgress = localStorage.getItem(TIMELINE_SHOW_PROGRESS_STORAGE_KEY);
        if (savedProgress === '1' || savedProgress === '0') {
            timelineShowProgress = savedProgress === '1';
        }
    } catch (e) {
        // Ignore localStorage issues.
    }

    try {
        const savedAssignee = localStorage.getItem(TIMELINE_SHOW_ASSIGNEE_STORAGE_KEY);
        if (savedAssignee === '1' || savedAssignee === '0') {
            timelineShowAssignee = savedAssignee === '1';
        }
    } catch (e) {
        // Ignore localStorage issues.
    }

    try {
        const savedNonWorking = localStorage.getItem(TIMELINE_SHOW_NON_WORKING_STORAGE_KEY);
        if (savedNonWorking === '1' || savedNonWorking === '0') {
            timelineShowNonWorking = savedNonWorking === '1';
        }
    } catch (e) {
        // Ignore localStorage issues.
    }

    try {
        const savedMachineColumnWidth = Number(localStorage.getItem(TIMELINE_MACHINE_COLUMN_WIDTH_STORAGE_KEY));
        if (Number.isFinite(savedMachineColumnWidth) && savedMachineColumnWidth > 0) {
            timelineMachineColumnWidthOverride = Math.round(savedMachineColumnWidth);
        }
    } catch (e) {
        // Ignore localStorage issues.
    }

    applyTimelineControlUI();
}

window.isTimelineEditEnabled = function () {
    return !!timelineEditModeEnabled;
};

window.setTimelineEditMode = function (enabled, persist = true) {
    const role = String(getUserRole() || '').toLowerCase();
    const roleCanEdit = ['planner', 'admin', 'supervisor'].includes(role);
    timelineEditModeEnabled = roleCanEdit && !!enabled;

    if (persist) {
        try {
            localStorage.setItem(TIMELINE_EDIT_MODE_STORAGE_KEY, timelineEditModeEnabled ? '1' : '0');
        } catch (e) {
            // Ignore localStorage issues.
        }
    }

    applyTimelineControlUI();
    if (document.getElementById('customTimeline')) {
        renderTimeline();
    }
};

window.toggleTimelineEditMode = function () {
    window.setTimelineEditMode(!timelineEditModeEnabled, true);
};

window.setTimelineSnapMinutes = function (value, persist = true) {
    timelineSnapMinutesOverride = normalizeSnapOverride(value);
    if (persist) {
        try {
            localStorage.setItem(TIMELINE_SNAP_STORAGE_KEY, timelineSnapMinutesOverride);
        } catch (e) {
            // Ignore localStorage issues.
        }
    }
    applyTimelineControlUI();
    if (document.getElementById('customTimeline')) {
        renderTimeline();
        if (timelineSnapEnabled && typeof window.applyTimelineSnapAlignment === 'function') {
            window.applyTimelineSnapAlignment();
        }
    }
};

window.setTimelineSnapEnabled = function (enabled, persist = true) {
    timelineSnapEnabled = !!enabled;
    if (persist) {
        try {
            localStorage.setItem(TIMELINE_SNAP_ENABLED_STORAGE_KEY, timelineSnapEnabled ? '1' : '0');
        } catch (e) {
            // Ignore localStorage issues.
        }
    }
    applyTimelineControlUI();
    if (document.getElementById('customTimeline')) {
        renderTimeline();
    }
};

window.toggleTimelineSnapMode = function () {
    if (!timelineSnapEnabled) {
        window.setTimelineSnapEnabled(true, true);
    }
    if (typeof window.applyTimelineSnapAlignment === 'function') {
        window.applyTimelineSnapAlignment();
    }
};

window.setTimelineMaximized = function (enabled, persist = true) {
    timelineMaximized = !!enabled;
    if (timelineMaximized) {
        setTimelineOverflowMenu(false);
    }
    if (persist) {
        try {
            localStorage.setItem(TIMELINE_MAXIMIZED_STORAGE_KEY, timelineMaximized ? '1' : '0');
        } catch (e) {
            // Ignore localStorage issues.
        }
    }
    applyTimelineControlUI();
    if (document.getElementById('customTimeline')) {
        window.hasAutoScrolled = false;
        window.requestAnimationFrame(() => {
            window.requestAnimationFrame(() => {
                if (typeof window.handleTimelineViewportChange === 'function') {
                    window.handleTimelineViewportChange();
                } else {
                    renderTimeline();
                }
            });
        });
    }
};

window.toggleTimelineMaximized = function () {
    window.setTimelineMaximized(!timelineMaximized, true);
};

window.setTimelineOptionsMinimized = function (enabled, persist = true) {
    timelineOptionsMinimized = false;
    setTimelineOverflowMenu(false);
    if (persist) {
        try {
            localStorage.removeItem(TIMELINE_OPTIONS_MINIMIZED_STORAGE_KEY);
        } catch (e) {
            // Ignore localStorage issues.
        }
    }
    applyTimelineControlUI();
    if (document.getElementById('customTimeline')) {
        window.requestAnimationFrame(() => {
            if (typeof window.handleTimelineViewportChange === 'function') {
                window.handleTimelineViewportChange();
            }
        });
    }
};

window.toggleTimelineOptionsMinimized = function () {
    window.setTimelineOptionsMinimized(false, true);
};

window.setTimelinePrimaryView = function (view) {
    const normalized = normalizeTimelinePrimaryView(view);
    const scope = getTimelineWorkspaceScope();

    if (scope && typeof scope.setPrimaryView === 'function') {
        scope.setPrimaryView(normalized);
    } else {
        setWorkspaceProperty('currentView', normalized);
        setWorkspaceProperty('scheduleView', normalized);
    }

    if (typeof window.plannerSetView === 'function') {
        window.plannerSetView(normalized);
    }

    applyTimelineControlUI();
    persistPlannerWorkspaceStatePatch({ currentView: normalized });

    if (normalized === 'gantt' && typeof window.handleTimelineViewportChange === 'function') {
        window.handleTimelineViewportChange();
    } else if (normalized === 'kanban' && typeof window.renderPlannerKanban === 'function') {
        window.renderPlannerKanban();
    } else if (normalized === 'list' && typeof window.renderPlannerList === 'function') {
        window.renderPlannerList();
    } else if (normalized === 'calendar' && typeof window.renderPlannerCalendar === 'function') {
        window.renderPlannerCalendar();
    }
};

window.toggleTimelineQueueRail = function () {
    const scope = getTimelineWorkspaceScope();
    if (!scope) return;

    if (typeof scope.toggleQueueRail === 'function') {
        scope.toggleQueueRail();
    } else if (typeof scope.showQueueRail === 'boolean') {
        scope.showQueueRail = !scope.showQueueRail;
    } else {
        return;
    }

    window.setTimeout(() => applyTimelineControlUI(), 30);
    persistPlannerWorkspaceStatePatch({ showQueueRail: getQueueRailState() });
};

window.openTimelineFilter = function () {
    const scope = getTimelineWorkspaceScope();
    if (scope && typeof scope.openFilterDrawer === 'function') {
        scope.openFilterDrawer();
        return;
    }

    const collapseInput = document.getElementById('timelineHeaderCollapse');
    const headerBlock = document.getElementById('timelineHeaderBlock');
    const headerToggle = document.getElementById('timelineHeaderToggle');
    if (collapseInput) collapseInput.checked = false;
    if (headerBlock) {
        headerBlock.classList.remove('hidden');
        headerBlock.style.display = '';
        headerBlock.dataset.collapsed = '0';
        headerBlock.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
    if (headerToggle) headerToggle.setAttribute('aria-expanded', 'true');
};

window.openTimelineAdvancedTools = function () {
    const scope = getTimelineWorkspaceScope();
    if (scope && Object.prototype.hasOwnProperty.call(scope, 'showAdvancedTools')) {
        scope.showAdvancedTools = true;
        return;
    }

    showTimelineToast('Advanced tools are available in Planner workspace.', 'info');
};

if (typeof window.openGlobalSettings !== 'function') {
    window.openGlobalSettings = function () {
        const bodyRoot = document.querySelector('body[x-data]');
        const alpineData = bodyRoot && bodyRoot._x_dataStack && bodyRoot._x_dataStack[0];
        if (alpineData && Object.prototype.hasOwnProperty.call(alpineData, 'showSettings')) {
            alpineData.showSettings = true;
            return;
        }
        window.location.href = '/manufacturing/settings/';
    };
}

window.toggleTimelineOverflowMenu = function (event) {
    if (event) {
        event.preventDefault();
        event.stopPropagation();
    }
    setTimelineOverflowMenu(!timelineOverflowMenuOpen);
    return false;
};

window.closeTimelineOverflowMenu = function () {
    setTimelineOverflowMenu(false);
    return false;
};

window.applyTimelineControlUI = applyTimelineControlUI;


// Timeline State Management
let timelineState = {
    viewMode: 'day',        // day | week | month
    shiftFilter: 'all',     // all | day | middle | night
    startDate: new Date(),  // Default to today
    endDate: new Date(),    // Calculated
    shiftConfig: null,      // Will be loaded from DOM
    weeklyHolidays: [],     // Python weekday indices: Monday=0 ... Sunday=6
    visibleSlots: [],
    rowDensity: 'comfort',  // comfort | compact
    rowHeight: 80,
    layout: {
        machineColumnWidth: 200,
        slotWidth: 100,
        totalColumns: 24,
        timeAreaWidth: 2400,
        totalTimelineWidth: 2600,
        hasHorizontalOverflow: false,
    }
};

function normalizeWeeklyHolidays(raw) {
    if (!Array.isArray(raw)) return [];
    const normalized = Array.from(new Set(
        raw
            .map((v) => Number(v))
            .filter((v) => Number.isInteger(v) && v >= 0 && v <= 6)
    ));
    return normalized.sort((a, b) => a - b);
}

function setTimelineWeeklyHolidays(raw) {
    timelineState.weeklyHolidays = normalizeWeeklyHolidays(raw);
}

function jsDayToPythonWeekday(jsDay) {
    // JS: Sunday=0..Saturday=6 => Python weekday: Monday=0..Sunday=6
    return ((Number(jsDay) + 6) % 7);
}

function isTimelineWeeklyHoliday(date) {
    if (!(date instanceof Date) || Number.isNaN(date.getTime())) return false;
    if (!Array.isArray(timelineState.weeklyHolidays) || timelineState.weeklyHolidays.length === 0) return false;
    const pyWeekday = jsDayToPythonWeekday(date.getDay());
    return timelineState.weeklyHolidays.includes(pyWeekday);
}

function alignStartDateForTimelineView(inputDate, viewMode) {
    const base = new Date(inputDate || new Date());
    if (Number.isNaN(base.getTime())) {
        const fallback = new Date();
        fallback.setHours(0, 0, 0, 0);
        return fallback;
    }

    base.setHours(0, 0, 0, 0);
    const mode = viewMode || timelineState.viewMode || 'day';

    if (mode === 'week') {
        // ISO week anchor: Monday.
        const mondayOffset = (base.getDay() + 6) % 7;
        base.setDate(base.getDate() - mondayOffset);
        base.setHours(0, 0, 0, 0);
        return base;
    }

    if (mode === 'month') {
        // Always anchor monthly window at day 1.
        base.setDate(1);
        base.setHours(0, 0, 0, 0);
        return base;
    }

    // Day mode: anchor to 00:00 and skip configured weekly holidays.
    let guard = 0;
    while (isTimelineWeeklyHoliday(base) && guard < 7) {
        base.setDate(base.getDate() + 1);
        base.setHours(0, 0, 0, 0);
        guard += 1;
    }
    return base;
}

function buildTimelineSlots() {
    const mode = timelineState.viewMode || 'day';
    const slots = [];
    const start = new Date(timelineState.startDate);

    if (Number.isNaN(start.getTime())) return slots;

    if (mode === 'day') {
        let startHour = 0;
        let endHour = 24;
        if (timelineState.shiftFilter !== 'all') {
            const window = getTimelineShiftWindow(timelineState.shiftFilter, timelineState.shiftConfig);
            startHour = window.startHour;
            endHour = window.endHour;
        }

        for (let h = startHour; h < endHour; h++) {
            const slotStart = new Date(start);
            slotStart.setHours(0, 0, 0, 0);
            slotStart.setHours(h, 0, 0, 0);
            const slotEnd = new Date(slotStart.getTime() + (60 * 60 * 1000));
            slots.push({
                index: slots.length,
                startMs: slotStart.getTime(),
                endMs: slotEnd.getTime(),
                date: slotStart,
                unit: 'hour',
            });
        }
        return slots;
    }

    if (mode === 'week') {
        for (let i = 0; i < 7; i++) {
            const slotStart = new Date(start);
            slotStart.setDate(start.getDate() + i);
            slotStart.setHours(0, 0, 0, 0);
            if (isTimelineWeeklyHoliday(slotStart)) continue;
            const slotEnd = new Date(slotStart);
            slotEnd.setDate(slotStart.getDate() + 1);
            slots.push({
                index: slots.length,
                startMs: slotStart.getTime(),
                endMs: slotEnd.getTime(),
                date: slotStart,
                unit: 'day',
            });
        }
        return slots.length ? slots : [];
    }

    // Month
    const monthStart = new Date(start);
    monthStart.setDate(1);
    monthStart.setHours(0, 0, 0, 0);
    const monthEnd = new Date(monthStart.getFullYear(), monthStart.getMonth() + 1, 1);
    for (let d = new Date(monthStart); d < monthEnd; d.setDate(d.getDate() + 1)) {
        const slotStart = new Date(d);
        slotStart.setHours(0, 0, 0, 0);
        if (isTimelineWeeklyHoliday(slotStart)) continue;
        const slotEnd = new Date(slotStart);
        slotEnd.setDate(slotStart.getDate() + 1);
        slots.push({
            index: slots.length,
            startMs: slotStart.getTime(),
            endMs: slotEnd.getTime(),
            date: slotStart,
            unit: 'day',
        });
    }
    return slots;
}

function parseTimelineHourValue(timeStr) {
    const raw = String(timeStr || '0').trim();
    const parts = raw.split(':');
    const hours = Number(parts[0]) || 0;
    const minutes = Number(parts[1]) || 0;
    return Math.max(0, Math.min(24, hours + (minutes / 60)));
}

function normalizeTimelineShiftConfig(rawConfig) {
    const source = (rawConfig && typeof rawConfig === 'object') ? rawConfig : {};
    const afternoonSource = source.afternoon || source.evening || {};
    return {
        morning: { start: source.morning?.start || '06:00', end: source.morning?.end || '14:00', enabled: source.morning?.enabled !== false },
        afternoon: { start: afternoonSource.start || '14:00', end: afternoonSource.end || '22:00', enabled: afternoonSource.enabled !== false },
        night: { start: source.night?.start || '22:00', end: source.night?.end || '06:00', enabled: source.night?.enabled !== false },
    };
}

function getTimelineShiftWindow(shiftFilter = null, rawConfig = null) {
    const filter = shiftFilter || timelineState.shiftFilter || 'all';
    const config = normalizeTimelineShiftConfig(rawConfig || timelineState.shiftConfig);
    const requestedKey = filter === 'day'
        ? 'morning'
        : filter === 'middle'
            ? 'afternoon'
            : 'night';
    const selectedKey = config[requestedKey]?.enabled === false
        ? (['morning', 'afternoon', 'night'].find((key) => config[key]?.enabled !== false) || requestedKey)
        : requestedKey;
    const selectedShift = config[selectedKey] || config[requestedKey] || config.morning || { start: '00:00', end: '24:00' };
    const startHour = parseTimelineHourValue(selectedShift.start);
    let endHour = parseTimelineHourValue(selectedShift.end);
    if (endHour <= startHour) endHour += 24;
    return {
        selectedKey,
        startHour,
        endHour,
        durationHours: Math.max(endHour - startHour, 1),
    };
}

function getTimelineShiftRanges(rawConfig = null) {
    const config = normalizeTimelineShiftConfig(rawConfig || timelineState.shiftConfig || {
        morning: { start: '06:00', end: '14:00' },
        afternoon: { start: '14:00', end: '22:00' },
        night: { start: '22:00', end: '06:00' }
    });

    return ['morning', 'afternoon', 'night']
        .map((key) => {
            if (config[key]?.enabled === false) return null;
            const start = parseTimelineHourValue(config[key]?.start);
            let end = parseTimelineHourValue(config[key]?.end);
            if (end <= start) end += 24;
            return { start, end };
        })
        .filter((range) => range && Number.isFinite(range.start) && Number.isFinite(range.end) && range.end > range.start);
}

function isTimelineHourCoveredByShift(hour, ranges) {
    const probe = Number(hour);
    if (!Number.isFinite(probe)) return false;
    return (ranges || []).some((range) => {
        if (!range || !Number.isFinite(range.start) || !Number.isFinite(range.end)) return false;
        const value = probe < range.start ? probe + 24 : probe;
        return value >= range.start && value < range.end;
    });
}

function isTimelineSlotNonWorking(slot, machine = null) {
    if (!timelineShowNonWorking || !slot) return false;

    const slotDate = new Date(slot.startMs);
    if (Number.isNaN(slotDate.getTime())) return false;

    if (isTimelineWeeklyHoliday(slotDate)) return true;

    if ((timelineState.viewMode || 'day') !== 'day') return false;
    if ((timelineState.shiftFilter || 'all') !== 'all') return false;

    const ranges = getTimelineShiftRanges(machine?.shift_configuration || null);
    if (!ranges.length) return false;

    const slotHour = slotDate.getHours() + (slotDate.getMinutes() / 60);
    return !isTimelineHourCoveredByShift(slotHour, ranges);
}

function createTimelineScale(slots, timeAreaWidth) {
    const normalized = Array.isArray(slots)
        ? slots
            .map((slot) => {
                const startMs = Number(slot?.startMs);
                const endMs = Number(slot?.endMs);
                if (!Number.isFinite(startMs) || !Number.isFinite(endMs) || endMs <= startMs) return null;
                return { ...slot, startMs, endMs, durationMs: endMs - startMs };
            })
            .filter(Boolean)
        : [];

    const fallbackStart = timelineState.startDate?.getTime?.() || Date.now();
    const fallbackEnd = timelineState.endDate?.getTime?.() || (fallbackStart + 1);

    if (!normalized.length) {
        const totalMs = Math.max(fallbackEnd - fallbackStart, 1);
        return {
            slots: [],
            slotCount: 0,
            slotWidthPx: Math.max(Number(timeAreaWidth) || 0, 1),
            windowStartMs: fallbackStart,
            windowEndMs: fallbackEnd,
            windowDurationMs: totalMs,
            getVisibleOverlapMs(startMs, endMs) {
                const overlap = Math.max(0, Math.min(endMs, fallbackEnd) - Math.max(startMs, fallbackStart));
                return overlap;
            },
            getIntervalPosition(startMs, endMs) {
                const overlap = Math.max(0, Math.min(endMs, fallbackEnd) - Math.max(startMs, fallbackStart));
                if (overlap <= 0) return null;
                const leftRatio = Math.max(0, (Math.max(startMs, fallbackStart) - fallbackStart) / totalMs);
                const widthRatio = overlap / totalMs;
                return {
                    leftPx: leftRatio * timeAreaWidth,
                    widthPx: Math.max(widthRatio * timeAreaWidth, 1),
                    overlapMs: overlap,
                };
            },
            getDateFromOffsetPx(offsetPx) {
                const clamped = Math.max(0, Math.min(offsetPx, Math.max(Number(timeAreaWidth) || 0, 1)));
                const ratio = clamped / Math.max(Number(timeAreaWidth) || 1, 1);
                return new Date(fallbackStart + (ratio * totalMs));
            }
        };
    }

    const totalVisibleMs = normalized.reduce((sum, slot) => sum + slot.durationMs, 0);
    const slotCount = normalized.length;
    const safeWidth = Math.max(Number(timeAreaWidth) || 0, 1);
    const slotWidthPx = safeWidth / Math.max(slotCount, 1);
    const windowStartMs = normalized[0].startMs;
    const windowEndMs = normalized[slotCount - 1].endMs;

    function getVisibleOverlapMs(startMs, endMs) {
        if (!Number.isFinite(startMs) || !Number.isFinite(endMs) || endMs <= startMs) return 0;
        let total = 0;
        for (const slot of normalized) {
            if (endMs <= slot.startMs) break;
            if (startMs >= slot.endMs) continue;
            total += Math.max(0, Math.min(endMs, slot.endMs) - Math.max(startMs, slot.startMs));
        }
        return total;
    }

    function getVisibleMsBefore(timestampMs) {
        if (!Number.isFinite(timestampMs)) return 0;
        if (timestampMs <= windowStartMs) return 0;
        let total = 0;
        for (const slot of normalized) {
            if (timestampMs <= slot.startMs) break;
            if (timestampMs >= slot.endMs) {
                total += slot.durationMs;
                continue;
            }
            total += Math.max(0, timestampMs - slot.startMs);
            break;
        }
        return total;
    }

    function getIntervalPosition(startMs, endMs) {
        const overlapMs = getVisibleOverlapMs(startMs, endMs);
        if (overlapMs <= 0) return null;
        const visibleBefore = getVisibleMsBefore(startMs);
        const leftPx = (visibleBefore / totalVisibleMs) * safeWidth;
        const widthPx = Math.max((overlapMs / totalVisibleMs) * safeWidth, 1);
        return { leftPx, widthPx, overlapMs };
    }

    function getDateFromOffsetPx(offsetPx) {
        const clamped = Math.max(0, Math.min(offsetPx, safeWidth));
        let slotIndex = Math.floor(clamped / slotWidthPx);
        if (!Number.isFinite(slotIndex)) slotIndex = 0;
        slotIndex = Math.max(0, Math.min(slotIndex, slotCount - 1));
        const slot = normalized[slotIndex];
        const slotStartPx = slotIndex * slotWidthPx;
        const inSlotRatio = Math.max(0, Math.min((clamped - slotStartPx) / Math.max(slotWidthPx, 1), 1));
        return new Date(slot.startMs + (inSlotRatio * slot.durationMs));
    }

    return {
        slots: normalized,
        slotCount,
        slotWidthPx,
        windowStartMs,
        windowEndMs,
        windowDurationMs: totalVisibleMs,
        getVisibleOverlapMs,
        getIntervalPosition,
        getDateFromOffsetPx,
    };
}

function getTimelineLayoutMetrics(totalColumns) {
    const scrollWrapper = document.getElementById('timelineScrollWrapper');
    const container = document.getElementById('customTimeline');
    const wrapperWidth = Math.floor(
        scrollWrapper?.clientWidth
        || scrollWrapper?.getBoundingClientRect().width
        || container?.parentElement?.clientWidth
        || container?.parentElement?.getBoundingClientRect().width
        || container?.clientWidth
        || container?.getBoundingClientRect().width
        || 0
    );
    const viewportFallback = Math.max(Math.min((window.innerWidth || 1200) - 32, 1200), 320);
    const usableWidth = wrapperWidth > 0 ? wrapperWidth : viewportFallback;

    let minMachineWidth = 120;
    let maxMachineWidth = 180;
    let preferredRatio = 0.15;
    let minSlotWidth = 40;

    if (timelineState.viewMode === 'week') {
        minMachineWidth = 118;
        maxMachineWidth = 164;
        preferredRatio = 0.13;
        minSlotWidth = 72;
    } else if (timelineState.viewMode === 'month') {
        minMachineWidth = 108;
        maxMachineWidth = 136;
        preferredRatio = 0.1;
        minSlotWidth = 30;
    }

    if (usableWidth <= 640) {
        minMachineWidth = timelineState.viewMode === 'month' ? 84 : 92;
        maxMachineWidth = timelineState.viewMode === 'month' ? 116 : 128;
        preferredRatio = timelineState.viewMode === 'month' ? 0.24 : 0.28;
        if (timelineState.viewMode === 'day') minSlotWidth = 32;
        if (timelineState.viewMode === 'week') minSlotWidth = 56;
        if (timelineState.viewMode === 'month') minSlotWidth = 24;
    }

    const maxManualMachineWidth = Math.min(
        Math.max(minMachineWidth + 88, Math.round(usableWidth * 0.58)),
        560
    );

    let machineColumnWidth = Number(timelineMachineColumnWidthOverride);
    if (Number.isFinite(machineColumnWidth) && machineColumnWidth > 0) {
        machineColumnWidth = Math.max(minMachineWidth, Math.min(maxManualMachineWidth, Math.round(machineColumnWidth)));
    } else {
        machineColumnWidth = Math.round(usableWidth * preferredRatio);
        machineColumnWidth = Math.max(minMachineWidth, Math.min(maxMachineWidth, machineColumnWidth));
        machineColumnWidth = Math.min(machineColumnWidth, Math.max(minMachineWidth, Math.floor(usableWidth * 0.2)));
    }

    const baseTimeAreaWidth = Math.max(usableWidth - machineColumnWidth, 120);
    const autoSlotWidth = baseTimeAreaWidth / Math.max(totalColumns, 1);
    const slotWidth = Math.max(autoSlotWidth, minSlotWidth);
    const timeAreaWidth = slotWidth * Math.max(totalColumns, 1);
    const totalTimelineWidth = machineColumnWidth + timeAreaWidth;
    const hasHorizontalOverflow = totalTimelineWidth > (usableWidth + 1);

    return {
        machineColumnWidth,
        slotWidth,
        totalColumns,
        timeAreaWidth,
        totalTimelineWidth,
        usableWidth,
        hasHorizontalOverflow,
    };
}

window.setRowDensity = function (mode) {
    timelineState.rowDensity = mode === 'compact' ? 'compact' : 'comfort';
    timelineState.rowHeight = timelineState.rowDensity === 'compact' ? 64 : 80;
    try {
        localStorage.setItem(TIMELINE_ROW_DENSITY_STORAGE_KEY, timelineState.rowDensity);
    } catch (e) {
        // Ignore localStorage issues.
    }
    applyTimelineControlUI();
    renderTimeline();
};

let timelineResourceResizeRaf = null;

function scheduleTimelineResourceResizeRender() {
    if (timelineResourceResizeRaf) {
        window.cancelAnimationFrame(timelineResourceResizeRaf);
    }
    timelineResourceResizeRaf = window.requestAnimationFrame(() => {
        timelineResourceResizeRaf = null;
        if (document.getElementById('customTimeline')) {
            renderTimeline();
        }
    });
}

function setTimelineMachineColumnWidth(width, { persist = false, rerender = true } = {}) {
    const parsedWidth = Number(width);
    if (!Number.isFinite(parsedWidth) || parsedWidth <= 0) return;
    timelineMachineColumnWidthOverride = Math.round(parsedWidth);
    if (persist) {
        try {
            localStorage.setItem(TIMELINE_MACHINE_COLUMN_WIDTH_STORAGE_KEY, String(timelineMachineColumnWidthOverride));
        } catch (e) {
            // Ignore localStorage issues.
        }
    }
    if (rerender) {
        scheduleTimelineResourceResizeRender();
    }
}

function resetTimelineMachineColumnWidth({ rerender = true } = {}) {
    timelineMachineColumnWidthOverride = null;
    try {
        localStorage.removeItem(TIMELINE_MACHINE_COLUMN_WIDTH_STORAGE_KEY);
    } catch (e) {
        // Ignore localStorage issues.
    }
    if (rerender) {
        scheduleTimelineResourceResizeRender();
    }
}

function attachTimelineResourceColumnResize(handle) {
    if (!handle || handle.dataset.bound === '1') return;
    handle.dataset.bound = '1';

    handle.addEventListener('dblclick', (event) => {
        event.preventDefault();
        event.stopPropagation();
        resetTimelineMachineColumnWidth({ rerender: true });
    });

    handle.addEventListener('pointerdown', (event) => {
        if (event.pointerType !== 'touch' && event.button !== 0) return;
        event.preventDefault();
        event.stopPropagation();

        const startX = event.clientX;
        const startWidth = Number(
            timelineState.layout?.machineColumnWidth
            || timelineMachineColumnWidthOverride
            || 200
        );
        let latestWidth = startWidth;

        document.body.classList.add('timeline-resizing');
        if (typeof handle.setPointerCapture === 'function') {
            try {
                handle.setPointerCapture(event.pointerId);
            } catch (e) {
                // Ignore pointer capture failures.
            }
        }

        const onMove = (moveEvent) => {
            latestWidth = startWidth + (moveEvent.clientX - startX);
            setTimelineMachineColumnWidth(latestWidth, { persist: false, rerender: true });
        };

        const cleanup = () => {
            window.removeEventListener('pointermove', onMove);
            window.removeEventListener('pointerup', onUp);
            window.removeEventListener('pointercancel', onUp);
            document.body.classList.remove('timeline-resizing');
            setTimelineMachineColumnWidth(latestWidth, { persist: true, rerender: true });
        };

        const onUp = () => cleanup();

        window.addEventListener('pointermove', onMove);
        window.addEventListener('pointerup', onUp);
        window.addEventListener('pointercancel', onUp);
    });
}

window.setTimelineShowProgress = function (enabled, persist = true) {
    timelineShowProgress = !!enabled;
    if (persist) {
        try {
            localStorage.setItem(TIMELINE_SHOW_PROGRESS_STORAGE_KEY, timelineShowProgress ? '1' : '0');
        } catch (e) {
            // Ignore localStorage issues.
        }
    }
    applyTimelineControlUI();
    renderTimeline();
};

window.toggleTimelineShowProgress = function () {
    window.setTimelineShowProgress(!timelineShowProgress, true);
};

window.setTimelineShowAssignee = function (enabled, persist = true) {
    timelineShowAssignee = !!enabled;
    if (persist) {
        try {
            localStorage.setItem(TIMELINE_SHOW_ASSIGNEE_STORAGE_KEY, timelineShowAssignee ? '1' : '0');
        } catch (e) {
            // Ignore localStorage issues.
        }
    }
    applyTimelineControlUI();
    renderTimeline();
};

window.toggleTimelineShowAssignee = function () {
    window.setTimelineShowAssignee(!timelineShowAssignee, true);
};

window.setTimelineShowNonWorking = function (enabled, persist = true) {
    timelineShowNonWorking = !!enabled;
    if (persist) {
        try {
            localStorage.setItem(TIMELINE_SHOW_NON_WORKING_STORAGE_KEY, timelineShowNonWorking ? '1' : '0');
        } catch (e) {
            // Ignore localStorage issues.
        }
    }
    applyTimelineControlUI();
    renderTimeline();
};

window.toggleTimelineShowNonWorking = function () {
    window.setTimelineShowNonWorking(!timelineShowNonWorking, true);
};

window.toggleTimelineCompactMode = function () {
    window.setRowDensity(timelineState.rowDensity === 'compact' ? 'comfort' : 'compact');
};

window.resetTimelineLayout = function () {
    window.setRowDensity('comfort');
    window.setTimelineShowProgress(true, true);
    window.setTimelineShowAssignee(true, true);
    window.setTimelineShowNonWorking(true, true);
    window.setTimelineSnapEnabled(true, true);
    window.setTimelineSnapMinutes('auto', true);
    window.setTimelineMaximized(false, true);

    const scope = getTimelineWorkspaceScope();
    if (scope && typeof scope.showQueueRail === 'boolean') {
        scope.showQueueRail = true;
    }

    if (typeof window.setTimelinePrimaryView === 'function') {
        window.setTimelinePrimaryView('gantt');
    }

    const dateInput = document.getElementById('timelineDate');
    if (dateInput) {
        const d = new Date();
        const y = d.getFullYear();
        const m = String(d.getMonth() + 1).padStart(2, '0');
        const day = String(d.getDate()).padStart(2, '0');
        dateInput.value = `${y}-${m}-${day}`;
        dateInput.dispatchEvent(new Event('change'));
    } else {
        renderTimeline();
    }

    persistPlannerWorkspaceStatePatch({
        showQueueRail: true,
        currentView: 'gantt',
        scale: 'day',
        shift: 'all',
        date: dateInput ? dateInput.value : '',
        timelineScrollLeft: 0,
        timelineScrollTop: 0,
    });

    showTimelineToast('Timeline layout reset', 'success');
};

window.handleTimelineViewportChange = function (forceFetch = false) {
    const rerender = () => {
        const timelineEl = document.getElementById('customTimeline');
        if (!timelineEl || timelineEl.offsetParent === null) return;

        if (forceFetch && typeof window.initGanttChart === 'function') {
            window.initGanttChart(true);
            return;
        }

        if ((Array.isArray(machinesCache) && machinesCache.length > 0) || (Array.isArray(tasksCache) && tasksCache.length > 0)) {
            renderTimeline();
        } else if (typeof window.initGanttChart === 'function') {
            window.initGanttChart();
        }
    };

    window.requestAnimationFrame(() => window.requestAnimationFrame(rerender));
};

function ensureTimelineTooltip() {
    let tooltip = document.getElementById('timelineHoverCard');
    if (tooltip) return tooltip;
    tooltip = document.createElement('div');
    tooltip.id = 'timelineHoverCard';
    tooltip.setAttribute('role', 'tooltip');
    tooltip.style.position = 'fixed';
    tooltip.style.zIndex = 9999;
    tooltip.style.pointerEvents = 'none';
    tooltip.style.background = 'white';
    tooltip.style.border = '1px solid #e2e8f0';
    tooltip.style.borderRadius = '12px';
    tooltip.style.boxShadow = '0 10px 30px rgba(15,23,42,0.12)';
    tooltip.style.padding = '10px 12px';
    tooltip.style.fontSize = '12px';
    tooltip.style.color = '#0f172a';
    tooltip.style.display = 'none';
    tooltip.style.maxWidth = '340px';
    tooltip.style.lineHeight = '1.45';
    document.body.appendChild(tooltip);
    return tooltip;
}

function getDisplayWorkOrderId(task) {
    if (!task) return '';
    const rawId = task.display_work_order_id
        ?? task.displayWorkOrderId
        ?? task.parent_id
        ?? task.parentId
        ?? task.id
        ?? '';
    return String(rawId || '').trim();
}

function getDisplayWorkOrderCode(task) {
    const displayId = getDisplayWorkOrderId(task);
    return displayId ? `WO-${displayId}` : 'Work Order';
}

function getDisplayWorkOrderHashLabel(task) {
    const displayId = getDisplayWorkOrderId(task);
    return displayId ? `WO #${displayId}` : 'Work Order';
}

// Order hues by contrast instead of by raw adjacency so neighboring active WOs are easier to tell apart.
const TIMELINE_DISTINCT_BASE_HUES = [12, 198, 84, 278, 46, 238, 132, 318, 64, 218, 156, 338, 28, 258, 108, 298, 178, 358];
const TIMELINE_DISTINCT_TONE_VARIANTS = [
    { solidL: 48, solidS: 80, tintL: 84, tintS: 84, surfaceL: 95, surfaceS: 82, borderL: 66, chipL: 91, chipS: 84, chipTextL: 22, textL: 20, calendarL: 88 },
    { solidL: 42, solidS: 76, tintL: 79, tintS: 76, surfaceL: 93, surfaceS: 72, borderL: 60, chipL: 88, chipS: 76, chipTextL: 20, textL: 18, calendarL: 84 },
    { solidL: 54, solidS: 84, tintL: 86, tintS: 86, surfaceL: 96, surfaceS: 84, borderL: 70, chipL: 92, chipS: 88, chipTextL: 24, textL: 22, calendarL: 90 },
];

let timelineWorkOrderPaletteRegistry = new Map();

function getTimelinePaletteHue(seedValue) {
    const seed = String(seedValue || '').trim();
    if (!seed) return 222;
    let hash = 0;
    for (let i = 0; i < seed.length; i += 1) {
        hash = ((hash * 31) + seed.charCodeAt(i)) % 3600;
    }
    return Math.abs(hash) % 360;
}

function getTimelineWorkOrderColorSeed(task) {
    if (!task) return '';
    const displayId = getDisplayWorkOrderId(task);
    if (displayId) return `wo-${displayId}`;
    if (task.parent_id !== undefined && task.parent_id !== null && String(task.parent_id).trim() !== '') {
        return `parent-${String(task.parent_id).trim()}`;
    }
    if (task.id !== undefined && task.id !== null && String(task.id).trim() !== '') {
        return `task-${String(task.id).trim()}`;
    }
    return String(task.product || task.product_name || 'work-order').trim().toLowerCase();
}

function buildTimelineDistinctPalette(index, seed = '') {
    const safeIndex = Math.max(0, Number(index) || 0);
    const baseHue = TIMELINE_DISTINCT_BASE_HUES[safeIndex % TIMELINE_DISTINCT_BASE_HUES.length];
    const tone = TIMELINE_DISTINCT_TONE_VARIANTS[Math.floor(safeIndex / TIMELINE_DISTINCT_BASE_HUES.length) % TIMELINE_DISTINCT_TONE_VARIANTS.length];
    const overflowCycle = Math.floor(safeIndex / TIMELINE_DISTINCT_BASE_HUES.length);
    const hueJitter = overflowCycle > 0 ? (getTimelinePaletteHue(seed) % 5) - 2 : 0;
    const hue = (baseHue + hueJitter) % 360;
    const secondaryHue = (hue + 26) % 360;

    return {
        hue,
        solid: `hsl(${hue}, ${tone.solidS}%, ${tone.solidL}%)`,
        tint: `linear-gradient(135deg, hsl(${hue}, ${tone.tintS}%, ${tone.tintL}%) 0%, hsl(${secondaryHue}, ${Math.max(tone.tintS - 8, 52)}%, ${Math.max(tone.tintL - 6, 68)}%) 100%)`,
        surface: `hsl(${hue}, ${tone.surfaceS}%, ${tone.surfaceL}%)`,
        border: `hsl(${hue}, 62%, ${tone.borderL}%)`,
        chip: `hsl(${hue}, ${tone.chipS}%, ${tone.chipL}%)`,
        chipText: `hsl(${hue}, 56%, ${tone.chipTextL}%)`,
        text: `hsl(${hue}, 48%, ${tone.textL}%)`,
        calendarTint: `hsl(${hue}, 72%, ${tone.calendarL}%)`,
    };
}

function syncTimelineWorkOrderPaletteRegistry(tasks = tasksCache) {
    const taskList = Array.isArray(tasks) ? tasks : [];
    const seen = new Set();
    const activeSeeds = [];
    const inactiveSeeds = [];
    const sorter = (a, b) => String(a).localeCompare(String(b), undefined, { numeric: true, sensitivity: 'base' });

    taskList.forEach((task) => {
        const seed = getTimelineWorkOrderColorSeed(task);
        if (!seed || seen.has(seed)) return;
        seen.add(seed);
        const status = String(task?.status || '').toLowerCase();
        if (['completed', 'done', 'canceled', 'archived'].includes(status)) {
            inactiveSeeds.push(seed);
        } else {
            activeSeeds.push(seed);
        }
    });

    activeSeeds.sort(sorter);
    inactiveSeeds.sort(sorter);

    const registry = new Map();
    activeSeeds.concat(inactiveSeeds).forEach((seed, index) => {
        registry.set(seed, buildTimelineDistinctPalette(index, seed));
    });
    timelineWorkOrderPaletteRegistry = registry;
    return registry;
}

function getTimelineWorkOrderPalette(task) {
    const seed = getTimelineWorkOrderColorSeed(task);
    if (seed && timelineWorkOrderPaletteRegistry.has(seed)) {
        return timelineWorkOrderPaletteRegistry.get(seed);
    }
    const fallbackIndex = Math.abs(getTimelinePaletteHue(seed)) % TIMELINE_DISTINCT_BASE_HUES.length;
    return buildTimelineDistinctPalette(fallbackIndex, `${seed}:fallback`);
}

function getTimelineStatusBadgeStyles(status) {
    const normalized = String(status || 'pending').toLowerCase();
    if (['completed', 'done'].includes(normalized)) {
        return {
            background: '#ecfdf3',
            border: '#bbf7d0',
            color: '#166534',
        };
    }
    if (['in_progress', 'active', 'hold', 'on_hold'].includes(normalized)) {
        return {
            background: '#eff6ff',
            border: '#bfdbfe',
            color: '#1d4ed8',
        };
    }
    return {
        background: '#fef3c7',
        border: '#fde68a',
        color: '#b45309',
    };
}

function clampTimelinePercent(value) {
    const numeric = Number(value);
    if (!Number.isFinite(numeric)) return 0;
    return Math.max(0, Math.min(numeric, 100));
}

function getTimelineExpectedProgressPercent(startMs, endMs, status = '', nowMs = Date.now()) {
    const normalizedStatus = String(status || '').toLowerCase();
    if (['completed', 'done', 'canceled', 'archived'].includes(normalizedStatus)) return null;
    if (!Number.isFinite(startMs) || !Number.isFinite(endMs) || endMs <= startMs) return null;
    if (!Number.isFinite(nowMs) || nowMs < startMs) return null;
    if (nowMs >= endMs) return 100;
    return clampTimelinePercent(((nowMs - startMs) / (endMs - startMs)) * 100);
}

function getTimelineTaskProgressSnapshot(task, nowMs = Date.now()) {
    const startMs = task?.start ? new Date(task.start).getTime() : NaN;
    let endMs = task?.end ? new Date(task.end).getTime() : NaN;
    if ((!Number.isFinite(endMs) || endMs <= startMs) && Number.isFinite(startMs)) {
        const estimatedMinutes = Number(task?.estimated_duration_minutes || 60) || 60;
        endMs = startMs + Math.max(estimatedMinutes, 1) * 60000;
    }

    const quantity = Number(task?.progress_stats ? task.progress_stats.target : (task?.quantity || 0)) || 0;
    const finished = Number(task?.progress_stats ? task.progress_stats.actual : (task?.finished_qty || 0)) || 0;
    const progressValue = Number(task?.progress);
    const reportedProgress = quantity > 0 ? (finished / quantity) * 100 : 0;
    const rawProgress = reportedProgress > 0
        ? reportedProgress
        : (Number.isFinite(progressValue) ? progressValue : 0);
    const actual = clampTimelinePercent(rawProgress);
    const expected = getTimelineExpectedProgressPercent(startMs, endMs, task?.status, nowMs);
    const gap = expected === null ? 0 : Math.max(expected - actual, 0);
    const overdue = expected !== null && Number.isFinite(endMs) && nowMs >= endMs && actual < 100;
    const behind = expected !== null && gap >= 10;

    return { startMs, endMs, actual, expected, gap, overdue, behind };
}

function setupTimelineExpectedProgressTicker() {
    if (timelineExpectedProgressTimer || typeof window === 'undefined') return;
    timelineExpectedProgressTimer = window.setInterval(() => {
        const timelineRoot = document.getElementById('customTimeline');
        if (!timelineRoot) {
            window.clearInterval(timelineExpectedProgressTimer);
            timelineExpectedProgressTimer = null;
            return;
        }
        renderTimeline();
        renderPlannerFollowUpQueue();
        renderPlannerDispatchReadinessQueue();
    }, 60000);
}

function isTimelineMachineFaultState(machine) {
    const status = String(machine?.status || '').trim().toLowerCase();
    return ['fault', 'broken', 'breakdown', 'maintenance', 'down', 'offline'].includes(status) || machine?.is_active === false;
}

function getTimelineMachineLampState(machine, visibleMachineTasks = []) {
    if (isTimelineMachineFaultState(machine)) return 'fault';
    const hasLoad = (visibleMachineTasks || []).some((task) => {
        const status = String(task?.status || '').toLowerCase();
        return !['completed', 'done', 'canceled', 'archived'].includes(status);
    });
    return hasLoad ? 'loaded' : 'idle';
}

function renderTimelineMachineLamps(machine, visibleMachineTasks = []) {
    const activeState = getTimelineMachineLampState(machine, visibleMachineTasks);
    const lamp = {
        idle: { title: 'Operational - no load', className: 'bg-emerald-400 ring-emerald-200' },
        loaded: { title: 'Operational - with load', className: 'bg-sky-500 ring-sky-200' },
        fault: { title: 'Fault / unavailable', className: 'bg-rose-500 ring-rose-200' },
    }[activeState] || { title: 'Machine state unavailable', className: 'bg-slate-300 ring-slate-200' };
    return `
        <div class="mt-1 flex items-center gap-1.5" aria-label="Machine status lamp">
            <span class="inline-flex h-2.5 w-2.5 rounded-full ${lamp.className} ring-2" title="${lamp.title}"></span>
        </div>
    `;
}

function updateDrawerCycleState(cycleState, fallbackStatus = '') {
    const container = document.getElementById('drawerCycleState');
    if (!container) return;

    const labelEl = document.getElementById('drawerCycleLabel');
    const ownerEl = document.getElementById('drawerCycleOwner');
    const iconEl = document.getElementById('drawerCycleIcon');
    const label = String(
        cycleState?.next_action
        || cycleState?.label
        || (fallbackStatus ? `Status: ${fallbackStatus}` : 'Cycle state unavailable')
    ).trim();

    const blocked = !!cycleState?.blocked;
    const owner = String(cycleState?.owner_role || '').trim();
    const blocker = String(cycleState?.blocker_reason || '').trim();
    container.classList.remove(
        'hidden',
        'border-rose-200',
        'bg-rose-50',
        'text-rose-700',
        'border-slate-200',
        'bg-white',
        'text-slate-700'
    );
    container.classList.add(
        blocked ? 'border-rose-200' : 'border-slate-200',
        blocked ? 'bg-rose-50' : 'bg-white',
        blocked ? 'text-rose-700' : 'text-slate-700'
    );

    if (labelEl) labelEl.textContent = label;
    if (ownerEl) {
        ownerEl.textContent = [
            owner ? `Owner: ${owner}` : '',
            blocker ? `Blocked: ${blocker}` : '',
        ].filter(Boolean).join(' | ');
    }
    if (iconEl) {
        iconEl.className = blocked
            ? 'ph ph-warning-circle mt-0.5 text-base'
            : 'ph ph-arrow-circle-right mt-0.5 text-base';
    }
}
window.updateDrawerCycleState = updateDrawerCycleState;

function getTimelineTaskStatusLabel(status) {
    const normalized = String(status || 'pending').trim().toLowerCase();
    const labels = {
        pending: 'Pending',
        in_progress: 'In Progress',
        completed: 'Completed',
        done: 'Completed',
        hold: 'On Hold',
        canceled: 'Canceled',
        archived: 'Archived',
    };
    return labels[normalized] || normalized.replace(/_/g, ' ').replace(/\b\w/g, (char) => char.toUpperCase());
}

function formatTimelineTooltipDate(value) {
    if (!value) return 'Unscheduled';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return 'Unscheduled';
    return date.toLocaleString(undefined, {
        month: 'short',
        day: 'numeric',
        year: 'numeric',
        hour: '2-digit',
        minute: '2-digit',
    });
}

function showTimelineTooltip(task, event, options = {}) {
    if (timelineTooltipTimer) {
        window.clearTimeout(timelineTooltipTimer);
    }
    const anchorEl = options.anchorEl || event?.currentTarget || null;
    timelineTooltipTimer = window.setTimeout(() => {
        timelineTooltipTimer = null;
        renderTimelineTooltip(task, event, anchorEl);
    }, 300);
}

function renderTimelineTooltip(task, event, anchorEl = null) {
    const tooltip = ensureTimelineTooltip();
    const start = formatTimelineTooltipDate(task.start);
    const end = formatTimelineTooltipDate(task.end);
    const stage = task.stage_name || '-';
    const qty = formatQuantityBreakdown(task);
    const product = escapeHtml(task.product || task.product_name || 'Work Order');
    const woCode = escapeHtml(getDisplayWorkOrderCode(task));
    const statusLabel = escapeHtml(getTimelineTaskStatusLabel(task.status));
    const statusStyles = getTimelineStatusBadgeStyles(task.status);
    const isCompTask = !!task.is_scrap_compensation_task;
    const setupMinutes = Number(task.setup_minutes || 0);
    const estimatedMinutes = Number(task.estimated_duration_minutes || 0);
    const progressStats = task.progress_stats || {};
    const reportedQty = Number(progressStats.actual ?? task.finished_qty ?? 0) || 0;
    const approvedQty = Number(progressStats.approved ?? task.approved_qty ?? 0) || 0;
    const targetQty = Number(progressStats.target ?? task.quantity ?? 0) || 0;
    const fallbackRemainingQty = Math.max(targetQty - reportedQty, 0);
    const remainingQty = task.is_visual_group
        ? fallbackRemainingQty
        : (Number(task.remaining_qty ?? fallbackRemainingQty) || 0);
    const completedSegments = Number(task.split_group_completed || 0) || 0;
    const totalSegments = Number(task.split_group_total || 0) || 0;
    tooltip.innerHTML = `
        <div style="display:flex;align-items:flex-start;justify-content:space-between;gap:12px;margin-bottom:8px;">
            <div style="min-width:0;">
                <div style="font-weight:800;color:#0f172a;font-size:13px;line-height:1.2;max-width:260px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${product}</div>
                <div style="margin-top:3px;color:#64748b;font-size:11px;font-weight:700;">${woCode}</div>
            </div>
            <span style="white-space:nowrap;border:1px solid ${statusStyles.border};background:${statusStyles.background};color:${statusStyles.color};border-radius:999px;padding:3px 8px;font-size:10px;font-weight:900;text-transform:uppercase;letter-spacing:0.08em;">${statusLabel}</span>
        </div>
        <div style="color:#64748b;">Stage: ${escapeHtml(stage)}</div>
        <div style="color:#64748b;">Quantity: ${escapeHtml(qty)}</div>
        <div style="color:#0f172a;font-weight:600;">Completed Items: ${approvedQty}</div>
        <div style="color:#64748b;">Reported Items: ${reportedQty}</div>
        <div style="color:#64748b;">Remaining Items: ${remainingQty}</div>
        ${totalSegments > 0 ? `<div style="color:#64748b;">Completed Segments: ${completedSegments} / ${totalSegments}</div>` : ''}
        ${estimatedMinutes > 0 ? `<div style="color:#64748b;">Stage Time: ${formatManufacturingDurationFromMinutes(estimatedMinutes)}</div>` : ''}
        ${setupMinutes > 0 ? `<div style="color:#64748b;">Setup Time: ${formatManufacturingDurationFromMinutes(setupMinutes)}</div>` : ''}
        ${isCompTask ? '<div style="color:#be123c;font-weight:600;">Scrap Compensation Task</div>' : ''}
        <div style="margin-top:6px;color:#64748b;">Planned Start: ${escapeHtml(start)}</div>
        <div style="color:#64748b;">Planned End: ${escapeHtml(end)}</div>
    `;
    tooltip.style.display = 'block';
    moveTimelineTooltip(event, anchorEl);
}

function moveTimelineTooltip(event, anchorEl = null) {
    const tooltip = ensureTimelineTooltip();
    if (!tooltip || tooltip.style.display === 'none') return;
    const pad = 14;
    const eventX = Number(event?.clientX);
    const eventY = Number(event?.clientY);
    const rect = anchorEl?.getBoundingClientRect ? anchorEl.getBoundingClientRect() : null;
    const baseX = Number.isFinite(eventX) ? eventX : (rect ? rect.left + Math.min(rect.width, 24) : 24);
    const baseY = Number.isFinite(eventY) ? eventY : (rect ? rect.top : 24);
    const tooltipRect = tooltip.getBoundingClientRect();
    const tooltipWidth = Math.max(tooltipRect.width || 0, 280);
    const tooltipHeight = Math.max(tooltipRect.height || 0, 160);
    const maxX = Math.max(8, window.innerWidth - tooltipWidth - 8);
    const maxY = Math.max(8, window.innerHeight - tooltipHeight - 8);
    const x = Math.max(8, Math.min(maxX, baseX + pad));
    const y = Math.max(8, Math.min(maxY, baseY + pad));
    tooltip.style.left = `${x}px`;
    tooltip.style.top = `${y}px`;
}

function hideTimelineTooltip() {
    if (timelineTooltipTimer) {
        window.clearTimeout(timelineTooltipTimer);
        timelineTooltipTimer = null;
    }
    const tooltip = document.getElementById('timelineHoverCard');
    if (tooltip) tooltip.style.display = 'none';
}

function showTimelineContextMenu(task, event) {
    event.preventDefault();
    const existing = document.getElementById('timelineContextMenu');
    if (existing) existing.remove();

    const menu = document.createElement('div');
    menu.id = 'timelineContextMenu';
    menu.style.position = 'fixed';
    menu.style.zIndex = 9999;
    menu.style.background = 'white';
    menu.style.border = '1px solid #e2e8f0';
    menu.style.borderRadius = '10px';
    menu.style.boxShadow = '0 12px 24px rgba(15,23,42,0.15)';
    menu.style.padding = '6px';
    menu.style.minWidth = '180px';

    const actions = [
        { label: 'Open Details', fn: () => window.openEditSheet && window.openEditSheet(task.id) },
        { label: 'Split Work Order', fn: () => { if (window.openEditSheet) window.openEditSheet(task.id); if (window.openSplitModal) setTimeout(window.openSplitModal, 150); } },
        { label: 'Assign Worker', fn: () => window.openEditSheet && window.openEditSheet(task.id) },
        { label: 'Move To Next Slot', fn: () => moveToNextSlot(task) }
    ];

    actions.forEach(item => {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.textContent = item.label;
        btn.style.display = 'block';
        btn.style.width = '100%';
        btn.style.textAlign = 'left';
        btn.style.padding = '8px 10px';
        btn.style.borderRadius = '8px';
        btn.style.fontSize = '12px';
        btn.style.fontWeight = '600';
        btn.style.color = '#334155';
        btn.onmouseenter = () => btn.style.background = '#f1f5f9';
        btn.onmouseleave = () => btn.style.background = 'transparent';
        btn.onclick = () => { item.fn(); menu.remove(); };
        menu.appendChild(btn);
    });

    const x = Math.min(window.innerWidth - 200, event.clientX);
    const y = Math.min(window.innerHeight - 160, event.clientY);
    menu.style.left = `${x}px`;
    menu.style.top = `${y}px`;
    document.body.appendChild(menu);

    const close = () => {
        if (menu && menu.parentNode) menu.parentNode.removeChild(menu);
        document.removeEventListener('click', close);
    };
    setTimeout(() => document.addEventListener('click', close), 0);
}

function moveToNextSlot(task) {
    if (!task || !task.machine_id) return;
    const role = getUserRole() || 'planner';
    if (!['planner', 'admin'].includes(role)) {
        showTimelineToast('Only planners can reschedule.', 'warning');
        return;
    }
    const durationMs = task.start && task.end ? (new Date(task.end) - new Date(task.start)) : (60 * 60 * 1000);
    const sameMachine = (tasksCache || []).filter(t => t.machine_id == task.machine_id && t.start && t.end && t.id !== task.id);
    const maxEnd = sameMachine.reduce((acc, t) => {
        const end = new Date(t.end).getTime();
        return end > acc ? end : acc;
    }, Date.now());
    const startDate = new Date(maxEnd);
    fetch(`/manufacturing/api/schedule-work-order/${task.id}/`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            start_date: startDate.toISOString(),
            machine_id: task.machine_id,
            stage_id: task.stage_id || null
        })
    })
        .then(res => res.json())
        .then(data => {
            if (data.success) {
                window.initGanttChart(true);
            } else {
                showTimelineToast(data.error || 'Unable to reschedule.', 'error');
            }
        })
        .catch(err => showTimelineToast(err.message || 'Unable to reschedule.', 'error'));
}

// Load shift configuration from DOM
function loadShiftConfig() {
    const shiftConfigScript = document.getElementById('data-shift-config');
    console.log('ðŸ”§ Loading shift configuration...');
    console.log('  - Script element found:', !!shiftConfigScript);

    if (shiftConfigScript) {
        try {
            const rawText = shiftConfigScript.textContent;
            console.log('  - Raw JSON text:', rawText);

            const config = normalizeTimelineShiftConfig(JSON.parse(rawText));
            console.log('  - Parsed config:', config);

            if (config && Object.keys(config).length > 0) {
                timelineState.shiftConfig = config;
                console.log('âœ“ Shift configuration loaded successfully!');
                console.log('  - Morning start:', config.morning?.start);
                console.log('  - Morning end:', config.morning?.end);
                console.log('  - Afternoon start:', config.afternoon?.start);
                console.log('  - Night start:', config.night?.start);
            } else {
                console.warn('âš  Shift config is empty, using defaults');
            }
        } catch (e) {
            console.error('âœ— Failed to parse shift configuration:', e);
        }
    } else {
        console.warn('âš  data-shift-config script tag not found in DOM');
    }
}

// Call on page load
document.addEventListener('DOMContentLoaded', loadShiftConfig);

function getUserRole() {
    const roleScript = document.getElementById('data-user-role');
    if (!roleScript) return '';
    const raw = (roleScript.textContent || '').trim();
    if (!raw) return '';
    try {
        const parsed = JSON.parse(raw);
        return String(parsed || '').toLowerCase();
    } catch (e) {
        return raw.replace(/"/g, '').trim().toLowerCase();
    }
}

function shouldForceMachinesRefresh() {
    try {
        if (window.__plannerResetWorkspaceState) {
            return true;
        }
        const stamp = localStorage.getItem('machines_updated_at');
        if (stamp && stamp !== window.__machinesUpdatedAt) {
            window.__machinesUpdatedAt = stamp;
            return true;
        }
    } catch (e) {
        return false;
    }
    return false;
}

function syncTimelineDomScriptsFromCache() {
    const mScript = document.getElementById('data-machines');
    const tScript = document.getElementById('data-tasks');
    const sScript = document.getElementById('data-stages');
    const hScript = document.getElementById('data-weekly-holidays');
    if (mScript) mScript.textContent = JSON.stringify(machinesCache);
    if (tScript) tScript.textContent = JSON.stringify(tasksCache);
    if (sScript) sScript.textContent = JSON.stringify(stagesCache);
    if (hScript) hScript.textContent = JSON.stringify(timelineState.weeklyHolidays || []);
}

function ensureTimelineUnassignedRow() {
    const roleForUnassigned = getUserRole() || 'planner';
    const allowUnassigned = ['planner', 'admin'].includes(roleForUnassigned);
    const unassignedTasks = allowUnassigned ? tasksCache.filter(t => !t.machine_id) : [];
    const shouldShowQueueLane = allowUnassigned && (unassignedTasks.length > 0 || machinesCache.length === 0);
    if (shouldShowQueueLane && !machinesCache.find(m => m.id === 'unassigned')) {
        machinesCache.push({
            id: 'unassigned',
            name: 'Unassigned / Pending',
            display_name: 'Unassigned / Pending',
            code: '',
            status: 'operational',
            type: 'Queue',
            category: 'Queue',
            use_factory_shifts: true,
            shift_configuration: normalizeTimelineShiftConfig(timelineState && timelineState.shiftConfig ? timelineState.shiftConfig : null),
            working_hours_summary: 'Queue'
        });
    }
}

function applyTimelinePayloadToCache(payload, options = {}) {
    const preserveExistingOnEmpty = options.preserveExistingOnEmpty !== false;
    const nextMachines = Array.isArray(payload?.machines) ? payload.machines : [];
    const nextTasks = Array.isArray(payload?.tasks) ? payload.tasks : [];
    const nextStages = Array.isArray(payload?.stages) ? payload.stages : [];
    const nextWeeklyHolidays = Array.isArray(payload?.weekly_holidays) ? payload.weekly_holidays : [];

    const existingHasRenderableData = Boolean(
        (Array.isArray(machinesCache) && machinesCache.length > 0) ||
        (Array.isArray(tasksCache) && tasksCache.length > 0) ||
        (Array.isArray(stagesCache) && stagesCache.length > 0)
    );
    const incomingHasRenderableData = Boolean(nextMachines.length || nextTasks.length || nextStages.length);

    if (!incomingHasRenderableData && preserveExistingOnEmpty && existingHasRenderableData) {
        console.warn('Timeline API returned empty payload; preserving existing template data.');
        return false;
    }

    machinesCache = sortMachinesForTimeline(
        nextMachines
            .map((machine, index) => normalizeTimelineMachinePayload(machine, index))
            .filter(Boolean)
    );
    tasksCache = nextTasks;
    stagesCache = nextStages;
    setTimelineWeeklyHolidays(nextWeeklyHolidays);
    populateStageFilterOptions();
    ensureTimelineUnassignedRow();
    syncTimelineDomScriptsFromCache();
    return incomingHasRenderableData;
}

function loadTimelineShiftConfigurationFromTemplate() {
    const sConfig = document.getElementById('data-shift-config');
    const defaultShiftConfig = normalizeTimelineShiftConfig({
        morning: { start: '06:00', end: '14:00' },
        afternoon: { start: '14:00', end: '22:00' },
        night: { start: '22:00', end: '06:00' }
    });

    if (sConfig && sConfig.textContent) {
        try {
            const loadedConfig = normalizeTimelineShiftConfig(JSON.parse(sConfig.textContent));
            if (!loadedConfig || Object.keys(loadedConfig).length === 0) {
                timelineState.shiftConfig = defaultShiftConfig;
            } else {
                timelineState.shiftConfig = normalizeTimelineShiftConfig({
                    morning: { ...defaultShiftConfig.morning, ...(loadedConfig.morning || {}) },
                    afternoon: { ...defaultShiftConfig.afternoon, ...(loadedConfig.afternoon || {}) },
                    night: { ...defaultShiftConfig.night, ...(loadedConfig.night || {}) }
                });
            }
        } catch (e) {
            console.error("Shift Config Parse Error:", e);
            timelineState.shiftConfig = defaultShiftConfig;
        }
    } else {
        timelineState.shiftConfig = defaultShiftConfig;
    }
    console.log("Timeline Shift Config:", timelineState.shiftConfig);
}

function loadTimelineTemplateDataIntoCache() {
    const mScript = document.getElementById('data-machines');
    const tScript = document.getElementById('data-tasks');
    const sScript = document.getElementById('data-stages');
    const hScript = document.getElementById('data-weekly-holidays');

    const parsedMachines = mScript && mScript.textContent ? safeParseJSON(mScript.textContent) : [];
    const parsedTasks = tScript && tScript.textContent ? safeParseJSON(tScript.textContent) : [];
    const parsedStages = sScript && sScript.textContent ? safeParseJSON(sScript.textContent) : [];
    const parsedWeeklyHolidays = hScript && hScript.textContent ? safeParseJSON(hScript.textContent) : [];

    loadTimelineShiftConfigurationFromTemplate();
    const hasRenderableData = applyTimelinePayloadToCache(
        {
            machines: Array.isArray(parsedMachines) ? parsedMachines : [],
            tasks: Array.isArray(parsedTasks) ? parsedTasks : [],
            stages: Array.isArray(parsedStages) ? parsedStages : [],
            weekly_holidays: Array.isArray(parsedWeeklyHolidays) ? parsedWeeklyHolidays : [],
        },
        { preserveExistingOnEmpty: false }
    );

    console.log("Timeline template cache loaded:", {
        machines: machinesCache.length,
        tasks: tasksCache.length,
        stages: stagesCache.length,
    });

    return hasRenderableData;
}

function renderTimelineFromCurrentCache() {
    try {
        initializeTimelineState();
        applyPlannerWorkspaceInputsFromState();
        setupEventListeners();
        setupTimelineExpectedProgressTicker();
        renderTimeline();
    } catch (error) {
        console.error('Timeline core render failed.', error);
        showTimelineToast(`Timeline Init Error: ${error.message}`, 'error');
        return;
    }

    [
        () => renderPlannerFollowUpQueue(),
        () => renderPlannerDispatchReadinessQueue(),
        () => { if (window.renderPlannerKanban) window.renderPlannerKanban(); },
        () => { if (window.renderPlannerList) window.renderPlannerList(); },
        () => { if (window.renderPlannerCalendar) window.renderPlannerCalendar(); },
    ].forEach((renderer) => {
        try {
            renderer();
        } catch (error) {
            console.warn('Timeline secondary renderer failed.', error);
        }
    });
}

window.initGanttChart = function (forceFetch = false) {
    console.log("Initializing Custom Grid Timeline...", forceFetch ? "(Forced Refresh)" : "(Cached)");
    initializeTimelineInteractionControls();

    const templateHasRenderableData = loadTimelineTemplateDataIntoCache();
    const shouldForce = forceFetch || shouldForceMachinesRefresh() || !templateHasRenderableData;
    if (shouldForce) {
        if (templateHasRenderableData) {
            renderTimelineFromCurrentCache();
        } else {
            setupEventListeners();
            renderTimelineFromCurrentCache();
        }
        const roleForFetch = getUserRole() || 'planner';
        const includeUnscheduled = ['planner', 'admin'].includes(roleForFetch);
        const requestedTimelineStatusFilter = normalizeTimelineStatusFilterToken(filterState.status || 'all');
        const timelineParams = new URLSearchParams();
        if (includeUnscheduled) timelineParams.set('include_unscheduled', '1');
        if (requestedTimelineStatusFilter === 'canceled') {
            timelineParams.set('status', 'canceled');
        }
        const timelineQuery = timelineParams.toString();
        const timelineUrl = `/manufacturing/api/timeline/${timelineQuery ? `?${timelineQuery}` : ''}`;
        // Fetch fresh data from API
        const btn = document.querySelector('button[onclick="window.initGanttChart(true)"]');
        if (btn) {
            const icon = btn.querySelector('i');
            if (icon) icon.classList.add('animate-spin');
            btn.disabled = true;
            btn.classList.add('opacity-75');
        }

        const cacheBust = timelineUrl.includes('?') ? `&_=${Date.now()}` : `?_=${Date.now()}`;
        return fetch(timelineUrl + cacheBust)
            .then(async (res) => {
                const responseText = await res.text();
                if (!res.ok) {
                    throw new Error(`Timeline refresh failed (${res.status})`);
                }
                const data = safeParseJSON(responseText);
                if (!data) {
                    throw new Error('Timeline refresh returned invalid JSON.');
                }
                if (!data.success) {
                    throw new Error(data.error || 'Timeline refresh returned no data.');
                }
                return data;
            })
            .then(data => {
                const applied = applyTimelinePayloadToCache(data, { preserveExistingOnEmpty: true });
                timelineLoadedStatusFilter = requestedTimelineStatusFilter === 'canceled' ? 'canceled' : 'default';
                renderTimelineFromCurrentCache();
            })
            .catch(err => {
                console.error("API Fetch/Render Error:", err);
                renderTimelineFromCurrentCache();
                if (!timelineFetchAlertShown) {
                    timelineFetchAlertShown = true;
                    showTimelineToast("Timeline Error: " + err.message, 'error');
                }
            })
            .finally(() => {
                if (btn) {
                    const icon = btn.querySelector('i');
                    if (icon) icon.classList.remove('animate-spin');
                    btn.disabled = false;
                    btn.classList.remove('opacity-75');
                }
            });
    } else {
        try {
            console.log("ðŸ” Init Cache Data Check:");
            if (machinesCache.length > 0) console.log("   - First Machine:", machinesCache[0]);
            else console.log("   - Machines Cache Empty!");

            if (tasksCache.length > 0) console.log("   - First Task:", tasksCache[0]);
            else console.log("   - Tasks Cache Empty!");

            renderTimelineFromCurrentCache();
            if (window.renderPlannerList) window.renderPlannerList();
            if (window.renderPlannerCalendar) window.renderPlannerCalendar();
        } catch (e) {
            console.error("Timeline Init Error:", e);
            showTimelineToast("Timeline Init Error: " + e.message, 'error');
        }
    }
    return Promise.resolve();
};

window.debugTimelineData = function () {
    const timelineEl = document.querySelector('#customTimeline');
    const infoEl = timelineEl?.closest('.glass-panel-premium')?.querySelector('.ph-info')?.parentElement;
    if (infoEl) {
        infoEl.innerHTML = `<i class="ph ph-info"></i> <span class="font-medium text-emerald-600">Debug: Loaded ${tasksCache.length} tasks, ${machinesCache.length} machines. Range: ${timelineState.startDate.toLocaleDateString()}</span>`;
    }
    console.log(`[Debug] Tasks: ${tasksCache.length}, Machines: ${machinesCache.length}`);
};

window.exportTimelineData = function () {
    console.log("Exporting Timeline Data...");
    if (!tasksCache || tasksCache.length === 0) {
        showTimelineToast("No data available to export.", 'warning');
        return;
    }

    // CSV Header
    const headers = ["Work Order ID", "Product", "Order Qty", "Done Qty", "Status", "Start Date", "End Date", "Assigned Machine", "Assigned Worker", "Assignment Type"];
    const rows = tasksCache.map(t => [
        t.id,
        `"${(t.product || '').replace(/"/g, '""')}"`, // Escape quotes
        Number(t.progress_stats ? t.progress_stats.target : (t.quantity || 0)) || 0,
        Number(t.progress_stats ? t.progress_stats.actual : (t.finished_qty || 0)) || 0,
        t.status,
        t.start || '',
        t.end || '',
        t.machine_id ? (machinesCache.find(m => m.id == t.machine_id)?.name || t.machine_id) : 'Unassigned',
        t.assigned_worker_name || 'Unassigned',
        t.assignment_type || 'Manual'
    ]);

    // Construct CSV String
    const csvContent = [
        headers.join(","),
        ...rows.map(r => r.join(","))
    ].join("\n");

    // Create Download Link
    const blob = new Blob([csvContent], { type: 'text/csv;charset=utf-8;' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.setAttribute("href", url);
    link.setAttribute("download", `production_schedule_${new Date().toISOString().slice(0, 10)}.csv`);
    link.style.visibility = 'hidden';
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
};

window.setGanttView = function (mode) {
    console.log("Setting View Mode:", mode);
    timelineState.viewMode = normalizeTimelineScaleToken(mode);
    // Recalculate date window using the selected date as anchor.
    calculateEndDate();
    applyTimelineControlUI();
    window.hasAutoScrolled = false;
    persistPlannerWorkspaceStatePatch({
        scale: timelineState.viewMode,
        date: document.getElementById('timelineDate')?.value || '',
    });
    renderTimeline();
    // Cache helper since we don't have full reactive store
};

window.setTimelineShift = function (shift) {
    console.log("Setting Shift Filter:", shift);
    timelineState.shiftFilter = normalizeTimelineShiftToken(shift);
    window.hasAutoScrolled = false;
    persistPlannerWorkspaceStatePatch({ shift: timelineState.shiftFilter });
    renderTimeline();
};

window.getTimelineScale = function () {
    return timelineState.viewMode || 'day';
};

window.getTimelineShift = function () {
    return timelineState.shiftFilter || 'all';
};

window.toggleTimelineHeader = function () {
    const block = document.getElementById('timelineHeaderBlock');
    const icon = document.getElementById('timelineCollapseIcon');
    const btn = document.getElementById('timelineHeaderToggle');
    if (!block) return;

    const isHidden = block.classList.contains('hidden') || block.dataset.collapsed === '1';
    if (isHidden) {
        block.classList.remove('hidden');
        block.style.display = '';
        block.dataset.collapsed = '0';
        if (icon) {
            icon.classList.remove('ph-caret-down');
            icon.classList.add('ph-caret-up');
        }
        if (btn) btn.setAttribute('aria-expanded', 'true');
    } else {
        block.classList.add('hidden');
        block.style.display = 'none';
        block.dataset.collapsed = '1';
        if (icon) {
            icon.classList.remove('ph-caret-up');
            icon.classList.add('ph-caret-down');
        }
        if (btn) btn.setAttribute('aria-expanded', 'false');
    }
};

document.addEventListener('DOMContentLoaded', () => {
    const btn = document.getElementById('timelineHeaderToggle');
    if (btn) {
        btn.addEventListener('click', window.toggleTimelineHeader);
    }
    document.addEventListener('click', (event) => {
        const menu = document.getElementById('timelineOverflowMenu');
        if (!menu || !timelineOverflowMenuOpen) return;
        if (menu.contains(event.target)) return;
        setTimelineOverflowMenu(false);
    });
    document.addEventListener('keydown', (event) => {
        if (event.key === 'Escape' && timelineOverflowMenuOpen) {
            setTimelineOverflowMenu(false);
        }
        if (event.key === 'Escape' && timelineMaximized) {
            window.setTimelineMaximized(false, true);
        }
    });
    initializeTimelineInteractionControls();
});

function initializeTimelineState() {
    console.log("Timeline Init: Determining Start Date...");
    const savedPlannerDate = getSavedPlannerWorkspaceDate();

    // 1. Absolute Default: Today
    let bestDate = new Date();
    bestDate.setHours(0, 0, 0, 0);

    if (savedPlannerDate) {
        bestDate = savedPlannerDate;
    } else if (tasksCache && tasksCache.length > 0) {
        // 2. Scan for recent tasks to snap to (prefer TODAY/UPCOMING)
        const now = new Date();
        const today = new Date(now);
        today.setHours(0, 0, 0, 0);
        const ninetyDaysMs = 90 * 24 * 60 * 60 * 1000;
        const candidates = [];

        tasksCache.forEach(task => {
            const status = String(task.status || '').toLowerCase();
            if (['completed', 'done', 'canceled', 'archived'].includes(status)) return;
            if (!task.start) return;
            const d = new Date(task.start);
            const ts = d.getTime();

            // Filter: Ignore Invalid, Null/Epoch, or dates before Year 2020
            if (isNaN(ts) || ts < 1000000 || d.getFullYear() < 2020) return;

            // Only snap if within 3 months of TODAY
            if (Math.abs(now - d) < ninetyDaysMs) {
                candidates.push(d);
            }
        });

        if (candidates.length > 0) {
            const upcoming = candidates.filter(d => d >= today).sort((a, b) => a - b);
            const chosen = upcoming.length > 0
                ? upcoming[0]
                : candidates.sort((a, b) => b - a)[0]; // most recent past

            if (chosen) {
                console.log("   - Snapping to nearest relevant task:", chosen.toDateString());
                bestDate = new Date(chosen);
                bestDate.setHours(0, 0, 0, 0);
            }
        }
    }

    // 3. Final Safety: Absolute protection against Epoch jump
    if (bestDate.getFullYear() < 2020) {
        console.warn("   - Safety Check Failed (Year < 2020). Reverting to Today.");
        bestDate = new Date();
        bestDate.setHours(0, 0, 0, 0);
    }

    timelineState.startDate = bestDate;
    console.log("   - Final Start Date Set:", timelineState.startDate.toDateString());

    // 4. Update UI Elements
    const datePicker = document.getElementById('timelineDate');
    if (datePicker) {
        datePicker.value = formatTimelineDateInputValue(timelineState.startDate);
    }

    const weekPicker = document.getElementById('timelineWeekPicker');
    if (weekPicker) {
        weekPicker.value = formatTimelineWeekInputValue(timelineState.startDate);
    }

    const monthPicker = document.getElementById('timelineMonthPicker');
    if (monthPicker) {
        monthPicker.value = formatTimelineMonthInputValue(timelineState.startDate);
    }

    calculateEndDate();
}

function calculateEndDate() {
    const start = alignStartDateForTimelineView(timelineState.startDate, timelineState.viewMode);
    start.setHours(0, 0, 0, 0);
    timelineState.startDate = start;

    switch (timelineState.viewMode) {
        case 'day':
            timelineState.endDate = new Date(start.getTime() + 24 * 60 * 60 * 1000);
            break;
        case 'week':
            timelineState.endDate = new Date(start.getTime() + 7 * 24 * 60 * 60 * 1000);
            break;
        case 'month':
            timelineState.endDate = new Date(start.getFullYear(), start.getMonth() + 1, 1);
            break;
    }

    const datePicker = document.getElementById('timelineDate');
    if (datePicker) {
        datePicker.value = formatTimelineDateInputValue(timelineState.startDate);
    }

    const weekPicker = document.getElementById('timelineWeekPicker');
    if (weekPicker) {
        weekPicker.value = formatTimelineWeekInputValue(timelineState.startDate);
    }

    const monthPicker = document.getElementById('timelineMonthPicker');
    if (monthPicker) {
        monthPicker.value = formatTimelineMonthInputValue(timelineState.startDate);
    }
}

function setupEventListeners() {
    window.changeViewMode = function (mode) {
        timelineState.viewMode = normalizeTimelineScaleToken(mode);
        calculateEndDate();
        applyTimelineControlUI();
        persistPlannerWorkspaceStatePatch({
            scale: timelineState.viewMode,
            date: document.getElementById('timelineDate')?.value || '',
        });
        rerenderTimelineSafely('changeViewMode');
    };

    window.changeShift = function (shift) {
        timelineState.shiftFilter = normalizeTimelineShiftToken(shift);
        persistPlannerWorkspaceStatePatch({ shift: timelineState.shiftFilter });
        rerenderTimelineSafely('changeShift');
    };

    const datePicker = document.getElementById('timelineDate');
    if (datePicker) {
        datePicker.onchange = (e) => {
            if (e.target.value) {
                const [y, m, d] = e.target.value.split('-').map(Number);
                timelineState.startDate = new Date(y, m - 1, d);
                calculateEndDate();
                applyTimelineControlUI();
                window.hasAutoScrolled = false;
                persistPlannerWorkspaceStatePatch({
                    date: e.target.value,
                    timelineScrollLeft: 0,
                    timelineScrollTop: 0,
                });
                rerenderTimelineSafely('timelineDateChange', true);
            }
        };
    }

    const weekPicker = document.getElementById('timelineWeekPicker');
    if (weekPicker) {
        weekPicker.onchange = (e) => {
            const selectedWeek = parseTimelineWeekInputValue(e.target.value);
            if (!selectedWeek) return;
            timelineState.viewMode = 'week';
            timelineState.startDate = selectedWeek;
            calculateEndDate();
            applyTimelineControlUI();
            window.hasAutoScrolled = false;
            persistPlannerWorkspaceStatePatch({
                scale: timelineState.viewMode,
                date: formatTimelineDateInputValue(timelineState.startDate),
                timelineScrollLeft: 0,
                timelineScrollTop: 0,
            });
            rerenderTimelineSafely('timelineWeekChange', true);
        };
    }

    const monthPicker = document.getElementById('timelineMonthPicker');
    if (monthPicker) {
        monthPicker.onchange = (e) => {
            const selectedMonth = parseTimelineMonthInputValue(e.target.value);
            if (!selectedMonth) return;
            timelineState.viewMode = 'month';
            timelineState.startDate = selectedMonth;
            calculateEndDate();
            applyTimelineControlUI();
            window.hasAutoScrolled = false;
            persistPlannerWorkspaceStatePatch({
                scale: timelineState.viewMode,
                date: formatTimelineDateInputValue(timelineState.startDate),
                timelineScrollLeft: 0,
                timelineScrollTop: 0,
            });
            rerenderTimelineSafely('timelineMonthChange', true);
        };
    }
}

window.shiftTimelineWeek = function (offset) {
    const delta = Number(offset || 0);
    if (!Number.isFinite(delta) || delta === 0) return false;
    const base = timelineState.startDate instanceof Date ? new Date(timelineState.startDate) : new Date();
    base.setHours(0, 0, 0, 0);
    base.setDate(base.getDate() + (delta * 7));
    timelineState.viewMode = 'week';
    timelineState.startDate = base;
    calculateEndDate();
    window.hasAutoScrolled = false;
    persistPlannerWorkspaceStatePatch({
        scale: timelineState.viewMode,
        date: formatTimelineDateInputValue(timelineState.startDate),
        timelineScrollLeft: 0,
        timelineScrollTop: 0,
    });
    rerenderTimelineSafely('timelineWeekShift', true);
    return false;
};

window.shiftTimelineWindow = function (offset) {
    const delta = Number(offset || 0);
    if (!Number.isFinite(delta) || delta === 0) return false;
    const base = timelineState.startDate instanceof Date ? new Date(timelineState.startDate) : new Date();
    base.setHours(0, 0, 0, 0);
    if (timelineState.viewMode === 'month') {
        base.setMonth(base.getMonth() + delta, 1);
    } else if (timelineState.viewMode === 'week') {
        base.setDate(base.getDate() + (delta * 7));
    } else {
        base.setDate(base.getDate() + delta);
    }
    timelineState.startDate = base;
    calculateEndDate();
    applyTimelineControlUI();
    window.hasAutoScrolled = false;
    persistPlannerWorkspaceStatePatch({
        scale: timelineState.viewMode,
        date: formatTimelineDateInputValue(timelineState.startDate),
        timelineScrollLeft: 0,
        timelineScrollTop: 0,
    });
    rerenderTimelineSafely('timelineWindowShift', true);
    return false;
};

window.jumpTimelineToToday = function () {
    timelineState.startDate = new Date();
    timelineState.startDate.setHours(0, 0, 0, 0);
    calculateEndDate();
    applyTimelineControlUI();
    window.hasAutoScrolled = false;
    persistPlannerWorkspaceStatePatch({
        scale: timelineState.viewMode,
        date: formatTimelineDateInputValue(timelineState.startDate),
        timelineScrollLeft: 0,
        timelineScrollTop: 0,
    });
    rerenderTimelineSafely('timelineJumpToToday', true);
    return false;
};

function rerenderTimelineSafely(context = 'timelineRender', forceFetchOnError = false) {
    try {
        renderTimeline();
    } catch (error) {
        console.error(`Timeline Render Error (${context}):`, error);
        showTimelineToast(`Timeline Error: ${error.message}`, 'error');
        if (forceFetchOnError && typeof window.initGanttChart === 'function') {
            window.setTimeout(() => window.initGanttChart(true), 0);
        }
    }
}

// Filter State
let filterState = {
    status: 'all',
    search: '',
    stage: 'all',
    machineActivity: 'all'
};

function normalizeTimelineStatusFilterToken(value) {
    const normalized = String(value || '').trim().toLowerCase();
    if (!normalized || normalized === 'open') return 'all';
    return normalized;
}

function getTaskStageMeta(task) {
    if (!task) return { id: '', name: '' };
    const stageId = task.stage_id || task.stageId || task.stage;
    const stageName = task.stage_name || task.stageName || '';
    return {
        id: stageId !== undefined && stageId !== null ? String(stageId) : '',
        name: stageName ? String(stageName) : ''
    };
}

function getCatalogStageMeta(stage) {
    if (!stage) return { id: '', name: '' };
    const stageId = stage.id ?? stage.stage_id ?? stage.stageId ?? stage.value;
    const stageName = stage.name ?? stage.stage_name ?? stage.stageName ?? stage.label ?? '';
    return {
        id: stageId !== undefined && stageId !== null ? String(stageId) : '',
        name: stageName ? String(stageName) : ''
    };
}

function resolveSelectedStageFilterData(stageFilterValue) {
    const value = String(stageFilterValue || '');
    if (!value || value === 'all' || value === '__none__') return null;

    const matches = (stagesCache || []).filter(stage => {
        const meta = getCatalogStageMeta(stage);
        const key = String(meta.id || meta.name || '');
        return key === value || String(meta.id || '') === value || String(meta.name || '') === value;
    });

    if (!matches.length) {
        return {
            value,
            label: value,
            machineIds: new Set(),
            requiredTypes: [],
        };
    }

    const machineIds = new Set();
    const requiredTypesSet = new Set();
    let label = '';
    matches.forEach(stage => {
        if (!label) label = String(stage.name || stage.stage_name || '').trim();
        const defaultMachineId = stage.default_machine_id ?? stage.machine_id ?? stage.machineId;
        if (defaultMachineId !== undefined && defaultMachineId !== null && defaultMachineId !== '') {
            machineIds.add(String(defaultMachineId));
        }
        const requiredType = String(stage.machine_type || stage.category || '').trim();
        if (requiredType) requiredTypesSet.add(requiredType);
    });

    return {
        value,
        label: label || value,
        machineIds,
        requiredTypes: Array.from(requiredTypesSet),
    };
}

function machineSupportsSelectedStage(machine, selectedStageData) {
    if (!machine || !selectedStageData) return false;
    if (String(machine.id || '') === 'unassigned') return false;

    const machineId = String(machine.id || '');
    if (selectedStageData.machineIds?.has(machineId)) return true;

    const requiredTypes = Array.isArray(selectedStageData.requiredTypes)
        ? selectedStageData.requiredTypes
        : [];
    if (!requiredTypes.length) return false;

    return requiredTypes.some(type => machineMatchesRequiredType(machine, type));
}

function buildTimelineStageRows(stageList) {
    const rowsByKey = new Map();
    (stageList || []).forEach(stage => {
        const meta = getCatalogStageMeta(stage);
        const key = String(meta.id || meta.name || '');
        if (!key || rowsByKey.has(key)) return;

        const stageName = String(meta.name || `Stage ${key}`);
        const stageMachineType = String(stage.machine_type || stage.category || '').trim();
        const defaultMachineId = stage.default_machine_id ?? stage.machine_id ?? stage.machineId ?? '';
        const rawOrder = Number(stage.order ?? stage.stage_order ?? 999999);

        rowsByKey.set(key, {
            id: `stage:${key}`,
            isStageRow: true,
            stageKey: key,
            stageId: meta.id || '',
            stageName,
            stageMachineType,
            defaultMachineId: defaultMachineId !== undefined && defaultMachineId !== null && defaultMachineId !== ''
                ? String(defaultMachineId)
                : '',
            stageOrder: Number.isFinite(rawOrder) ? rawOrder : 999999,
            name: stageName,
            type: stageMachineType,
            category: 'Stage',
            status: 'operational',
        });
    });

    return Array.from(rowsByKey.values()).sort((a, b) => {
        if (a.stageOrder !== b.stageOrder) return a.stageOrder - b.stageOrder;
        return String(a.stageName).localeCompare(String(b.stageName));
    });
}

function renderTimelineStageQuickList() {
    const host = document.getElementById('timelineStageQuickList');
    if (!host) return;

    const entriesMap = new Map();
    (stagesCache || []).forEach(stage => {
        const meta = getCatalogStageMeta(stage);
        if (!meta.id && !meta.name) return;
        const label = (meta.name || '').trim();
        if (!label) return;
        const key = meta.id || label;
        if (!entriesMap.has(key)) entriesMap.set(key, { id: meta.id || '', label });
    });

    const entries = Array.from(entriesMap.values());
    if (!entries.length) {
        host.innerHTML = '';
        host.classList.add('hidden');
        return;
    }

    entries.sort((a, b) => {
        const ai = Number(a.id);
        const bi = Number(b.id);
        if (Number.isFinite(ai) && Number.isFinite(bi)) return bi - ai;
        return String(a.label).localeCompare(String(b.label));
    });

    host.classList.remove('hidden');
    host.innerHTML = entries
        .map(item => `<span class="px-2 py-1 rounded-full border border-slate-200 bg-slate-50 text-[10px] font-bold text-slate-600 whitespace-nowrap">${item.label}</span>`)
        .join('');
}

function populateStageFilterOptions() {
    const select = document.getElementById('timelineStageFilter');
    if (!select) return;

    const previous = select.value || 'all';
    const stages = new Map();

    (tasksCache || []).forEach(task => {
        const meta = getTaskStageMeta(task);
        if (meta.id || meta.name) {
            const key = meta.id || meta.name;
            if (!stages.has(key)) {
                stages.set(key, meta.name || `Stage ${key}`);
            }
        } else {
            stages.set('__none__', 'Unspecified');
        }
    });

    (stagesCache || []).forEach(stage => {
        const meta = getCatalogStageMeta(stage);
        if (!meta.id && !meta.name) return;
        const key = meta.id || meta.name;
        if (!stages.has(key)) {
            stages.set(key, meta.name || `Stage ${key}`);
        }
    });

    const entries = Array.from(stages.entries())
        .filter(([key]) => key !== 'all')
        .map(([key, name]) => ({ key, name }))
        .sort((a, b) => String(a.name).localeCompare(String(b.name)));

    select.innerHTML = '<option value="all">All Stages</option><option value="__none__">Unspecified</option>';
    entries.forEach(item => {
        if (item.key === '__none__') return;
        const opt = document.createElement('option');
        opt.value = item.key;
        opt.textContent = item.name || `Stage ${item.key}`;
        select.appendChild(opt);
    });

    if (previous && Array.from(select.options).some(opt => opt.value === previous)) {
        select.value = previous;
    } else {
        select.value = 'all';
    }

    if (typeof window.syncPlannerStageFilterOptions === 'function') {
        window.syncPlannerStageFilterOptions();
    }

    renderTimelineStageQuickList();
}

window.filterTimeline = function () {
    console.log("ðŸ” filterTimeline CALLED");
    const statusBtn = document.getElementById('timelineFilter');
    const localSearch = document.getElementById('timelineSearch');
    const smartSearch = document.getElementById('timelineSmartSearch');
    const globalSearch = document.getElementById('globalSmartSearch');
    const stageSelect = document.getElementById('timelineStageFilter');
    const machineSelect = document.getElementById('timelineMachineFilter');

    if (statusBtn) filterState.status = normalizeTimelineStatusFilterToken(statusBtn.value);
    if (stageSelect) filterState.stage = stageSelect.value || 'all';
    if (machineSelect) filterState.machineActivity = machineSelect.value || 'all';

    let query = "";
    if (globalSearch && globalSearch.value) query = globalSearch.value;
    else if (smartSearch && smartSearch.value) query = smartSearch.value;
    else if (localSearch && localSearch.value) query = localSearch.value;

    filterState.search = query.toLowerCase();
    persistPlannerWorkspaceStatePatch({
        status: filterState.status,
        stage: filterState.stage,
        machineActivity: filterState.machineActivity,
        search: query,
    });

    console.log(`ðŸ” Search Query: "${filterState.search}"`);
    console.log(`ðŸ” Global Search Element:`, globalSearch);
    console.log(`ðŸ” Global Search Value:`, globalSearch ? globalSearch.value : "N/A");

    if (filterState.status === 'canceled' || timelineLoadedStatusFilter === 'canceled') {
        if (window.initGanttChart) {
            window.initGanttChart(true);
        }
        return;
    }

    // 1. Render Timeline
    console.log("ðŸ” Re-rendering Timeline...");
    renderTimeline();

    // 2. Filter Pending Approvals
    const approvalsList = document.getElementById('approvalsList');
    if (approvalsList) {
        console.log(`ðŸ” Filtering Approvals List (${approvalsList.children.length} items)`);
        Array.from(approvalsList.children).forEach(el => {
            if (el.innerText.includes("All Caught Up!")) return;
            const text = el.innerText.toLowerCase();
            const match = text.includes(filterState.search);
            if (match) {
                // console.log(`   âœ… Showing Approval: ${text.substring(0, 15)}...`);
                el.classList.remove('hidden');
            } else {
                el.classList.add('hidden');
            }
        });
    }

    // 3. Filter Pending Orders
    const pendingOrdersList = document.getElementById('pendingOrdersList');
    if (pendingOrdersList) {
        console.log(`ðŸ” Filtering Pending Orders List (${pendingOrdersList.children.length} items)`);
        Array.from(pendingOrdersList.children).forEach(el => {
            if (el.innerText.includes("No Pending Orders")) return;
            const text = el.innerText.toLowerCase();
            const match = text.includes(filterState.search);
            if (match) {
                // console.log(`   âœ… Showing Order: ${text.substring(0, 15)}...`);
                // el.classList.remove('hidden'); // Was double removing? 
                el.classList.remove('hidden');
            } else {
                el.classList.add('hidden');
            }
        });
    }

    // 4. Auto-Scroll to results if search is active
    if (filterState.search.length > 2 && !suppressTimelineSearchAutoScroll) {
        const container = document.getElementById('timelineContainer');
        if (container) {
            console.log("ðŸ” Auto-scrolling to Timeline results...");
            container.scrollIntoView({ behavior: 'smooth', block: 'center' });
        }
    }
};

// Bind Search Input Listener
// Bind Search Input Listener
document.addEventListener('DOMContentLoaded', () => {
    const searchInput = document.getElementById('timelineSearch');
    if (searchInput) {
        searchInput.addEventListener('input', window.filterTimeline);
    }

    const smartSearch = document.getElementById('timelineSmartSearch');
    if (smartSearch) {
        smartSearch.addEventListener('input', window.filterTimeline);
    }

    // Explicitly bind Global Search (Robustness fix)
    const globalSearch = document.getElementById('globalSmartSearch');
    if (globalSearch) {
        globalSearch.addEventListener('input', window.filterTimeline);
        // Also bind to 'change' and 'keyup' just in case
        globalSearch.addEventListener('keyup', window.filterTimeline);
    }

    // Auto-Init if container exists (Fix for "No Data" issue)
    if (document.getElementById('customTimeline') && window.initGanttChart) {
        window.initGanttChart();
    }

    const mainContent = document.getElementById('main-content');
    if (mainContent && !mainContent.dataset.plannerStateScrollBound) {
        mainContent.dataset.plannerStateScrollBound = '1';
        mainContent.addEventListener('scroll', () => {
            window.clearTimeout(mainContent.__plannerStatePersistTimer);
            mainContent.__plannerStatePersistTimer = window.setTimeout(() => {
                persistPlannerWorkspaceStatePatch({ mainScrollTop: Math.round(mainContent.scrollTop || 0) });
            }, 80);
        }, { passive: true });
    }

    const timelineScrollWrapper = document.getElementById('timelineScrollWrapper');
    if (timelineScrollWrapper && !timelineScrollWrapper.dataset.plannerStateScrollBound) {
        timelineScrollWrapper.dataset.plannerStateScrollBound = '1';
        timelineScrollWrapper.addEventListener('scroll', () => {
            window.clearTimeout(timelineScrollWrapper.__plannerStatePersistTimer);
            timelineScrollWrapper.__plannerStatePersistTimer = window.setTimeout(() => {
                persistPlannerWorkspaceStatePatch({
                    timelineScrollLeft: Math.round(timelineScrollWrapper.scrollLeft || 0),
                    timelineScrollTop: Math.round(timelineScrollWrapper.scrollTop || 0),
                });
            }, 80);
        }, { passive: true });
    }
});

// setGanttView Removed (Legacy)

// Global Drag Start Handler for Sidebar Items
window.handleDragStart = function (e, id, content) {
    if (!id || !content) return;
    const payload = JSON.stringify({ id: id, content: content });
    e.dataTransfer.setData('text/plain', payload);
    e.dataTransfer.effectAllowed = 'copyMove';
    // Optional: Add drag image or styling
};


function renderTimeline() {
    const container = document.getElementById('customTimeline');
    const header = document.getElementById('timelineHeader');
    const body = document.getElementById('timelineBody');
    const scrollWrapper = document.getElementById('timelineScrollWrapper');

    if (!container || !header || !body) return;
    const previousHeaderHtml = header.innerHTML;
    const previousBodyHtml = body.innerHTML;
    let layout = timelineState.layout || getTimelineLayoutMetrics(1);
    try {
        syncTimelineWorkOrderPaletteRegistry(tasksCache);

        const timelineSlots = buildTimelineSlots();
        timelineState.visibleSlots = timelineSlots;
        let totalColumns = timelineSlots.length;
        if (totalColumns <= 0) {
            // Safety fallback: never render an empty grid.
            const fallbackStart = new Date(timelineState.startDate);
            fallbackStart.setHours(0, 0, 0, 0);
            const fallbackEnd = new Date(fallbackStart.getTime() + 24 * 60 * 60 * 1000);
            timelineState.visibleSlots = [{
                index: 0,
                startMs: fallbackStart.getTime(),
                endMs: fallbackEnd.getTime(),
                date: fallbackStart,
                unit: 'day',
            }];
            totalColumns = 1;
        }

        layout = getTimelineLayoutMetrics(totalColumns);
        timelineState.layout = layout;
        const gridTemplate = `${layout.machineColumnWidth}px repeat(${totalColumns}, ${layout.slotWidth}px)`;

        // Fit the active period to the visible card width instead of forcing a scroll canvas.
        container.style.minWidth = `${layout.totalTimelineWidth}px`;
        container.style.width = `${layout.totalTimelineWidth}px`;
        container.style.maxWidth = `${layout.totalTimelineWidth}px`;

        // --- Render Header ---
        header.style.display = 'grid';
        header.style.width = `${layout.totalTimelineWidth}px`;
        header.style.gridTemplateColumns = gridTemplate;
        header.innerHTML = `
            <div class="timeline-resource-header-cell text-xs font-bold text-slate-500" title="Drag to resize the resource column. Double-click to reset.">
                <span class="truncate">Resource</span>
                <button
                    type="button"
                    id="timelineResourceResizeHandle"
                    class="timeline-resource-resize-handle"
                    aria-label="Resize resource column"
                    title="Drag to resize the resource column. Double-click to reset."></button>
            </div>`;
        attachTimelineResourceColumnResize(document.getElementById('timelineResourceResizeHandle'));

        if (timelineState.viewMode === 'day') {
            renderDayHeader(header, timelineState.shiftFilter, layout.slotWidth, timelineState.visibleSlots);
        } else if (timelineState.viewMode === 'week') {
            renderWeekHeader(header, layout.slotWidth, timelineState.visibleSlots);
        } else {
            renderMonthHeader(header, layout.slotWidth, timelineState.visibleSlots);
        }

        // --- Render Body ---
        renderBody(body, gridTemplate, totalColumns);
    } catch (error) {
        console.error('Timeline render failed; preserving previous timeline rows.', error);
        if (previousHeaderHtml) header.innerHTML = previousHeaderHtml;
        if (previousBodyHtml) body.innerHTML = previousBodyHtml;
        throw error;
    }

    // Sticky "Now" line (day view only)
    const existingNowLine = document.getElementById('timelineNowLine');
    if (existingNowLine) existingNowLine.remove();

    if (timelineState.viewMode === 'day') {
        const now = new Date();
        const windowStart = new Date(timelineState.startDate);
        let durationHours = 24;
        if (timelineState.shiftFilter !== 'all') {
            const shiftWindow = getTimelineShiftWindow(timelineState.shiftFilter, timelineState.shiftConfig);
            windowStart.setHours(shiftWindow.startHour, 0, 0, 0);
            durationHours = shiftWindow.durationHours;
        } else {
            windowStart.setHours(0, 0, 0, 0);
        }
        const windowStartMs = windowStart.getTime();
        const windowEndMs = windowStartMs + (durationHours * 60 * 60 * 1000);
        if (now.getTime() >= windowStartMs && now.getTime() <= windowEndMs) {
            const leftPx = ((now.getTime() - windowStartMs) / (windowEndMs - windowStartMs)) * layout.timeAreaWidth;
            const nowLine = document.createElement('div');
            nowLine.id = 'timelineNowLine';
            nowLine.style.position = 'absolute';
            nowLine.style.top = '0';
            nowLine.style.bottom = '0';
            nowLine.style.left = `${layout.machineColumnWidth + leftPx}px`;
            nowLine.style.width = '2px';
            nowLine.style.background = '#3b82f6';
            nowLine.style.boxShadow = '0 0 10px rgba(59,130,246,0.6)';
            nowLine.style.pointerEvents = 'none';
            nowLine.style.zIndex = '30';
            body.appendChild(nowLine);
        }
    }

    // Auto-Scroll Logic for Day View (snap to next task or now)
    if (timelineState.viewMode === 'day' && !window.hasAutoScrolled && !plannerWorkspaceStateRestorePending && layout.hasHorizontalOverflow) {
        setTimeout(() => {
            const now = new Date();
            const windowStart = new Date(timelineState.startDate);
            let durationHours = 24;
            if (timelineState.shiftFilter !== 'all') {
                const shiftWindow = getTimelineShiftWindow(timelineState.shiftFilter, timelineState.shiftConfig);
                windowStart.setHours(shiftWindow.startHour, 0, 0, 0);
                durationHours = shiftWindow.durationHours;
            } else {
                windowStart.setHours(0, 0, 0, 0);
            }
            const windowStartMs = windowStart.getTime();
            const windowEndMs = windowStartMs + (durationHours * 60 * 60 * 1000);

            const starts = (tasksCache || [])
                .filter(t => t.start)
                .map(t => new Date(t.start).getTime())
                .filter(ts => ts >= windowStartMs && ts <= windowEndMs)
                .sort((a, b) => a - b);

            let targetMs = now.getTime();
            const future = starts.filter(ts => ts >= now.getTime());
            if (future.length > 0) {
                targetMs = future[0];
            } else if (starts.length > 0) {
                targetMs = starts[0];
            }

            const offsetHours = (targetMs - windowStartMs) / (60 * 60 * 1000);
            const scrollPos = offsetHours * (timelineState.layout?.slotWidth || 100);
            const centerOffset = scrollPos - (scrollWrapper.clientWidth / 2) + (timelineState.layout?.machineColumnWidth || 200);
            scrollWrapper.scrollTo({ left: Math.max(0, centerOffset), behavior: 'smooth' });
            window.hasAutoScrolled = true;
        }, 400);
    }

    if (plannerWorkspaceStateRestorePending) {
        window.requestAnimationFrame(() => restorePlannerWorkspaceScrollIfNeeded());
    }

    if (window.debugTimelineData) window.debugTimelineData();
}

// Header Renderers
function renderDayHeader(headerContainer, shift, slotWidth = 100, slots = []) {
    const now = new Date();
    const effectiveSlots = Array.isArray(slots) ? slots : [];
    const denseView = slotWidth < 34;
    const compactView = slotWidth < 54;

    effectiveSlots.forEach((slot, idx) => {
        const slotDate = new Date(slot.startMs);
        const displayHour = slotDate.getHours();
        const shouldRenderLabel = !denseView || (idx % 2 === 0);
        const hourLabel = compactView
            ? String(displayHour).padStart(2, '0')
            : `${displayHour}:00`;

        const isCurrentSlot = slotDate.getHours() === now.getHours()
            && slotDate.toDateString() === now.toDateString();

        const cell = document.createElement('div');
        cell.className = "p-2 border-r border-gray-200 text-center relative flex flex-col justify-center bg-gray-50";
        if (isTimelineSlotNonWorking(slot)) {
            cell.classList.remove('bg-gray-50');
            cell.classList.add('bg-slate-100');
            cell.style.backgroundImage = 'repeating-linear-gradient(135deg, rgba(148,163,184,0.12), rgba(148,163,184,0.12) 6px, transparent 6px, transparent 12px)';
        }
        cell.innerHTML = shouldRenderLabel
            ? `<span class="${denseView ? 'text-[10px]' : 'text-xs'} font-bold ${isCurrentSlot ? 'text-blue-600' : 'text-gray-500'}">${hourLabel}</span>`
            : `<span class="text-[10px] font-bold text-gray-300">.</span>`;

        if (isCurrentSlot) {
            const indicator = document.createElement('div');
            indicator.className = "absolute bottom-0 left-0 right-0 h-0.5 bg-blue-500 shadow-[0_0_8px_rgba(59,130,246,0.5)]";
            cell.appendChild(indicator);
        }
        headerContainer.appendChild(cell);
    });
}

function renderWeekHeader(headerContainer, slotWidth = 100, slots = []) {
    const compactView = slotWidth < 72;
    const effectiveSlots = Array.isArray(slots) ? slots : [];

    effectiveSlots.forEach((slot) => {
        const d = new Date(slot.startMs);
        const isToday = d.toDateString() === new Date().toDateString();

        const cell = document.createElement('div');
        cell.className = `p-2 border-r border-gray-200 text-center flex flex-col justify-center ${isToday ? 'bg-blue-50' : 'bg-gray-50'}`;
        if (isTimelineSlotNonWorking(slot)) {
            cell.classList.remove('bg-blue-50', 'bg-gray-50');
            cell.classList.add('bg-slate-100');
            cell.style.backgroundImage = 'repeating-linear-gradient(135deg, rgba(148,163,184,0.12), rgba(148,163,184,0.12) 6px, transparent 6px, transparent 12px)';
        }

        cell.innerHTML = `
            <div class="text-[10px] text-gray-500 uppercase font-bold">${compactView ? d.toLocaleDateString('en-US', { weekday: 'narrow' }) : d.toLocaleDateString('en-US', { weekday: 'short' })}</div>
            <div class="text-xs font-bold ${isToday ? 'text-blue-600' : 'text-gray-700'}">${d.getDate()}</div>
        `;
        headerContainer.appendChild(cell);
    });
}

function renderMonthHeader(headerContainer, slotWidth = 100, slots = []) {
    const compactView = slotWidth < 34;
    const effectiveSlots = Array.isArray(slots) ? slots : [];

    effectiveSlots.forEach((slot) => {
        const d = new Date(slot.startMs);
        const isToday = d.toDateString() === new Date().toDateString();

        const cell = document.createElement('div');
        cell.className = `p-2 border-r border-gray-200 text-center flex flex-col justify-center ${isToday ? 'bg-blue-50' : 'bg-gray-50'}`;
        if (isTimelineSlotNonWorking(slot)) {
            cell.classList.remove('bg-blue-50', 'bg-gray-50');
            cell.classList.add('bg-slate-100');
            cell.style.backgroundImage = 'repeating-linear-gradient(135deg, rgba(148,163,184,0.12), rgba(148,163,184,0.12) 6px, transparent 6px, transparent 12px)';
        }
        cell.innerHTML = `<div class="${compactView ? 'text-[10px]' : 'text-xs'} font-bold ${isToday ? 'text-blue-600' : 'text-gray-700'}">${d.getDate()}</div>`;
        headerContainer.appendChild(cell);
    });
}

function getTimelineTaskTarget(task) {
    const target = Number(task && task.progress_stats ? task.progress_stats.target : (task && task.quantity ? task.quantity : 0)) || 0;
    return Math.max(target, 0);
}

function getTimelineTaskActual(task) {
    const explicitActual = Number(task && task.progress_stats ? task.progress_stats.actual : (task && task.finished_qty ? task.finished_qty : 0)) || 0;
    const target = getTimelineTaskTarget(task);
    const status = String(task && task.status ? task.status : '').toLowerCase();
    if (explicitActual > 0) return Math.min(explicitActual, target || explicitActual);
    if (['completed', 'done'].includes(status)) return target;
    return 0;
}

function getTimelineTaskDurationMinutes(task) {
    const estimated = Number(task && task.estimated_duration_minutes ? task.estimated_duration_minutes : 0);
    if (Number.isFinite(estimated) && estimated > 0) return estimated;

    if (task && task.start && task.end) {
        const startMs = new Date(task.start).getTime();
        const endMs = new Date(task.end).getTime();
        if (Number.isFinite(startMs) && Number.isFinite(endMs) && endMs > startMs) {
            return Math.max(Math.round((endMs - startMs) / 60000), 1);
        }
    }

    return 0;
}

function getTimelineTaskGroupKey(task) {
    if (!task) return '';

    const taskId = task.id !== undefined && task.id !== null ? String(task.id) : '';
    const parentId = task.parent_id !== undefined && task.parent_id !== null ? String(task.parent_id) : '';
    const stageId = task.stage_id ?? task.stageId ?? task.current_stage_id ?? task.currentStageId ?? '';
    const stageKey = stageId !== undefined && stageId !== null && String(stageId) !== ''
        ? String(stageId)
        : '';

    if (!parentId) {
        return taskId ? `task-${taskId}` : '';
    }

    if (stageKey) {
        return `parent-${parentId}-stage-${stageKey}`;
    }

    return `parent-${parentId}`;
}

function getTimelineVisualTaskGroupKey(task) {
    const baseKey = getTimelineTaskGroupKey(task);
    if (!baseKey || !task?.parent_id) return baseKey;

    const machineId = task.machine_id ?? task.machineId ?? '';
    const machineKey = machineId !== undefined && machineId !== null && String(machineId) !== ''
        ? String(machineId)
        : 'unassigned';
    return `${baseKey}-machine-${machineKey}`;
}

function getTimelineSplitStatusSummary(tasks = []) {
    const counts = {};
    (tasks || []).forEach((task) => {
        const rawStatus = String(task?.status || 'pending').trim().toLowerCase() || 'pending';
        const label = rawStatus.replace(/_/g, ' ');
        counts[label] = (counts[label] || 0) + 1;
    });
    return Object.entries(counts)
        .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
        .map(([label, count]) => `${count} ${label}`)
        .join(' | ');
}

function getTimelineSplitVisualMeta(task, splitGroups = new Map()) {
    if (!task) return null;
    if (task.is_visual_group) {
        const total = Number(task.split_group_total || 0);
        if (total <= 1) return null;
        return {
            isGroup: true,
            groupKey: getTimelineVisualTaskGroupKey(task),
            parentCode: getDisplayWorkOrderCode(task),
            segmentLabel: `${Number(task.split_group_completed || 0)}/${total} segments done`,
            statusSummary: task.split_group_status_summary || getTimelineSplitStatusSummary(task.source_tasks || []),
        };
    }

    if (!task.parent_id) return null;
    const list = splitGroups.get(getTimelineTaskGroupKey(task)) || [];
    if (list.length <= 1) return null;
    const idx = list.indexOf(task.id);
    return {
        isGroup: false,
        groupKey: getTimelineTaskGroupKey(task),
        parentCode: getDisplayWorkOrderCode(task),
        segmentLabel: idx >= 0 ? `split ${idx + 1}/${list.length}` : `split route/${list.length}`,
        statusSummary: '',
    };
}

function buildTimelineVisualTasks(tasks) {
    if (!Array.isArray(tasks) || tasks.length === 0) return [];

    const grouped = new Map();
    tasks.forEach((task) => {
        const groupKey = getTimelineVisualTaskGroupKey(task);
        if (!groupKey) return;
        if (!grouped.has(groupKey)) grouped.set(groupKey, []);
        grouped.get(groupKey).push(task);
    });

    return Array.from(grouped.values()).map((group) => {
        if (group.length === 1) {
            return group[0];
        }

        const sorted = group.slice().sort((a, b) => {
            const aStart = a && a.start ? new Date(a.start).getTime() : Number.MAX_SAFE_INTEGER;
            const bStart = b && b.start ? new Date(b.start).getTime() : Number.MAX_SAFE_INTEGER;
            return aStart - bStart;
        });

        const first = sorted[0];
        const primary = group.find((task) => !['completed', 'done', 'canceled', 'archived'].includes(String(task.status || '').toLowerCase())) || first;
        const allStatuses = group.map((task) => String(task.status || '').toLowerCase());
        const allCompleted = allStatuses.every((status) => ['completed', 'done'].includes(status));
        const anyActive = allStatuses.some((status) => ['in_progress', 'active', 'on_hold'].includes(status));
        const anyPending = allStatuses.some((status) => ['pending', 'planned', 'scheduled'].includes(status));

        const starts = group
            .filter((task) => task && task.start)
            .map((task) => new Date(task.start).getTime())
            .filter((ts) => Number.isFinite(ts));
        const ends = group
            .filter((task) => task && task.end)
            .map((task) => new Date(task.end).getTime())
            .filter((ts) => Number.isFinite(ts));
        const totalDurationMinutes = group.reduce((sum, task) => sum + getTimelineTaskDurationMinutes(task), 0);

        const totalTarget = group.reduce((sum, task) => sum + getTimelineTaskTarget(task), 0);
        const totalActual = group.reduce((sum, task) => sum + getTimelineTaskActual(task), 0);
        const totalRemaining = Math.max((totalTarget || 0) - (totalActual || 0), 0);
        const completedSegments = group.filter((task) => ['completed', 'done'].includes(String(task.status || '').toLowerCase())).length;
        const earliestStartMs = starts.length ? Math.min(...starts) : null;
        const scheduledEndMs = ends.length ? Math.max(...ends) : null;
        const projectedEndMs = (earliestStartMs !== null && totalDurationMinutes > 0)
            ? earliestStartMs + (totalDurationMinutes * 60000)
            : null;
        const finalEndMs = scheduledEndMs || projectedEndMs;

        return {
            ...primary,
            id: primary.id,
            source_ids: group.map((task) => task.id),
            source_tasks: group,
            is_visual_group: true,
            split_group_total: group.length,
            split_group_completed: completedSegments,
            split_group_status_summary: getTimelineSplitStatusSummary(group),
            start: earliestStartMs !== null ? new Date(earliestStartMs).toISOString() : primary.start,
            end: finalEndMs !== null ? new Date(finalEndMs).toISOString() : primary.end,
            quantity: totalTarget || primary.quantity,
            finished_qty: totalActual,
            remaining_qty: totalRemaining,
            progress_stats: {
                target: totalTarget || primary.quantity || 0,
                actual: totalActual,
            },
            status: allCompleted ? 'completed' : (anyActive ? 'in_progress' : (anyPending ? 'pending' : primary.status)),
            assigned_worker_name: primary.assigned_worker_name || first.assigned_worker_name,
            assignment_type: primary.assignment_type || first.assignment_type,
            setup_minutes: Number(first.setup_minutes || 0),
            estimated_duration_minutes: group.reduce((sum, task) => {
                const duration = Number(task.estimated_duration_minutes || 0);
                return sum + (Number.isFinite(duration) ? duration : 0);
            }, 0),
            product: primary.product || first.product,
            parent_id: primary.parent_id || first.parent_id,
            stage_id: primary.stage_id || first.stage_id,
            stage_name: primary.stage_name || first.stage_name,
            machine_id: primary.machine_id || first.machine_id,
        };
    });
}

function renderBody(bodyContainer, gridTemplate, totalColumns) {
    bodyContainer.innerHTML = '';
    const machineColumnWidth = timelineState.layout?.machineColumnWidth || 200;
    const timeAreaWidth = timelineState.layout?.timeAreaWidth || 0;
    const totalTimelineWidth = timelineState.layout?.totalTimelineWidth || (machineColumnWidth + timeAreaWidth);
    bodyContainer.style.width = `${totalTimelineWidth}px`;

    const slots = Array.isArray(timelineState.visibleSlots) && timelineState.visibleSlots.length
        ? timelineState.visibleSlots
        : [{
            startMs: timelineState.startDate.getTime(),
            endMs: timelineState.endDate.getTime(),
            unit: 'day',
        }];
    const timeScale = createTimelineScale(slots, timeAreaWidth);
    const windowStartMs = timeScale.windowStartMs;
    const windowEndMs = timeScale.windowEndMs;
    const windowDurationMs = Math.max(timeScale.windowDurationMs, 1);

    // Get user role once (outside loop for performance)
    const userRoleScript = document.getElementById('data-user-role');
    const userRole = userRoleScript ? JSON.parse(userRoleScript.textContent) : 'planner';
    console.log('ðŸ‘¤ User role detected:', userRole);
    console.log('  - Report button will be', userRole === 'supervisor' ? 'VISIBLE' : 'HIDDEN');

    // Build split group map
    const splitGroups = new Map();
    (tasksCache || []).forEach(t => {
        const key = getTimelineTaskGroupKey(t);
        if (!key || !t?.parent_id) return;
        if (!splitGroups.has(key)) splitGroups.set(key, []);
        splitGroups.get(key).push(t.id);
    });
    splitGroups.forEach((list, key) => {
        splitGroups.set(key, list.slice().sort((a, b) => a - b));
    });

    const selectedStageFilter = filterState.stage || 'all';
    const selectedStageData = resolveSelectedStageFilterData(selectedStageFilter);

    const stageRows = selectedStageFilter !== 'all' && selectedStageFilter !== '__none__'
        ? buildTimelineStageRows(stagesCache)
        : [];
    const sortedMachines = sortMachinesForTimeline(machinesCache).map(machine => ({
        ...machine,
        isStageRow: false,
    }));
    const timelineRows = [...stageRows, ...sortedMachines];
    const isNarrowViewport = (timelineState.layout?.usableWidth || 0) <= 640;
    const effectiveRowHeight = isNarrowViewport
        ? Math.min(timelineState.rowHeight || 80, 72)
        : (timelineState.rowHeight || 80);

    timelineRows.forEach(machine => {
        const row = document.createElement('div');
        const isStageRow = !!machine.isStageRow;
        const searchQuery = filterState.search;
        const machineLeftTasks = isStageRow
            ? tasksCache.filter(t => {
                const meta = getTaskStageMeta(t);
                if (machine.stageId) return String(meta.id || '') === String(machine.stageId);
                return normalizeSortToken(meta.name || '') === normalizeSortToken(machine.stageName || '');
            })
            : (machine.id === 'unassigned'
                ? tasksCache.filter(t => !t.machine_id)
                : tasksCache.filter(t => t.machine_id == machine.id));
        const statusFilter = normalizeTimelineStatusFilterToken(filterState.status || 'all');
        const visibleMachineTasks = machineLeftTasks.filter((task) => {
            const status = String(task?.status || '').toLowerCase();
            return status !== 'archived' && (status !== 'canceled' || statusFilter === 'canceled');
        });
        const isMaintenance = !isStageRow && isTimelineMachineFaultState(machine);
        const stageFilter = selectedStageFilter;
        const machineActivityFilter = filterState.machineActivity || 'all';

        if (statusFilter === 'maintenance' && !isMaintenance) {
            return;
        }

        if (machineActivityFilter === 'maintenance' && !isMaintenance) {
            return;
        }

        // Visual Cue: Striped Background for Maintenance
        let rowClass = "relative border-b border-gray-100 group";
        if (isMaintenance) {
            rowClass += " bg-red-50/30";
            row.style.backgroundImage = "repeating-linear-gradient(45deg, rgba(239, 68, 68, 0.03), rgba(239, 68, 68, 0.03) 10px, transparent 10px, transparent 20px)";
        } else {
            rowClass += " hover:bg-gray-50";
        }
        row.className = rowClass;

        row.style.display = 'grid';
        row.style.width = `${totalTimelineWidth}px`;
        row.style.gridTemplateColumns = gridTemplate;
        row.style.height = `${effectiveRowHeight}px`;

        // Machine Info Cell
        const infoCell = document.createElement('div');
        infoCell.className = `p-2 border-r border-gray-200 flex flex-col justify-center z-30 sticky left-0 shadow-sm ${isMaintenance ? 'bg-red-50' : 'bg-white'}`;

        // Only show Report button for supervisors on machine rows
        const reportButtonHTML = (!isStageRow && userRole === 'supervisor') ? `
            <button onclick="openFaultModal('${machine.id}', '${(machine.display_name || machine.name || '').replace(/'/g, "\\'")}')" class="mt-1 text-[10px] text-red-600 bg-red-50 hover:bg-red-100 border border-red-200 px-1.5 py-0.5 rounded flex items-center gap-1 w-fit transition-colors" title="Report Fault">
                <i class="ph ph-warning"></i> Report
            </button>
        ` : '';

        const machineImageHtml = isStageRow
            ? `<div class="w-8 h-8 rounded-lg bg-cyan-50 border border-cyan-100 flex items-center justify-center text-cyan-600"><i class="ph ph-git-branch text-sm"></i></div>`
            : machine.image_url
                ? `<img src="${machine.image_url}" class="w-8 h-8 rounded-lg object-cover border border-slate-200 shadow-sm" alt="${escapeHtml(machine.display_name || machine.name || 'Machine')}">`
                : `<div class="w-8 h-8 rounded-lg bg-slate-100 border border-slate-200 flex items-center justify-center text-slate-400"><i class="ph ph-gear text-xs"></i></div>`;

        const machineMeta = isStageRow
            ? (machine.stageMachineType || machine.type || '')
            : (machine.type || machine.category || '');
        const machineCode = String(machine.code || '').trim();
        const machineNameRaw = String(machine.name || '').trim();
        const machineDisplayRaw = String(machine.display_name || '').trim();
        const machinePresentation = isStageRow ? null : getTimelineMachinePresentation(machine);
        const machinePrimaryLabel = isStageRow
            ? (machineDisplayRaw || machineNameRaw || 'Unnamed resource')
            : (machinePresentation?.name || machinePresentation?.combinedLabel || machineCode || machineNameRaw || 'Unnamed resource');
        const machineSecondaryParts = isStageRow
            ? [machineMeta]
            : [machineMeta];
        const machineName = escapeHtml(machinePrimaryLabel);
        const isCompact = timelineState.rowDensity === 'compact' || isNarrowViewport;
        const isTightResourceColumn = machineColumnWidth < 240;
        const machineHoursSummary = !isStageRow
            ? String(machine.working_hours_summary || (machine.use_factory_shifts ? 'Factory hours' : '') || '').trim()
            : '';
        const showHoursBadge = !isStageRow && machineHoursSummary && !isCompact && !isTightResourceColumn;
        const machineMetaText = escapeHtml(
            machineSecondaryParts
                .concat(showHoursBadge ? [] : [machineHoursSummary])
                .filter(Boolean)
                .join(' • ')
        );
        const nameClass = isCompact ? 'text-[12px] leading-tight' : 'text-sm';
        const metaClass = isCompact ? 'text-[9px]' : 'text-[10px]';
        const statusClass = isCompact ? 'text-[9px]' : 'text-[10px]';
        const resourceSubline = machineMetaText;
        const resourceSublineClass = isStageRow
            ? `${metaClass} uppercase tracking-wide text-slate-500 font-semibold`
            : `${metaClass} text-slate-500 font-medium`;
        const machineHoursBadge = showHoursBadge
            ? `<div class="mt-1"><span class="inline-flex items-center rounded-full border border-blue-100 bg-blue-50 px-2 py-0.5 text-[9px] font-semibold text-blue-700">${escapeHtml(machineHoursSummary)}</span></div>`
            : '';
        const machineCodeBadgeHtml = !isStageRow && machinePresentation?.showCodeBadge
            ? `<span class="inline-flex shrink-0 items-center rounded-md border border-slate-200 bg-slate-100 px-1.5 py-0.5 font-mono text-[10px] font-semibold text-slate-700">${escapeHtml(machinePresentation.code)}</span>`
            : '';
        const machineLampHtml = !isStageRow
            ? renderTimelineMachineLamps(machine, visibleMachineTasks)
            : '';

        const statusLine = isStageRow
            ? `<div class="mt-1"><span class="${statusClass} text-blue-600 uppercase font-bold tracking-wide">Stage Lane</span></div>`
            : '';

        const resourceTitle = isStageRow
            ? `${machineDisplayRaw || machine.name || 'Unnamed resource'}${machine.stageId ? ` (Stage ID: ${machine.stageId})` : ''}${machine.stageMachineType ? ` - ${machine.stageMachineType}` : ''}`
            : `${machineDisplayRaw || machineCode || machineNameRaw || 'Unnamed resource'}${machine.id !== undefined && machine.id !== null && String(machine.id) !== '' ? ` (ID: ${machine.id})` : ''}${machineMeta ? ` - ${machineMeta}` : ''}`;

        infoCell.title = resourceTitle;

        infoCell.innerHTML = `
            <div class="flex items-center gap-2">
                ${isStageRow ? machineImageHtml : ''}
                <div class="min-w-0">
                    <div class="${isStageRow ? '' : 'flex items-center gap-1.5 min-w-0'}">
                        ${machineCodeBadgeHtml}
                        <div class="font-bold ${nameClass} text-gray-800 truncate" title="${escapeHtml(resourceTitle)}">${machineName}</div>
                    </div>
                    ${resourceSubline ? `<div class="${resourceSublineClass} truncate" title="${escapeHtml(resourceTitle)}">${resourceSubline}</div>` : ''}
                    ${machineLampHtml}
                    ${statusLine}
                    ${machineHoursBadge}
                </div>
            </div>
            ${reportButtonHTML}
        `;
        row.appendChild(infoCell);

        // Grid Cells
        for (let i = 0; i < totalColumns; i++) {
            const cell = document.createElement('div');
            cell.className = "border-r border-gray-100 h-full w-full";
            const slot = slots[i];
            if (isTimelineSlotNonWorking(slot, machine)) {
                cell.classList.add('bg-slate-50');
                cell.style.backgroundImage = 'repeating-linear-gradient(135deg, rgba(148,163,184,0.08), rgba(148,163,184,0.08) 6px, transparent 6px, transparent 12px)';
                if (!isStageRow && machineHoursSummary) {
                    cell.title = `Non-working time for ${machine.display_name || machine.name || 'machine'} (${machineHoursSummary})`;
                }
            }
            if (isMaintenance) {
                cell.title = "Machine Maintenance - Locked";
                cell.style.cursor = "not-allowed";
            }
            row.appendChild(cell);
        }

        // Task Container
        const taskLayer = document.createElement('div');
        taskLayer.className = "absolute inset-0 z-20 pointer-events-none"; // Events pass through
        taskLayer.style.left = `${machineColumnWidth}px`;
        taskLayer.style.width = `${timeAreaWidth}px`;

        // Filter Tasks (Smart Search: ID, Product, or Machine Name)

        // Utilization strip (based on tasks within current window)
        const utilMs = (visibleMachineTasks || []).reduce((sum, t) => {
            if (!t.start || !t.end) return sum;
            const s = new Date(t.start).getTime();
            const e = new Date(t.end).getTime();
            if (!Number.isFinite(s) || !Number.isFinite(e) || e <= s) return sum;
            return sum + timeScale.getVisibleOverlapMs(s, e);
        }, 0);
        const utilPct = Math.max(0, Math.min(100, Math.round((utilMs / windowDurationMs) * 100)));
        const utilColor = utilPct > 70 ? '#ef4444' : utilPct > 40 ? '#f59e0b' : '#10b981';
        const utilContainer = infoCell.querySelector('.min-w-0');
        if (utilContainer && !isCompact) {
            const utilWrap = document.createElement('div');
            utilWrap.className = 'mt-1';
            utilWrap.innerHTML = `
                <div class="h-1.5 bg-slate-200 rounded-full overflow-hidden">
                    <div style="width:${utilPct}%; background:${utilColor}; height:100%;"></div>
                </div>
                <div class="text-[9px] text-slate-400 mt-1">Utilization ${utilPct}%</div>
            `;
            utilContainer.appendChild(utilWrap);
        }

        let machineTasks = buildTimelineVisualTasks(visibleMachineTasks);
        if (statusFilter && statusFilter !== 'all' && statusFilter !== 'maintenance') {
            machineTasks = machineTasks.filter(t => {
                const status = String(t.status || '').toLowerCase();
                if (statusFilter === 'active') {
                    return status === 'in_progress';
                }
                if (statusFilter === 'pending') {
                    return ['pending', 'hold'].includes(status);
                }
                if (statusFilter === 'completed') {
                    return ['completed', 'done'].includes(status);
                }
                if (statusFilter === 'canceled') {
                    return status === 'canceled';
                }
                return true;
            });
        }

        if (stageFilter && stageFilter !== 'all') {
            machineTasks = machineTasks.filter(t => {
                const meta = getTaskStageMeta(t);
                if (stageFilter === '__none__') {
                    return !meta.id && !meta.name;
                }
                return String(meta.id || meta.name) === String(stageFilter);
            });
        }
        const rowStageKey = String(machine.stageId || machine.stageName || '');
        const rowMatchesSelectedStage = isStageRow && stageFilter !== 'all' && stageFilter !== '__none__'
            ? rowStageKey === String(stageFilter)
            : false;

        if (isStageRow) {
            if (stageFilter === '__none__') {
                return;
            }
            if (stageFilter !== 'all' && !rowMatchesSelectedStage) {
                return;
            }
        }

        const machineSupportsStage = !isStageRow && stageFilter !== 'all' && stageFilter !== '__none__'
            ? machineSupportsSelectedStage(machine, selectedStageData)
            : false;

        if (stageFilter === '__none__' && machineTasks.length === 0) {
            return;
        }

        if (stageFilter !== 'all' && stageFilter !== '__none__' && machineTasks.length === 0 && !machineSupportsStage && !rowMatchesSelectedStage) {
            return;
        }

        const machineSearchBlob = [
            machine.display_name || '',
            machine.code || '',
            machine.name || '',
            machine.stageName || '',
            machine.type || '',
            machine.category || ''
        ].join(' ').toLowerCase();
        const machineNameMatch = machineSearchBlob.includes(searchQuery);

        if (searchQuery) {
            const baseTasksForSearch = machineTasks;
            machineTasks = baseTasksForSearch.filter(t => {
                const matchProduct = t.product && t.product.toLowerCase().includes(searchQuery);
                const matchId = t.id && t.id.toString().includes(searchQuery);
                return matchProduct || matchId || machineNameMatch;
            });
        }

        const hasActiveTask = machineTasks.some(t => String(t.status || '').toLowerCase() === 'in_progress');
        if (machineActivityFilter === 'active' && !hasActiveTask) {
            return;
        }
        if (machineActivityFilter === 'idle' && hasActiveTask) {
            return;
        }

        // HIDE ROW if: Search is active AND Machine doesn't match AND No matching tasks
        if (searchQuery && !machineNameMatch && machineTasks.length === 0) {
            // console.log(`   â›” Hiding Machine: ${machine.name} (No match, 0 tasks)`);
            return; // Skip this iteration (Hide Row) // Skip this iteration (Hide Row)
        } else if (searchQuery) {
            console.log(`   âœ… Showing Machine: ${machine.name} (Match: ${machineNameMatch}, Tasks: ${machineTasks.length})`);
        }

        const rowHeight = effectiveRowHeight;
        const taskHeight = Math.max(rowHeight - 22, 26);
        const taskTop = Math.round((rowHeight - taskHeight) / 2);

        if (stageFilter !== 'all' && stageFilter !== '__none__' && machineTasks.length === 0 && (machineSupportsStage || rowMatchesSelectedStage)) {
            const placeholder = document.createElement('div');
            placeholder.className = 'absolute inset-y-1 left-2 right-2 rounded-lg border border-dashed border-slate-300 bg-slate-50/70 text-slate-500 text-[11px] font-semibold flex items-center justify-center';
            placeholder.style.pointerEvents = 'none';
            placeholder.textContent = selectedStageData?.label
                ? `No scheduled WO for stage: ${selectedStageData.label}`
                : 'No scheduled WO for selected stage';
            taskLayer.appendChild(placeholder);
        }

        machineTasks.forEach(t => {
            const startValue = t.start || timelineState.startDate || new Date();
            const tStart = new Date(startValue).getTime();
            const tEnd = t.end ? new Date(t.end).getTime() : tStart + (3600000); // default 1hr

            if (!Number.isFinite(tStart) || !Number.isFinite(tEnd) || tEnd <= tStart) return;

            const position = timeScale.getIntervalPosition(tStart, tEnd);
            if (!position) return;

            const durationMs = tEnd - tStart;
            const leftPx = position.leftPx;
            const widthPx = position.widthPx;

            // Shared work-order palette so all stages under one parent route keep the same color.
            const palette = getTimelineWorkOrderPalette(t);
            const baseColor = palette.solid;
            const fadedColor = palette.tint;

            const quantity = Number(t.progress_stats ? t.progress_stats.target : (t.quantity || 0)) || 0;
            const finished = Number(t.progress_stats ? t.progress_stats.actual : (t.finished_qty || 0)) || 0;
            const progressValue = Number(t.progress);
            const reportedProgress = quantity > 0 ? (finished / quantity) * 100 : 0;
            const rawProgress = reportedProgress > 0
                ? reportedProgress
                : (Number.isFinite(progressValue) ? progressValue : 0);
            const progress = Math.max(0, Math.min(rawProgress, 100));
            const expectedProgress = getTimelineExpectedProgressPercent(tStart, tEnd, t.status);
            const expectedProgressRounded = expectedProgress === null ? null : Math.round(expectedProgress);
            const isExpectedOverdue = expectedProgress !== null && Date.now() >= tEnd && progress < 100;
            const isBehindExpected = expectedProgress !== null && progress + 1 < expectedProgress;
            const expectedProgressGap = expectedProgress === null ? 0 : Math.max(expectedProgress - progress, 0);
            const expectedProgressGapRounded = Math.round(expectedProgressGap);
            const expectedMarkerColor = isExpectedOverdue
                ? '#ef4444'
                : (isBehindExpected ? '#f97316' : '#22c55e');
            const expectedMarkerTitle = expectedProgress === null
                ? ''
                : `System expected progress: ${expectedProgressRounded}% by elapsed planned time. Reported progress: ${Math.round(progress)}%. Gap: ${expectedProgressGapRounded}%.`;
            const expectedProgressMarkerHtml = (timelineShowProgress && expectedProgress !== null) ? `
                <div class="absolute top-0 bottom-0 pointer-events-none" style="left:${expectedProgress.toFixed(2)}%; width:2px; transform:translateX(-1px); background:${expectedMarkerColor}; box-shadow:0 0 0 1px rgba(255,255,255,0.75), 0 0 8px rgba(15,23,42,0.35); z-index:22;" aria-label="System expected progress marker">
                    <span style="position:absolute; top:0; left:50%; transform:translateX(-50%); width:0; height:0; border-left:5px solid transparent; border-right:5px solid transparent; border-top:7px solid ${expectedMarkerColor};"></span>
                </div>
            ` : '';
            const expectedProgressText = expectedProgress === null
                ? `Progress: ${Math.round(progress)}%`
                : `Exp ${expectedProgressRounded}% / Act ${Math.round(progress)}%`;
            const expectedGapBadgeHtml = (timelineShowProgress && expectedProgress !== null && isBehindExpected) ? `
                <span class="px-1 py-0.5 rounded-[4px] text-[6px] font-black uppercase tracking-tighter border ${isExpectedOverdue ? 'bg-red-500/30 border-red-200/50 text-red-50' : 'bg-orange-400/30 border-orange-200/50 text-orange-50'}">
                    ${isExpectedOverdue ? 'Overdue' : 'Behind'} ${expectedProgressGapRounded}%
                </span>
            ` : '';

            // Styles
            const taskDiv = document.createElement('div');
            // Base classes
            taskDiv.className = "absolute rounded-lg shadow-lg border-0 overflow-hidden pointer-events-auto cursor-pointer hover:ring-2 hover:ring-white hover:scale-105 transition-all group";
            taskDiv.style.height = `${taskHeight}px`;
            taskDiv.style.top = `${taskTop}px`;

            taskDiv.style.background = fadedColor;
            taskDiv.className += " text-white";
            taskDiv.style.left = `${leftPx}px`;
            taskDiv.style.width = `${Math.max(widthPx, 1)}px`; // Min width visibility
            const splitMeta = getTimelineSplitVisualMeta(t, splitGroups);
            const splitTitle = splitMeta
                ? `${splitMeta.parentCode} ${splitMeta.segmentLabel}${splitMeta.statusSummary ? ` (${splitMeta.statusSummary})` : ''}`
                : '';
            taskDiv.setAttribute('aria-label', [
                t.product || getDisplayWorkOrderCode(t),
                splitTitle,
                `Reported progress: ${Math.round(progress)}%`,
                expectedMarkerTitle,
            ].filter(Boolean).join(' | '));

            // Late Order Logic
            const isLate = t.is_late === true;
            if (isExpectedOverdue) {
                taskDiv.className += " ring-2 ring-red-500 ring-offset-1";
            } else if (isBehindExpected) {
                taskDiv.className += " ring-2 ring-orange-400 ring-offset-1";
            } else if (isLate) {
                taskDiv.className += " ring-2 ring-red-500 ring-offset-1";
            }
            if (splitMeta) {
                taskDiv.dataset.timelineSplitGroup = splitMeta.groupKey || '';
                taskDiv.className += splitMeta.isGroup ? " ring-2 ring-indigo-300/80" : " ring-1 ring-indigo-200/70";
            }

            // Setup segment width should reflect real setup time, not a fixed percent.
            const setupMinutes = Number(t.setup_minutes || 0);
            const actualDurationMinutes = Math.max(Math.round(durationMs / 60000), 1);
            const derivedDuration = Number(t.estimated_duration_minutes || actualDurationMinutes || 0);
            let setupPct = 0;
            if (setupMinutes > 0 && actualDurationMinutes > 0) {
                // Use visual block duration so setup lane always matches the timeline axis minutes.
                setupPct = (setupMinutes / actualDurationMinutes) * 100;
            }
            setupPct = Math.max(0, Math.min(setupPct, 80));
            const showSetupLane = setupPct > 0.1;
            const bufferWidth = `${setupPct.toFixed(2)}%`;
            const mainWidth = `${Math.max(100 - setupPct, 0).toFixed(2)}%`;

            const progressTrackColor = 'rgba(255,255,255,0.25)';
            const progressFillColor = 'rgba(255,255,255,0.9)';

            const compactTaskBar = widthPx < 260;
            const tinyTaskBar = widthPx < 160;
            const splitRouteBadgeHtml = splitMeta ? `
                <span class="inline-flex min-w-0 max-w-full items-center gap-1 rounded-[5px] border border-white/35 bg-white/18 px-1.5 py-0.5 text-[7px] font-black uppercase tracking-tight text-white shadow-sm" aria-label="${escapeHtml(splitMeta.statusSummary || splitMeta.segmentLabel)}">
                    <i class="ph ph-git-branch text-[8px]"></i>
                    ${tinyTaskBar ? '' : `<span class="shrink-0">${escapeHtml(compactTaskBar ? 'Split' : splitMeta.parentCode)}</span>`}
                    <span class="min-w-0 truncate opacity-80">${escapeHtml(splitMeta.segmentLabel)}</span>
                </span>
            ` : '';
            const qcBadge = t.qc_requirement ? `<span class="px-1 py-0.5 rounded-[4px] text-[6px] font-black uppercase tracking-tighter border bg-rose-400/20 border-rose-400/40 text-rose-100">QC</span>` : '';
            const compQty = getCompensationQty(t);
            const compBadge = compQty > 0
                ? `<span class="px-1 py-0.5 rounded-[4px] text-[6px] font-black uppercase tracking-tighter border bg-cyan-400/20 border-cyan-400/40 text-cyan-100">+${compQty} Scrap</span>`
                : '';
            const compTaskBadge = t.is_scrap_compensation_task
                ? `<span class="px-1 py-0.5 rounded-[4px] text-[6px] font-black uppercase tracking-tighter border bg-fuchsia-400/20 border-fuchsia-400/40 text-fuchsia-100">Comp Task</span>`
                : '';

            taskDiv.innerHTML = `
                ${timelineShowProgress ? `
                <div class="absolute inset-y-0 left-0 rounded-l-lg pointer-events-none" style="width:${progress}%; background:${baseColor};"></div>
                ` : ''}
                ${expectedProgressMarkerHtml}
                <div class="flex h-full w-full text-white relative">
                    ${showSetupLane ? `
                    <div class="h-full relative border-r border-white/20 overflow-hidden" 
                         style="flex: 0 0 ${bufferWidth}; max-width: ${bufferWidth}; min-width: 0; background: repeating-linear-gradient(45deg, rgba(255,255,255,0.1), rgba(255,255,255,0.1) 4px, transparent 4px, transparent 8px);"
                         title="Setup ${formatManufacturingDurationFromMinutes(setupMinutes)} of ${formatManufacturingDurationFromMinutes(actualDurationMinutes)}">
                         ${setupPct >= 6 ? '<div class="absolute inset-0 flex items-center justify-center text-[7px] font-bold rotate-90 opacity-80" title="Setup">SETUP</div>' : ''}
                    </div>` : ''}
                    <div class="p-1.5 flex flex-col justify-between min-w-0" style="flex: 1 1 ${mainWidth}; width: ${mainWidth};">
                         <div class="flex justify-between items-start leading-none gap-2">
                            <span class="font-bold ${tinyTaskBar ? 'text-[8px]' : 'text-[10px]'} truncate max-w-full" style="color: white">${t.product}</span>
                            ${t.assignment_type ? `
                                <span class="px-1 py-0.5 rounded-[4px] text-[6px] font-black uppercase tracking-tighter border ${t.assignment_type === 'auto' ? 'bg-emerald-400/20 border-emerald-400/40 text-emerald-100' : 'bg-amber-400/20 border-amber-400/40 text-amber-100'}">
                                    ${t.assignment_type}
                                </span>
                            ` : ''}
                         </div>
                         ${!tinyTaskBar && (splitRouteBadgeHtml || qcBadge || compBadge || compTaskBadge || expectedGapBadgeHtml) ? `
                         <div class="flex items-center gap-1 mt-1 min-w-0 overflow-hidden">
                            ${splitRouteBadgeHtml}
                            ${qcBadge}
                            ${compBadge}
                            ${compTaskBadge}
                            ${expectedGapBadgeHtml}
                         </div>` : ''}
                         
                         <div class="flex items-center justify-between mt-auto">
                            ${timelineShowAssignee ? `
                            <div class="flex items-center gap-1.5 min-w-0">
                                <i class="ph ph-user text-[10px] opacity-80"></i>
                                <span class="text-[9px] font-black tracking-tight truncate" style="color: rgba(255,255,255,0.9)">
                                    ${t.assigned_worker_name || 'Unassigned'}
                                </span>
                            </div>` : '<div></div>'}
                            ${timelineShowProgress ? (window.currentUserRole === 'supervisor' ? `
                                <span class="text-[9px] font-mono font-bold opacity-90 drop-shadow-md">
                                    Qty: ${finished} / ${quantity}
                                </span>
                            ` : `
                                <span class="text-[8px] font-bold opacity-80 uppercase tracking-widest">
                                    ${t.status === 'done' ? 'COMPLETED' : expectedProgressText}
                                </span>
                            `) : ''}
                        </div>
                    </div>
                    ${timelineShowProgress ? `<div class="absolute left-0 right-0 bottom-0 h-1.5" style="background: ${progressTrackColor};">
                        <div class="h-full" style="width: ${progress}%; background: ${progressFillColor};"></div>
                    </div>` : ''}
                </div>
            `;

            const dragPayload = {
                id: t.id,
                dragType: 'timeline-task',
                content: t.product || getDisplayWorkOrderCode(t),
                status: t.status || 'pending',
                machine_id: t.machine_id || null,
                stage_id: t.stage_id || null,
                parent_id: t.parent_id || null,
                source_task_id: t.source_task_id || null,
                required_type: getCurrentStageRequiredType(t, stagesCache) || machine.stageMachineType || machine.type || machine.category || '',
                source_ids: Array.isArray(t.source_ids) ? t.source_ids : [t.id],
                duration_ms: Math.max(durationMs, 60000),
            };
            let suppressOpenUntil = 0;
            const normalizedRole = String(userRole || '').toLowerCase();
            const normalizedStatus = String(t.status || '').toLowerCase();
            const isEditEnabled = typeof window.isTimelineEditEnabled === 'function'
                ? window.isTimelineEditEnabled()
                : true;
            // Timeline cards remain draggable, but edge-resizing is disabled.
            const canResizeDuration = false;

            if (canResizeDuration) {
                const leftHandle = document.createElement('div');
                leftHandle.className = 'absolute top-0 left-0 h-full w-2.5 cursor-ew-resize z-30 border-r border-white/40 bg-white/20 hover:bg-white/40';
                leftHandle.style.touchAction = 'none';
                leftHandle.title = 'Drag to adjust WO start';
                leftHandle.setAttribute('aria-label', 'Resize work order start');
                taskDiv.appendChild(leftHandle);

                const rightHandle = document.createElement('div');
                rightHandle.className = 'absolute top-0 right-0 h-full w-2.5 cursor-ew-resize z-30 border-l border-white/40 bg-white/20 hover:bg-white/40';
                rightHandle.style.touchAction = 'none';
                rightHandle.title = 'Drag to adjust WO end';
                rightHandle.setAttribute('aria-label', 'Resize work order end');
                taskDiv.appendChild(rightHandle);

                const minDurationMs = 15 * 60 * 1000;
                let isResizing = false;
                let resizeEdge = 'end';
                let liveStartMs = tStart;
                let liveEndMs = tEnd;
                let originalLeftPx = Math.max(leftPx, 0);
                let originalWidthPx = Math.max(widthPx, 1);
                const taskRef = (tasksCache || []).find(item => String(item.id) === String(t.id));
                const previousEndIso = taskRef?.end || t.end || new Date(tEnd).toISOString();
                const previousStartIso = taskRef?.start || t.start || new Date(tStart).toISOString();
                const previousCursor = document.body.style.cursor;
                const previousUserSelect = document.body.style.userSelect;

                const clampResizeRange = (startMs, endMs, edge) => {
                    let nextStart = Number(startMs);
                    let nextEnd = Number(endMs);

                    if (!Number.isFinite(nextStart) || !Number.isFinite(nextEnd)) {
                        nextStart = tStart;
                        nextEnd = tEnd;
                    }

                    if (nextEnd - nextStart < minDurationMs) {
                        if (edge === 'start') {
                            nextStart = nextEnd - minDurationMs;
                        } else {
                            nextEnd = nextStart + minDurationMs;
                        }
                    }

                    if (nextStart < windowStartMs) {
                        nextStart = windowStartMs;
                        nextEnd = Math.max(nextEnd, nextStart + minDurationMs);
                    }
                    if (nextEnd > windowEndMs) {
                        nextEnd = windowEndMs;
                        nextStart = Math.min(nextStart, nextEnd - minDurationMs);
                    }

                    if (nextEnd - nextStart < minDurationMs) {
                        nextEnd = Math.min(windowEndMs, nextStart + minDurationMs);
                    }

                    return {
                        startMs: nextStart,
                        endMs: Math.max(nextEnd, nextStart + 60000),
                    };
                };

                const updateResizeVisual = (startMs, endMs) => {
                    const position = timeScale.getIntervalPosition(startMs, endMs);
                    if (!position) return false;
                    taskDiv.style.left = `${position.leftPx}px`;
                    taskDiv.style.width = `${Math.max(position.widthPx, 1)}px`;
                    const durMinutes = Math.max(Math.round((endMs - startMs) / 60000), 1);
                    leftHandle.title = `Start ${new Date(startMs).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}`;
                    rightHandle.title = `End ${new Date(endMs).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })} (${formatManufacturingDurationFromMinutes(durMinutes)})`;
                    return true;
                };

                const getSnappedPointerMs = (clientX) => {
                    const rowRect = row.getBoundingClientRect();
                    let offsetX = clientX - rowRect.left - machineColumnWidth;
                    if (!Number.isFinite(offsetX)) offsetX = 0;
                    const clamped = Math.max(0, Math.min(offsetX, Math.max(timeAreaWidth - 1, 0)));
                    const pointerDate = timeScale.getDateFromOffsetPx(clamped);
                    const snappedDate = snapDateToTimelineGrid(pointerDate) || pointerDate;
                    return Number.isFinite(snappedDate?.getTime?.()) ? snappedDate.getTime() : NaN;
                };

                const cleanupResize = () => {
                    isResizing = false;
                    resizeEdge = 'end';
                    taskDiv.draggable = isEditEnabled;
                    document.body.style.cursor = previousCursor;
                    document.body.style.userSelect = previousUserSelect;
                    window.removeEventListener('pointermove', onPointerMove);
                    window.removeEventListener('pointerup', onPointerUp);
                    window.removeEventListener('pointercancel', onPointerCancel);
                };

                const onPointerMove = (evt) => {
                    if (!isResizing) return;
                    evt.preventDefault();
                    const pointerMs = getSnappedPointerMs(evt.clientX);
                    if (!Number.isFinite(pointerMs)) return;

                    let nextStart = liveStartMs;
                    let nextEnd = liveEndMs;
                    if (resizeEdge === 'start') {
                        nextStart = pointerMs;
                    } else {
                        nextEnd = pointerMs;
                    }

                    const clamped = clampResizeRange(nextStart, nextEnd, resizeEdge);
                    if (!updateResizeVisual(clamped.startMs, clamped.endMs)) return;
                    liveStartMs = clamped.startMs;
                    liveEndMs = clamped.endMs;
                };

                const persistResize = () => {
                    const previousStartMs = new Date(previousStartIso).getTime();
                    const previousEndMs = new Date(previousEndIso).getTime();
                    if (
                        Number.isFinite(previousStartMs)
                        && Number.isFinite(previousEndMs)
                        && Math.abs(liveStartMs - previousStartMs) < 500
                        && Math.abs(liveEndMs - previousEndMs) < 500
                    ) {
                        updateResizeVisual(previousStartMs, previousEndMs);
                        return;
                    }

                    const conflictTask = findTimelinePlacementConflict(dragPayload, {
                        isStageRow: !!isStageRow,
                        targetMachineId: isStageRow
                            ? (taskRef?.machine_id ?? t.machine_id ?? machine.defaultMachineId ?? null)
                            : (machine.id === 'unassigned' ? null : machine.id),
                        targetStageId: isStageRow
                            ? (machine.stageId || taskRef?.stage_id || t.stage_id || null)
                            : (taskRef?.stage_id ?? t.stage_id ?? null),
                        startMs: liveStartMs,
                        endMs: liveEndMs,
                    });
                    if (conflictTask) {
                        showTimelineToast(`Cannot resize: conflicts with ${getDisplayWorkOrderHashLabel(conflictTask)}`, 'warning');
                        renderTimeline();
                        return;
                    }

                    suppressOpenUntil = Date.now() + 350;
                    const resizedStartIso = new Date(liveStartMs).toISOString();
                    const resizedEndIso = new Date(liveEndMs).toISOString();
                    const undoAction = buildTimelineTaskUndoAction(taskRef || t, `Undo resize for ${getDisplayWorkOrderHashLabel(taskRef || t)}`);
                    if (taskRef) {
                        taskRef.start = resizedStartIso;
                        taskRef.end = resizedEndIso;
                    }
                    setTaskSavingState(t.id, true);

                    const formData = new FormData();
                    formData.append('id', t.id);
                    const machineId = taskRef?.machine_id ?? t.machine_id ?? null;
                    const stageId = taskRef?.stage_id ?? t.stage_id ?? null;
                    const status = taskRef?.status ?? t.status ?? 'pending';
                    if (machineId !== null && machineId !== undefined && String(machineId) !== '') {
                        formData.append('machine_id', String(machineId));
                    }
                    if (stageId !== null && stageId !== undefined && String(stageId) !== '') {
                        formData.append('stage_id', String(stageId));
                    }
                    formData.append('status', String(status));
                    formData.append('start_date', resizedStartIso);
                    formData.append('end_date', resizedEndIso);

                    fetch('/manufacturing/api/work-order/' + t.id + '/update/', {
                        method: 'POST',
                        body: formData
                    })
                        .then(res => res.json())
                        .then(resp => {
                            if (!resp.success) {
                                throw new Error(resp.error || 'Unable to update work order duration');
                            }
                            recordTimelineUndoAction(undoAction);
                            showTimelineToast('Work order duration updated', 'success');
                            window.initGanttChart(true);
                        })
                        .catch((err) => {
                            if (taskRef) {
                                taskRef.start = previousStartIso;
                                taskRef.end = previousEndIso;
                            }
                            showTimelineToast(err?.message || 'Failed to resize work order', 'error');
                            renderTimeline();
                        })
                        .finally(() => {
                            setTaskSavingState(t.id, false);
                        });
                };

                const onPointerUp = () => {
                    if (!isResizing) return;
                    cleanupResize();
                    persistResize();
                };

                const onPointerCancel = () => {
                    if (!isResizing) return;
                    taskDiv.style.left = `${originalLeftPx}px`;
                    taskDiv.style.width = `${originalWidthPx}px`;
                    cleanupResize();
                };

                const startResize = (edge, evt) => {
                    evt.preventDefault();
                    evt.stopPropagation();
                    isResizing = true;
                    resizeEdge = edge;
                    originalLeftPx = Math.max(parseFloat(taskDiv.style.left) || leftPx, 0);
                    originalWidthPx = Math.max(parseFloat(taskDiv.style.width) || widthPx, 1);
                    liveStartMs = parseTaskDateMs(taskRef?.start || t.start) || tStart;
                    liveEndMs = parseTaskDateMs(taskRef?.end || t.end) || tEnd;
                    taskDiv.draggable = false;
                    document.body.style.cursor = 'ew-resize';
                    document.body.style.userSelect = 'none';
                    window.addEventListener('pointermove', onPointerMove);
                    window.addEventListener('pointerup', onPointerUp);
                    window.addEventListener('pointercancel', onPointerCancel);
                    try {
                        if (evt.pointerId !== undefined) {
                            if (edge === 'start' && leftHandle.setPointerCapture) {
                                leftHandle.setPointerCapture(evt.pointerId);
                            }
                            if (edge === 'end' && rightHandle.setPointerCapture) {
                                rightHandle.setPointerCapture(evt.pointerId);
                            }
                        }
                    } catch (captureErr) {
                        // Ignore pointer capture failures.
                    }
                };

                leftHandle.addEventListener('pointerdown', (evt) => startResize('start', evt));
                rightHandle.addEventListener('pointerdown', (evt) => startResize('end', evt));
            }

            taskDiv.classList.add('timeline-task');
            taskDiv.dataset.taskId = String(t.id);
            taskDiv.setAttribute('tabindex', '0');
            taskDiv.draggable = isEditEnabled;
            taskDiv.addEventListener('dragstart', (evt) => {
                if (!isEditEnabled) {
                    evt.preventDefault();
                    showTimelineToast('Enable edit mode to move work orders', 'warning');
                    return;
                }
                if (!evt.dataTransfer) return;
                evt.stopPropagation();
                evt.dataTransfer.effectAllowed = 'move';
                evt.dataTransfer.dropEffect = 'move';
                evt.dataTransfer.setData('text/plain', JSON.stringify(dragPayload));
                window.currentlyDraggingAllowedTypes = [];
                window.currentTimelineDragPayload = dragPayload;
                taskDiv.classList.add('opacity-70');
            });
            taskDiv.addEventListener('dragend', () => {
                taskDiv.classList.remove('opacity-70');
                if (typeof window.handleDragEnd === 'function') {
                    window.handleDragEnd();
                }
            });

            taskDiv.onclick = (e) => {
                e.stopPropagation();
                if (Date.now() < suppressOpenUntil) return;
                if (window.openEditSheet) window.openEditSheet(t.id);
            };
            taskDiv.oncontextmenu = (e) => showTimelineContextMenu(t, e);
            taskDiv.addEventListener('mouseenter', (e) => showTimelineTooltip(t, e, { anchorEl: taskDiv }));
            taskDiv.addEventListener('mousemove', (e) => moveTimelineTooltip(e, taskDiv));
            taskDiv.addEventListener('mouseleave', hideTimelineTooltip);
            taskDiv.addEventListener('focus', (e) => showTimelineTooltip(t, e, { anchorEl: taskDiv }));
            taskDiv.addEventListener('blur', hideTimelineTooltip);

            taskLayer.appendChild(taskDiv);
        });

        row.appendChild(taskLayer);

        // Drag Interactions
        row.dataset.machineType = machine.type || machine.category || machine.stageMachineType || "General";
        row.className += " machine-row transition-opacity duration-200";

        row.ondragover = (e) => {
            e.preventDefault();
            clearDropPreview(row);

            if (typeof window.isTimelineEditEnabled === 'function' && !window.isTimelineEditEnabled()) {
                e.dataTransfer.dropEffect = 'none';
                row.classList.add('cursor-not-allowed');
                return;
            }

            if (isMaintenance) {
                e.dataTransfer.dropEffect = 'none';
                return;
            }

            const activeDragPayload = parseTimelineDragPayload(e) || window.currentTimelineDragPayload || {};
            const activeDragType = activeDragPayload.dragType || 'queue-item';
            if (isStageRow && activeDragType !== 'timeline-task') {
                e.dataTransfer.dropEffect = 'none';
                row.classList.add('cursor-not-allowed');
                return;
            }

            const existingTask = (tasksCache || []).find(t => String(t.id) === String(activeDragPayload.id));
            const draggedTypes = getTimelineDragRequiredTypes(activeDragPayload, existingTask);

            let isValid = true;
            if (draggedTypes && draggedTypes.length > 0) {
                const matchesAnyRequiredType = draggedTypes.some((requiredType) => {
                    if (String(requiredType).trim().toLowerCase() === 'general') return true;
                    return machineMatchesRequiredType(machine, requiredType);
                });
                if (!matchesAnyRequiredType) {
                    isValid = false;
                }
            }

            if (isValid) {
                const dropDate = getDropDateFromPointer(row, {
                    rowData: machine,
                    isStageRow,
                    machineColumnWidth,
                    timeAreaWidth,
                    windowStartMs,
                    windowDurationMs,
                    timeScale,
                }, e.clientX);

                let conflictTask = null;
                if (dropDate) {
                    if (isTimelineStartBeforeNow(dropDate)) {
                        e.dataTransfer.dropEffect = 'none';
                        setDropInvalidPreview(row, 'Cannot schedule before current time');
                        return;
                    }

                    const durationMs = getDragTaskDurationMs(existingTask, activeDragPayload);
                    const dropEnd = new Date(dropDate.getTime() + durationMs);
                    const targetMachineId = isStageRow
                        ? (existingTask?.machine_id ?? activeDragPayload.machine_id ?? machine.defaultMachineId ?? null)
                        : (machine.id === 'unassigned' ? null : machine.id);
                    const targetStageId = isStageRow
                        ? (machine.stageId || existingTask?.stage_id || activeDragPayload.stage_id || null)
                        : (existingTask?.stage_id || activeDragPayload.stage_id || null);

                    conflictTask = findTimelinePlacementConflict(activeDragPayload, {
                        isStageRow,
                        targetMachineId,
                        targetStageId,
                        startMs: dropDate.getTime(),
                        endMs: dropEnd.getTime(),
                    });
                }

                if (conflictTask) {
                    e.dataTransfer.dropEffect = 'none';
                    row.classList.add('cursor-not-allowed');
                    setDropConflictPreview(row, conflictTask);
                } else {
                    e.dataTransfer.dropEffect = activeDragType === 'timeline-task' ? 'move' : 'copy';
                    row.classList.add('bg-blue-50');
                }
            } else {
                e.dataTransfer.dropEffect = 'none';
                setDropInvalidPreview(row, `Invalid stage category. Requires: ${draggedTypes.join(', ')}`);
            }
        };

        row.ondragleave = () => {
            clearDropPreview(row);
        };

        row.ondrop = (e) => {
            clearDropPreview(row);
            e.preventDefault();

            if (typeof window.isTimelineEditEnabled === 'function' && !window.isTimelineEditEnabled()) {
                showTimelineToast('Enable edit mode to move work orders', 'warning');
                return;
            }

            const activeDragPayload = parseTimelineDragPayload(e) || window.currentTimelineDragPayload || {};
            const activeDragType = activeDragPayload.dragType || 'queue-item';
            if (isStageRow && activeDragType !== 'timeline-task') return;

            const existingTask = (tasksCache || []).find(t => String(t.id) === String(activeDragPayload.id));
            const draggedTypes = getTimelineDragRequiredTypes(activeDragPayload, existingTask);
            if (draggedTypes && draggedTypes.length > 0) {
                const matchesAnyRequiredType = draggedTypes.some((requiredType) => {
                    if (String(requiredType).trim().toLowerCase() === 'general') return true;
                    return machineMatchesRequiredType(machine, requiredType);
                });
                if (!matchesAnyRequiredType) {
                    showTimelineToast(`Invalid stage category. Requires: ${draggedTypes.join(', ')}`, 'warning');
                    return;
                }
            }

            handleRowDrop(
                e,
                {
                    rowData: machine,
                    isStageRow,
                    isMaintenance,
                    machineColumnWidth,
                    timeAreaWidth,
                    windowStartMs,
                    windowDurationMs,
                    timeScale,
                },
                row
            );
        };

        bodyContainer.appendChild(row);
    });

    // Show "No Results" message if empty
    if (bodyContainer.children.length === 0) {
        const hasSearchQuery = Boolean(String(filterState.search || '').trim());
        const hasVisibleMachinesInCache = Array.isArray(machinesCache) && machinesCache.length > 0;
        const hasRestrictiveStageFilter = String(filterState.stage || 'all') !== 'all';
        const hasRestrictiveMachineActivity = String(filterState.machineActivity || 'all') !== 'all';
        const hasRestrictiveStatusFilter = normalizeTimelineStatusFilterToken(filterState.status || 'all') !== 'all';

        if (!hasSearchQuery && hasVisibleMachinesInCache && !timelineBlankGridRecoveryAttempted) {
            const stageSelect = document.getElementById('timelineStageFilter');
            const machineSelect = document.getElementById('timelineMachineFilter');
            const statusSelect = document.getElementById('timelineFilter');
            const globalSearch = document.getElementById('globalSmartSearch');
            const smartSearch = document.getElementById('timelineSmartSearch');
            const localSearch = document.getElementById('timelineSearch');

            timelineBlankGridRecoveryAttempted = true;
            filterState.status = 'all';
            filterState.stage = 'all';
            filterState.machineActivity = 'all';
            filterState.search = '';
            if (statusSelect) statusSelect.value = 'all';
            if (stageSelect) stageSelect.value = 'all';
            if (machineSelect) machineSelect.value = 'all';
            if (globalSearch) globalSearch.value = '';
            if (smartSearch) smartSearch.value = '';
            if (localSearch) localSearch.value = '';
            persistPlannerWorkspaceStatePatch({
                status: 'all',
                stage: 'all',
                machineActivity: 'all',
                search: '',
            });
            if (hasRestrictiveStatusFilter || hasRestrictiveStageFilter || hasRestrictiveMachineActivity) {
                console.warn('Timeline rendered zero rows with machine payload present; resetting stale filters to recover.');
            } else {
                console.warn('Timeline rendered zero rows with machine payload present; retrying with a clean planner filter state.');
            }
            renderTimeline();
            return;
        }

        bodyContainer.innerHTML = `
            <div class="flex flex-col items-center justify-center h-64 text-gray-400">
                <i class="ph ph-magnifying-glass text-4xl mb-2"></i>
                <p class="text-sm font-medium">No matches found for "<span class="text-gray-600">${filterState.search}</span>"</p>
                <button onclick="document.getElementById('globalSmartSearch').value=''; window.filterTimeline()" class="mt-2 text-xs text-blue-600 hover:underline">Clear Search</button>
            </div>
        `;
    }

    // Cleanup
    document.addEventListener('dragend', window.handleDragEnd);
}

// --- DRAWER & EDIT LOGIC ---

// Store current WO for Split functionality
window.currentDrawerWO = null;
window.currentDrawerMachines = [];
window.currentDrawerRemainingQty = null;
window.currentDrawerApprovedQty = 0;
window.currentDrawerReleasedQty = 0;
window.currentDrawerReleaseAvailable = 0;
window.currentDrawerNextStage = null;
window.currentDrawerRoutePlanner = false;
window.currentDrawerRouteStages = [];
window.currentDrawerRouteMachineAssignments = {};
window.currentDrawerRouteMachineModes = {};
window.currentDrawerRouteStartOverrides = {};
window.currentDrawerOperationFlowMode = 'series';
window.currentDrawerRouteDragMachineId = null;
window.currentDrawerRouteSearch = '';
window.currentDrawerRouteMachineSearch = {};
window.currentDrawerActiveStageId = '';
window.currentDrawerWorkOrderHistoryEntries = [];
window.timelineUndoStack = [];
window.timelineUndoBusy = false;

const MAX_TIMELINE_UNDO_ACTIONS = 20;

function getDrawerMachines() {
    if (Array.isArray(window.currentDrawerMachines) && window.currentDrawerMachines.length > 0) {
        return window.currentDrawerMachines;
    }
    if (Array.isArray(machinesCache) && machinesCache.length > 0) {
        return machinesCache;
    }
    return [];
}

function buildTimelineUndoRestoreFields(source = {}, overrides = {}) {
    const startDate = overrides.start_date !== undefined
        ? overrides.start_date
        : (source.start_date ?? source.start ?? null);
    const endDate = overrides.end_date !== undefined
        ? overrides.end_date
        : (source.end_date ?? source.end ?? null);
    const scheduledStartDate = overrides.scheduled_start_date !== undefined
        ? overrides.scheduled_start_date
        : (source.scheduled_start_date ?? startDate ?? null);

    return {
        status: overrides.status !== undefined ? overrides.status : (source.status || 'pending'),
        machine_id: overrides.machine_id !== undefined ? overrides.machine_id : (source.machine_id ?? null),
        stage_id: overrides.stage_id !== undefined ? overrides.stage_id : (source.stage_id ?? null),
        current_stage_id: overrides.current_stage_id !== undefined
            ? overrides.current_stage_id
            : (source.current_stage_id ?? source.stage_id ?? null),
        start_date: startDate || null,
        end_date: endDate || null,
        scheduled_start_date: scheduledStartDate || null,
        operation_flow_mode: overrides.operation_flow_mode !== undefined
            ? overrides.operation_flow_mode
            : (source.operation_flow_mode || 'series'),
        next_stage_ready: overrides.next_stage_ready !== undefined
            ? !!overrides.next_stage_ready
            : !!source.next_stage_ready,
        planner_action_required: overrides.planner_action_required !== undefined
            ? !!overrides.planner_action_required
            : !!source.planner_action_required,
        closed_by_planner: overrides.closed_by_planner !== undefined
            ? !!overrides.closed_by_planner
            : !!source.closed_by_planner,
        assigned_worker_id: overrides.assigned_worker_id !== undefined
            ? overrides.assigned_worker_id
            : (source.assigned_worker_id ?? source.assigned_worker ?? null),
        assignment_type: overrides.assignment_type !== undefined
            ? overrides.assignment_type
            : (source.assignment_type || 'auto'),
        planner_start_at: overrides.planner_start_at !== undefined
            ? overrides.planner_start_at
            : (source.planner_start_at || null),
    };
}

function buildTimelineUndoEntry(id, fields, extra = {}) {
    return {
        id,
        ...extra,
        fields,
    };
}

function buildTimelineUndoEntryFromSource(source = {}, overrides = {}, extra = {}) {
    const workOrderId = Number(source?.id || 0);
    if (!workOrderId) return null;
    return buildTimelineUndoEntry(workOrderId, buildTimelineUndoRestoreFields(source, overrides), extra);
}

function isTimelineUndoEligibleSource(source) {
    const statusValue = String(source?.status || '').trim().toLowerCase();
    return ['pending', 'in_progress'].includes(statusValue);
}

function getTimelineTaskStageRank(task) {
    const stageId = String(task?.current_stage_id ?? task?.stage_id ?? task?.stageId ?? '').trim();
    if (!stageId) return Number.MAX_SAFE_INTEGER;
    const stage = (stagesCache || []).find((item) => String(item?.id ?? item?.stage_id ?? item?.stageId ?? '').trim() === stageId);
    const rawOrder = Number(stage?.order ?? stage?.stage_order ?? Number.MAX_SAFE_INTEGER);
    return Number.isFinite(rawOrder) ? rawOrder : Number.MAX_SAFE_INTEGER;
}

function getTimelineRelatedTasksForUndo(task) {
    if (!task) return [];
    if (Array.isArray(task.source_tasks) && task.source_tasks.length > 0) {
        return task.source_tasks.filter(isTimelineUndoEligibleSource);
    }
    if (task.parent_id) {
        const siblingTasks = (tasksCache || []).filter(item =>
            String(item?.parent_id || '') === String(task.parent_id)
            && isTimelineUndoEligibleSource(item)
        );
        const flowMode = String(task?.operation_flow_mode || '').trim().toLowerCase() || 'series';
        if (flowMode === 'parallel') {
            return siblingTasks.filter(item => String(item?.id || '') === String(task.id || ''));
        }
        const anchorRank = getTimelineTaskStageRank(task);
        return siblingTasks.filter((item) => getTimelineTaskStageRank(item) >= anchorRank);
    }
    return isTimelineUndoEligibleSource(task) ? [task] : [];
}

function buildTimelineTaskUndoAction(task, label) {
    const relatedTasks = getTimelineRelatedTasksForUndo(task);
    const items = relatedTasks
        .map(item => buildTimelineUndoEntryFromSource(item))
        .filter(Boolean);
    if (!items.length) return null;
    return {
        label: label || `Undo planner action for ${getDisplayWorkOrderHashLabel(task)}`,
        items,
    };
}

function buildTimelineQueueScheduleUndoAction(payload) {
    const workOrderId = Number(payload?.id || 0);
    if (!workOrderId) return null;
    return {
        label: `Undo scheduling ${getDisplayWorkOrderHashLabel(payload)}`,
        items: [
            buildTimelineUndoEntry(
                workOrderId,
                buildTimelineUndoRestoreFields(
                    {
                        id: workOrderId,
                        status: payload?.status || 'pending',
                        stage_id: payload?.stage_id || null,
                        current_stage_id: payload?.stage_id || null,
                        machine_id: null,
                        start_date: null,
                        end_date: null,
                    },
                    {
                        machine_id: null,
                        start_date: null,
                        end_date: null,
                        scheduled_start_date: null,
                    }
                )
            ),
        ],
    };
}

function buildDrawerUndoAction(workOrder, label) {
    const entry = buildTimelineUndoEntryFromSource(workOrder);
    if (!entry) return null;
    return {
        label: label || `Undo changes for ${getDisplayWorkOrderHashLabel(workOrder)}`,
        items: [entry],
    };
}

function buildRoutePlanningUndoAction(workOrder, routeStages) {
    const items = [];
    const parentEntry = buildTimelineUndoEntryFromSource(workOrder, {
        machine_id: workOrder?.machine_id ?? null,
        stage_id: workOrder?.stage_id ?? null,
        current_stage_id: workOrder?.current_stage_id ?? null,
        start_date: workOrder?.start_date ?? null,
        end_date: workOrder?.end_date ?? null,
        scheduled_start_date: workOrder?.scheduled_start_date ?? workOrder?.start_date ?? null,
        planner_start_at: workOrder?.planner_start_at ?? null,
    });
    if (parentEntry) items.push(parentEntry);

    const existingStageTaskIds = new Set();
    (Array.isArray(routeStages) ? routeStages : []).forEach((stage) => {
        if (!stage?.planned_task_id) return;
        existingStageTaskIds.add(String(stage.planned_task_id));
        const entry = buildTimelineUndoEntry(stage.planned_task_id, buildTimelineUndoRestoreFields({
            id: stage.planned_task_id,
            status: 'pending',
            machine_id: stage.assigned_machine_id ?? null,
            stage_id: stage.id,
            current_stage_id: stage.id,
            start_date: stage.planned_start_date ?? null,
            end_date: stage.planned_end_date ?? null,
            scheduled_start_date: stage.planned_start_date ?? null,
            operation_flow_mode: workOrder?.operation_flow_mode || 'series',
            assignment_type: 'auto',
            assigned_worker: null,
        }));
        items.push(entry);
    });

    return {
        label: `Undo route plan for ${getDisplayWorkOrderHashLabel(workOrder)}`,
        items,
        existingStageTaskIds,
    };
}

function attachRoutePlanUndoDeletes(action, createdTasks) {
    if (!action || !Array.isArray(createdTasks)) return action;
    const existingIds = action.existingStageTaskIds || new Set();
    createdTasks.forEach((task) => {
        const taskId = String(task?.id || '').trim();
        if (!taskId || existingIds.has(taskId)) return;
        action.items.push({ id: Number(taskId), delete: true });
    });
    delete action.existingStageTaskIds;
    return action;
}

function updateTimelineUndoButtonState() {
    const button = document.getElementById('timelineUndoButton');
    if (!button) return;
    const lastAction = window.timelineUndoStack.length
        ? window.timelineUndoStack[window.timelineUndoStack.length - 1]
        : null;
    const isDisabled = window.timelineUndoBusy || !lastAction;
    button.disabled = isDisabled;
    button.classList.toggle('opacity-40', isDisabled);
    button.classList.toggle('cursor-not-allowed', isDisabled);
    const label = lastAction?.label || 'Undo last planning action';
    button.title = label;
    button.setAttribute('aria-label', label);
}

function recordTimelineUndoAction(action) {
    if (!action || !Array.isArray(action.items) || !action.items.length) return;
    if (!['planner', 'admin'].includes(String(getUserRole() || '').toLowerCase())) return;
    delete action.existingStageTaskIds;
    window.timelineUndoStack.push(action);
    if (window.timelineUndoStack.length > MAX_TIMELINE_UNDO_ACTIONS) {
        window.timelineUndoStack = window.timelineUndoStack.slice(-MAX_TIMELINE_UNDO_ACTIONS);
    }
    updateTimelineUndoButtonState();
}

window.undoLastTimelineAction = async function () {
    if (window.timelineUndoBusy) return false;
    const action = window.timelineUndoStack.length ? window.timelineUndoStack.pop() : null;
    if (!action) {
        updateTimelineUndoButtonState();
        showTimelineToast('Nothing to undo.', 'info');
        return false;
    }

    window.timelineUndoBusy = true;
    updateTimelineUndoButtonState();
    try {
        const response = await fetch('/manufacturing/api/planner/undo/', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': getCookie('csrftoken'),
            },
            body: JSON.stringify({ items: action.items }),
        });
        const data = await response.json();
        if (!response.ok || !data.success) {
            throw new Error(data.error || data.message || 'Unable to undo the last planning action.');
        }
        if (typeof window.initGanttChart === 'function') {
            window.initGanttChart(true);
        }
        const readableLabel = String(action.label || 'last planning action').replace(/^Undo\s+/i, '').trim();
        showTimelineToast(data.message || `Undid ${readableLabel}.`, 'success');
        return true;
    } catch (err) {
        window.timelineUndoStack.push(action);
        showTimelineToast(err?.message || 'Unable to undo the last planning action.', 'error');
        return false;
    } finally {
        window.timelineUndoBusy = false;
        updateTimelineUndoButtonState();
    }
};

function getPlannerData() {
    if (tasksCache.length === 0) {
        const tasksEl = document.getElementById('data-tasks');
        if (tasksEl && tasksEl.textContent) {
            const parsed = safeParseJSON(tasksEl.textContent);
            tasksCache = Array.isArray(parsed) ? parsed : [];
        }
    }
    if (machinesCache.length === 0) {
        const machinesEl = document.getElementById('data-machines');
        if (machinesEl && machinesEl.textContent) {
            const parsed = safeParseJSON(machinesEl.textContent);
            machinesCache = Array.isArray(parsed) ? parsed : [];
            machinesCache = sortMachinesForTimeline(machinesCache);
        }
    }
    syncTimelineWorkOrderPaletteRegistry(tasksCache);
    return { tasks: tasksCache || [], machines: machinesCache || [] };
}

function formatShortDate(value) {
    if (!value) return '';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return '';
    return date.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}

function escapeHtml(value) {
    return String(value || '')
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/\"/g, "&quot;")
        .replace(/'/g, "&#39;");
}

function getPlannerIntakeData() {
    const intakeEl = document.getElementById('data-intake-orders');
    if (!intakeEl || !intakeEl.textContent) return [];
    const parsed = safeParseJSON(intakeEl.textContent);
    return Array.isArray(parsed) ? parsed : [];
}

function getPlannerFollowUpReason(task, snapshot) {
    const cycleState = task?.cycle_state || {};
    const step = String(cycleState.step || '').toLowerCase();
    const ownerRole = String(cycleState.owner_role || '').toLowerCase();

    if (snapshot.overdue) return 'Planned time ended before reported completion.';
    if (snapshot.behind) return 'Actual production is behind expected progress.';
    if (cycleState.blocker_reason) return cycleState.blocker_reason;
    if (['production_approval', 'supervisor_dispatch', 'planning', 'machine_unavailable', 'next_stage_release', 'planner_close'].includes(step)) {
        return cycleState.next_action || cycleState.label || 'Waiting for action.';
    }
    if (ownerRole && ownerRole !== 'quality') {
        return cycleState.next_action || cycleState.label || 'Waiting for action.';
    }
    return '';
}

function isPlannerClosedTask(task) {
    const cycleState = task?.cycle_state || {};
    const step = String(cycleState.step || '').toLowerCase();
    const label = String(cycleState.label || '').toLowerCase();
    return !!task?.closed_by_planner || step === 'planner_closed' || label === 'planner closed';
}

function getDispatchMachineMap(machines = []) {
    return new Map((machines || []).map((machine) => [String(machine.id), machine]));
}

function isDispatchMachineFault(machine) {
    const status = String(machine?.status || '').toLowerCase();
    return ['maintenance', 'breakdown', 'broken', 'fault', 'faulty', 'unavailable', 'down'].includes(status);
}

function getPlannerDispatchReadinessState(task, machine, nowMs = Date.now()) {
    const status = String(task?.status || '').toLowerCase();
    const startMs = task?.start ? new Date(task.start).getTime() : null;
    const endMs = task?.end ? new Date(task.end).getTime() : null;
    const hasWorker = !!String(task?.assigned_worker_name || '').trim();
    const hasMachine = !!task?.machine_id;

    if (!hasMachine) {
        return {
            type: 'Missing Worker',
            severity: 4,
            tone: 'amber',
            reason: 'Assign machine and worker before supervisor dispatch.',
        };
    }
    if (isDispatchMachineFault(machine)) {
        return {
            type: 'Machine Fault',
            severity: 5,
            tone: 'rose',
            reason: 'Assigned machine is unavailable for execution.',
        };
    }
    if (status === 'in_progress' || status === 'active' || (hasWorker && startMs && startMs <= nowMs && (!endMs || endMs > nowMs))) {
        return {
            type: 'Already Running',
            severity: 1,
            tone: 'emerald',
            reason: 'Execution is already live on the shop floor.',
        };
    }
    if (startMs && startMs > nowMs) {
        return {
            type: 'Not Started Yet',
            severity: 2,
            tone: 'slate',
            reason: `Planned start ${formatShortDate(task.start)}.`,
        };
    }
    if (!hasWorker) {
        return {
            type: 'Missing Worker',
            severity: 4,
            tone: 'amber',
            reason: 'Supervisor still needs to assign an operator.',
        };
    }
    return {
        type: 'Ready for Supervisor',
        severity: 3,
        tone: 'cyan',
        reason: 'Machine, timing, and worker are ready for dispatch.',
    };
}

function buildPlannerDispatchReadinessQueue(nowMs = Date.now()) {
    const { tasks, machines } = getPlannerData();
    const machineMap = getDispatchMachineMap(machines);
    const dispatchStates = ['Ready for Supervisor', 'Missing Worker', 'Machine Fault', 'Not Started Yet', 'Already Running'];

    return (tasks || [])
        .filter((task) => {
            const status = String(task?.status || '').toLowerCase();
            return task
                && !task.parent_planner_closed
                && !isPlannerClosedTask(task)
                && !['completed', 'canceled', 'archived'].includes(status);
        })
        .map((task) => {
            const machine = task.machine_id ? machineMap.get(String(task.machine_id)) : null;
            const state = getPlannerDispatchReadinessState(task, machine, nowMs);
            if (!dispatchStates.includes(state.type)) return null;
            return {
                task,
                machine,
                state,
                stageLabel: task.stage_name || task.current_stage_name || 'Unspecified stage',
                machineLabel: machine?.display_name || machine?.name || task.machine_name || 'Unassigned',
            };
        })
        .filter(Boolean)
        .sort((a, b) => {
            if (b.state.severity !== a.state.severity) return b.state.severity - a.state.severity;
            const aStart = a.task.start ? new Date(a.task.start).getTime() : Number.MAX_SAFE_INTEGER;
            const bStart = b.task.start ? new Date(b.task.start).getTime() : Number.MAX_SAFE_INTEGER;
            return aStart - bStart;
        });
}

function renderPlannerDispatchReadinessQueue() {
    const mount = document.getElementById('plannerDispatchReadinessList');
    const countEl = document.getElementById('plannerDispatchReadinessCount');
    if (!mount) return;

    const queue = buildPlannerDispatchReadinessQueue();
    if (countEl) countEl.textContent = String(queue.length);

    if (!queue.length) {
        mount.innerHTML = `
            <div class="rounded-xl border border-dashed border-cyan-200 bg-white/70 px-3 py-4 text-center text-xs font-semibold text-cyan-700/70">
                No dispatch items.
            </div>
        `;
        return;
    }

    const toneClasses = {
        rose: 'border-rose-200 bg-rose-50 text-rose-700',
        amber: 'border-amber-200 bg-amber-50 text-amber-700',
        cyan: 'border-cyan-200 bg-cyan-50 text-cyan-700',
        emerald: 'border-emerald-200 bg-emerald-50 text-emerald-700',
        slate: 'border-slate-200 bg-slate-50 text-slate-700',
    };

    mount.innerHTML = queue.map((item) => {
        const task = item.task;
        const state = item.state;
        const statusClass = toneClasses[state.tone] || toneClasses.slate;
        const woCode = `WO #${escapeHtml(getDisplayWorkOrderId(task))}`;
        const product = escapeHtml(task.product || task.product_name || 'Work Order');
        const startText = task.start ? formatShortDate(task.start) : 'No start';
        return `
            <button type="button"
                class="w-full rounded-xl border border-cyan-100 bg-white p-3 text-left shadow-sm transition hover:border-cyan-200 hover:bg-cyan-50/70"
                onclick="window.openWorkOrderModal && window.openWorkOrderModal(${Number(task.id)})">
                <div class="flex items-start justify-between gap-2">
                    <div class="min-w-0">
                        <div class="text-[11px] font-black text-slate-900">${woCode}</div>
                        <div class="mt-0.5 truncate text-xs font-bold text-slate-700">${product}</div>
                    </div>
                    <span class="shrink-0 rounded-full border px-2 py-0.5 text-[9px] font-black uppercase ${statusClass}">${state.type}</span>
                </div>
                <div class="mt-2 text-[11px] font-semibold leading-snug text-slate-600">
                    ${escapeHtml(item.machineLabel)} - ${escapeHtml(item.stageLabel)}
                </div>
                <div class="mt-1 text-[11px] font-bold leading-snug text-cyan-800">
                    ${escapeHtml(state.reason)}
                </div>
                <div class="mt-2 inline-flex items-center rounded-full border border-slate-200 bg-slate-50 px-2 py-0.5 text-[9px] font-black uppercase tracking-wide text-slate-500">
                    ${escapeHtml(startText)}
                </div>
            </button>
        `;
    }).join('');
}

function buildPlannerFollowUpQueue(nowMs = Date.now()) {
    const { tasks, machines } = getPlannerData();
    const intakeOrders = getPlannerIntakeData();
    const combined = new Map();

    [...(intakeOrders || []), ...(tasks || [])].forEach((task) => {
        if (!task || task.id === undefined || task.id === null) return;
        const key = String(task.id);
        const previous = combined.get(key) || {};
        combined.set(key, {
            ...previous,
            ...task,
            cycle_state: task.cycle_state || previous.cycle_state || null,
        });
    });

    const machineMap = new Map(
        (machines || []).map((machine) => [
            String(machine.id),
            machine.display_name || machine.name || machine.content || `Machine #${machine.id}`,
        ])
    );

    return Array.from(combined.values())
        .filter((task) => task && !['canceled', 'archived'].includes(String(task.status || '').toLowerCase()))
        .map((task) => {
            const snapshot = getTimelineTaskProgressSnapshot(task, nowMs);
            const cycleState = task.cycle_state || {};
            const step = String(cycleState.step || '').toLowerCase();
            const ownerRole = String(cycleState.owner_role || '').toLowerCase();
            const qualityOnly = ownerRole === 'quality' || step === 'quality_inspection';
            const reason = getPlannerFollowUpReason(task, snapshot);
            const actionBlocked = !!reason && !qualityOnly;
            const include = snapshot.overdue || snapshot.behind || actionBlocked;
            if (!include) return null;

            const severity = snapshot.overdue ? 3 : (snapshot.behind ? 2 : 1);
            const type = snapshot.overdue ? 'Overdue' : (snapshot.behind ? 'Behind' : 'Action');
            return {
                task,
                snapshot,
                cycleState,
                type,
                severity,
                reason,
                ownerRole: cycleState.owner_role || (snapshot.overdue || snapshot.behind ? 'planner' : 'planner'),
                machineLabel: task.machine_id ? (machineMap.get(String(task.machine_id)) || 'Assigned machine') : 'Unassigned',
                stageLabel: task.stage_name || task.current_stage_name || 'Unspecified stage',
            };
        })
        .filter(Boolean)
        .sort((a, b) => {
            if (b.severity !== a.severity) return b.severity - a.severity;
            if (b.snapshot.gap !== a.snapshot.gap) return b.snapshot.gap - a.snapshot.gap;
            return String(a.task.product || '').localeCompare(String(b.task.product || ''));
        });
}

function renderPlannerFollowUpQueue() {
    const mount = document.getElementById('plannerFollowUpList');
    const countEl = document.getElementById('plannerFollowUpCount');
    if (!mount) return;

    const queue = buildPlannerFollowUpQueue();
    if (countEl) countEl.textContent = String(queue.length);

    if (!queue.length) {
        mount.innerHTML = `
            <div class="rounded-xl border border-dashed border-rose-200 bg-white/70 px-3 py-4 text-center text-xs font-semibold text-rose-700/70">
                No follow-up items.
            </div>
        `;
        return;
    }

    mount.innerHTML = queue.slice(0, 10).map((item) => {
        const task = item.task;
        const snapshot = item.snapshot;
        const typeClasses = item.type === 'Overdue'
            ? 'border-red-200 bg-red-50 text-red-700'
            : (item.type === 'Behind'
                ? 'border-orange-200 bg-orange-50 text-orange-700'
                : 'border-blue-200 bg-blue-50 text-blue-700');
        const expectedText = snapshot.expected === null ? '-' : `${Math.round(snapshot.expected)}%`;
        const actualText = `${Math.round(snapshot.actual)}%`;
        const gapText = snapshot.expected === null ? '-' : `${Math.round(snapshot.gap)}%`;
        const woCode = `WO #${escapeHtml(getDisplayWorkOrderId(task))}`;
        const product = escapeHtml(task.product || task.product_name || 'Work Order');
        const owner = escapeHtml(item.ownerRole || 'planner');
        return `
            <button type="button"
                class="w-full rounded-xl border border-rose-100 bg-white p-3 text-left shadow-sm transition hover:border-rose-200 hover:bg-rose-50/70"
                onclick="window.openWorkOrderModal && window.openWorkOrderModal(${Number(task.id)})">
                <div class="flex items-start justify-between gap-2">
                    <div class="min-w-0">
                        <div class="text-[11px] font-black text-slate-900">${woCode}</div>
                        <div class="mt-0.5 truncate text-xs font-bold text-slate-700">${product}</div>
                    </div>
                    <span class="shrink-0 rounded-full border px-2 py-0.5 text-[9px] font-black uppercase ${typeClasses}">${item.type}</span>
                </div>
                <div class="mt-2 grid grid-cols-3 gap-1 text-center">
                    <div class="rounded-lg bg-slate-50 px-1.5 py-1">
                        <div class="text-[8px] font-black uppercase tracking-wide text-slate-400">Exp</div>
                        <div class="text-[11px] font-black text-slate-800">${expectedText}</div>
                    </div>
                    <div class="rounded-lg bg-slate-50 px-1.5 py-1">
                        <div class="text-[8px] font-black uppercase tracking-wide text-slate-400">Act</div>
                        <div class="text-[11px] font-black text-slate-800">${actualText}</div>
                    </div>
                    <div class="rounded-lg bg-slate-50 px-1.5 py-1">
                        <div class="text-[8px] font-black uppercase tracking-wide text-slate-400">Gap</div>
                        <div class="text-[11px] font-black text-slate-800">${gapText}</div>
                    </div>
                </div>
                <div class="mt-2 text-[11px] font-semibold leading-snug text-slate-600">
                    ${escapeHtml(item.machineLabel)} - ${escapeHtml(item.stageLabel)}
                </div>
                <div class="mt-1 text-[11px] font-bold leading-snug text-rose-700">
                    ${escapeHtml(item.reason)}
                </div>
                <div class="mt-2 inline-flex items-center rounded-full border border-slate-200 bg-slate-50 px-2 py-0.5 text-[9px] font-black uppercase tracking-wide text-slate-500">
                    Owner: ${owner}
                </div>
            </button>
        `;
    }).join('');
}

window.buildPlannerFollowUpQueue = buildPlannerFollowUpQueue;
window.renderPlannerFollowUpQueue = renderPlannerFollowUpQueue;
window.buildPlannerDispatchReadinessQueue = buildPlannerDispatchReadinessQueue;
window.renderPlannerDispatchReadinessQueue = renderPlannerDispatchReadinessQueue;

window.renderPlannerKanban = function () {
    const alpineBoard = document.querySelector('#plannerViewKanban [x-data*="kanbanBoard"]');
    const alpineInitialized = !!(alpineBoard && alpineBoard.__x);
    const container = document.getElementById('plannerViewContainer');
    const fallbackMount = document.getElementById('plannerKanbanFallback');
    const mount = fallbackMount || document.getElementById('plannerKanbanMount');
    const alpineRoot = document.querySelector('#plannerViewKanban .planner-kanban-alpine');
    const isPlannerDashboard = !!document.querySelector('[data-planner-dashboard="true"]');

    // Avoid affecting non-planner screens (e.g. Supervisor).
    if (!isPlannerDashboard) {
        if (alpineBoard) {
            window.dispatchEvent(new CustomEvent('planner-refresh-kanban'));
        }
        return;
    }

    // Prefer Alpine Kanban when it is actually running (avoids white screen when Alpine fails to load).
    if (alpineBoard && window.Alpine && alpineInitialized) {
        window.__plannerKanbanInitRetry = 0;
        if (fallbackMount) fallbackMount.classList.add('hidden');
        if (alpineRoot) alpineRoot.classList.remove('hidden');
        window.dispatchEvent(new CustomEvent('planner-refresh-kanban'));
        return;
    }

    // If Alpine exists but hasn't initialized yet, give it a moment before falling back.
    if (alpineBoard && window.Alpine && !alpineInitialized) {
        window.dispatchEvent(new CustomEvent('planner-refresh-kanban'));
        window.__plannerKanbanInitRetry = (window.__plannerKanbanInitRetry || 0) + 1;
        if (window.__plannerKanbanInitRetry <= 3) {
            setTimeout(() => {
                if (window.renderPlannerKanban) window.renderPlannerKanban();
            }, 150);
            return;
        }
    }

    // Ensure dispatch for Alpine components (like in supervisor dashboard)
    window.dispatchEvent(new CustomEvent('planner-refresh-kanban'));

    if (!mount) {
        console.log("Kanban: Mount point not found, skipping vanilla render.");
        return;
    }
    if (alpineRoot) alpineRoot.classList.add('hidden');
    if (fallbackMount) fallbackMount.classList.remove('hidden');

    const { tasks, machines } = getPlannerData();
    const machineMap = new Map(machines.map(m => [String(m.id), m.name || m.content || `Machine #${m.id}`]));

    const columns = [
        { id: 'pending', title: 'Pending', color: 'bg-amber-400' },
        { id: 'active', title: 'In Progress', color: 'bg-blue-500' },
        { id: 'complete', title: 'Completed', color: 'bg-emerald-500' }
    ];

    const normalized = (tasks || []).filter(t => t && t.status !== 'canceled' && t.status !== 'archived');
    const byColumn = {
        pending: normalized.filter(t => !t.start || ['pending', 'planned', 'scheduled'].includes(t.status)),
        active: normalized.filter(t => (t.start || t.status === 'in_progress' || t.status === 'active' || t.status === 'on_hold') && !['completed', 'done', 'pending'].includes(t.status)),
        complete: normalized.filter(t => ['completed', 'done'].includes(t.status))
    };

    console.log(`Kanban Stats: Total ${normalized.length}, P:${byColumn.pending.length}, A:${byColumn.active.length}, C:${byColumn.complete.length}`);

    const html = `
        <div style="display:flex;gap:16px;flex-wrap:nowrap;align-items:flex-start;padding:4px;">
            ${columns.map(col => `
                <div style="width:320px;background:#f8fafc;border:1px solid #e2e8f0;border-radius:14px;box-shadow:0 2px 8px rgba(15,23,42,0.04);overflow:hidden;display:flex;flex-direction:column;max-height:600px;">
                    <div style="padding:12px 14px;border-bottom:1px solid #e2e8f0;background:#ffffff;display:flex;justify-content:space-between;align-items:center;flex-shrink:0;">
                        <div style="display:flex;align-items:center;gap:8px;">
                            <span style="width:8px;height:8px;border-radius:999px;background:${col.id === 'pending' ? '#f59e0b' : col.id === 'active' ? '#3b82f6' : '#10b981'};"></span>
                            <div style="font-weight:700;color:#334155;font-size:13px;">${col.title}</div>
                        </div>
                        <div style="background:#f1f5f9;color:#475569;padding:2px 8px;border-radius:999px;font-size:11px;font-weight:700;">
                            ${(byColumn[col.id] || []).length}
                        </div>
                    </div>
                    <div style="padding:12px;display:flex;flex-direction:column;gap:10px;overflow-y:auto;flex-1:1 auto;">
                        ${(byColumn[col.id] || []).map(task => {
                            const palette = getTimelineWorkOrderPalette(task);
                            return `
                            <div style="background:linear-gradient(180deg, ${palette.surface} 0%, #ffffff 30%);border:1px solid ${palette.border};border-top:3px solid ${palette.solid};border-radius:12px;padding:10px 12px;box-shadow:0 1px 2px rgba(15,23,42,0.06);cursor:pointer;"
                                onclick="window.openWorkOrderModal && openWorkOrderModal(${task.id})">
                                <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:8px;">
                                    <div style="font-weight:700;color:#0f172a;font-size:13px;line-height:1.2;">${escapeHtml(task.product || task.product_name || 'Work Order')}</div>
                                    <div style="font-size:11px;color:${palette.chipText};background:${palette.chip};border:1px solid ${palette.border};padding:2px 8px;border-radius:999px;">#${escapeHtml(getDisplayWorkOrderId(task))}</div>
                                </div>
                                <div style="margin-top:6px;display:flex;flex-wrap:wrap;gap:6px;align-items:center;">
                                    <span style="font-size:10px;font-weight:700;color:#334155;background:#f8fafc;border:1px solid #e2e8f0;border-radius:999px;padding:1px 8px;">
                                        Qty: ${escapeHtml(formatQuantityBreakdown(task))}
                                    </span>
                                    ${task.is_scrap_compensation_task ? `
                                        <span style="font-size:10px;font-weight:700;color:#9d174d;background:#fdf2f8;border:1px solid #fbcfe8;border-radius:999px;padding:1px 8px;">
                                            Compensation Task
                                        </span>
                                    ` : ''}
                                </div>
                                <div style="margin-top:8px;padding-top:8px;border-top:1px solid #f1f5f9;display:flex;justify-content:space-between;align-items:center;">
                                    <div style="font-size:11px;color:#2563eb;background:#eff6ff;border-radius:999px;padding:2px 8px;font-weight:600;">
                                        ${escapeHtml(machineMap.get(String(task.machine_id)) || 'Unassigned')}
                                    </div>
                                    <div style="font-size:11px;color:#94a3b8;">${formatShortDate(task.start)}</div>
                                </div>
                            </div>
                        `; }).join('') || `
                            <div style="text-align:center;padding:24px 0;color:#94a3b8;font-size:12px;">No tasks</div>
                        `}
                    </div>
                </div>
            `).join('')}
        </div>
    `;

    mount.innerHTML = html;
    mount.classList.remove('hidden');
    mount.style.setProperty('display', 'block', 'important');
    mount.style.visibility = 'visible';
    mount.style.opacity = '1';
    mount.style.height = 'auto';
    if (!mount.style.minHeight) mount.style.minHeight = '360px';
};

window.renderPlannerList = function () {
    const container = document.getElementById('plannerViewContainer');
    const mount = document.getElementById('plannerListMount') || document.getElementById('plannerListFallback');
    const alpineRoot = document.querySelector('#plannerViewList .planner-list-alpine');

    // Ensure dispatch for Alpine components
    window.dispatchEvent(new CustomEvent('planner-refresh-list'));

    if (!mount) {
        console.log("List: Mount point not found, skipping vanilla render.");
        return;
    }
    if (alpineRoot) alpineRoot.classList.add('hidden');

    const { tasks, machines } = getPlannerData();
    const machineMap = new Map(machines.map(m => [String(m.id), m.name || m.content || `Machine #${m.id}`]));
    const normalized = (tasks || []).filter(t => t && t.status !== 'canceled' && t.status !== 'archived');

    const html = `

    ${normalized.map(task => {
        const palette = getTimelineWorkOrderPalette(task);
        const statusStyles = getTimelineStatusBadgeStyles(task.status);
        return `
        <div style="background:linear-gradient(180deg, ${palette.surface} 0%, #ffffff 36%);border:1px solid ${palette.border};border-left:5px solid ${palette.solid};border-radius:16px;padding:16px;margin-bottom:12px;box-shadow:0 2px 6px rgba(15,23,42,0.06);cursor:pointer;"
            onclick="window.openWorkOrderModal && openWorkOrderModal(${task.id})">
            <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:12px;">
                <div style="font-weight:700;color:#0f172a;font-size:15px;">${escapeHtml(task.product || task.product_name || 'Work Order')}</div>
                <div style="font-size:12px;color:${palette.chipText};background:${palette.chip};border:1px solid ${palette.border};padding:3px 10px;border-radius:999px;">#${escapeHtml(getDisplayWorkOrderId(task))}</div>
            </div>
            <div style="margin-top:10px;display:flex;flex-wrap:wrap;gap:8px;align-items:center;">
                <span style="font-size:11px;font-weight:700;padding:4px 10px;border-radius:999px;border:1px solid ${statusStyles.border};color:${statusStyles.color};background:${statusStyles.background};">
                    ${escapeHtml(task.status || 'pending')}
                </span>
                <span style="font-size:11px;font-weight:700;padding:4px 10px;border-radius:999px;border:1px solid #e2e8f0;color:#1e3a8a;background:#eef2ff;">
                    Qty: ${escapeHtml(formatQuantityBreakdown(task))}
                </span>
                ${task.is_scrap_compensation_task ? `
                    <span style="font-size:11px;font-weight:700;padding:4px 10px;border-radius:999px;border:1px solid #fbcfe8;color:#9d174d;background:#fdf2f8;">
                        Compensation Task
                    </span>
                ` : ''}
            </div>
            <div style="margin-top:12px;padding-top:10px;border-top:1px solid #f1f5f9;display:flex;justify-content:space-between;align-items:center;">
                <div style="font-size:12px;color:#475569;">${escapeHtml(machineMap.get(String(task.machine_id)) || 'Unassigned')}</div>
                <div style="font-size:11px;color:#94a3b8;">${formatShortDate(task.start)}</div>
            </div>
        </div>
    `; }).join('') || `
        <div style="text-align:center;padding:32px;color:#94a3b8;font-size:13px;">No work orders found</div>
    `}

    `;

    mount.innerHTML = html;
    mount.classList.remove('hidden');
    mount.style.setProperty('display', 'block', 'important');
    mount.style.visibility = 'visible';
    mount.style.opacity = '1';
    mount.style.height = 'auto';
    if (!mount.style.minHeight) mount.style.minHeight = '360px';
};

window.renderPlannerCalendar = function () {
    // If the page provides an Alpine-based Calendar (template include), don't overwrite it.
    if (document.querySelector('#plannerViewCalendar [x-data*="productionCalendar"]')) {
        window.dispatchEvent(new CustomEvent('planner-refresh-calendar'));
        return;
    }
    const container = document.getElementById('plannerViewContainer');
    const mount = document.getElementById('plannerCalendarMount') || document.getElementById('plannerCalendarFallback');
    const alpineRoot = document.querySelector('#plannerViewCalendar .planner-calendar-alpine');

    // Ensure dispatch for Alpine components
    window.dispatchEvent(new CustomEvent('planner-refresh-calendar'));

    if (!mount) {
        console.log("Calendar: Mount point not found, skipping vanilla render.");
        return;
    }
    if (alpineRoot) alpineRoot.classList.add('hidden');

    const { tasks, machines } = getPlannerData();
    const machineMap = new Map(machines.map(m => [String(m.id), m.name || m.content || `Machine #${m.id}`]));

    const now = new Date();
    const year = now.getFullYear();
    const month = now.getMonth();
    const monthName = now.toLocaleString('default', { month: 'long' });
    const daysInMonth = new Date(year, month + 1, 0).getDate();
    const startDay = new Date(year, month, 1).getDay();
    const dayLabels = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];

    const tasksByDate = {};
    (tasks || []).forEach(task => {
        if (!task.start) return;
        const d = new Date(task.start);
        if (Number.isNaN(d.getTime())) return;
        if (d.getFullYear() !== year || d.getMonth() !== month) return;
        const key = d.getDate();
        tasksByDate[key] = tasksByDate[key] || [];
        tasksByDate[key].push(task);
    });

    let cells = '';
    for (let i = 0; i < startDay; i += 1) {
        cells += `<div style="background:#f1f5f9;min-height:120px;"></div>`;
    }
    for (let day = 1; day <= daysInMonth; day += 1) {
        const dayTasks = tasksByDate[day] || [];
        cells += `
            <div style="background:#ffffff;min-height:120px;padding:6px;border:1px solid #e2e8f0;overflow:hidden;">
                <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:6px;">
                    <span style="display:inline-flex;align-items:center;justify-content:center;width:24px;height:24px;border-radius:999px;font-weight:700;font-size:12px;${day === now.getDate() ? 'background:#4f46e5;color:#ffffff;' : 'color:#334155;'}">${day}</span>
                </div>
                <div style="display:flex;flex-direction:column;gap:4px;max-height:90px;overflow:auto;">
                    ${dayTasks.map(task => {
                        const palette = getTimelineWorkOrderPalette(task);
                        return `
                        <div style="padding:4px 6px;border-radius:8px;border:1px solid ${palette.border};font-size:10px;cursor:pointer;background:${palette.calendarTint};box-shadow:inset 3px 0 0 ${palette.solid};"
                            onclick="window.openWorkOrderModal && openWorkOrderModal(${task.id})">
                            <div style="font-weight:700;color:${palette.text};">#${escapeHtml(getDisplayWorkOrderId(task))}</div>
                            <div style="white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:${palette.text};">${escapeHtml(task.product || task.product_name || 'WO')}</div>
                            <div style="font-size:9px;color:#64748b;">${escapeHtml(machineMap.get(String(task.machine_id)) || 'Unassigned')}</div>
                            ${getCompensationQty(task) > 0 ? `<div style="font-size:9px;color:#9d174d;font-weight:600;">+ Scrap ${escapeHtml(getCompensationQty(task))}</div>` : ''}
                            ${task.is_scrap_compensation_task ? `<div style="font-size:9px;color:#9d174d;">Comp Task</div>` : ''}
                        </div>
                    `; }).join('')}
                </div>
            </div>
        `;
    }

    const html = `

        <div style="background:#ffffff;border:1px solid #e2e8f0;border-radius:16px;box-shadow:0 2px 6px rgba(15,23,42,0.06);min-height:360px;overflow:hidden;">
            <div style="display:flex;justify-content:space-between;align-items:center;padding:14px 16px;border-bottom:1px solid #e2e8f0;">
                <div style="font-weight:700;color:#0f172a;">${monthName} ${year}</div>
                <div style="font-size:11px;color:#94a3b8;">Planner Calendar</div>
            </div>
            <div style="display:grid;grid-template-columns:repeat(7, minmax(0, 1fr));background:#f8fafc;border-bottom:1px solid #e2e8f0;">
                ${dayLabels.map(label => `<div style="padding:8px 0;text-align:center;font-size:11px;font-weight:700;color:#94a3b8;">${label}</div>`).join('')}
            </div>
            <div style="display:grid;grid-template-columns:repeat(7, minmax(0, 1fr));background:#e2e8f0;gap:1px;">
                ${cells}
            </div>
        </div>

    `;

    mount.innerHTML = html;
    mount.classList.remove('hidden');
    mount.style.setProperty('display', 'block', 'important');
    mount.style.visibility = 'visible';
    mount.style.opacity = '1';
    mount.style.height = 'auto';
    if (!mount.style.minHeight) mount.style.minHeight = '360px';
};

function getPlannerViewElements() {
    return {
        container: document.getElementById('plannerViewContainer'),
        label: document.querySelector('#plannerViewContainer [data-planner-view-label]'),
        gantt: document.getElementById('plannerViewGantt'),
        kanban: document.getElementById('plannerViewKanban'),
        list: document.getElementById('plannerViewList'),
        calendar: document.getElementById('plannerViewCalendar')
    };
}

window.plannerSetView = function (view) {
    const els = getPlannerViewElements();
    if (!els.container) return;
    const views = {
        gantt: els.gantt,
        kanban: els.kanban,
        list: els.list,
        calendar: els.calendar
    };

    Object.entries(views).forEach(([key, el]) => {
        if (!el) return;
        if (key === view) {
            el.classList.remove('hidden');
            el.style.removeProperty('display');
            el.style.height = '100%';
            el.style.minHeight = '100%';
            el.style.zIndex = '2';
            el.style.pointerEvents = 'auto';
            el.setAttribute('aria-hidden', 'false');
        } else {
            el.classList.add('hidden');
            el.style.display = 'none';
            el.style.height = '0';
            el.style.minHeight = '0';
            el.style.zIndex = '1';
            el.style.pointerEvents = 'none';
            el.setAttribute('aria-hidden', 'true');
        }
    });

    if (els.label) {
        els.label.textContent = view.charAt(0).toUpperCase() + view.slice(1) + ' View';
    }

    if (view === 'kanban' && window.renderPlannerKanban) {
        requestAnimationFrame(() => window.renderPlannerKanban());
    }
    if (view === 'calendar' && window.renderPlannerCalendar) {
        requestAnimationFrame(() => window.renderPlannerCalendar());
    }
    if (view === 'list' && window.renderPlannerList) {
        requestAnimationFrame(() => window.renderPlannerList());
    }
};

function bindPlannerViewSwitcher() {
    const els = getPlannerViewElements();
    if (!els.container) return;
    // Planner dashboard now uses Alpine `x-show` for view switching.
    // Avoid fighting Alpine by toggling Tailwind `hidden` / inline display styles.
    if (els.container.getAttribute('data-planner-view-switcher') === 'alpine') {
        return;
    }
    const buttons = els.container.querySelectorAll('[data-planner-view]');
    buttons.forEach(btn => {
        if (btn.__plannerBound) return;
        btn.__plannerBound = true;
        btn.addEventListener('click', () => {
            const view = btn.getAttribute('data-planner-view');
            if (view) window.plannerSetView(view);
        });
    });
    window.plannerSetView('gantt');
}

function normalizeMachineToken(value) {
    return String(value || "")
        .toLowerCase()
        .replace(/[^a-z0-9]+/g, " ")
        .trim();
}

function machineMatchesRequiredType(machine, requiredType) {
    const target = normalizeMachineToken(requiredType);
    if (!target) return true;

    const fields = [
        machine.display_name,
        machine.code,
        machine.type,
        machine.category,
        machine.name
    ];
    const haystack = fields.map(normalizeMachineToken).filter(Boolean).join(" ");
    if (!haystack) return false;

    if (haystack.includes(target)) return true;

    const targetTokens = new Set(target.split(" ").filter(Boolean));
    const hayTokens = new Set(haystack.split(" ").filter(Boolean));
    for (const token of targetTokens) {
        if (hayTokens.has(token)) return true;
    }
    return false;
}

function getNextStageForCurrent(stages, currentStageId) {
    if (!Array.isArray(stages) || !currentStageId) return null;
    const ordered = stages.slice().sort((a, b) => (a.order || 0) - (b.order || 0));
    const idx = ordered.findIndex(s => String(s.id) === String(currentStageId));
    if (idx >= 0 && idx + 1 < ordered.length) {
        return ordered[idx + 1];
    }
    return null;
}

function getCurrentStageRequiredType(wo, stages) {
    if (!wo || !Array.isArray(stages)) return '';
    const stageId = wo.current_stage_id || wo.stage_id;
    if (!stageId) return '';
    const stage = stages.find(s => String(s.id) === String(stageId));
    if (!stage) return '';
    return stage.machine_type || '';
}

function getCurrentDrawerActiveStageId(stages, wo) {
    const routeStages = Array.isArray(stages) ? stages.filter(Boolean) : [];
    if (!routeStages.length) return '';

    const explicitStageId = String(window.currentDrawerActiveStageId || '').trim();
    if (explicitStageId && routeStages.some(stage => String(stage.id) === explicitStageId)) {
        return explicitStageId;
    }

    const fallbackStageId = String(wo?.current_stage_id || wo?.stage_id || routeStages[0]?.id || '').trim();
    if (fallbackStageId && routeStages.some(stage => String(stage.id) === fallbackStageId)) {
        return fallbackStageId;
    }

    return String(routeStages[0]?.id || '').trim();
}

function setCurrentDrawerActiveStage(stages, wo, stageId) {
    const requestedStageId = String(stageId || '').trim();
    const routeStages = Array.isArray(stages) ? stages.filter(Boolean) : [];
    const resolvedStageId = requestedStageId && routeStages.some(stage => String(stage.id) === requestedStageId)
        ? requestedStageId
        : getCurrentDrawerActiveStageId(routeStages, wo);

    window.currentDrawerActiveStageId = resolvedStageId;

    const stageSelect = document.getElementById('drawerStage');
    if (stageSelect && resolvedStageId && Array.from(stageSelect.options).some(option => String(option.value) === resolvedStageId)) {
        stageSelect.value = resolvedStageId;
    }

    if (Array.isArray(window.currentDrawerWorkOrderHistoryEntries) && window.currentDrawerWorkOrderHistoryEntries.length) {
        renderDrawerWorkOrderHistorySections(window.currentDrawerWorkOrderHistoryEntries, wo, stages);
    } else {
        updateDrawerAuditLinks(wo, stages);
    }

    return resolvedStageId;
}

function isDrawerRoutePlannerMode(wo, stages, options = {}) {
    const role = String(getUserRole() || '').toLowerCase();
    const forceRoutePlanner = !!options?.forceRoutePlanner;
    return ['planner', 'admin'].includes(role)
        && !!wo
        && !wo.parent_id
        && (!!wo.route_container || forceRoutePlanner)
        && Array.isArray(stages)
        && stages.length > 0;
}

function getRouteStageCandidateMachines(stage) {
    if (Array.isArray(stage?.candidate_machines) && stage.candidate_machines.length > 0) {
        return sortMachinesForTimeline(stage.candidate_machines);
    }
    const availableMachines = getDrawerMachines();
    const requiredType = String(stage?.machine_type || '').trim().toLowerCase();
    if (!requiredType) return [];

    const filtered = availableMachines.filter(machine => machineMatchesRequiredType(machine, requiredType));
    return sortMachinesForTimeline(filtered);
}

function getRecommendedRouteMachine(stage) {
    const candidateMachines = getRouteStageCandidateMachines(stage);
    const defaultMachineId = String(stage?.default_machine_id || '');
    if (defaultMachineId) {
        const explicitDefault = candidateMachines.find(machine => String(machine.id) === defaultMachineId);
        if (explicitDefault) return explicitDefault;
    }
    return candidateMachines.find(machine => machine.status !== 'maintenance' && machine.status !== 'breakdown') || null;
}

function isRouteMachineFault(machine) {
    const status = String(machine?.status || '').trim().toLowerCase();
    return ['maintenance', 'breakdown', 'broken', 'fault', 'faulty', 'unavailable', 'down'].includes(status);
}

function getRouteStageDurationMinutes(stage) {
    const estimated = Number(stage?.estimated_duration_minutes || 0);
    if (estimated > 0) return estimated;
    const duration = Number(stage?.duration_minutes || 0);
    if (duration > 0) return duration;
    const setup = Number(stage?.setup_time || 0);
    const run = Number(stage?.run_time || 0);
    return setup + run;
}

function getRouteStageReadiness(stage) {
    const selectedMachineId = String(window.currentDrawerRouteMachineAssignments[String(stage?.id)] || '').trim();
    const candidateMachines = getRouteStageCandidateMachines(stage);
    const selectedMachine = candidateMachines.find(machine => String(machine.id) === selectedMachineId) || null;
    const recommendedMachine = getRecommendedRouteMachine(stage);
    const effectiveMachine = selectedMachine || recommendedMachine;
    const durationMinutes = getRouteStageDurationMinutes(stage);

    if (!effectiveMachine) {
        return {
            state: 'Missing Machine',
            blocking: true,
            tone: 'amber',
            reason: 'Assign a machine or fix BOM machine candidates.',
        };
    }
    if (isRouteMachineFault(effectiveMachine)) {
        return {
            state: 'Machine Fault',
            blocking: true,
            tone: 'rose',
            reason: `${effectiveMachine.name || 'Selected machine'} is not operational.`,
        };
    }
    if (durationMinutes <= 0) {
        return {
            state: 'Missing Duration',
            blocking: true,
            tone: 'orange',
            reason: 'Add setup/run time or duration to this BOM operation.',
        };
    }
    return {
        state: 'Ready',
        blocking: false,
        tone: 'emerald',
        reason: `${Math.round(durationMinutes)} min planned duration.`,
    };
}

function getRoutePlannerBlockingIssues(stages = window.currentDrawerRouteStages) {
    return (Array.isArray(stages) ? stages : [])
        .map((stage) => ({ stage, readiness: getRouteStageReadiness(stage) }))
        .filter((item) => item.readiness.blocking);
}

function getRouteStageSelectionMode(stageId) {
    const key = String(stageId);
    const explicitMode = String(window.currentDrawerRouteMachineModes[key] || '').trim().toLowerCase();
    if (explicitMode) return explicitMode;
    const selectedMachineId = String(window.currentDrawerRouteMachineAssignments[key] || '').trim();
    return selectedMachineId ? 'manual' : 'auto';
}

function getCurrentDrawerOperationFlowMode() {
    return String(window.currentDrawerOperationFlowMode || 'series').trim().toLowerCase() === 'parallel'
        ? 'parallel'
        : 'series';
}

function formatDateTimeLocalValue(value) {
    if (!value) return '';
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return '';
    return new Date(parsed.getTime() - (parsed.getTimezoneOffset() * 60000)).toISOString().slice(0, 16);
}

function renderDrawerHistoryEntries(containerId, entries, emptyText) {
    const mount = document.getElementById(containerId);
    if (!mount) return;

    const items = Array.isArray(entries) ? entries : [];
    if (!items.length) {
        mount.innerHTML = `<div class="px-3 py-4 text-sm text-slate-400">${escapeHtml(emptyText || 'No history entries yet.')}</div>`;
        return;
    }

    mount.innerHTML = items.map((entry) => {
        const action = escapeHtml(entry.action || 'Activity recorded');
        const timestamp = escapeHtml(entry.timestamp || '');
        const actor = escapeHtml(entry.actor || entry.worker || entry.editor || entry.reviewed_by || '');
        const eventLabel = escapeHtml(String(entry.event || entry.source || '').replace(/_/g, ' ').trim());
        const statusLabel = escapeHtml(String(entry.status || '').replace(/_/g, ' ').trim());
        const details = escapeHtml(entry.details || '');
        const note = escapeHtml(entry.note || '');
        const quantity = entry.quantity !== null && entry.quantity !== undefined && `${entry.quantity}` !== ''
            ? `<span class="rounded-full border border-indigo-200 bg-indigo-50 px-2 py-0.5 text-[10px] font-bold text-indigo-700">Qty ${escapeHtml(entry.quantity)}</span>`
            : '';
        const eventChip = eventLabel
            ? `<span class="rounded-full border border-slate-200 bg-white px-2 py-0.5 text-[10px] font-semibold uppercase tracking-[0.12em] text-slate-500">${eventLabel}</span>`
            : '';
        const statusChip = statusLabel
            ? `<span class="rounded-full border border-slate-200 bg-slate-50 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-[0.12em] text-slate-500">${statusLabel}</span>`
            : '';
        return `
            <div class="px-3 py-3">
                <div class="flex flex-wrap items-start justify-between gap-2">
                    <div class="min-w-0">
                        <div class="text-sm font-bold text-slate-800">${action}</div>
                        <div class="mt-1 flex flex-wrap items-center gap-1.5">
                            ${eventChip}
                            ${statusChip}
                            ${quantity}
                        </div>
                    </div>
                    <div class="text-[11px] font-medium text-slate-400">${timestamp}</div>
                </div>
                ${actor ? `<div class="mt-2 text-[11px] text-slate-500">By: <span class="font-semibold text-slate-700">${actor}</span></div>` : ''}
                ${details ? `<div class="mt-1 text-[11px] text-slate-500">${details}</div>` : ''}
                ${note ? `<div class="mt-1 rounded-lg bg-white px-2.5 py-1.5 text-[11px] text-slate-600 border border-slate-200">${note}</div>` : ''}
            </div>
        `;
    }).join('');
}

function buildDrawerAuditTrailUrl(params = {}) {
    const actionsHost = document.getElementById('drawerHistoryActions');
    const baseHref = actionsHost?.dataset.auditBaseHref || '/manufacturing/reports/';
    const url = new URL(baseHref, window.location.origin);
    url.searchParams.set('section', 'audit');
    Object.entries(params).forEach(([key, value]) => {
        const normalized = value === undefined || value === null ? '' : String(value).trim();
        if (!normalized) return;
        url.searchParams.set(key, normalized);
    });
    return `${url.pathname}${url.search}`;
}

function getCurrentDrawerActiveStageContext(stages = [], wo = window.currentDrawerWO) {
    if (!Array.isArray(stages) || !stages.length) return null;
    const activeStageId = getCurrentDrawerActiveStageId(stages, wo);
    if (!activeStageId) return null;
    return stages.find((stage) => String(stage.id) === activeStageId) || null;
}

function getDrawerTaskById(taskId) {
    const targetId = String(taskId || '').trim();
    if (!targetId || !Array.isArray(tasksCache)) return null;
    return tasksCache.find((task) => String(task?.id || '') === targetId) || null;
}

function getActiveSplitChildrenForSource(sourceTaskId) {
    const sourceId = String(sourceTaskId || '').trim();
    if (!sourceId || !Array.isArray(tasksCache)) return [];
    const terminalStatuses = ['completed', 'done', 'canceled', 'archived'];
    return tasksCache.filter((task) => (
        String(task?.source_task_id || '') === sourceId
        && !terminalStatuses.includes(String(task?.status || '').toLowerCase())
    ));
}

function getCurrentDrawerCombineSplitContext(wo = window.currentDrawerWO, stages = window.currentDrawerRouteStages || window.currentDrawerStages || []) {
    if (!wo) return null;
    const terminalStatuses = ['completed', 'done', 'canceled', 'archived'];
    if (terminalStatuses.includes(String(wo.status || '').toLowerCase())) return null;

    const activeStage = getCurrentDrawerActiveStageContext(stages, wo);
    const activeStageTaskId = String(activeStage?.planned_task_id || '').trim();
    const directSourceId = String(wo.source_task_id || '').trim();
    const sourceId = directSourceId || activeStageTaskId || String(wo.id || '').trim();
    if (!sourceId) return null;

    const activeChildren = getActiveSplitChildrenForSource(sourceId);
    const rawStageChildIds = Array.isArray(activeStage?.split_child_ids)
        ? activeStage.split_child_ids.map((id) => String(id || '').trim()).filter(Boolean)
        : [];
    const taskCacheAvailable = Array.isArray(tasksCache) && tasksCache.length > 0;
    const stageChildIds = rawStageChildIds.filter((id) => {
        if (!taskCacheAvailable) return true;
        const task = tasksCache.find((item) => String(item?.id || '') === id);
        if (!task) return false;
        const status = String(task.status || '').toLowerCase();
        if (terminalStatuses.includes(status)) return false;
        return String(task.source_task_id || '').trim() === sourceId;
    });
    const selectedId = String(wo.id || '').trim();
    if (directSourceId && !activeChildren.some((task) => String(task?.id || '') === selectedId)) {
        activeChildren.push(wo);
    }

    const childIds = Array.from(new Set([
        ...stageChildIds,
        ...activeChildren
            .map((task) => String(task?.id || '').trim())
            .filter((id) => id && id !== sourceId)
    ].filter((id) => id && id !== sourceId)));
    if (!childIds.length) return null;

    return {
        sourceId,
        childIds,
        workOrderIds: [sourceId, ...childIds],
    };
}

function getDrawerSplitRemainingQty(wo) {
    if (!wo) return 0;
    const approvedQtyOnly = Number(wo.approved_qty ?? 0);
    const producedQtyRaw = Number(wo.produced_qty ?? approvedQtyOnly);
    const splitRemainingQty = Math.max(Number(wo.quantity || 0) - approvedQtyOnly, 0);
    const fallbackRemaining = Math.max(Number(wo.quantity || 0) - producedQtyRaw, 0);
    return Math.max(
        Number(wo.remaining_qty ?? fallbackRemaining),
        splitRemainingQty
    );
}

function getCurrentDrawerSplitContext(wo, stages = []) {
    const activeStage = getCurrentDrawerActiveStageContext(stages, wo);
    const activeStageTaskId = String(activeStage?.planned_task_id || '').trim();
    const sourceTask = activeStageTaskId ? getDrawerTaskById(activeStageTaskId) : null;
    const sourceWo = sourceTask || wo;
    const sourceId = String(sourceWo?.id || wo?.id || '').trim();
    const displayId = sourceWo?.parent_id || wo?.parent_id || sourceWo?.id || wo?.id || '';
    return {
        sourceWo,
        sourceId,
        displayId,
        remainingQty: getDrawerSplitRemainingQty(sourceWo),
    };
}

function updateRoutePlannerSaveButtonLabel() {
    const saveButton = document.getElementById('drawerSaveButton');
    const statusSelect = document.getElementById('drawerStatus');
    if (!saveButton || !statusSelect || !window.currentDrawerRoutePlanner) return;
    if (isDrawerPlannerClosed(window.currentDrawerWO)) {
        saveButton.textContent = 'Already Closed';
        return;
    }
    const terminalStatuses = ['completed', 'canceled', 'archived'];
    const selectedStatus = String(statusSelect.value || '').toLowerCase();
    const isTerminalStatus = terminalStatuses.includes(selectedStatus);
    const blockers = getRoutePlannerBlockingIssues();
    if (isTerminalStatus) {
        saveButton.disabled = false;
        saveButton.textContent = 'Apply Status';
        saveButton.title = '';
        return;
    }
    saveButton.disabled = blockers.length > 0;
    saveButton.textContent = blockers.length > 0 ? `Fix ${blockers.length} Stage Issue${blockers.length === 1 ? '' : 's'}` : 'Plan Full Route';
    saveButton.title = blockers.length > 0
        ? blockers.map(item => `${item.stage?.name || 'Stage'}: ${item.readiness.state}`).join('\n')
        : '';
}

function isDrawerPlannerClosed(wo = window.currentDrawerWO) {
    const cycleState = wo?.cycle_state || {};
    const step = String(cycleState.step || '').toLowerCase();
    const label = String(cycleState.label || '').toLowerCase();
    return !!wo?.closed_by_planner || step === 'planner_closed' || label === 'planner closed';
}

function getDrawerDisplayWorkOrderId(wo = window.currentDrawerWO) {
    return String(wo?.display_work_order_id || wo?.parent_id || wo?.id || '').trim();
}

function getDrawerDisplayProductName(wo = window.currentDrawerWO) {
    return String(wo?.product_name || wo?.product || 'Work Order').trim();
}

function updateDrawerIdentity(wo = window.currentDrawerWO) {
    const titleEl = document.getElementById('drawerTitle');
    const subtitleEl = document.getElementById('drawerSubtitle');
    const routeCodeEl = document.getElementById('drawerRouteWorkOrderCode');
    const routeProductEl = document.getElementById('drawerRouteProductName');
    const displayId = getDrawerDisplayWorkOrderId(wo);
    const productName = getDrawerDisplayProductName(wo);
    const title = displayId ? `WO #${displayId} - ${productName}` : productName;

    if (titleEl) titleEl.textContent = title;
    if (routeCodeEl) routeCodeEl.textContent = displayId ? `WO #${displayId}` : 'WO';
    if (routeProductEl) routeProductEl.textContent = productName;
    if (subtitleEl) {
        const customer = String(wo?.customer?.name || wo?.customer || wo?.customer_name || '').trim();
        subtitleEl.textContent = customer ? `Customer: ${customer}` : 'No customer assigned';
    }
}

function updateDrawerBomVersionCard(wo = window.currentDrawerWO) {
    const targets = [
        {
            card: document.getElementById('drawerBomVersionCard'),
            text: document.getElementById('drawerBomVersionText'),
            hint: document.getElementById('drawerBomVersionHint'),
            button: document.getElementById('drawerApplyLatestBomBtn'),
            actions: document.getElementById('drawerBomChangeActions'),
        },
        {
            card: document.getElementById('drawerRouteBomVersionCard'),
            text: document.getElementById('drawerRouteBomVersionText'),
            hint: document.getElementById('drawerRouteBomVersionHint'),
            button: document.getElementById('drawerRouteApplyLatestBomBtn'),
            actions: document.getElementById('drawerRouteBomChangeActions'),
        },
    ].filter(target => target.card && target.text && target.hint && target.button);
    if (!targets.length) return;

    const hasBom = !!wo?.bom_id;
    const currentVersion = wo?.bom_version || 'Not captured';
    const bomChange = wo?.bom_change || {};
    const hasActionRequired = !!(wo?.bom_change_action_required || bomChange.action_required);
    const latestBomId = bomChange.latest_bom_id || wo?.latest_bom_id;
    const latestVersion = bomChange.latest_bom_version || wo?.latest_bom_version || '';
    const hasNewerBom = !!latestBomId;
    const canApplyLatestBom = !!wo?.can_apply_latest_bom;

    targets.forEach(({ card, text, hint, button, actions }) => {
        card.classList.toggle('hidden', !hasBom);
        if (!hasBom) return;
        if (actions) {
            actions.classList.add('hidden');
            actions.innerHTML = '';
        }

        text.textContent = latestVersion
            ? `WO uses ${currentVersion}; latest active BOM is ${latestVersion}.`
            : `WO uses ${currentVersion}.`;

        if (hasActionRequired && bomChange.has_started && actions) {
            hint.textContent = `Production has already started. Reported qty ${Number(bomChange.reported_qty || 0)} must be handled before continuing.`;
            button.classList.add('hidden');
            button.disabled = true;
            actions.classList.remove('hidden');
            actions.innerHTML = `
                <button type="button" data-bom-decision="archive_new" class="rounded-lg bg-slate-900 px-3 py-2 text-[11px] font-black uppercase tracking-[0.08em] text-white hover:bg-slate-800">Archive + New WO</button>
                <button type="button" data-bom-decision="scrap_apply" class="rounded-lg bg-rose-600 px-3 py-2 text-[11px] font-black uppercase tracking-[0.08em] text-white hover:bg-rose-700">Scrap Done + Apply</button>
                <button type="button" data-bom-decision="continue_old" class="rounded-lg border border-amber-300 bg-white px-3 py-2 text-[11px] font-black uppercase tracking-[0.08em] text-amber-800 hover:bg-amber-100">Continue Old BOM</button>
            `;
            actions.querySelectorAll('[data-bom-decision]').forEach((actionButton) => {
                actionButton.addEventListener('click', () => {
                    window.decideBomChangeForDrawerWO(actionButton.dataset.bomDecision);
                });
            });
        } else if (hasNewerBom && canApplyLatestBom) {
            hint.textContent = 'This WO is still eligible. Applying will refresh its BOM snapshot and reset material readiness.';
            button.classList.remove('hidden');
            button.disabled = false;
            button.classList.remove('opacity-60', 'cursor-not-allowed');
        } else if (hasNewerBom) {
            hint.textContent = wo?.apply_latest_bom_blocker || 'This WO cannot apply the latest BOM.';
            button.classList.remove('hidden');
            button.disabled = true;
            button.classList.add('opacity-60', 'cursor-not-allowed');
        } else {
            hint.textContent = 'No newer active BOM version is available.';
            button.classList.add('hidden');
            button.disabled = true;
            button.classList.remove('opacity-60', 'cursor-not-allowed');
        }
    });
}

function normalizeTimelineMachinePayload(machine, index = 0) {
    if (!machine || typeof machine !== 'object') return null;
    const rawId = machine.id;
    const id = rawId !== undefined && rawId !== null && String(rawId).trim() !== ''
        ? rawId
        : `machine-${index + 1}`;
    const code = String(machine.code || '').trim();
    const name = String(machine.name || machine.display_name || code || `Machine #${id}`).trim();
    const category = String(machine.category || '').trim();
    const type = String(machine.type || category || 'General').trim();
    const status = String(machine.status || 'operational').trim().toLowerCase() || 'operational';
    return {
        ...machine,
        id,
        code,
        name,
        display_name: String(machine.display_name || (code && code !== name ? `${code} - ${name}` : name)).trim(),
        type,
        category,
        status,
        use_factory_shifts: machine.use_factory_shifts !== false,
        shift_configuration: normalizeTimelineShiftConfig(machine.shift_configuration || timelineState.shiftConfig),
        working_hours_summary: String(machine.working_hours_summary || '').trim(),
        image_url: String(machine.image_url || '').trim(),
    };
}

function setDrawerElementDisabled(element, disabled) {
    if (!element) return;
    element.disabled = !!disabled;
    element.classList.toggle('opacity-60', !!disabled);
    element.classList.toggle('cursor-not-allowed', !!disabled);
}

function applyDrawerClosedState(wo = window.currentDrawerWO) {
    const closed = isDrawerPlannerClosed(wo);
    const cancelButton = document.getElementById('drawerCancelButton');
    const saveButton = document.getElementById('drawerSaveButton');
    const unscheduleButton = document.getElementById('drawerUnscheduleButton');
    const splitBtn = document.getElementById('drawerSplitBtn_New');
    const releaseBtn = document.getElementById('drawerReleaseBtn_New');
    const splitHint = document.getElementById('drawerSplitHint');
    const releaseHint = document.getElementById('drawerReleaseHint');
    const routeCard = document.getElementById('drawerRoutePlannerCard');

    [
        'drawerQuantity',
        'drawerPriority',
        'drawerStatus',
        'drawerStartDate',
        'drawerStage',
        'drawerMachine',
        'drawerRouteSearch',
        'drawerRouteFlowSeries',
        'drawerRouteFlowParallel',
        'drawerMaterialShortageNote',
    ].forEach((id) => setDrawerElementDisabled(document.getElementById(id), closed));

    if (routeCard) {
        routeCard.classList.toggle('opacity-75', closed);
        routeCard.querySelectorAll('input, select, button').forEach((element) => {
            setDrawerElementDisabled(element, closed);
        });
    }

    if (unscheduleButton) unscheduleButton.classList.toggle('hidden', closed);
    if (splitBtn) splitBtn.classList.toggle('hidden', closed);
    if (releaseBtn) releaseBtn.classList.toggle('hidden', closed);
    if (splitHint) splitHint.classList.toggle('hidden', closed);
    if (releaseHint) releaseHint.classList.toggle('hidden', closed);

    if (saveButton) {
        saveButton.disabled = closed;
        saveButton.textContent = closed
            ? 'Already Closed'
            : (window.currentDrawerRoutePlanner ? saveButton.textContent || 'Plan Full Route' : 'Save Changes');
        saveButton.classList.toggle('opacity-70', closed);
        saveButton.classList.toggle('cursor-not-allowed', closed);
    }
    if (cancelButton) {
        cancelButton.textContent = closed ? 'Close' : 'Cancel';
    }
}
window.updateDrawerIdentity = updateDrawerIdentity;
window.updateDrawerBomVersionCard = updateDrawerBomVersionCard;
window.isDrawerPlannerClosed = isDrawerPlannerClosed;
window.applyDrawerClosedState = applyDrawerClosedState;

window.applyLatestBomToDrawerWO = async function () {
    const wo = window.currentDrawerWO;
    if (!wo?.id) return;
    if (!wo.can_apply_latest_bom) {
        showTimelineToast(wo.apply_latest_bom_blocker || 'This WO cannot apply the latest BOM.', 'warning');
        updateDrawerBomVersionCard(wo);
        return;
    }

    const confirmed = typeof window.appConfirm === 'function'
        ? await window.appConfirm(`Apply BOM ${wo.latest_bom_version} to ${getDisplayWorkOrderHashLabel(wo)}? Existing production history will not be rewritten.`, {
            title: 'Apply Latest BOM',
            confirmText: 'Apply BOM',
            kind: 'warning'
        })
        : window.confirm(`Apply BOM ${wo.latest_bom_version} to this work order?`);
    if (!confirmed) return;

    try {
        const response = await fetch(`/manufacturing/api/work-order/${wo.id}/apply-latest-bom/`, {
            method: 'POST',
            headers: { 'X-CSRFToken': getCookie('csrftoken') }
        });
        const data = await response.json();
        if (!response.ok || !data.success) {
            showTimelineToast(data.error || 'Could not apply latest BOM.', 'error');
            return;
        }
        showTimelineToast(data.message || 'Latest BOM applied.', 'success');
        await window.openWorkOrderModal(wo.id);
        if (window.initGanttChart) window.initGanttChart(true);
        else if (window.reloadPlannerWorkspacePreservingState) window.reloadPlannerWorkspacePreservingState();
    } catch (err) {
        console.error(err);
        showTimelineToast('Network error while applying latest BOM.', 'error');
    }
};

window.decideBomChangeForDrawerWO = async function (decision) {
    const wo = window.currentDrawerWO;
    if (!wo?.id || !decision) return;
    const labels = {
        archive_new: 'Archive this WO and create a new WO with the latest BOM?',
        scrap_apply: 'Scrap reported finished quantity and apply the latest BOM to this WO?',
        continue_old: 'Continue this WO with the old BOM version?',
    };
    const confirmed = typeof window.appConfirm === 'function'
        ? await window.appConfirm(labels[decision] || 'Apply BOM change decision?', {
            title: 'BOM Change Decision',
            confirmText: 'Confirm',
            kind: decision === 'continue_old' ? 'info' : 'warning'
        })
        : window.confirm(labels[decision] || 'Apply BOM change decision?');
    if (!confirmed) return;

    let note = '';
    if (decision === 'continue_old' && typeof window.prompt === 'function') {
        note = window.prompt('Reason for continuing with the old BOM:', '') || '';
    }

    try {
        const response = await fetch(`/manufacturing/api/work-order/${wo.id}/bom-change-decision/`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': getCookie('csrftoken')
            },
            body: JSON.stringify({ decision, note })
        });
        const data = await response.json();
        if (!response.ok || !data.success) {
            showTimelineToast(data.error || 'Could not apply BOM change decision.', 'error');
            return;
        }
        showTimelineToast(data.message || 'BOM change decision saved.', 'success');
        if (window.reloadPlannerWorkspacePreservingState) {
            window.reloadPlannerWorkspacePreservingState({
                activeScreen: 'schedule',
                currentView: 'gantt',
                showQueueRail: true,
            });
            return;
        }
        const nextId = data.replacement_wo_id || wo.id;
        await window.openWorkOrderModal(nextId);
        if (window.initGanttChart) window.initGanttChart(true);
        else if (window.reloadPlannerWorkspacePreservingState) window.reloadPlannerWorkspacePreservingState();
    } catch (err) {
        console.error(err);
        showTimelineToast('Network error while applying BOM change decision.', 'error');
    }
};

function materialReadinessTone(status) {
    if (status === 'ready') return { lamp: 'bg-emerald-500', card: 'border-emerald-200 bg-emerald-50/50', text: 'text-emerald-800' };
    if (status === 'partial') return { lamp: 'bg-amber-500', card: 'border-amber-200 bg-amber-50/50', text: 'text-amber-800' };
    if (status === 'shortage') return { lamp: 'bg-rose-500', card: 'border-rose-200 bg-rose-50/50', text: 'text-rose-800' };
    return { lamp: 'bg-slate-300', card: 'border-slate-200 bg-white', text: 'text-slate-900' };
}

function getMaterialReadinessDecisionCopy(readiness = {}, wo = {}) {
    const status = String(readiness.status || wo?.material_readiness_status || 'not_checked').toLowerCase();
    const availableQty = Number(readiness.available_qty ?? wo?.material_available_qty ?? 0);
    const availablePercent = Number(readiness.available_percent ?? wo?.material_available_percent ?? 0);
    const shortfallQty = Number(readiness.shortfall_qty ?? 0);
    const orderQty = Number(readiness.work_order_quantity ?? wo?.quantity ?? 0);
    const deliveryDate = readiness.expected_delivery_date || wo?.material_expected_delivery_date || '';
    const updatedBy = readiness.updated_by ? ` by ${readiness.updated_by}` : '';

    if (status === 'ready') {
        return {
            label: readiness.status_label || 'Material OK',
            summary: `Store confirmed BOM materials for the full order${updatedBy}.`,
            nextAction: readiness.planner_next_action || 'Planner can schedule or release the full work order quantity.',
        };
    }
    if (status === 'partial') {
        const qtyText = availableQty > 0 ? `${availableQty}${orderQty ? ` of ${orderQty}` : ''} units` : 'a partial quantity';
        const percentText = availablePercent > 0 ? ` (${availablePercent}% OK)` : '';
        const shortfallText = shortfallQty > 0 ? ` Shortfall: ${shortfallQty} units.` : '';
        const deliveryText = deliveryDate ? ` Expected delivery: ${deliveryDate}.` : '';
        return {
            label: availableQty > 0 ? `Partially OK: ${availableQty} units available${percentText}` : (readiness.status_label || 'Partially OK'),
            summary: readiness.planner_blocker || `Store confirmed material for ${qtyText}.${shortfallText}${deliveryText}`,
            nextAction: readiness.planner_next_action || 'Planner should split or reduce the work order before scheduling the remaining quantity.',
        };
    }
    if (status === 'shortage') {
        const deliveryText = deliveryDate ? ` Expected delivery: ${deliveryDate}.` : '';
        return {
            label: readiness.status_label || 'Not Available',
            summary: readiness.shortage_note || `Store marked BOM materials as not available.${deliveryText}`,
            nextAction: readiness.planner_next_action || 'Planner should wait for store update or resolve the shortage before scheduling.',
        };
    }
    return {
        label: readiness.status_label || 'Waiting for Store Confirmation',
        summary: 'Store has not confirmed BOM materials for this work order.',
        nextAction: readiness.planner_next_action || 'Ask store to mark OK, Partially OK, or Not Available before planning.',
    };
}

function renderDrawerMaterialReadiness(wo = window.currentDrawerWO) {
    const readiness = wo?.material_readiness || {};
    const status = String(readiness.status || wo?.material_readiness_status || 'not_checked').toLowerCase();
    const tone = materialReadinessTone(status);
    const copy = getMaterialReadinessDecisionCopy(readiness, wo);
    const card = document.getElementById('drawerMaterialReadinessCard');
    const label = document.getElementById('drawerMaterialStatusLabel');
    const summary = document.getElementById('drawerMaterialSummary');
    const nextAction = document.getElementById('drawerMaterialNextAction');
    const lamp = document.getElementById('drawerMaterialStatusLamp');
    const rows = document.getElementById('drawerMaterialRows');
    const availableWrap = document.getElementById('drawerMaterialAvailablePercentWrap');
    const availableInput = document.getElementById('drawerMaterialAvailablePercent');
    const deliveryWrap = document.getElementById('drawerMaterialDeliveryDateWrap');
    const deliveryInput = document.getElementById('drawerMaterialDeliveryDate');
    const noteInput = document.getElementById('drawerMaterialShortageNote');
    const actions = document.getElementById('drawerMaterialActions');
    if (!card) return;

    card.className = `rounded-2xl border px-3 py-3 shadow-sm ${tone.card}`;
    if (label) {
        label.textContent = copy.label || readiness.status_label || status.replace('_', ' ').replace(/\b\w/g, char => char.toUpperCase());
        label.className = `mt-1 text-sm font-black ${tone.text}`;
    }
    if (lamp) lamp.className = `mt-1 h-3 w-3 shrink-0 rounded-full ${tone.lamp}`;
    if (summary) summary.textContent = copy.summary;
    if (nextAction) nextAction.textContent = copy.nextAction;
    if (availableWrap) availableWrap.classList.toggle('hidden', !['partial', 'not_checked'].includes(status));
    if (availableInput) {
        availableInput.value = status === 'partial' && readiness.available_percent ? readiness.available_percent : '';
    }
    if (deliveryWrap) deliveryWrap.classList.toggle('hidden', status === 'ready' || status === 'not_checked');
    if (deliveryInput) {
        deliveryInput.value = readiness.expected_delivery_date || wo?.material_expected_delivery_date || '';
    }
    if (noteInput) {
        noteInput.value = readiness.shortage_note || '';
        noteInput.classList.toggle('hidden', !['partial', 'shortage'].includes(status));
    }
    if (rows) {
        const materials = Array.isArray(readiness.materials) ? readiness.materials : [];
        rows.innerHTML = materials.length
            ? materials.map(item => `
                <div class="flex items-center justify-between gap-3 px-3 py-2 text-xs">
                    <div class="min-w-0">
                        <div class="truncate font-bold text-slate-800">${escapeHtml(item.name || '-')}</div>
                        <div class="text-[10px] font-semibold text-slate-400">BOM qty ${escapeHtml(item.bom_quantity ?? '')} per base batch</div>
                    </div>
                    <div class="shrink-0 text-right">
                        <div class="text-[10px] font-black uppercase tracking-[0.12em] text-slate-400">Required</div>
                        <div class="font-black text-slate-700">${escapeHtml(item.required_quantity ?? '')} ${escapeHtml(item.unit || '')}</div>
                    </div>
                </div>
            `).join('')
            : '<div class="px-3 py-4 text-center text-xs font-semibold text-slate-400">No BOM materials found.</div>';
    }

    const canUpdate = ['planner', 'admin'].includes(getUserRole()) && !isDrawerPlannerClosed(wo);
    if (actions) {
        actions.querySelectorAll('.drawer-material-action').forEach((button) => {
            button.disabled = !canUpdate;
            button.classList.toggle('opacity-50', !canUpdate);
            button.classList.toggle('cursor-not-allowed', !canUpdate);
            button.onclick = () => updateDrawerMaterialReadiness(button.dataset.materialStatus || 'not_checked');
        });
    }
}

function syncMaterialReadinessToPlannerCaches(workOrderId, materialReadiness) {
    if (!workOrderId || !materialReadiness) return;
    const targetId = String(workOrderId);
    const applyReadiness = (item) => {
        if (!item || typeof item !== 'object') return;
        const itemId = String(item.id || '');
        const parentId = String(item.parent_id || '');
        if (itemId !== targetId && parentId !== targetId) return;
        item.material_readiness = materialReadiness;
        item.material_readiness_status = materialReadiness.status;
        item.material_shortage_note = materialReadiness.shortage_note || '';
        item.material_available_qty = materialReadiness.available_qty ?? null;
        item.material_available_percent = materialReadiness.available_percent ?? null;
        item.material_expected_delivery_date = materialReadiness.expected_delivery_date || '';
    };

    (Array.isArray(tasksCache) ? tasksCache : []).forEach(applyReadiness);

    const intakeEl = document.getElementById('data-intake-orders');
    if (intakeEl && intakeEl.textContent) {
        const intakeOrders = safeParseJSON(intakeEl.textContent);
        if (Array.isArray(intakeOrders)) {
            intakeOrders.forEach(applyReadiness);
            intakeEl.textContent = JSON.stringify(intakeOrders);
        }
    }

    if (window.renderPlannerFollowUpQueue) window.renderPlannerFollowUpQueue();
    if (window.renderPlannerDispatchReadinessQueue) window.renderPlannerDispatchReadinessQueue();
    if (window.renderPlannerKanban) window.renderPlannerKanban();
    if (window.renderPlannerList) window.renderPlannerList();
    if (window.renderPlannerCalendar) window.renderPlannerCalendar();
}

function rememberPlannerWorkOrderAfterReload(workOrderId) {
    const targetId = String(
        workOrderId
        || window.currentDrawerWO?.parent_id
        || window.currentDrawerWO?.id
        || document.getElementById('drawerTaskId')?.value
        || ''
    ).trim();
    if (!targetId) return '';
    try {
        sessionStorage.setItem('planner-open-wo-after-reload', targetId);
    } catch (e) {}
    return targetId;
}

function refreshPlannerQueuesAfterMaterialReadinessUpdate(materialReadiness, workOrderId) {
    const status = String(materialReadiness?.status || '').toLowerCase();
    if (status !== 'ready') return;

    const isPlannerDashboard = !!document.querySelector('[data-planner-dashboard="true"]');
    if (!isPlannerDashboard) return;

    rememberPlannerWorkOrderAfterReload(workOrderId);
    window.setTimeout(() => {
        if (typeof window.reloadPlannerWorkspacePreservingState === 'function') {
            window.reloadPlannerWorkspacePreservingState({ activeScreen: 'schedule' });
            return;
        }
        if (typeof window.persistPlannerWorkspaceState === 'function') {
            window.persistPlannerWorkspaceState({ activeScreen: 'schedule' });
        }
        window.location.reload();
    }, 650);
}

function updateDrawerMaterialReadiness(status) {
    const wo = window.currentDrawerWO || {};
    const woId = wo.parent_id || wo.id || document.getElementById('drawerTaskId')?.value;
    if (!woId) return;
    const noteInput = document.getElementById('drawerMaterialShortageNote');
    const availableInput = document.getElementById('drawerMaterialAvailablePercent');
    const deliveryInput = document.getElementById('drawerMaterialDeliveryDate');
    const shortageNote = String(noteInput?.value || '').trim();
    const availablePercent = Number.parseFloat(availableInput?.value || '');
    if (status === 'partial' && (!Number.isFinite(availablePercent) || availablePercent <= 0 || availablePercent >= 100)) {
        if (availableInput) {
            availableInput.classList.remove('hidden');
            availableInput.focus();
        }
        showTimelineToast('Enter the available percent between 1 and 99 before marking partially OK.', 'warning');
        return;
    }

    const payload = { status, shortage_note: shortageNote, expected_delivery_date: deliveryInput?.value || '' };
    if (status === 'partial') {
        payload.available_percent = availablePercent;
    }

    fetch(`/manufacturing/api/work-order/${woId}/material-readiness/`, {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            'X-CSRFToken': getCookie('csrftoken')
        },
        body: JSON.stringify(payload)
    })
        .then(res => res.json().then(data => ({ ok: res.ok, data })))
        .then(({ ok, data }) => {
            if (!ok || !data.success) {
                throw new Error(data.error || 'Unable to update material readiness.');
            }
            window.currentDrawerWO.material_readiness = data.material_readiness;
            window.currentDrawerWO.material_readiness_status = data.material_readiness.status;
            window.currentDrawerWO.material_shortage_note = data.material_readiness.shortage_note;
            window.currentDrawerWO.material_available_qty = data.material_readiness.available_qty;
            window.currentDrawerWO.material_available_percent = data.material_readiness.available_percent;
            window.currentDrawerWO.material_expected_delivery_date = data.material_readiness.expected_delivery_date;
            syncMaterialReadinessToPlannerCaches(data.work_order_id || woId, data.material_readiness);
            renderDrawerMaterialReadiness(window.currentDrawerWO);
            showTimelineToast(
                data.material_readiness.status === 'ready'
                    ? 'Material ready. Refreshing planner queue...'
                    : (data.material_readiness.status === 'partial'
                        ? 'Partial material percent saved. Split or reduce before planning.'
                        : 'Material readiness updated.'),
                'success'
            );
            refreshPlannerQueuesAfterMaterialReadinessUpdate(data.material_readiness, data.work_order_id || woId);
        })
        .catch(err => {
            showTimelineToast(err?.message || 'Unable to update material readiness.', 'error');
        });
}
window.updateDrawerMaterialReadiness = updateDrawerMaterialReadiness;
window.renderDrawerMaterialReadiness = renderDrawerMaterialReadiness;
window.syncMaterialReadinessToPlannerCaches = syncMaterialReadinessToPlannerCaches;
window.refreshPlannerQueuesAfterMaterialReadinessUpdate = refreshPlannerQueuesAfterMaterialReadinessUpdate;

function confirmMaterialShortageOverride(messagePrefix = 'Continue anyway?') {
    const wo = window.currentDrawerWO || {};
    const status = String(wo.material_readiness_status || wo.material_readiness?.status || '');
    if (status !== 'shortage') return true;
    const note = String(wo.material_shortage_note || wo.material_readiness?.shortage_note || '').trim();
    return window.confirm(`Material is marked not available for this work order.${note ? `\n\n${note}` : ''}\n\n${messagePrefix}`);
}

function getMaterialPlanningBlocker(wo = window.currentDrawerWO) {
    const readiness = wo?.material_readiness || {};
    const status = String(readiness.status || wo?.material_readiness_status || 'not_checked');
    if (status === 'ready') return '';
    if (readiness.can_plan === true) return '';
    if (readiness.planner_blocker) return readiness.planner_blocker;
    if (status === 'partial') {
        const available = readiness.available_qty || wo?.material_available_qty || 0;
        const shortfall = readiness.shortfall_qty ? ` Shortfall: ${readiness.shortfall_qty} units.` : '';
        return `Store confirmed material for ${available} units only.${shortfall} Reduce or split the WO before planning.`;
    }
    if (status === 'shortage') return readiness.shortage_note || wo?.material_shortage_note || 'Store marked BOM materials as not available.';
    return 'Store has not confirmed BOM materials.';
}

function splitDrawerHistoryEntries(entries, wo, stages = []) {
    const activeStage = getCurrentDrawerActiveStageContext(stages);
    const activeStageTaskId = Number(activeStage?.planned_task_id || 0);
    if (!activeStageTaskId) {
        return {
            activeStage,
            activeStageTaskId: null,
            routeEntries: Array.isArray(entries) ? entries : [],
            stageEntries: [],
        };
    }

    const routeEntries = [];
    const stageEntries = [];
    (Array.isArray(entries) ? entries : []).forEach((entry) => {
        const relatedId = Number(entry?.related_work_order_id || 0);
        if (relatedId && relatedId === activeStageTaskId) {
            stageEntries.push(entry);
        } else {
            routeEntries.push(entry);
        }
    });
    return { activeStage, activeStageTaskId, routeEntries, stageEntries };
}

function updateDrawerAuditLinks(wo, stages = []) {
    const routeLink = document.getElementById('drawerRouteAuditLink');
    const stageLink = document.getElementById('drawerStageAuditLink');
    const machineLink = document.getElementById('drawerMachineAuditLink');
    if (routeLink && wo?.id) {
        routeLink.href = buildDrawerAuditTrailUrl({ audit_work_order: wo.id });
    }

    const activeStage = getCurrentDrawerActiveStageContext(stages);
    const activeStageTaskId = String(activeStage?.planned_task_id || '').trim();
    if (stageLink) {
        if (activeStageTaskId) {
            stageLink.href = buildDrawerAuditTrailUrl({ audit_work_order: activeStageTaskId });
            stageLink.classList.remove('hidden');
            stageLink.textContent = `Stage Audit${activeStage?.name ? `: ${activeStage.name}` : ''}`;
        } else {
            stageLink.classList.add('hidden');
            stageLink.textContent = 'Stage Audit';
        }
    }

    if (machineLink) {
        const machineContext = resolveDrawerMachineContext(wo, stages);
        if (machineContext?.id) {
            machineLink.href = buildDrawerAuditTrailUrl({ audit_machine: machineContext.id });
            machineLink.classList.remove('hidden');
        } else {
            machineLink.classList.add('hidden');
        }
    }
}

function renderDrawerWorkOrderHistorySections(entries, wo, stages = []) {
    const parentMount = document.getElementById('drawerParentHistory');
    const stageMount = document.getElementById('drawerStageHistory');
    const stageTitle = document.getElementById('drawerStageHistoryTitle');
    if (!parentMount || !stageMount || !stageTitle) return;

    const { activeStage, activeStageTaskId, routeEntries, stageEntries } = splitDrawerHistoryEntries(entries, wo, stages);
    renderDrawerHistoryEntries(
        'drawerParentHistory',
        routeEntries,
        'No route-level history has been recorded yet.'
    );
    stageTitle.textContent = activeStage?.name
        ? `Selected Stage History - ${activeStage.name}`
        : 'Selected Stage History';
    renderDrawerHistoryEntries(
        'drawerStageHistory',
        stageEntries,
        activeStageTaskId
            ? 'No stage-specific history has been recorded yet.'
            : 'Select a planned stage to see its stage-specific history.'
    );
    updateDrawerAuditLinks(wo, stages);
}

function resolveDrawerMachineContext(wo, stages = []) {
    if (!window.currentDrawerRoutePlanner) {
        const machineSelect = document.getElementById('drawerMachine');
        const selectedMachineId = String(machineSelect?.value || '').trim();
        if (selectedMachineId) {
            const selectedName = machineSelect.options[machineSelect.selectedIndex]?.textContent?.trim() || `Machine #${selectedMachineId}`;
            return { id: selectedMachineId, name: selectedName };
        }
    }

    if (wo?.machine_id) {
        return {
            id: String(wo.machine_id),
            name: wo.machine_name || `Machine #${wo.machine_id}`,
        };
    }

    const activeStageId = String(window.currentDrawerActiveStageId || '').trim();
    const activeStage = Array.isArray(stages)
        ? stages.find((stage) => String(stage.id) === activeStageId)
        : null;
    const stageMachineId = String(activeStage?.assigned_machine_id || '').trim();
    if (!stageMachineId) return null;

    const stageMachine = Array.isArray(activeStage?.candidate_machines)
        ? activeStage.candidate_machines.find((machine) => String(machine.id) === stageMachineId)
        : null;
    return {
        id: stageMachineId,
        name: stageMachine?.name || `Machine #${stageMachineId}`,
    };
}

function loadDrawerWorkOrderHistory(wo, stages = [], token) {
    const parentMount = document.getElementById('drawerParentHistory');
    const stageMount = document.getElementById('drawerStageHistory');
    if (!parentMount || !stageMount || !wo?.id) return Promise.resolve();

    parentMount.innerHTML = '<div class="px-3 py-4 text-sm text-slate-400">Loading route history...</div>';
    stageMount.innerHTML = '<div class="px-3 py-4 text-sm text-slate-400">Loading selected stage history...</div>';
    return fetch(`/manufacturing/api/work-order/${wo.id}/log/`)
        .then((res) => res.json())
        .then((data) => {
            if (window.currentDrawerHistoryToken !== token) return;
            if (!data.success) {
                throw new Error(data.error || 'Unable to load work order history.');
            }
            window.currentDrawerWorkOrderHistoryEntries = Array.isArray(data.logs) ? data.logs : [];
            renderDrawerWorkOrderHistorySections(window.currentDrawerWorkOrderHistoryEntries, wo, stages);
        })
        .catch((err) => {
            if (window.currentDrawerHistoryToken !== token) return;
            const errorText = escapeHtml(err?.message || 'Unable to load work order history.');
            parentMount.innerHTML = `<div class="px-3 py-4 text-sm text-rose-600">${errorText}</div>`;
            stageMount.innerHTML = `<div class="px-3 py-4 text-sm text-rose-600">${errorText}</div>`;
        });
}

function loadDrawerMachineHistory(wo, stages = [], token = null) {
    const currentToken = token || window.currentDrawerHistoryToken;
    const section = document.getElementById('drawerMachineHistorySection');
    const title = document.getElementById('drawerMachineHistoryTitle');
    const mount = document.getElementById('drawerMachineHistory');
    if (!section || !title || !mount) return Promise.resolve();

    const context = resolveDrawerMachineContext(wo, stages);
    if (!context) {
        title.textContent = 'Machine History';
        mount.innerHTML = '<div class="px-3 py-4 text-sm text-slate-400">No machine is linked to this work order yet.</div>';
        section.classList.add('opacity-70');
        updateDrawerAuditLinks(wo, stages);
        return Promise.resolve();
    }

    section.classList.remove('opacity-70');
    title.textContent = `Machine History - ${context.name}`;
    mount.innerHTML = '<div class="px-3 py-4 text-sm text-slate-400">Loading machine history...</div>';

    return fetch(`/manufacturing/api/machine/${context.id}/log/`)
        .then((res) => res.json())
        .then((data) => {
            if (window.currentDrawerHistoryToken !== currentToken) return;
            if (!data.success) {
                throw new Error(data.error || 'Unable to load machine history.');
            }
            renderDrawerHistoryEntries(
                'drawerMachineHistory',
                data.logs || [],
                'No machine history has been recorded yet.'
            );
            updateDrawerAuditLinks(wo, stages);
        })
        .catch((err) => {
            if (window.currentDrawerHistoryToken !== currentToken) return;
            mount.innerHTML = `<div class="px-3 py-4 text-sm text-rose-600">${escapeHtml(err?.message || 'Unable to load machine history.')}</div>`;
            updateDrawerAuditLinks(wo, stages);
        });
}

function loadDrawerHistories(wo, stages = []) {
    const token = `${wo?.id || 'wo'}-${Date.now()}`;
    window.currentDrawerHistoryToken = token;
    updateDrawerAuditLinks(wo, stages);
    loadDrawerWorkOrderHistory(wo, stages, token);
    loadDrawerMachineHistory(wo, stages, token);
}

function getRouteStageStartOverride(stageId) {
    const key = String(stageId);
    return String(window.currentDrawerRouteStartOverrides[key] || '').trim();
}

function assignRouteStageStart(stageId, startValue = '') {
    const key = String(stageId);
    const normalizedValue = String(startValue || '').trim();
    if (normalizedValue) {
        window.currentDrawerRouteStartOverrides[key] = normalizedValue;
    } else {
        delete window.currentDrawerRouteStartOverrides[key];
    }
}

function assignRouteStageMachine(stageId, machineId = '', mode = 'manual') {
    const key = String(stageId);
    const normalizedMachineId = String(machineId || '');
    const normalizedMode = String(mode || '').trim().toLowerCase() || (normalizedMachineId ? 'manual' : 'auto');
    window.currentDrawerRouteMachineAssignments[key] = normalizedMachineId;
    window.currentDrawerRouteMachineModes[key] = normalizedMode;
}

function applyRecommendedRouteMachines(stages) {
    stages.forEach(stage => {
        const recommendedMachine = getRecommendedRouteMachine(stage);
        assignRouteStageMachine(stage.id, recommendedMachine?.id || '', recommendedMachine ? 'recommended' : 'auto');
    });
}

function buildQuickRouteAssignments(routeStages, workOrder) {
    if (!Array.isArray(routeStages) || routeStages.length === 0) {
        return { firstStage: null, firstMachineId: '', routeAssignments: [] };
    }

    const firstStage = routeStages[0];
    const firstCandidates = getRouteStageCandidateMachines(firstStage);
    const explicitFirstMachineId = String(
        workOrder?.machine_id
        || firstStage?.default_machine_id
        || ''
    ).trim();

    let firstMachineId = explicitFirstMachineId;
    if (!firstMachineId && firstCandidates.length === 1) {
        firstMachineId = String(firstCandidates[0].id);
    }

    if (!firstMachineId && firstCandidates.length !== 1) {
        return { firstStage, firstMachineId: '', routeAssignments: [] };
    }

    const routeAssignments = routeStages.map((stage, index) => {
        const stageCandidates = getRouteStageCandidateMachines(stage);
        const defaultMachineId = String(
            index === 0
                ? firstMachineId
                : (stage?.default_machine_id || (stageCandidates.length === 1 ? stageCandidates[0].id : ''))
        ).trim();

        return {
            stage_id: stage.id,
            machine_id: defaultMachineId,
            selection_mode: defaultMachineId ? 'manual' : 'auto',
        };
    });

    return { firstStage, firstMachineId, routeAssignments };
}

function renderDrawerRouteStageList(stages, activeStageId) {
    const mount = document.getElementById('drawerRouteStageList');
    if (!mount) return;

    const searchQuery = String(window.currentDrawerRouteSearch || '').trim().toLowerCase();
    mount.className = 'grid gap-2 md:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 2xl:grid-cols-5';
    mount.innerHTML = '';
    const orderedStages = Array.isArray(stages) ? stages.filter(Boolean) : [];

    orderedStages.forEach((stage, index) => {
        const candidateMachines = getRouteStageCandidateMachines(stage);
        const recommendedMachine = getRecommendedRouteMachine(stage);
        const stageSearchBlob = [
            stage?.name || '',
            stage?.machine_type || '',
            recommendedMachine?.display_name || recommendedMachine?.name || '',
            ...candidateMachines.map(machine => `${machine.display_name || ''} ${machine.code || ''} ${machine.name || ''} ${machine.type || ''} ${machine.category || ''}`),
        ].join(' ').toLowerCase();
        if (searchQuery && !stageSearchBlob.includes(searchQuery)) {
            return;
        }
        const machineSearchQuery = String(window.currentDrawerRouteMachineSearch[String(stage.id)] || '').trim().toLowerCase();
        const filteredMachines = machineSearchQuery
            ? candidateMachines.filter((machine) => {
                const blob = `${machine.display_name || ''} ${machine.code || ''} ${machine.name || ''} ${machine.type || ''} ${machine.category || ''}`.toLowerCase();
                return blob.includes(machineSearchQuery);
            })
            : candidateMachines;
        const flowMode = getCurrentDrawerOperationFlowMode();
        const selectedMachineId = String(window.currentDrawerRouteMachineAssignments[String(stage.id)] || '');
        const selectionMode = getRouteStageSelectionMode(stage.id);
        const selectedMachine = candidateMachines.find(machine => String(machine.id) === selectedMachineId) || null;
        const readiness = getRouteStageReadiness(stage);
        const selectMachines = filteredMachines.slice();
        if (selectedMachine && !selectMachines.some(machine => String(machine.id) === selectedMachineId)) {
            selectMachines.unshift(selectedMachine);
        }
        if (recommendedMachine && !selectMachines.some(machine => String(machine.id) === String(recommendedMachine.id))) {
            selectMachines.unshift(recommendedMachine);
        }
        const row = document.createElement('section');
        const isActive = String(stage.id) === String(activeStageId || '');
        row.dataset.stageId = String(stage.id);
        row.className = [
            'min-w-0 rounded-xl border p-2',
            readiness.blocking
                ? (isActive ? 'border-rose-300 bg-rose-50/50 shadow-lg shadow-rose-100/70' : 'border-rose-200 bg-rose-50/40')
                : (isActive ? 'border-indigo-300 bg-white shadow-lg shadow-indigo-100/70' : 'border-slate-200 bg-white/90')
        ].join(' ');

        const top = document.createElement('div');
        top.className = 'flex items-start gap-2 cursor-pointer';
        top.addEventListener('click', () => {
            const activeStageId = setCurrentDrawerActiveStage(stages, window.currentDrawerWO, stage.id);
            renderDrawerRouteStageList(stages, activeStageId);
        });

        const badge = document.createElement('div');
        badge.className = [
            'mt-0.5 flex h-6 w-6 items-center justify-center rounded-lg text-[10px] font-black',
            isActive ? 'bg-indigo-600 text-white' : 'bg-indigo-100 text-indigo-700'
        ].join(' ');
        badge.textContent = String(index + 1);

        const body = document.createElement('div');
        body.className = 'min-w-0 flex-1';

        const title = document.createElement('div');
        title.className = 'text-[12px] font-extrabold text-slate-900 truncate';
        title.textContent = stage.name || `Stage ${index + 1}`;

        const meta = document.createElement('div');
        meta.className = 'mt-0.5 text-[9px] font-semibold uppercase tracking-[0.14em] text-slate-400';
        meta.textContent = stage.machine_type
            ? stage.machine_type
            : 'GENERAL';

        body.appendChild(title);
        body.appendChild(meta);
        top.appendChild(badge);
        top.appendChild(body);

        const rightMeta = document.createElement('div');
        rightMeta.className = 'min-w-0 text-right';
        rightMeta.innerHTML = `
            <div class="text-[9px] font-bold uppercase tracking-[0.14em] ${selectionMode === 'recommended' ? 'text-emerald-600' : selectedMachine ? 'text-slate-700' : 'text-amber-600'}">
                ${selectionMode === 'recommended' ? 'Recommended' : selectedMachine ? 'Selected' : 'Planner'}
            </div>
            <div class="mt-0.5 max-w-[120px] truncate text-[11px] font-bold ${selectedMachine ? 'text-slate-900' : 'text-slate-400'}">
                ${selectedMachine
                ? selectedMachine.name
                : 'Unassigned'}
            </div>
        `;
        top.appendChild(rightMeta);
        row.appendChild(top);

        const readinessClasses = {
            emerald: 'border-emerald-200 bg-emerald-50 text-emerald-700',
            amber: 'border-amber-200 bg-amber-50 text-amber-700',
            orange: 'border-orange-200 bg-orange-50 text-orange-700',
            rose: 'border-rose-200 bg-rose-50 text-rose-700',
        };
        const readinessBar = document.createElement('div');
        readinessBar.className = [
            'mt-2 rounded-lg border px-2.5 py-1.5',
            readinessClasses[readiness.tone] || readinessClasses.amber,
        ].join(' ');
        readinessBar.innerHTML = `
            <div class="flex items-center justify-between gap-2">
                <span class="text-[10px] font-black uppercase tracking-[0.12em]">${escapeHtml(readiness.state)}</span>
                ${readiness.blocking ? '<span class="text-[9px] font-black uppercase tracking-[0.12em]">Blocks plan</span>' : '<span class="text-[9px] font-black uppercase tracking-[0.12em]">Can plan</span>'}
            </div>
            <div class="mt-0.5 text-[10px] font-semibold leading-snug">${escapeHtml(readiness.reason)}</div>
        `;
        row.appendChild(readinessBar);

        const summaryBar = document.createElement('div');
        summaryBar.className = [
            'mt-2 rounded-lg border px-2.5 py-1.5 transition',
            selectionMode === 'recommended'
                ? 'border-cyan-200 bg-cyan-50/80'
                : selectedMachine
                    ? 'border-indigo-200 bg-indigo-50/80'
                    : 'border-amber-200 bg-amber-50/80'
        ].join(' ');
        summaryBar.dataset.stageId = String(stage.id);
        summaryBar.innerHTML = `
            <div class="flex items-center justify-between gap-2">
                <span class="min-w-0 truncate text-[10px] font-semibold ${recommendedMachine ? 'text-indigo-600' : 'text-slate-400'}">
                    ${recommendedMachine ? `Rec: ${recommendedMachine.name}` : 'No recommendation'}
                </span>
                <span class="text-[9px] text-slate-400 whitespace-nowrap">
                    ${flowMode === 'parallel' ? 'Runs in parallel' : index === 0 ? 'Starts first' : 'Follows previous'}
                </span>
            </div>
        `;

        summaryBar.addEventListener('dragover', (event) => {
            event.preventDefault();
            summaryBar.classList.add('ring-2', 'ring-indigo-200');
        });
        summaryBar.addEventListener('dragleave', () => {
            summaryBar.classList.remove('ring-2', 'ring-indigo-200');
        });
        summaryBar.addEventListener('drop', (event) => {
            event.preventDefault();
            summaryBar.classList.remove('ring-2', 'ring-indigo-200');
            const machineId = event.dataTransfer?.getData('text/plain') || window.currentDrawerRouteDragMachineId || '';
            if (!machineId) return;
            if (!candidateMachines.some(machine => String(machine.id) === String(machineId))) return;
            assignRouteStageMachine(stage.id, machineId, 'manual');
            renderDrawerRouteStageList(stages, stage.id);
        });
        row.appendChild(summaryBar);

        const stageStartWrap = document.createElement('div');
        stageStartWrap.className = 'mt-2 rounded-lg border border-slate-200 bg-slate-50/80 p-2';

        const stageStartHeader = document.createElement('div');
        stageStartHeader.className = 'mb-1 flex items-center justify-between gap-2';

        const stageStartLabel = document.createElement('div');
        stageStartLabel.className = 'text-[10px] font-bold uppercase tracking-[0.14em] text-slate-500';
        stageStartLabel.textContent = index === 0 ? 'Route Start' : 'Manual Start';

        const stageStartHint = document.createElement('div');
        stageStartHint.className = 'text-[9px] font-medium text-slate-400';
        stageStartHint.textContent = index === 0
            ? 'Default start time for this route'
            : flowMode === 'parallel'
                ? 'Optional exact start for this stage'
                : 'Optional override after previous stage';

        stageStartHeader.appendChild(stageStartLabel);
        stageStartHeader.appendChild(stageStartHint);
        stageStartWrap.appendChild(stageStartHeader);

        const stageStartControls = document.createElement('div');
        stageStartControls.className = 'flex items-center gap-2';

        const stageStartInput = document.createElement('input');
        stageStartInput.type = 'datetime-local';
        stageStartInput.value = getRouteStageStartOverride(stage.id);
        stageStartInput.className = 'min-w-0 flex-1 rounded-lg border border-slate-200 bg-white px-2.5 py-1.5 text-[11px] font-semibold text-slate-700 focus:border-indigo-400 focus:ring-2 focus:ring-indigo-100';
        stageStartInput.addEventListener('change', () => {
            assignRouteStageStart(stage.id, stageStartInput.value || '');
        });
        stageStartControls.appendChild(stageStartInput);

        if (index > 0) {
            const clearStageStartButton = document.createElement('button');
            clearStageStartButton.type = 'button';
            clearStageStartButton.className = 'inline-flex h-8 shrink-0 items-center justify-center rounded-lg bg-slate-100 px-2.5 text-[10px] font-bold text-slate-600 transition hover:bg-slate-200';
            clearStageStartButton.textContent = 'Auto';
            clearStageStartButton.title = 'Clear manual start override';
            clearStageStartButton.addEventListener('click', () => {
                assignRouteStageStart(stage.id, '');
                renderDrawerRouteStageList(stages, stage.id);
            });
            stageStartControls.appendChild(clearStageStartButton);
        }

        stageStartWrap.appendChild(stageStartControls);
        row.appendChild(stageStartWrap);

        const machineSearchWrap = document.createElement('label');
        machineSearchWrap.className = 'mt-2 relative block';

        const machineSearchIcon = document.createElement('i');
        machineSearchIcon.className = 'ph ph-magnifying-glass pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-slate-400 text-[12px]';

        const machineSearchInput = document.createElement('input');
        machineSearchInput.type = 'text';
        machineSearchInput.value = machineSearchQuery;
        machineSearchInput.placeholder = `Search ${String(stage.machine_type || 'machine').toLowerCase()}...`;
        machineSearchInput.className = 'w-full rounded-lg border border-slate-200 bg-white pl-8 pr-3 py-1.5 text-[11px] font-medium text-slate-700 placeholder-slate-400 focus:border-indigo-400 focus:ring-2 focus:ring-indigo-100';
        machineSearchInput.addEventListener('input', () => {
            window.currentDrawerRouteMachineSearch[String(stage.id)] = String(machineSearchInput.value || '').trim().toLowerCase();
            renderDrawerRouteStageList(stages, stage.id);
        });

        machineSearchWrap.appendChild(machineSearchIcon);
        machineSearchWrap.appendChild(machineSearchInput);
        row.appendChild(machineSearchWrap);

        const controlRow = document.createElement('div');
        controlRow.className = 'mt-2 flex items-center gap-2';

        const select = document.createElement('select');
        select.className = 'min-w-0 flex-1 rounded-lg border border-slate-200 bg-white px-2.5 py-1.5 text-[11px] font-semibold text-slate-700 focus:border-indigo-400 focus:ring-2 focus:ring-indigo-100';

        const autoOption = document.createElement('option');
        autoOption.value = '';
        autoOption.textContent = recommendedMachine ? 'Leave unassigned / keep recommendation' : 'Leave unassigned';
        select.appendChild(autoOption);

        selectMachines.forEach(machine => {
            const option = document.createElement('option');
            option.value = machine.id;
            option.textContent = formatTimelineMachineLabel(machine, { includeStatus: true });
            if (machine.status === 'maintenance' || machine.status === 'breakdown') {
                option.disabled = true;
            }
            select.appendChild(option);
        });

        if (selectedMachineId && Array.from(select.options).some(option => String(option.value) === selectedMachineId && !option.disabled)) {
            select.value = selectedMachineId;
        } else {
            select.value = '';
        }
        select.addEventListener('change', () => {
            assignRouteStageMachine(stage.id, select.value || '', select.value ? 'manual' : 'auto');
            renderDrawerRouteStageList(stages, stage.id);
        });
        controlRow.appendChild(select);

        const recommendButton = document.createElement('button');
        recommendButton.type = 'button';
        recommendButton.className = 'inline-flex h-8 w-8 items-center justify-center rounded-lg bg-cyan-50 text-[10px] font-bold text-cyan-700 transition hover:bg-cyan-100 disabled:cursor-not-allowed disabled:opacity-50';
        recommendButton.innerHTML = '<i class="ph ph-magic-wand text-sm"></i>';
        recommendButton.title = 'Use system recommendation';
        recommendButton.addEventListener('click', () => {
            assignRouteStageMachine(stage.id, recommendedMachine?.id || '', recommendedMachine ? 'recommended' : 'auto');
            renderDrawerRouteStageList(stages, stage.id);
        });

        const clearButton = document.createElement('button');
        clearButton.type = 'button';
        clearButton.className = 'inline-flex h-8 w-8 items-center justify-center rounded-lg bg-slate-100 text-[10px] font-bold text-slate-600 transition hover:bg-slate-200 disabled:cursor-not-allowed disabled:opacity-50';
        clearButton.innerHTML = '<i class="ph ph-x text-sm"></i>';
        clearButton.title = 'Clear machine selection';
        clearButton.addEventListener('click', () => {
            assignRouteStageMachine(stage.id, '', 'auto');
            renderDrawerRouteStageList(stages, stage.id);
        });

        controlRow.appendChild(recommendButton);
        controlRow.appendChild(clearButton);
        row.appendChild(controlRow);

        const chipList = document.createElement('div');
        chipList.className = 'mt-1 flex gap-1.5 overflow-x-auto pb-1';
        if (filteredMachines.length === 0) {
            const empty = document.createElement('div');
            empty.className = 'rounded-lg border border-slate-200 bg-slate-50 px-2.5 py-1.5 text-[10px] font-semibold text-slate-500';
            empty.textContent = machineSearchQuery
                ? 'No machine matches this search'
                : 'No BOM machine found for this stage';
            chipList.appendChild(empty);
        } else {
            filteredMachines.forEach(machine => {
                const chip = document.createElement('button');
                chip.type = 'button';
                chip.draggable = true;
                chip.className = [
                    'inline-flex shrink-0 items-center gap-1 rounded-lg border px-2 py-1 text-[10px] font-bold transition',
                    String(machine.id) === selectedMachineId
                        ? 'border-indigo-300 bg-indigo-100 text-indigo-700'
                        : 'border-slate-200 bg-slate-50 text-slate-600 hover:border-indigo-200 hover:bg-indigo-50'
                ].join(' ');
                chip.textContent = formatTimelineMachineLabel(machine);
                if (machine.status === 'maintenance' || machine.status === 'breakdown') {
                    chip.disabled = true;
                    chip.className += ' opacity-50 cursor-not-allowed';
                } else {
                    chip.addEventListener('click', () => {
                        assignRouteStageMachine(stage.id, machine.id, 'manual');
                        renderDrawerRouteStageList(stages, stage.id);
                    });
                    chip.addEventListener('dragstart', (event) => {
                        window.currentDrawerRouteDragMachineId = String(machine.id);
                        event.dataTransfer?.setData('text/plain', String(machine.id));
                        event.dataTransfer.effectAllowed = 'move';
                    });
                    chip.addEventListener('dragend', () => {
                        window.currentDrawerRouteDragMachineId = null;
                    });
                }
                chipList.appendChild(chip);
            });
        }
        row.appendChild(chipList);
        mount.appendChild(row);
    });

    updateRoutePlannerSaveButtonLabel();

    if (!mount.children.length) {
        const emptyState = document.createElement('div');
        emptyState.className = 'rounded-2xl border border-dashed border-slate-200 bg-slate-50 px-4 py-6 text-center text-sm font-semibold text-slate-500 md:col-span-2 2xl:col-span-3';
        emptyState.textContent = searchQuery
            ? `No BOM stage or machine matches "${searchQuery}".`
            : 'No BOM stages are available for planning.';
        mount.appendChild(emptyState);
        return;
    }

    const normalizedActiveStageId = String(activeStageId || '').trim();
    if (normalizedActiveStageId) {
        window.requestAnimationFrame(() => {
            const activeCard = mount.querySelector(`[data-stage-id="${normalizedActiveStageId}"]`);
            if (activeCard && typeof activeCard.scrollIntoView === 'function') {
                activeCard.scrollIntoView({ block: 'nearest', inline: 'nearest', behavior: 'smooth' });
            }
        });
    }
}

function setDrawerPlanningMode(wo, stages, options = {}) {
    const routePlanner = isDrawerRoutePlannerMode(wo, stages, options);
    window.currentDrawerRoutePlanner = routePlanner;
    if (document.body) {
        document.body.classList.add('work-order-drawer-open');
        document.body.classList.toggle('route-planner-open', routePlanner);
    }

    const routeCard = document.getElementById('drawerRoutePlannerCard');
    const stageLabel = document.getElementById('drawerStageLabel');
    const stageSection = document.getElementById('drawerStageSection');
    const machineLabel = document.getElementById('drawerMachineLabel');
    const machineSection = document.getElementById('drawerMachineSection');
    const startLabel = document.getElementById('drawerStartDateLabel');
    const startSection = document.getElementById('drawerStartDateSection');
    const statusContainer = document.getElementById('drawerStatusContainer');
    const statusSelect = document.getElementById('drawerStatus');
    const plannerMetaGrid = document.getElementById('drawerPlannerMetaGrid');
    const flowSeriesRadio = document.getElementById('drawerRouteFlowSeries');
    const flowParallelRadio = document.getElementById('drawerRouteFlowParallel');
    const routeSearchInput = document.getElementById('drawerRouteSearch');
    const saveButton = document.getElementById('drawerSaveButton');
    const unscheduleButton = document.getElementById('drawerUnscheduleButton');
    const splitHint = document.getElementById('drawerSplitHint');
    const splitBtn = document.getElementById('drawerSplitBtn_New');
    const releaseHint = document.getElementById('drawerReleaseHint');
    const releaseBtn = document.getElementById('drawerReleaseBtn_New');
    const drawer = document.getElementById('editTaskDrawer');
    const overlay = document.getElementById('drawerOverlay');
    const renderActiveRouteStageList = function (stageId = '') {
        const activeStageId = setCurrentDrawerActiveStage(stages, wo, stageId);
        renderDrawerRouteStageList(stages, activeStageId);
        loadDrawerMachineHistory(wo, stages);
    };

    if (routePlanner) {
        window.currentDrawerRouteSearch = '';
        window.currentDrawerRouteMachineSearch = {};
        window.currentDrawerRouteMachineModes = {};
        if (drawer) {
            drawer.classList.remove('w-96', 'right-0', 'top-0', 'h-full');
            drawer.classList.add(
                'left-3',
                'right-3',
                'top-4',
                'bottom-4',
                'h-auto',
                'w-auto',
                'rounded-3xl',
                'border',
                'border-slate-200',
                'md:left-6',
                'md:right-6',
                'md:top-6',
                'md:bottom-6'
            );
        }
        if (overlay) {
            overlay.classList.remove('bg-black/20', 'backdrop-blur-[1px]');
            overlay.classList.add('bg-slate-900/60', 'backdrop-blur-sm');
        }
        if (routeCard) routeCard.classList.remove('hidden');
        if (stageSection) stageSection.classList.add('hidden');
        if (machineSection) machineSection.classList.add('hidden');
        if (stageLabel) stageLabel.textContent = 'Route Start Stage';
        if (machineLabel) machineLabel.textContent = 'Route Machine';
        if (startLabel) startLabel.textContent = 'Route Start Time';
        if (startSection) startSection.classList.add('hidden');
        if (plannerMetaGrid) {
            plannerMetaGrid.classList.remove('space-y-4');
            plannerMetaGrid.classList.add(
                'grid',
                'gap-3',
                'items-start',
                'lg:grid-cols-[minmax(220px,1.4fr)_minmax(120px,0.7fr)_minmax(150px,0.8fr)]'
            );
        }
        if (statusContainer) statusContainer.classList.remove('hidden');
        if (statusSelect) statusSelect.value = wo?.status || 'pending';
        updateRoutePlannerSaveButtonLabel();
        if (statusSelect) {
            statusSelect.onchange = () => updateRoutePlannerSaveButtonLabel();
        }
        if (splitHint) splitHint.classList.add('hidden');
        if (splitBtn) splitBtn.classList.add('hidden');
        if (releaseHint) releaseHint.classList.add('hidden');
        if (releaseBtn) releaseBtn.classList.add('hidden');
        if (unscheduleButton) {
            const hasPublishedPlan = !!wo.start_date || !!wo.has_sub_tasks;
            unscheduleButton.classList.toggle('hidden', !hasPublishedPlan);
        }
        const syncRouteFlowMode = function () {
            const selectedMode = flowParallelRadio?.checked ? 'parallel' : 'series';
            window.currentDrawerOperationFlowMode = selectedMode;
            renderActiveRouteStageList();
        };
        if (flowSeriesRadio && flowParallelRadio) {
            const selectedMode = getCurrentDrawerOperationFlowMode();
            flowSeriesRadio.checked = selectedMode !== 'parallel';
            flowParallelRadio.checked = selectedMode === 'parallel';
            flowSeriesRadio.onchange = syncRouteFlowMode;
            flowParallelRadio.onchange = syncRouteFlowMode;
        }
        if (routeSearchInput) {
            routeSearchInput.value = '';
            routeSearchInput.oninput = function () {
                window.currentDrawerRouteSearch = String(this.value || '').trim().toLowerCase();
                renderActiveRouteStageList();
            };
        }
        renderActiveRouteStageList();
    } else {
        window.currentDrawerRouteSearch = '';
        window.currentDrawerRouteMachineSearch = {};
        window.currentDrawerRouteMachineModes = {};
        window.currentDrawerRouteStartOverrides = {};
        window.currentDrawerActiveStageId = '';
        if (drawer) {
            drawer.classList.add('w-96', 'right-0', 'top-0', 'h-full');
            drawer.classList.remove(
                'left-3',
                'right-3',
                'top-4',
                'bottom-4',
                'h-auto',
                'w-auto',
                'rounded-3xl',
                'border',
                'border-slate-200',
                'md:left-6',
                'md:right-6',
                'md:top-6',
                'md:bottom-6'
            );
        }
        if (overlay) {
            overlay.classList.add('bg-black/20', 'backdrop-blur-[1px]');
            overlay.classList.remove('bg-slate-900/60', 'backdrop-blur-sm');
        }
        if (routeCard) routeCard.classList.add('hidden');
        if (stageSection) stageSection.classList.remove('hidden');
        if (machineSection) machineSection.classList.remove('hidden');
        if (stageLabel) stageLabel.textContent = 'Production Stage';
        if (machineLabel) machineLabel.textContent = 'Assigned Machine';
        if (startLabel) startLabel.textContent = 'Start Date & Time';
        if (startSection) startSection.classList.remove('hidden');
        if (plannerMetaGrid) {
            plannerMetaGrid.classList.remove(
                'grid',
                'gap-3',
                'items-start',
                'lg:grid-cols-[minmax(220px,1.4fr)_minmax(120px,0.7fr)_minmax(150px,0.8fr)]'
            );
            plannerMetaGrid.classList.add('space-y-4');
        }
        if (statusContainer) statusContainer.classList.remove('hidden');
        if (saveButton) saveButton.textContent = 'Save Changes';
        if (unscheduleButton) unscheduleButton.classList.remove('hidden');
        if (flowSeriesRadio) {
            flowSeriesRadio.onchange = null;
            flowSeriesRadio.checked = true;
        }
        if (flowParallelRadio) {
            flowParallelRadio.onchange = null;
            flowParallelRadio.checked = false;
        }
        if (routeSearchInput) {
            routeSearchInput.value = '';
            routeSearchInput.oninput = null;
        }
    }

    applyDrawerClosedState(wo);
    return routePlanner;
}

window.openWorkOrderModal = function (woId, options = {}) {
    // 1. Open Drawer
    // 2. Fetch specific WO details (API)
    return fetch(`/manufacturing/api/work-order/${woId}/`)
        .then(res => res.json())
        .then(data => {
            if (data.success) {
                const wo = data.work_order;
                window.currentDrawerWO = wo; // Store for other actions

                const stages = data.stages || [];
                const routeStages = Array.isArray(data.route_stages) && data.route_stages.length > 0
                    ? data.route_stages
                    : stages.filter(stage => stage && stage.is_bom_stage);
                window.currentDrawerStages = stages; // Store globally for filtering
                window.currentDrawerRouteStages = routeStages;

                const drawerMachines = Array.isArray(data.machines) && data.machines.length > 0
                    ? data.machines
                    : machinesCache;
                window.currentDrawerMachines = drawerMachines;
                if (machinesCache.length === 0 && drawerMachines.length > 0) {
                    machinesCache = drawerMachines;
                }

                updateDrawerIdentity(wo);
                updateDrawerBomVersionCard(wo);
                updateDrawerCycleState(wo.cycle_state, wo.status);
                renderDrawerMaterialReadiness(wo);
                document.getElementById('drawerTaskId').value = wo.id;
                document.getElementById('drawerStatus').value = wo.status;
                window.currentDrawerOperationFlowMode = String(
                    wo.operation_flow_mode
                    || wo.company_default_operation_flow_mode
                    || 'series'
                ).trim().toLowerCase() === 'parallel' ? 'parallel' : 'series';
                window.currentDrawerRouteMachineAssignments = {};
                window.currentDrawerRouteMachineModes = {};
                window.currentDrawerRouteStartOverrides = {};
                window.currentDrawerActiveStageId = '';
                window.currentDrawerWorkOrderHistoryEntries = [];
                const initialRouteStartValue = formatDateTimeLocalValue(routeStages[0]?.planned_start_date || wo.start_date);
                routeStages.forEach((stage) => {
                    const existingMachineId = stage.assigned_machine_id || '';
                    window.currentDrawerRouteMachineAssignments[String(stage.id)] = String(existingMachineId || '');
                    window.currentDrawerRouteMachineModes[String(stage.id)] = existingMachineId ? 'manual' : 'auto';
                    const existingStageStart = String(stage.id) === String(routeStages[0]?.id || '')
                        ? initialRouteStartValue
                        : formatDateTimeLocalValue(stage.planned_start_date);
                    if (existingStageStart) {
                        window.currentDrawerRouteStartOverrides[String(stage.id)] = existingStageStart;
                    }
                });
                const requestedActiveStageId = String(options?.activeStageId || '').trim();
                if (requestedActiveStageId && routeStages.some(stage => String(stage.id) === requestedActiveStageId)) {
                    window.currentDrawerActiveStageId = requestedActiveStageId;
                }
                const routePlannerMode = setDrawerPlanningMode(wo, routeStages, options);
                applyDrawerClosedState(wo);
                updateDrawerBomVersionCard(wo);

                // ðŸ†• Populate Customer & Edit Fields
                const custName = wo.customer ? wo.customer.name : (wo.customer_name || 'Walk-in');
                document.getElementById('drawerCustomerDisplay').innerText = custName;
                document.getElementById('drawerQuantity').value = wo.quantity;
                document.getElementById('drawerPriority').value = wo.priority;

                const qtyBreakdown = document.getElementById('drawerQtyBreakdown');
                const baseQtyEl = document.getElementById('drawerBaseQty');
                const compQtyEl = document.getElementById('drawerCompQty');
                const compQty = Number(wo.scrap_compensation_qty || 0);
                const baseQty = Number(
                    wo.base_quantity !== undefined && wo.base_quantity !== null
                        ? wo.base_quantity
                        : Math.max(Number(wo.quantity || 0) - compQty, 0)
                );
                if (qtyBreakdown && baseQtyEl && compQtyEl) {
                    if (compQty > 0) {
                        baseQtyEl.textContent = String(baseQty);
                        compQtyEl.textContent = String(compQty);
                        qtyBreakdown.classList.remove('hidden');
                    } else {
                        qtyBreakdown.classList.add('hidden');
                    }
                }

                const splitContext = getCurrentDrawerSplitContext(wo, routeStages.length ? routeStages : stages);
                const remainingQty = splitContext.remainingQty;
                const hintEl = document.getElementById('drawerSplitHint');
                const remainingEl = document.getElementById('drawerRemainingQty');
                const splitBtn = document.getElementById('drawerSplitBtn_New');
                const combineSplitBtn = document.getElementById('drawerCombineSplitBtn');
                const releaseHintEl = document.getElementById('drawerReleaseHint');
                const approvedEl = document.getElementById('drawerApprovedQty');
                const releasedEl = document.getElementById('drawerReleasedQty');
                const availableEl = document.getElementById('drawerReleaseAvailable');
                const releaseBtn = document.getElementById('drawerReleaseBtn_New');
                const role = getUserRole();
                const canSplitRole = ['planner', 'admin', 'supervisor'].includes(role);
                const canReleaseRole = ['planner', 'admin', 'supervisor'].includes(role);
                const hasSplitModal = !!document.getElementById('splitModal');
                const hasReleaseModal = !!document.getElementById('releaseModal');
                window.currentDrawerRemainingQty = remainingQty;
                window.currentDrawerSplitSourceId = splitContext.sourceId || String(wo.id || '');
                window.currentDrawerSplitDisplayId = splitContext.displayId || (wo.parent_id || wo.id || '');
                window.currentDrawerSplitSourceWO = splitContext.sourceWo || wo;
                window.currentDrawerCombineSplitContext = getCurrentDrawerCombineSplitContext(wo, routeStages.length ? routeStages : stages);

                const approvedQtyOnly = Number(wo.approved_qty ?? 0);
                const producedQtyRaw = Number(wo.produced_qty ?? approvedQtyOnly);
                const producedQty = producedQtyRaw;
                const releasedQty = Number(wo.released_qty ?? 0);
                const availableRelease = Number(wo.available_release_qty ?? Math.max(producedQty - releasedQty, 0));
                const approvedQty = producedQty;
                const qcPending = !!wo.qc_pending;
                window.currentDrawerApprovedQty = producedQty;
                window.currentDrawerReleasedQty = releasedQty;
                window.currentDrawerReleaseAvailable = availableRelease;

                const currentStageId = wo.current_stage_id || wo.stage_id;
                const nextStage = getNextStageForCurrent(stages, currentStageId);
                window.currentDrawerNextStage = nextStage;

                if (hintEl && remainingEl) {
                    remainingEl.textContent = remainingQty;
                    const inactiveStatuses = ['completed', 'canceled', 'archived'];
                    const canShowSplit = !inactiveStatuses.includes(wo.status) && canSplitRole && hasSplitModal;
                    const shouldShowSplit = remainingQty > 0 && canShowSplit;
                    if (shouldShowSplit) {
                        hintEl.classList.remove('hidden');
                    } else {
                        hintEl.classList.add('hidden');
                    }
                    if (splitBtn) {
                        splitBtn.disabled = !canShowSplit || remainingQty <= 0;
                        splitBtn.classList.toggle('hidden', !canShowSplit);
                    }
                    if (combineSplitBtn) {
                        const canCombineSplit = canSplitRole && !!window.currentDrawerCombineSplitContext;
                        combineSplitBtn.disabled = !canCombineSplit;
                        combineSplitBtn.classList.toggle('hidden', !canCombineSplit);
                    }
                }
                if (releaseHintEl && approvedEl && releasedEl && availableEl) {
                    approvedEl.textContent = approvedQty;
                    releasedEl.textContent = releasedQty;
                    availableEl.textContent = availableRelease;
                    const inactiveStatuses = ['canceled', 'archived'];
                    const canShowRelease = !!nextStage && !inactiveStatuses.includes(wo.status) && canReleaseRole && hasReleaseModal;
                    const shouldShowRelease = canShowRelease;
                    const showReleaseHint = shouldShowRelease || qcPending;
                    if (showReleaseHint) {
                        releaseHintEl.classList.remove('hidden');
                    } else {
                        releaseHintEl.classList.add('hidden');
                    }
                    if (releaseBtn) {
                        releaseBtn.disabled = !canShowRelease || qcPending;
                        releaseBtn.classList.toggle('hidden', !canShowRelease);
                    }
                    const qcMsg = document.getElementById('drawerQcPendingMsg');
                    if (qcMsg) {
                        qcMsg.classList.toggle('hidden', !qcPending);
                    }
                    if (routePlannerMode) {
                        releaseHintEl.classList.add('hidden');
                        if (releaseBtn) releaseBtn.classList.add('hidden');
                    }
                }

                if (window.__openReleaseAfterDrawer) {
                    window.__openReleaseAfterDrawer = false;
                    if (availableRelease > 0 && nextStage) {
                        const materialShortageAcknowledged = confirmMaterialShortageOverride('Continue releasing to the next stage anyway?');
                        if (!materialShortageAcknowledged) {
                            showTimelineToast('Release was stopped because material shortage is still open.', 'warning');
                            return;
                        }
                        // Auto-release full available qty, then open the normal drawer
                        fetch(`/manufacturing/api/work-order/${wo.id}/release/`, {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ release_quantity: availableRelease, machine_id: null, material_shortage_acknowledged: materialShortageAcknowledged })
                        })
                            .then(res => res.json())
                            .then(data => {
                                if (data.success) {
                                    if (data.new_wo_id && window.openWorkOrderModal) {
                                        setTimeout(() => window.openWorkOrderModal(data.new_wo_id), 50);
                                    }
                                    if (window.initGanttChart) window.initGanttChart(true);
                                } else {
                                    showTimelineToast(data.error || "Failed to start next stage.", 'error');
                                }
                            })
                            .catch(err => {
                                console.error(err);
                                showTimelineToast("Release Error: " + err, 'error');
                            });
                    }
                }

                // Populate Start Date (convert ISO to datetime-local format)
                const initialRouteStart = routePlannerMode
                    ? (routeStages[0]?.planned_start_date || wo.start_date)
                    : wo.start_date;
                if (initialRouteStart) {
                    document.getElementById('drawerStartDate').value = formatDateTimeLocalValue(initialRouteStart);
                } else {
                    document.getElementById('drawerStartDate').value = '';
                }

                // Populate Stages
                const stageSelect = document.getElementById('drawerStage');
                const stageContainer = document.getElementById('drawerStageContainer');
                stageSelect.innerHTML = '<option value="">-- Select Stage --</option>';

                if (stages.length > 0) {
                    stageContainer.classList.remove('hidden');
                    stages.forEach(s => {
                        const opt = document.createElement('option');
                        opt.value = s.id;
                        opt.innerText = `${s.order}. ${s.name}`;
                        stageSelect.appendChild(opt);
                    });

                    let selectedStageId = String(wo.current_stage_id || wo.stage_id || '');
                    if (routePlannerMode) {
                        selectedStageId = setCurrentDrawerActiveStage(routeStages.length ? routeStages : stages, wo, requestedActiveStageId);
                    } else if (!selectedStageId && stages[0] && stages[0].id) {
                        selectedStageId = String(stages[0].id);
                    }
                    stageSelect.value = selectedStageId;

                    stageSelect.onchange = function () {
                        const activeStageId = this.value || selectedStageId;
                        if (window.currentDrawerRoutePlanner) {
                            selectedStageId = setCurrentDrawerActiveStage(routeStages.length ? routeStages : stages, wo, activeStageId);
                            renderDrawerRouteStageList(routeStages.length ? routeStages : stages, selectedStageId);
                            loadDrawerMachineHistory(wo, routeStages.length ? routeStages : stages);
                        } else {
                            window.currentDrawerActiveStageId = '';
                            updateDrawerAuditLinks(wo, stages);
                            loadDrawerMachineHistory(wo, stages);
                        }
                        filterMachinesByStage(this.value, '');
                    };
                } else {
                    stageContainer.classList.add('hidden');
                }

                const selectedStageMeta = stages.find(s => String(s.id) === String(stageSelect.value || ''));
                const preferredMachineId = wo.machine_id || (selectedStageMeta && selectedStageMeta.default_machine_id) || '';
                filterMachinesByStage(stageSelect.value || '', preferredMachineId);
                const machineSelect = document.getElementById('drawerMachine');
                if (machineSelect) {
                    machineSelect.onchange = function () {
                        loadDrawerMachineHistory(wo, routeStages.length ? routeStages : stages);
                    };
                }
                loadDrawerHistories(wo, routeStages.length ? routeStages : stages);

                // Show Drawer
                document.getElementById('editTaskDrawer').classList.remove('translate-x-full');
                document.getElementById('drawerOverlay').classList.remove('hidden');
                if (document.body) {
                    document.body.classList.add('work-order-drawer-open');
                }
            } else {
                showTimelineToast("API Error: " + (data.error || "Unknown"), 'error');
            }
        })
        .catch(err => {
            console.error(err);
            showTimelineToast("Script Error: " + err, 'error');
        });
};

// Keep a reference to this version so quickPlanPendingWorkOrder always
// uses the BOM Route Board version, even if edit_task_drawer.html
// overrides window.openWorkOrderModal later.
window.__timelineOpenWorkOrderModal = window.openWorkOrderModal;

window.quickPlanPendingWorkOrder = async function (woId) {
    if (!woId) return;
    // Use the timeline version of openWorkOrderModal (which shows the full BOM
    // Route Board with all stages for machine assignment).
    const fn = window.__timelineOpenWorkOrderModal || window.openWorkOrderModal;
    if (fn) fn(woId, { forceRoutePlanner: true });
};

window.openWorkOrderForRelease = function (woId, presetQty = null) {
    if (presetQty !== null && presetQty !== undefined) {
        window.__releasePresetQty = Number(presetQty);
    } else {
        window.__releasePresetQty = null;
    }
    window.__openReleaseAfterDrawer = true;
    window.openWorkOrderModal(woId);
};

// Quick start next stage: release full available qty, then open the normal edit panel
window.startNextStageNow = function (woId, availableQty) {
    const qty = Number(availableQty || 0);
    if (!woId || qty <= 0) {
        showTimelineToast("No available quantity to release.", 'warning');
        return;
    }

    fetch(`/manufacturing/api/work-order/${woId}/release/`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ release_quantity: qty, machine_id: null })
    })
        .then(res => res.json())
        .then(data => {
            if (data.success) {
                if (data.new_wo_id && window.openWorkOrderModal) {
                    setTimeout(() => {
                        window.openWorkOrderModal(data.new_wo_id);
                    }, 50);
                }
                if (window.initGanttChart) window.initGanttChart(true);
            } else {
                showTimelineToast(data.error || "Failed to start next stage.", 'error');
            }
        })
        .catch(err => {
            console.error(err);
            showTimelineToast("Release Error: " + err, 'error');
        });
};

// --- SPLIT LOGIC ---
window.openSplitModal = function () {
    if (!window.currentDrawerWO) return;

    const wo = window.currentDrawerWO;
    const splitSourceId = String(window.currentDrawerSplitSourceId || wo.id || '').trim();
    const splitDisplayId = String(window.currentDrawerSplitDisplayId || wo.parent_id || wo.id || '').trim();
    const splitSourceWO = window.currentDrawerSplitSourceWO || wo;
    const remainingQty = window.currentDrawerRemainingQty;
    const currentQty = Number.isFinite(remainingQty) ? remainingQty : wo.quantity;
    document.getElementById('splitOriginalId').value = splitSourceId;
    document.getElementById('splitOriginalIdDisplay').innerText = `#WO-${splitDisplayId}`;
    document.getElementById('splitCurrentQty').innerText = currentQty;
    document.getElementById('splitProductName').innerText = splitSourceWO.product_name || wo.product_name;
    document.getElementById('splitQtyInput').value = '';
    document.getElementById('splitQtyInput').max = Math.max(currentQty, 1);
    const splitStartInput = document.getElementById('splitStartDate');
    if (splitStartInput) {
        splitStartInput.value = formatDateTimeLocalValue(
            splitSourceWO.start_date || splitSourceWO.start || wo.start_date || wo.start || new Date().toISOString()
        );
    }
    document.getElementById('splitMaxQty').innerText = Math.max(currentQty, 0);
    document.getElementById('splitQtyDisplay').innerText = '0';
    document.getElementById('splitRemainingQty').innerText = currentQty;
    document.getElementById('splitImpactBox').classList.add('hidden');
    const confirmSplitBtn = document.getElementById('btnConfirmSplit');
    confirmSplitBtn.disabled = true;
    confirmSplitBtn.innerHTML = '<span>Confirm Split</span><i class="ph-bold ph-arrow-right"></i>';
    document.getElementById('splitQtyError').classList.add('hidden');
    const combineContext = getCurrentDrawerCombineSplitContext(
        wo,
        window.currentDrawerRouteStages || window.currentDrawerStages || []
    );
    window.currentDrawerCombineSplitContext = combineContext;
    const existingNotice = document.getElementById('splitExistingNotice');
    const existingCount = document.getElementById('splitExistingCount');
    window.currentDrawerHasActiveSplitChildren = false;
    if (existingNotice && existingCount) {
        const childCount = combineContext ? combineContext.childIds.length : 0;
        window.currentDrawerHasActiveSplitChildren = childCount > 0;
        existingCount.innerText = String(childCount);
        existingNotice.classList.toggle('hidden', childCount <= 0);
        if (childCount > 0) {
            confirmSplitBtn.disabled = true;
            confirmSplitBtn.innerHTML = '<span>Combine Split First</span>';
        }
    }

    // Populate Split Machine Select (Exclude current if needed? Or allow same?)
    // Usually split to DIFFERENT machine.
    const splitMachSelect = document.getElementById('splitMachineSelect');
    splitMachSelect.innerHTML = '<option value="">-- Select Target Machine --</option>';
    clearInlineFieldError('splitMachineSelect');
    splitMachSelect.onchange = () => {
        clearInlineFieldError('splitMachineSelect');
        if (typeof window.validateSplit === 'function') {
            window.validateSplit();
        }
    };
    const drawerMachines = getDrawerMachines();
    const requiredType = getCurrentStageRequiredType(wo, window.currentDrawerStages || []);
    let filteredMachines = drawerMachines;
    if (requiredType) {
        const matches = drawerMachines.filter(m => machineMatchesRequiredType(m, requiredType));
        if (matches.length > 0) {
            filteredMachines = matches;
        }
    }
    filteredMachines.forEach(m => {
        // Exclude current check logic if desired?
        // if (m.id == wo.machine_id) return;

        const isMaint = m.status === 'maintenance' || m.status === 'breakdown';
        const opt = document.createElement('option');
        opt.value = m.id;
        opt.innerText = formatTimelineMachineLabel(m, {
            includeType: true,
            includeStatus: isMaint,
            statusPrefix: 'Maintenance ',
        });

        if (isMaint) {
            opt.disabled = true;
            opt.classList.add('bg-red-50', 'text-red-500');
        }

        splitMachSelect.appendChild(opt);
    });

    document.getElementById('splitModal').classList.remove('hidden', 'pointer-events-none');
    document.getElementById('splitModalOverlay').classList.remove('hidden');

    // Small delay for animation
    setTimeout(() => {
        document.getElementById('splitModalContent').classList.remove('scale-95', 'opacity-0');
    }, 10);
};

window.closeSplitModal = function () {
    document.getElementById('splitModalContent').classList.add('scale-95', 'opacity-0');
    setTimeout(() => {
        document.getElementById('splitModal').classList.add('hidden', 'pointer-events-none');
        document.getElementById('splitModalOverlay').classList.add('hidden');
    }, 300);
};

window.validateSplit = function () {
    const input = document.getElementById('splitQtyInput');
    const rawQty = input.value === '' ? 0 : Number(input.value);
    const qty = Number.isFinite(rawQty) ? rawQty : 0;
    const isWholeQty = Number.isInteger(qty);
    const fallbackQty = window.currentDrawerWO ? window.currentDrawerWO.quantity : 0;
    const currentQty = Number.isFinite(window.currentDrawerRemainingQty)
        ? window.currentDrawerRemainingQty
        : fallbackQty;
    const btn = document.getElementById('btnConfirmSplit');
    const errorMsg = document.getElementById('splitQtyError');
    const display = document.getElementById('splitQtyDisplay');
    const remainingDisplay = document.getElementById('splitRemainingQty');
    const impactBox = document.getElementById('splitImpactBox');
    const machineId = document.getElementById('splitMachineSelect')?.value;

    display.innerText = qty;
    if (remainingDisplay) {
        remainingDisplay.innerText = Math.max(currentQty - qty, 0);
    }
    if (impactBox) {
        impactBox.classList.toggle('hidden', qty <= 0);
    }

    if (window.currentDrawerHasActiveSplitChildren) {
        btn.disabled = true;
        errorMsg.classList.add('hidden');
        return;
    }

    if (qty <= 0) {
        btn.disabled = true;
        errorMsg.classList.add('hidden');
        return;
    }

    if (!isWholeQty || qty > currentQty) {
        errorMsg.classList.remove('hidden');
        btn.disabled = true;
    } else {
        errorMsg.classList.add('hidden');
        btn.disabled = !machineId;
    }
};

window.combineExistingSplitFromModal = function () {
    if (typeof window.closeSplitModal === 'function') {
        window.closeSplitModal();
    }
    if (typeof window.combineCurrentSplitGroup === 'function') {
        window.combineCurrentSplitGroup();
    } else {
        showTimelineToast('Combine split is not available right now.', 'warning');
    }
};

window.confirmSplit = function () {
    const woId = document.getElementById('splitOriginalId').value;
    const input = document.getElementById('splitQtyInput');
    const qty = input.value === '' ? NaN : Number(input.value);
    const machineId = document.getElementById('splitMachineSelect').value;
    const fallbackQty = window.currentDrawerWO ? window.currentDrawerWO.quantity : 0;
    const currentQty = Number.isFinite(window.currentDrawerRemainingQty)
        ? window.currentDrawerRemainingQty
        : fallbackQty;

    if (window.currentDrawerHasActiveSplitChildren) {
        showTimelineToast('Combine the active split segment before splitting again.', 'warning');
        return;
    }

    if (!Number.isInteger(qty) || qty <= 0 || qty > currentQty) {
        if (typeof window.validateSplit === 'function') {
            window.validateSplit();
        }
        showTimelineToast("Please enter a valid whole quantity", 'warning');
        return;
    }
    input.value = String(qty);

    if (!machineId) {
        showInlineFieldError('splitMachineSelect', 'Please select a target machine.');
        showTimelineToast("Please select a target machine", 'warning');
        return;
    }
    clearInlineFieldError('splitMachineSelect');

    const btn = document.getElementById('btnConfirmSplit');
    btn.disabled = true;
    btn.innerHTML = '<i class="ph ph-spinner animate-spin"></i> Splitting...';

    fetch(`/manufacturing/api/work-order/${woId}/split/`, {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            'X-CSRFToken': getCookie('csrftoken')
        },
        body: JSON.stringify({
            split_quantity: qty,
            machine_id: machineId,
            planned_start: document.getElementById('splitStartDate')?.value
                ? new Date(document.getElementById('splitStartDate').value).toISOString()
                : '',
            setup_time: document.getElementById('splitSetupTime').value || 0,
            time_per_unit: document.getElementById('splitTimePerUnit').value || 0
        })
    })
        .then(res => res.json())
        .then(data => {
            if (data.success) {
                showTimelineToast(data.message || 'Split completed successfully.', 'success');
                window.closeSplitModal();
                window.closeEditDrawer(); // Close parent drawer too
                window.initGanttChart(true); // Refresh timeline
            } else {
                showTimelineToast("Error: " + data.error, 'error');
                btn.disabled = false;
                btn.innerHTML = '<i class="ph ph-arrows-split"></i> Confirm Split';
            }
        })
        .catch(err => {
            console.error(err);
            showTimelineToast("Network Error", 'error');
            btn.disabled = false;
            btn.innerHTML = '<i class="ph ph-arrows-split"></i> Confirm Split';
        });
};

window.combineCurrentSplitGroup = function () {
    const context = getCurrentDrawerCombineSplitContext(window.currentDrawerWO);
    if (!context) {
        showTimelineToast('No active split segment is available to combine.', 'warning');
        return;
    }

    const btn = document.getElementById('drawerCombineSplitBtn');
    combineSplitGroupWithConfirmation(context, {
        button: btn,
        reopenSourceDrawer: true,
    });
};

// --- RELEASE TO NEXT STAGE LOGIC ---
window.openReleaseModal = function () {
    if (!window.currentDrawerWO) {
        showTimelineToast("Release is unavailable right now. Reopen the work order drawer and try again.", 'warning');
        return;
    }
    const availableQty = Number(window.currentDrawerReleaseAvailable || 0);
    const nextStage = window.currentDrawerNextStage;
    if (!nextStage) {
        showTimelineToast("No next stage available.", 'warning');
        return;
    }

    const wo = window.currentDrawerWO;
    document.getElementById('releaseOriginalId').value = wo.id;
    document.getElementById('releaseNextStageName').innerText = nextStage.name || 'Next Stage';
    document.getElementById('releaseApprovedQty').innerText = window.currentDrawerApprovedQty || 0;
    document.getElementById('releaseAlreadyQty').innerText = window.currentDrawerReleasedQty || 0;
    document.getElementById('releaseAvailableQty').innerText = availableQty;

    const qtyInput = document.getElementById('releaseQtyInput');
    qtyInput.value = '';
    qtyInput.max = availableQty;
    document.getElementById('releaseQtyDisplay').innerText = '0';
    document.getElementById('btnConfirmRelease').disabled = true;
    const errEl = document.getElementById('releaseQtyError');
    if (availableQty <= 0) {
        errEl.classList.remove('hidden');
        errEl.textContent = 'No approved quantity available to release.';
    } else {
        errEl.classList.add('hidden');
        errEl.textContent = 'Quantity exceeds available output.';
    }

    const machineSelect = document.getElementById('releaseMachineSelect');
    machineSelect.innerHTML = '<option value="">-- Unassigned --</option>';
    const drawerMachines = getDrawerMachines();
    const requiredType = nextStage.machine_type || '';
    const filteredMachines = requiredType
        ? drawerMachines.filter(m => machineMatchesRequiredType(m, requiredType))
        : drawerMachines;
    filteredMachines.forEach(m => {
        const isMaint = m.status === 'maintenance' || m.status === 'breakdown';
        const opt = document.createElement('option');
        opt.value = m.id;
        opt.innerText = formatTimelineMachineLabel(m, {
            includeType: true,
            includeStatus: isMaint,
            statusPrefix: 'Maintenance ',
        });
        if (isMaint) {
            opt.disabled = true;
            opt.classList.add('bg-red-50', 'text-red-500');
        }
        machineSelect.appendChild(opt);
    });

    document.getElementById('releaseModal').classList.remove('hidden', 'pointer-events-none');
    document.getElementById('releaseModalOverlay').classList.remove('hidden');
    setTimeout(() => {
        document.getElementById('releaseModalContent').classList.remove('scale-95', 'opacity-0');
    }, 10);

    if (window.__releasePresetQty !== null && window.__releasePresetQty !== undefined) {
        const preset = Number(window.__releasePresetQty);
        if (qtyInput && preset > 0) {
            qtyInput.value = preset;
            if (typeof window.validateRelease === 'function') {
                window.validateRelease();
            }
        }
        window.__releasePresetQty = null;
    }
};

window.closeReleaseModal = function () {
    document.getElementById('releaseModalContent').classList.add('scale-95', 'opacity-0');
    setTimeout(() => {
        document.getElementById('releaseModal').classList.add('hidden', 'pointer-events-none');
        document.getElementById('releaseModalOverlay').classList.add('hidden');
    }, 300);
};

window.validateRelease = function () {
    const input = document.getElementById('releaseQtyInput');
    const qty = parseInt(input.value) || 0;
    const availableQty = Number(window.currentDrawerReleaseAvailable || 0);
    const btn = document.getElementById('btnConfirmRelease');
    const errorMsg = document.getElementById('releaseQtyError');
    const display = document.getElementById('releaseQtyDisplay');

    display.innerText = qty;

    if (qty <= 0) {
        btn.disabled = true;
        return;
    }
    if (qty > availableQty) {
        errorMsg.classList.remove('hidden');
        btn.disabled = true;
        return;
    }
    errorMsg.classList.add('hidden');
    btn.disabled = false;
};

window.confirmRelease = function () {
    const woId = document.getElementById('releaseOriginalId').value;
    const qty = document.getElementById('releaseQtyInput').value;
    const machineId = document.getElementById('releaseMachineSelect').value;

    if (!woId || !qty) return;
    const materialShortageAcknowledged = confirmMaterialShortageOverride('Continue releasing to the next stage anyway?');
    if (!materialShortageAcknowledged) {
        showTimelineToast('Release was stopped because material shortage is still open.', 'warning');
        return;
    }

    const btn = document.getElementById('btnConfirmRelease');
    btn.disabled = true;
    btn.innerHTML = '<i class="ph ph-spinner animate-spin"></i> Releasing...';

    fetch(`/manufacturing/api/work-order/${woId}/release/`, {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json'
        },
        body: JSON.stringify({
            release_quantity: qty,
            machine_id: machineId || null,
            material_shortage_acknowledged: materialShortageAcknowledged
        })
    })
        .then(res => res.json())
        .then(data => {
            if (data.success) {
                window.closeReleaseModal();
                if (data.new_wo_id && window.openWorkOrderModal) {
                    setTimeout(() => {
                        window.openWorkOrderModal(data.new_wo_id);
                    }, 50);
                }
                window.initGanttChart(true);
            } else {
                showTimelineToast(data.error || "Failed to release to next stage.", 'error');
            }
        })
        .catch(err => {
            console.error(err);
            showTimelineToast("Release Error: " + err, 'error');
        })
        .finally(() => {
            btn.innerHTML = '<i class="ph ph-arrow-right"></i> Release';
            btn.disabled = false;
        });
};


window.openEditSheet = function (taskId) {
    const role = String(getUserRole() || '').toLowerCase();
    const task = Array.isArray(tasksCache)
        ? tasksCache.find((item) => String(item?.id) === String(taskId))
        : null;
    const clickedStageId = String(
        task?.stage_id
        ?? task?.stageId
        ?? task?.current_stage_id
        ?? task?.currentStageId
        ?? ''
    ).trim();
    const isSplitChild = !!String(task?.source_task_id || '').trim();
    const targetId = ['planner', 'admin'].includes(role) && task?.parent_id && !isSplitChild
        ? task.parent_id
        : taskId;
    window.openWorkOrderModal(targetId, clickedStageId ? { activeStageId: clickedStageId } : {});
};

window.closeEditDrawer = function () {
    const drawer = document.getElementById('editTaskDrawer');
    const overlay = document.getElementById('drawerOverlay');
    if (document.body) {
        document.body.classList.remove('work-order-drawer-open');
        document.body.classList.remove('route-planner-open');
    }
    window.currentDrawerRouteSearch = '';
    window.currentDrawerRouteMachineSearch = {};
    window.currentDrawerRouteMachineModes = {};
    window.currentDrawerOperationFlowMode = 'series';
    window.currentDrawerActiveStageId = '';
    if (drawer) {
        drawer.classList.add('translate-x-full', 'w-96', 'right-0', 'top-0', 'h-full');
        drawer.classList.remove(
            'left-3',
            'right-3',
            'top-4',
            'bottom-4',
            'h-auto',
            'w-auto',
            'rounded-3xl',
            'border',
            'border-slate-200',
            'md:left-6',
            'md:right-6',
            'md:top-6',
            'md:bottom-6'
        );
    }
    window.currentDrawerRouteDragMachineId = null;
    window.currentDrawerRouteStartOverrides = {};
    window.currentDrawerHistoryToken = null;
    window.currentDrawerWorkOrderHistoryEntries = [];
    const parentHistory = document.getElementById('drawerParentHistory');
    const stageHistory = document.getElementById('drawerStageHistory');
    const stageHistoryTitle = document.getElementById('drawerStageHistoryTitle');
    const machineHistory = document.getElementById('drawerMachineHistory');
    const machineHistoryTitle = document.getElementById('drawerMachineHistoryTitle');
    const machineHistorySection = document.getElementById('drawerMachineHistorySection');
    const routeAuditLink = document.getElementById('drawerRouteAuditLink');
    const stageAuditLink = document.getElementById('drawerStageAuditLink');
    const machineAuditLink = document.getElementById('drawerMachineAuditLink');
    if (parentHistory) {
        parentHistory.innerHTML = '<div class="px-3 py-4 text-sm text-slate-400">Open a work order to load its history.</div>';
    }
    if (stageHistory) {
        stageHistory.innerHTML = '<div class="px-3 py-4 text-sm text-slate-400">Select a stage to load its stage-specific history.</div>';
    }
    if (stageHistoryTitle) {
        stageHistoryTitle.textContent = 'Selected Stage History';
    }
    if (machineHistory) {
        machineHistory.innerHTML = '<div class="px-3 py-4 text-sm text-slate-400">No machine is linked to this work order yet.</div>';
    }
    if (machineHistoryTitle) {
        machineHistoryTitle.textContent = 'Machine History';
    }
    if (machineHistorySection) {
        machineHistorySection.classList.add('opacity-70');
    }
    if (routeAuditLink) {
        routeAuditLink.href = buildDrawerAuditTrailUrl();
    }
    if (stageAuditLink) {
        stageAuditLink.href = buildDrawerAuditTrailUrl();
        stageAuditLink.classList.add('hidden');
        stageAuditLink.textContent = 'Stage Audit';
    }
    if (machineAuditLink) {
        machineAuditLink.href = buildDrawerAuditTrailUrl();
        machineAuditLink.classList.add('hidden');
    }
    if (overlay) {
        overlay.classList.add('hidden', 'bg-black/20', 'backdrop-blur-[1px]');
        overlay.classList.remove('bg-slate-900/60', 'backdrop-blur-sm');
    }
};

window.saveTaskChanges = function () {
    if (isDrawerPlannerClosed(window.currentDrawerWO)) {
        showTimelineToast('This work order is planner closed and cannot be edited.', 'warning');
        applyDrawerClosedState(window.currentDrawerWO);
        return;
    }
    const woId = document.getElementById('drawerTaskId').value;
    const machineId = document.getElementById('drawerMachine').value;
    const stageId = document.getElementById('drawerStage').value;
    const status = document.getElementById('drawerStatus').value;
    const quantity = document.getElementById('drawerQuantity').value;
    const priority = document.getElementById('drawerPriority').value;
    const startDate = document.getElementById('drawerStartDate').value;
    const saveButton = document.getElementById('drawerSaveButton');
    const terminalStatuses = ['completed', 'canceled', 'archived'];
    const isTerminalStatus = terminalStatuses.includes(String(status || '').toLowerCase());

    if (window.currentDrawerRoutePlanner) {
        const routeStages = Array.isArray(window.currentDrawerRouteStages) ? window.currentDrawerRouteStages : [];
        const firstRouteStage = routeStages[0];
        const routeStartValue = firstRouteStage ? getRouteStageStartOverride(firstRouteStage.id) : '';
        const undoAction = buildRoutePlanningUndoAction(window.currentDrawerWO, routeStages);
        const routeBlockers = getRoutePlannerBlockingIssues(routeStages);

        if (!isTerminalStatus && !routeStartValue) {
            showTimelineToast("Select the first stage start time.", 'warning');
            return;
        }
        if (!isTerminalStatus && (!firstRouteStage || !firstRouteStage.id)) {
            showTimelineToast("No route stages found for this work order.", 'error');
            return;
        }
        if (!isTerminalStatus && routeBlockers.length > 0) {
            updateRoutePlannerSaveButtonLabel();
            const firstBlocker = routeBlockers[0];
            showTimelineToast(`${firstBlocker.stage?.name || 'Stage'}: ${firstBlocker.readiness.reason}`, 'warning');
            return;
        }

        const operationFlowMode = getCurrentDrawerOperationFlowMode();
        const routeAssignments = routeStages.map(stage => {
            const stageId = String(stage.id);
            const stageStartOverride = getRouteStageStartOverride(stageId);
            return {
                stage_id: stage.id,
                machine_id: String(window.currentDrawerRouteMachineAssignments[stageId] || ''),
                selection_mode: getRouteStageSelectionMode(stageId),
                start_date: stageStartOverride ? new Date(stageStartOverride).toISOString() : ''
            };
        });

        const firstStageAssignment = routeAssignments.find(item => String(item.stage_id) === String(firstRouteStage?.id || ''));
        if (!isTerminalStatus && (!firstStageAssignment || !firstStageAssignment.machine_id)) {
            const firstStageLabel = String(firstRouteStage.name || 'the first stage');
            showTimelineToast(`Assign a machine to ${firstStageLabel} before publishing this work order.`, 'warning');
            return;
        }
        const materialPlanningBlocker = !isTerminalStatus ? getMaterialPlanningBlocker(window.currentDrawerWO) : '';
        if (materialPlanningBlocker) {
            showTimelineToast(materialPlanningBlocker, 'warning');
            return;
        }

        if (saveButton) {
            saveButton.disabled = true;
            saveButton.textContent = isTerminalStatus ? 'Updating...' : 'Planning...';
        }

        fetch(`/manufacturing/api/schedule-work-order/${woId}/`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': getCookie('csrftoken')
            },
            body: JSON.stringify({
                stage_id: firstRouteStage?.id || '',
                machine_id: firstStageAssignment?.machine_id || '',
                start_date: routeStartValue ? new Date(routeStartValue).toISOString() : '',
                status,
                quantity,
                operation_flow_mode: operationFlowMode,
                route_assignments: routeAssignments,
                material_shortage_acknowledged: false
            })
        })
            .then(async (res) => {
                const rawText = await res.text();
                let data = {};
                try {
                    data = rawText ? JSON.parse(rawText) : {};
                } catch (parseError) {
                    throw new Error(rawText || `Unexpected server response (${res.status})`);
                }
                if (!res.ok) {
                    throw new Error(data.error || data.message || `Unable to plan route (${res.status})`);
                }
                return data;
            })
            .then(data => {
                if (data.success) {
                    if (!isTerminalStatus) {
                        attachRoutePlanUndoDeletes(undoAction, data.tasks || []);
                        recordTimelineUndoAction(undoAction);
                    }
                    window.closeEditDrawer();
                    window.initGanttChart(true);
                    showTimelineToast(
                        data.message || (isTerminalStatus ? 'Work order status updated.' : 'Route planned successfully.'),
                        'success'
                    );
                } else {
                    showTimelineToast("Error: " + data.error, 'error');
                }
            })
            .catch(err => {
                console.error(err);
                showTimelineToast(err?.message || "Unable to plan route. Check connection and retry.", 'error');
            })
            .finally(() => {
                if (saveButton) {
                    if (isDrawerPlannerClosed(window.currentDrawerWO)) {
                        applyDrawerClosedState(window.currentDrawerWO);
                    } else {
                        saveButton.disabled = false;
                        saveButton.textContent = isTerminalStatus ? 'Apply Status' : 'Plan Full Route';
                    }
                }
            });
        return;
    }

    const undoAction = buildDrawerUndoAction(window.currentDrawerWO, `Undo changes for ${getDisplayWorkOrderHashLabel(window.currentDrawerWO || { id: woId })}`);

    // Validate: At least one field should be changed
    if (!machineId && !status && !quantity && !priority && !startDate && !stageId) {
        showTimelineToast("Please make at least one change before saving.", 'warning');
        return;
    }
    let materialShortageAcknowledged = false;
    const materialPlanningBlocker = (machineId || stageId || startDate) ? getMaterialPlanningBlocker(window.currentDrawerWO) : '';
    if (materialPlanningBlocker) {
        showTimelineToast(materialPlanningBlocker, 'warning');
        return;
    }

    const formData = new FormData();
    formData.append('wo_id', woId);
    if (machineId) formData.append('machine_id', machineId);
    if (stageId) formData.append('stage_id', stageId);
    if (status) formData.append('status', status);
    if (quantity) formData.append('quantity', quantity);
    if (priority) formData.append('priority', priority);
    if (startDate) formData.append('start_date', startDate);
    if (materialShortageAcknowledged) formData.append('material_shortage_acknowledged', '1');

    fetch('/manufacturing/api/assign-wo/', {
        method: 'POST',
        body: formData
    })
        .then(res => res.json())
        .then(data => {
            if (data.success) {
                recordTimelineUndoAction(undoAction);
                // alert("Assigned successfully!");
                window.closeEditDrawer();
                // Refresh Timeline to show new task
                window.initGanttChart(true);
                showTimelineToast(data.message || 'Work order updated.', 'success');
            } else {
                showTimelineToast("Error: " + data.error, 'error');
            }
        })
        .catch(err => {
            console.error(err);
            showTimelineToast("Unable to save changes. Check connection and retry.", 'error');
        });
};

window.deleteTaskFromSchedule = async function () {
    const id = document.getElementById('drawerTaskId').value;
    const undoAction = buildDrawerUndoAction(window.currentDrawerWO, `Undo unschedule for ${getDisplayWorkOrderHashLabel(window.currentDrawerWO || { id })}`);
    const confirmed = await confirmTimelineAction(
        "Are you sure you want to remove this task from the schedule? It will move back to Pending.",
        { title: 'Unschedule Work Order', confirmText: 'Unschedule', kind: 'warning' }
    );
    if (!confirmed) return;

    fetch(`/manufacturing/api/workorder/${id}/unschedule/`, {
        method: 'POST',
        headers: {
            'X-CSRFToken': getCookie('csrftoken'),
        },
    })
        .then(res => res.json())
        .then(data => {
            if (!data.success) {
                showTimelineToast(data.error || 'Unable to unschedule work order.', 'error');
                return;
            }
            recordTimelineUndoAction(undoAction);
            closeEditDrawer();
            window.initGanttChart(true);
            showTimelineToast(data.message || 'Work order removed from schedule.', 'success');
        })
        .catch(err => {
            console.error(err);
            showTimelineToast(err?.message || 'Unable to unschedule work order.', 'error');
        });
};


function parseTimelineDragPayload(e) {
    let payload = null;
    try {
        const raw = e?.dataTransfer?.getData('text/plain');
        if (raw) payload = JSON.parse(raw);
    } catch (err) {
        payload = null;
    }
    if (!payload) payload = window.currentTimelineDragPayload || null;
    if (!payload || payload.id === undefined || payload.id === null || payload.id === '') return null;
    if (!payload.dragType) payload.dragType = 'queue-item';
    return payload;
}

function getDropDateFromPointer(rowElem, context, clientX) {
    const rect = rowElem.getBoundingClientRect();
    const machineColumnWidth = Number(context?.machineColumnWidth ?? timelineState.layout?.machineColumnWidth ?? 200);
    const timeAreaWidth = Math.max(
        Number(context?.timeAreaWidth ?? (rect.width - machineColumnWidth)) || 0,
        1
    );
    let offsetX = clientX - rect.left - machineColumnWidth;
    if (!Number.isFinite(offsetX)) offsetX = 0;
    const clampedOffset = Math.max(0, Math.min(offsetX, timeAreaWidth - 1));
    const timeScale = context?.timeScale;
    let dropDate = null;
    if (timeScale && typeof timeScale.getDateFromOffsetPx === 'function') {
        dropDate = timeScale.getDateFromOffsetPx(clampedOffset);
    } else {
        const ratio = clampedOffset / timeAreaWidth;
        const fallbackWindowDuration = Math.max(
            (timelineState.endDate?.getTime?.() || 0) - (timelineState.startDate?.getTime?.() || 0),
            1
        );
        const windowStartMs = Number(context?.windowStartMs ?? timelineState.startDate.getTime()) || timelineState.startDate.getTime();
        const windowDurationMs = Math.max(Number(context?.windowDurationMs ?? fallbackWindowDuration) || 0, 1);
        const droppedMs = windowStartMs + (ratio * windowDurationMs);
        dropDate = new Date(droppedMs);
    }
    if (Number.isNaN(dropDate.getTime())) return null;
    dropDate.setSeconds(0, 0);
    return dropDate;
}

function getDragTaskDurationMs(task, payload) {
    const payloadDuration = Number(payload?.duration_ms || 0);
    if (Number.isFinite(payloadDuration) && payloadDuration > 0) {
        return Math.max(payloadDuration, 60000);
    }
    const estimatedMinutes = Number(task?.estimated_duration_minutes || 0);
    if (Number.isFinite(estimatedMinutes) && estimatedMinutes > 0) {
        return Math.max(estimatedMinutes * 60000, 60000);
    }
    if (task?.start && task?.end) {
        const startMs = new Date(task.start).getTime();
        const endMs = new Date(task.end).getTime();
        if (Number.isFinite(startMs) && Number.isFinite(endMs) && endMs > startMs) {
            return Math.max(endMs - startMs, 60000);
        }
    }
    return 60 * 60 * 1000;
}

function ensureTimelineToastContainer() {
    let host = document.getElementById('timelineToastHost');
    if (host) return host;
    host = document.createElement('div');
    host.id = 'timelineToastHost';
    host.style.position = 'fixed';
    host.style.right = '18px';
    host.style.bottom = '18px';
    host.style.zIndex = '10050';
    host.style.display = 'flex';
    host.style.flexDirection = 'column';
    host.style.gap = '8px';
    host.style.pointerEvents = 'none';
    document.body.appendChild(host);
    return host;
}

function showTimelineToast(message, variant = 'info') {
    if (typeof window.appNotify === 'function') {
        window.appNotify(message, variant);
        return;
    }
    if (window.Toast && typeof window.Toast.show === 'function') {
        const toastVariant = variant === 'warning' ? 'info' : variant;
        window.Toast.show(String(message || 'Updated'), toastVariant);
        return;
    }

    const host = ensureTimelineToastContainer();
    const toast = document.createElement('div');
    toast.className = 'rounded-xl border px-4 py-2 text-sm font-semibold shadow-lg';
    toast.style.pointerEvents = 'auto';
    toast.style.background = '#ffffff';
    toast.style.color = '#0f172a';
    toast.style.borderColor = '#cbd5e1';

    if (variant === 'success') {
        toast.style.background = '#ecfdf5';
        toast.style.color = '#065f46';
        toast.style.borderColor = '#6ee7b7';
    } else if (variant === 'error') {
        toast.style.background = '#fef2f2';
        toast.style.color = '#991b1b';
        toast.style.borderColor = '#fca5a5';
    } else if (variant === 'warning') {
        toast.style.background = '#fffbeb';
        toast.style.color = '#92400e';
        toast.style.borderColor = '#fcd34d';
    }

    toast.textContent = String(message || 'Updated');
    host.appendChild(toast);
    setTimeout(() => {
        toast.style.opacity = '0';
        toast.style.transform = 'translateY(4px)';
        toast.style.transition = 'all 140ms ease';
        setTimeout(() => toast.remove(), 160);
    }, 1800);
}

function confirmTimelineAction(message, options = {}) {
    if (typeof window.appConfirm === 'function') {
        return window.appConfirm(message, options);
    }
    return Promise.resolve(window.confirm(message));
}

function getOrCreateInlineFieldError(fieldId) {
    const field = document.getElementById(fieldId);
    if (!field || !field.parentElement) return null;
    const errorId = `${fieldId}InlineError`;
    let errorEl = document.getElementById(errorId);
    if (!errorEl) {
        errorEl = document.createElement('div');
        errorEl.id = errorId;
        errorEl.className = 'mt-1 text-xs font-semibold text-rose-600 hidden';
        field.parentElement.appendChild(errorEl);
    }
    return { field, errorEl };
}

function showInlineFieldError(fieldId, message) {
    const refs = getOrCreateInlineFieldError(fieldId);
    if (!refs) return;
    refs.field.classList.add('border-rose-400', 'ring-1', 'ring-rose-200');
    refs.errorEl.textContent = String(message || 'Invalid value');
    refs.errorEl.classList.remove('hidden');
}

function clearInlineFieldError(fieldId) {
    const refs = getOrCreateInlineFieldError(fieldId);
    if (!refs) return;
    refs.field.classList.remove('border-rose-400', 'ring-1', 'ring-rose-200');
    refs.errorEl.classList.add('hidden');
}

function setTaskSavingState(taskId, isSaving) {
    const key = String(taskId || '');
    if (!key) return;
    const elements = document.querySelectorAll(`.timeline-task[data-task-id="${key}"]`);
    elements.forEach((el) => {
        if (isSaving) {
            el.classList.add('timeline-task-saving');
            el.setAttribute('data-saving', '1');
        } else {
            el.classList.remove('timeline-task-saving');
            el.removeAttribute('data-saving');
        }
    });
}

function getTimelineDragRequiredTypes(payload, existingTask = null) {
    if (!payload) return [];
    if (payload.dragType === 'queue-item') {
        return Array.isArray(window.currentlyDraggingAllowedTypes)
            ? window.currentlyDraggingAllowedTypes.filter(Boolean)
            : [];
    }

    const explicitRequiredType = String(
        payload.required_type
        || existingTask?.required_type
        || getCurrentStageRequiredType(existingTask || payload, stagesCache)
        || ''
    ).trim();

    return explicitRequiredType ? [explicitRequiredType] : [];
}

function parseTaskDateMs(value) {
    if (!value) return NaN;
    const ms = new Date(value).getTime();
    return Number.isFinite(ms) ? ms : NaN;
}

function rangesOverlap(startA, endA, startB, endB) {
    if (!Number.isFinite(startA) || !Number.isFinite(endA) || !Number.isFinite(startB) || !Number.isFinite(endB)) return false;
    if (endA <= startA || endB <= startB) return false;
    return startA < endB && endA > startB;
}

function findTimelinePlacementConflict(payload, options = {}) {
    const isStageRow = !!options.isStageRow;
    const targetMachineId = options.targetMachineId;
    const targetStageId = options.targetStageId;
    const startMs = Number(options.startMs);
    const endMs = Number(options.endMs);

    if (!Number.isFinite(startMs) || !Number.isFinite(endMs) || endMs <= startMs) return null;

    if (!isStageRow) {
        const machineKey = String(targetMachineId ?? '');
        if (!machineKey || machineKey === 'unassigned') return null;
    } else if (!String(targetStageId ?? '').trim()) {
        return null;
    }

    const sourceIds = new Set(
        (Array.isArray(payload?.source_ids) ? payload.source_ids : [payload?.id])
            .filter((v) => v !== undefined && v !== null && String(v) !== '')
            .map((v) => String(v))
    );

    const laneKey = isStageRow
        ? String(targetStageId ?? '')
        : String(targetMachineId ?? '');

    const tasks = Array.isArray(tasksCache) ? tasksCache : [];
    for (const task of tasks) {
        if (!task) continue;
        const taskId = String(task.id ?? '');
        if (sourceIds.has(taskId)) continue;

        const status = String(task.status || '').toLowerCase();
        if (['completed', 'done', 'canceled', 'archived'].includes(status)) continue;

        const taskStart = parseTaskDateMs(task.start);
        const taskEnd = parseTaskDateMs(task.end);
        if (!Number.isFinite(taskStart) || !Number.isFinite(taskEnd) || taskEnd <= taskStart) continue;

        const taskLane = isStageRow
            ? String(task.stage_id ?? '')
            : String(task.machine_id ?? '');
        if (taskLane !== laneKey) continue;

        if (rangesOverlap(startMs, endMs, taskStart, taskEnd)) {
            return task;
        }
    }
    return null;
}

function clearDropPreview(rowElem) {
    if (!rowElem) return;
    rowElem.classList.remove('bg-blue-50', 'cursor-not-allowed', 'timeline-drop-conflict');
    if (rowElem.dataset) delete rowElem.dataset.dropConflict;
    if (rowElem.dataset) delete rowElem.dataset.dropConflictMsg;
    rowElem.removeAttribute('title');
}

function setDropConflictPreview(rowElem, conflictTask) {
    if (!rowElem) return;
    if (conflictTask) {
        rowElem.classList.add('timeline-drop-conflict');
        rowElem.dataset.dropConflict = '1';
        const msg = `Conflicts with ${getDisplayWorkOrderHashLabel(conflictTask)}`;
        rowElem.dataset.dropConflictMsg = msg;
        rowElem.title = msg;
    } else {
        rowElem.classList.remove('timeline-drop-conflict');
        if (rowElem.dataset) delete rowElem.dataset.dropConflict;
        if (rowElem.dataset) delete rowElem.dataset.dropConflictMsg;
        rowElem.removeAttribute('title');
    }
}

function setDropInvalidPreview(rowElem, message) {
    if (!rowElem) return;
    const msg = String(message || 'Invalid drop target');
    rowElem.classList.add('cursor-not-allowed', 'timeline-drop-conflict');
    if (rowElem.dataset) {
        rowElem.dataset.dropConflict = '1';
        rowElem.dataset.dropConflictMsg = msg;
    }
    rowElem.title = msg;
}

window.applyTimelineSnapAlignment = function () {
    const snapButton = document.getElementById('timelineSnapToggle');
    const snapMinutes = getTimelineSnapMinutes();
    if (!snapMinutes || !document.getElementById('customTimeline')) {
        showTimelineToast('Open the planner timeline and enable snap first.', 'warning');
        return Promise.resolve(false);
    }

    if (snapButton) {
        snapButton.disabled = true;
        snapButton.classList.add('opacity-60', 'cursor-wait');
    }

    return fetch('/manufacturing/api/timeline/snap/', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            'X-CSRFToken': getCookie('csrftoken')
        },
        body: JSON.stringify({ snap_minutes: snapMinutes })
    })
        .then(async (res) => {
            const rawText = await res.text();
            let data = {};
            try {
                data = rawText ? JSON.parse(rawText) : {};
            } catch (err) {
                throw new Error(rawText || `Snap failed (${res.status})`);
            }
            if (!res.ok || !data.success) {
                throw new Error(data.error || data.message || `Snap failed (${res.status})`);
            }
            return data;
        })
        .then((data) => {
            window.initGanttChart(true);
            const changedCount = Number(data.changed_count || 0);
            if (changedCount > 0) {
                showTimelineToast(data.message || `Snapped ${changedCount} work order(s).`, 'success');
            } else {
                showTimelineToast('All visible planned work orders are already aligned.', 'info');
            }
            return true;
        })
        .catch((err) => {
            console.error(err);
            showTimelineToast(err?.message || 'Unable to snap the current schedule.', 'error');
            return false;
        })
        .finally(() => {
            if (snapButton) {
                snapButton.disabled = false;
                snapButton.classList.remove('opacity-60', 'cursor-wait');
            }
        });
};

function getTimelineSnapMinutes() {
    if (!timelineSnapEnabled) return 0;
    const override = normalizeSnapOverride(timelineSnapMinutesOverride);
    if (override !== 'auto') return Number(override);
    if (timelineState.viewMode === 'week') return 30;
    if (timelineState.viewMode === 'month') return 60;
    return 5;
}

function snapDateToTimelineGrid(date) {
    if (!(date instanceof Date) || Number.isNaN(date.getTime())) return null;
    if (!timelineSnapEnabled) return new Date(date.getTime());
    const snapped = new Date(date.getTime());
    const snapMinutes = getTimelineSnapMinutes();
    if (!Number.isFinite(snapMinutes) || snapMinutes <= 0) return snapped;
    const minute = snapped.getMinutes();
    const roundedMinute = Math.round(minute / snapMinutes) * snapMinutes;
    snapped.setMinutes(roundedMinute, 0, 0);
    return snapped;
}

function isTimelineStartBeforeNow(dateValue) {
    const candidateMs = dateValue instanceof Date ? dateValue.getTime() : new Date(dateValue).getTime();
    if (!Number.isFinite(candidateMs)) return false;
    return candidateMs < Date.now();
}

function normalizeTimelineDropKey(value) {
    return value === undefined || value === null ? '' : String(value).trim();
}

function getSplitCombineDropContext(payload, existingTask, targetMachineId, targetStageId, dropDate, endDate) {
    if (!payload || payload.dragType !== 'timeline-task') return null;

    const sourceId = normalizeTimelineDropKey(existingTask?.source_task_id || payload.source_task_id);
    const draggedId = normalizeTimelineDropKey(existingTask?.id || payload.id);
    if (!sourceId || !draggedId || sourceId === draggedId) return null;

    const sourceTask = (tasksCache || []).find(task => normalizeTimelineDropKey(task?.id) === sourceId);
    if (!sourceTask) return null;

    const terminalStatuses = ['completed', 'done', 'canceled', 'archived'];
    const childStatus = String(existingTask?.status || payload.status || '').toLowerCase();
    const sourceStatus = String(sourceTask.status || '').toLowerCase();
    if (terminalStatuses.includes(childStatus) || terminalStatuses.includes(sourceStatus)) return null;

    const targetMachineKey = normalizeTimelineDropKey(targetMachineId);
    const targetStageKey = normalizeTimelineDropKey(targetStageId);
    const sourceMachineKey = normalizeTimelineDropKey(sourceTask.machine_id);
    const sourceStageKey = normalizeTimelineDropKey(sourceTask.stage_id);
    if (targetMachineKey !== sourceMachineKey || targetStageKey !== sourceStageKey) return null;

    const activeChildIds = getActiveSplitChildrenForSource(sourceId)
        .map(task => normalizeTimelineDropKey(task?.id))
        .filter(Boolean);
    const childIds = Array.from(new Set([draggedId, ...activeChildIds])).filter(id => id !== sourceId);
    if (!childIds.length) return null;

    return {
        sourceId,
        childIds,
        workOrderIds: [sourceId, ...childIds],
    };
}

async function combineSplitGroupWithConfirmation(context, options = {}) {
    if (!context || !context.sourceId || !Array.isArray(context.childIds) || context.childIds.length === 0) {
        showTimelineToast('No active split segment is available to combine.', 'warning');
        return false;
    }

    const confirmed = window.confirm(
        options.message
        || `Combine ${context.childIds.length} split segment${context.childIds.length === 1 ? '' : 's'} back into WO #${context.sourceId}?`
    );
    if (!confirmed) return false;

    const btn = options.button || null;
    const previousButtonHtml = btn ? btn.innerHTML : '';
    if (btn) {
        btn.disabled = true;
        btn.innerHTML = '<i class="ph ph-spinner animate-spin"></i> Combining...';
    }
    (options.savingIds || []).forEach(id => setTaskSavingState(id, true));

    try {
        const response = await fetch('/manufacturing/api/work-order/combine/', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': getCookie('csrftoken')
            },
            body: JSON.stringify({
                source_wo_id: context.sourceId,
                work_order_ids: context.workOrderIds || [context.sourceId, ...context.childIds],
                target_wo_id: context.sourceId
            })
        });
        const data = await response.json();
        if (!response.ok || !data.success) {
            throw new Error(data.error || 'Failed to combine split work orders.');
        }
        showTimelineToast(data.message || 'Split segments combined successfully.', 'success');
        window.currentDrawerCombineSplitContext = null;
        window.currentDrawerHasActiveSplitChildren = false;
        if (Array.isArray(window.currentDrawerRouteStages)) {
            window.currentDrawerRouteStages = window.currentDrawerRouteStages.map((stage) => {
                if (String(stage?.planned_task_id || '') !== String(context.sourceId)) return stage;
                return {
                    ...stage,
                    split_child_ids: [],
                    split_child_count: 0,
                };
            });
        }
        if (window.initGanttChart) {
            await window.initGanttChart(true);
        }
        const drawerTaskId = document.getElementById('drawerTaskId')?.value;
        if ((options.reopenSourceDrawer || (options.refreshOpenDrawer && drawerTaskId)) && window.openWorkOrderModal) {
            await window.openWorkOrderModal(data.target_wo_id || context.sourceId);
        }
        return true;
    } catch (err) {
        console.error(err);
        showTimelineToast(err.message || 'Combine split failed.', 'error');
        if (btn) {
            btn.disabled = false;
            btn.innerHTML = previousButtonHtml || 'Combine Split';
        }
        return false;
    } finally {
        (options.savingIds || []).forEach(id => setTaskSavingState(id, false));
        if (typeof window.handleDragEnd === 'function') {
            window.handleDragEnd();
        }
    }
}

function handleRowDrop(e, context, rowElem) {
    e.preventDefault();
    clearDropPreview(rowElem);

    if (typeof window.isTimelineEditEnabled === 'function' && !window.isTimelineEditEnabled()) {
        showTimelineToast('Enable edit mode to move work orders', 'warning');
        return;
    }

    const payload = parseTimelineDragPayload(e);
    if (!payload) return;

    const rowData = context?.rowData || {};
    const dragType = payload.dragType || 'queue-item';
    const isTimelineTask = dragType === 'timeline-task';

    if (context?.isMaintenance) return;
    if (context?.isStageRow && !isTimelineTask) return;

    const existingTask = (tasksCache || []).find(t => String(t.id) === String(payload.id));
    const existingStatus = String(existingTask?.status || payload.status || '').toLowerCase();
    if (isTimelineTask && ['completed', 'done', 'canceled', 'archived'].includes(existingStatus)) {
        showTimelineToast('Completed or archived work orders cannot be moved', 'warning');
        return;
    }

    const dropDate = getDropDateFromPointer(rowElem, context, e.clientX);
    if (!dropDate) return;

    const durationMs = getDragTaskDurationMs(existingTask, payload);
    const endDate = new Date(dropDate.getTime() + durationMs);

    const targetMachineId = context?.isStageRow
        ? (
            existingTask?.machine_id
            ?? payload.machine_id
            ?? rowData.defaultMachineId
            ?? null
        )
        : (rowData.id === 'unassigned' ? null : rowData.id);

    const targetStageId = context?.isStageRow
        ? (rowData.stageId || existingTask?.stage_id || payload.stage_id || null)
        : (existingTask?.stage_id || payload.stage_id || null);

    const targetStageName = context?.isStageRow
        ? (rowData.stageName || existingTask?.stage_name || payload.stage_name || '')
        : (existingTask?.stage_name || payload.stage_name || '');

    const targetStatus = isTimelineTask
        ? (existingTask?.status || payload.status || 'pending')
        : 'in_progress';

    const splitCombineContext = getSplitCombineDropContext(
        payload,
        existingTask,
        targetMachineId,
        targetStageId,
        dropDate,
        endDate
    );
    if (splitCombineContext) {
        combineSplitGroupWithConfirmation(splitCombineContext, {
            savingIds: splitCombineContext.childIds,
            refreshOpenDrawer: true,
            message: `Combine split WO #${payload.id} back into WO #${splitCombineContext.sourceId}? This will restore one work order again.`,
        });
        return;
    }

    if (isTimelineStartBeforeNow(dropDate)) {
        showTimelineToast('Cannot move a work order to a time before now', 'warning');
        return;
    }

    const conflictTask = findTimelinePlacementConflict(payload, {
        isStageRow: !!context?.isStageRow,
        targetMachineId,
        targetStageId,
        startMs: dropDate.getTime(),
        endMs: endDate.getTime(),
    });
    if (conflictTask) {
        showTimelineToast(`Scheduling conflict with ${getDisplayWorkOrderHashLabel(conflictTask)}`, 'warning');
        return;
    }

    const previousSnapshot = existingTask
        ? {
            start: existingTask.start,
            end: existingTask.end,
            machine_id: existingTask.machine_id,
            stage_id: existingTask.stage_id,
            stage_name: existingTask.stage_name,
            status: existingTask.status,
        }
        : null;
    const undoAction = existingTask
        ? buildTimelineTaskUndoAction(existingTask, `Undo move for ${getDisplayWorkOrderHashLabel(existingTask || payload)}`)
        : buildTimelineQueueScheduleUndoAction(payload);

    let optimisticTask = null;
    if (existingTask) {
        existingTask.start = dropDate.toISOString();
        existingTask.end = endDate.toISOString();
        existingTask.machine_id = targetMachineId === '' ? null : targetMachineId;
        existingTask.stage_id = targetStageId || existingTask.stage_id || null;
        if (targetStageName) existingTask.stage_name = targetStageName;
        if (targetStatus) existingTask.status = targetStatus;
    } else {
        optimisticTask = {
            id: payload.id,
            machine_id: targetMachineId,
            stage_id: targetStageId || null,
            stage_name: targetStageName || null,
            product: payload.content || getDisplayWorkOrderCode(payload),
            start: dropDate.toISOString(),
            end: endDate.toISOString(),
            status: targetStatus || 'in_progress',
            progress: 0
        };
        tasksCache.push(optimisticTask);
    }
    renderTimeline();
    setTaskSavingState(payload.id, true);

    const rollback = () => {
        if (previousSnapshot && existingTask) {
            Object.assign(existingTask, previousSnapshot);
        } else if (optimisticTask) {
            const idx = tasksCache.indexOf(optimisticTask);
            if (idx > -1) tasksCache.splice(idx, 1);
        }
        renderTimeline();
    };

    const formData = new FormData();
    formData.append('id', payload.id);
    if (targetMachineId !== undefined && targetMachineId !== null && String(targetMachineId) !== '') {
        formData.append('machine_id', String(targetMachineId));
    }
    if (targetStageId !== undefined && targetStageId !== null && String(targetStageId) !== '') {
        formData.append('stage_id', String(targetStageId));
    }
    formData.append('status', targetStatus || 'pending');
    formData.append('start_date', dropDate.toISOString());
    formData.append('end_date', endDate.toISOString());

    fetch('/manufacturing/api/work-order/' + payload.id + '/update/', {
        method: 'POST',
        body: formData
    })
        .then(res => res.json())
        .then(resp => {
            if (!resp.success) {
                rollback();
                showTimelineToast(resp.error || 'Unable to update work order', 'error');
                return;
            }
            recordTimelineUndoAction(undoAction);
            showTimelineToast('Schedule saved', 'success');
            window.initGanttChart(true);
        })
        .catch((err) => {
            rollback();
            showTimelineToast(err?.message || 'Network error while saving', 'error');
        })
        .finally(() => {
            setTaskSavingState(payload.id, false);
            if (typeof window.handleDragEnd === 'function') {
                window.handleDragEnd();
            }
        });
}

// Auto-Init on Load
document.addEventListener('DOMContentLoaded', () => {
    updateTimelineUndoButtonState();
    if (typeof window.initGanttChart === 'function') {
        window.initGanttChart();
    }
});

let timelineResizeRaf = null;
window.addEventListener('resize', () => {
    if (timelineResizeRaf) {
        window.cancelAnimationFrame(timelineResizeRaf);
    }
    timelineResizeRaf = window.requestAnimationFrame(() => {
        timelineResizeRaf = null;
        if (document.getElementById('customTimeline')) {
            if (typeof window.handleTimelineViewportChange === 'function') {
                window.handleTimelineViewportChange();
            } else {
                renderTimeline();
            }
        }
    });
});


function filterMachinesByStage(stageId, preferredMachineId = '') {
    const machSelect = document.getElementById('drawerMachine');
    if (!machSelect) return;

    const availableMachines = getDrawerMachines();
    const stages = Array.isArray(window.currentDrawerStages) ? window.currentDrawerStages : [];
    const stage = stages.find(s => String(s.id) === String(stageId || ''));
    const requiredType = String((stage && stage.machine_type) || '').toLowerCase().trim();

    let filteredMachines = Array.isArray(stage?.candidate_machines) && stage.candidate_machines.length > 0
        ? sortMachinesForTimeline(stage.candidate_machines)
        : [];
    if (!filteredMachines.length && requiredType) {
        filteredMachines = sortMachinesForTimeline(
            availableMachines.filter(machine => machineMatchesRequiredType(machine, requiredType))
        );
    }
    if (!filteredMachines.length && !requiredType) {
        filteredMachines = [];
    }

    machSelect.innerHTML = '<option value="">-- Select Machine --</option>';
    filteredMachines.forEach(machine => {
        const isMaint = machine.status === 'maintenance' || machine.status === 'breakdown';
        const opt = document.createElement('option');
        opt.value = machine.id;
        opt.innerText = formatTimelineMachineLabel(machine, {
            includeType: true,
            includeStatus: isMaint,
            statusPrefix: 'Maintenance ',
        });
        if (isMaint) {
            opt.disabled = true;
            opt.classList.add('bg-red-50', 'text-red-500');
        }
        machSelect.appendChild(opt);
    });

    const stageDefaultMachineId = (stage && stage.default_machine_id) || '';
    const nextMachineId = String(preferredMachineId || stageDefaultMachineId || '');
    if (nextMachineId && Array.from(machSelect.options).some(opt => String(opt.value) === nextMachineId && !opt.disabled)) {
        machSelect.value = nextMachineId;
    } else {
        machSelect.value = "";
    }
}


// --- Helper: CSRF Token ---
function getCookie(name) {
    if (name === 'csrftoken') {
        const meta = document.querySelector('meta[name="csrf-token"]');
        if (meta && meta.content && meta.content !== 'NOTPROVIDED') {
            return meta.content;
        }
        const input = document.querySelector('input[name="csrfmiddlewaretoken"]');
        if (input && input.value) {
            return input.value;
        }
        if (typeof window.getCsrfToken === 'function') {
            const fallback = window.getCsrfToken();
            if (fallback) return fallback;
        }
    }
    let cookieValue = null;
    if (document.cookie && document.cookie !== '') {
        const cookies = document.cookie.split(';');
        for (let i = 0; i < cookies.length; i++) {
            const cookie = cookies[i].trim();
            // Does this cookie string begin with the name we want?
            if (cookie.substring(0, name.length + 1) === (name + '=')) {
                cookieValue = decodeURIComponent(cookie.substring(name.length + 1));
                break;
            }
        }
    }
    return cookieValue;
}

function formatTimelineMachineLabel(machine, options = {}) {
    const explicitDisplay = String(machine?.display_name || '').trim();
    if (explicitDisplay) {
        const status = String(machine?.status || '').trim();
        let primary = explicitDisplay;
        if (options.includeStatus && status && status !== 'active' && status !== 'operational') {
            primary += ` ${options.statusPrefix || ''}${status}`.trimStart();
        }
        return primary;
    }
    const code = String(machine?.code || '').trim();
    const name = String(machine?.name || '').trim();
    const status = String(machine?.status || '').trim();
    const typeLabel = machine?.type || machine?.category || '';
    let primary = code && name && code !== name
        ? `${code} - ${name}`
        : (code || name || `Machine #${machine?.id || ''}`.trim());

    if (options.includeType && typeLabel) {
        primary += ` (${typeLabel})`;
    }
    if (options.includeStatus && status && status !== 'active' && status !== 'operational') {
        primary += ` ${options.statusPrefix || ''}${status}`.trimStart();
    }
    return primary;
}

function getTimelineMachinePresentation(machine) {
    const code = String(machine?.code || '').trim();
    const rawName = String(machine?.name || '').trim();
    const explicitDisplay = String(machine?.display_name || '').trim();
    let name = rawName;

    if (!name && explicitDisplay) {
        const displayPrefix = code ? `${code} - ` : '';
        if (displayPrefix && explicitDisplay.toLowerCase().startsWith(displayPrefix.toLowerCase())) {
            name = explicitDisplay.slice(displayPrefix.length).trim();
        } else {
            name = explicitDisplay;
        }
    }

    const combinedLabel = explicitDisplay || (
        code && name && normalizeSortToken(code) !== normalizeSortToken(name)
            ? `${code} - ${name}`
            : (code || name || `Machine #${machine?.id || ''}`.trim())
    );
    const resolvedName = name || combinedLabel;
    const showCodeBadge = Boolean(code) && Boolean(resolvedName)
        && normalizeSortToken(code) !== normalizeSortToken(resolvedName);

    return {
        code,
        name: resolvedName,
        combinedLabel,
        showCodeBadge,
    };
}

// --- Planner Actions ---
window.closePlannerAction = async function (woId) {
    // Prevent event bubbling if called from a card
    if (window.event) window.event.stopPropagation();

    const confirmed = await confirmTimelineAction(
        "Are you sure you want to close this Work Order?",
        { title: 'Close Work Order', confirmText: 'Close WO', kind: 'warning' }
    );
    if (!confirmed) return;

    try {
        const res = await fetch(`/manufacturing/api/work-order/${woId}/close/`, {
            method: 'POST',
            headers: {
                'X-CSRFToken': getCookie('csrftoken'),
                'Content-Type': 'application/json'
            }
        });

        if (!res.ok) throw new Error("Request failed");

        const data = await res.json();
        if (data.success) {
            showTimelineToast(data.message || "Work order closed.", 'success');
            if (typeof window.reloadPlannerWorkspacePreservingState === 'function') {
                window.reloadPlannerWorkspacePreservingState({ activeScreen: 'notifications' });
            } else {
                location.reload();
            }
        } else {
            showTimelineToast("Error closing order: " + (data.error || "Unknown error"), 'error');
        }
    } catch (e) {
        console.error(e);
        showTimelineToast("Network error or server failed to respond.", 'error');
    }
};



