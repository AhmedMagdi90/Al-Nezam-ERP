# Phase 3B Service Split Plan

Date: 2026-06-18

## Objective

Prepare `manufacturing/services.py` for a future behavior-preserving split. Phase 3B
does not move runtime code, change imports, change URLs, change models, or change API
behavior.

## Current Constraint

`manufacturing/services.py` is the active public module imported by views, models,
serializers, tests, analytics, and integrations. Creating a real
`manufacturing/services/` package at the same path would require replacing that file
with a package and would change Python import resolution.

For Phase 3B, runtime imports remain unchanged. Placeholder modules were created under
`manufacturing/services_blueprint/` as a non-runtime blueprint. The exact
`manufacturing/services/` package should only be introduced in a later compatibility
step after `manufacturing.services` can re-export the existing public surface.

## Current Service Map

`manufacturing/services.py` is 6,594 lines.

| Domain | Current locations | Estimated lines | Notes |
| --- | --- | ---: | --- |
| BOM logic | `resolve_bom_for_work_order`, BOM change helpers, `BOMService` | 500-650 | Costing, requirements, BOM simulation, active BOM resolution, BOM change decisions. |
| Work order logic | Work order quantity helpers, execution readiness, `WorkOrderService` split/combine/recommendation methods | 1,000-1,300 | Strongly coupled to scheduling, BOM, QC, and material readiness. |
| Production cycle logic | `WorkOrderLifecycle`, `WorkOrderCycleService`, `ProductionLogService`, parent completion paths | 900-1,100 | Handles status transitions, close behavior, production logs, approval/rejection. |
| Scheduling logic | `DashboardService` timeline methods, route/stage methods, machine slot methods in `WorkOrderService` | 1,600-2,000 | Largest cross-cutting area; depends on shifts, machines, stages, and work order flow mode. |
| Quality logic | `QualityService`, QC annotation/helpers, scrap acceptance/compensation | 450-650 | QC is embedded in work order completion and scrap workflows. |
| Inventory/material logic | material readiness helpers, store receipt request, material usage in `ProductionLogService` | 250-450 | Depends on BOM scaling, store views, and production logging. |
| Reporting logic | `DashboardService.get_dashboard_context`, timeline summaries, `LiveCostService` | 1,000-1,500 | Read-model heavy; depends on most write-side services. |

## Target Categories

| Future module | Intended contents |
| --- | --- |
| `bom_service.py` | `BOMService`, BOM resolution, BOM change impact/decision helpers. |
| `work_order_service.py` | split/combine, assignment, quantity edits, recommendation, work order readiness. |
| `production_service.py` | `WorkOrderLifecycle`, `WorkOrderCycleService`, `ProductionLogService`. |
| `scheduling_service.py` | route scheduling, machine slots, shift windows, timeline scheduling helpers. |
| `quality_service.py` | `QualityService`, QC metrics, scrap compensation and QC gates. |
| `inventory_service.py` | material readiness, shortage/blocker checks, store receipt notifications, material usage helpers. |
| `reporting_service.py` | dashboard context, timeline read models, live cost/reporting read services. |

## Dependency Risks

### Circular Dependency Risks

- `WorkOrderService` currently calls `WorkOrderLifecycle`, `NotificationService`,
  material readiness helpers, BOM flow helpers, and QC helpers.
- `ProductionLogService` calls `WorkOrderService`, `NotificationService`, and writes
  material usage.
- `DashboardService` reads outputs from work order, material, scheduling, production,
  and QC logic.
- BOM change helpers mutate work orders and create notifications.

Mitigation: extract pure/read-only helpers first, then introduce explicit function
imports in one direction. Keep `reporting_service.py` last because it depends on
almost every domain.

### Model Coupling

- Heavy direct coupling to `WorkOrder`, `BillOfMaterial`, `BOMOperation`,
  `ProductionStage`, `ProductionLog`, `QualityCheck`, `MaterialUsage`, `Machine`,
  `SystemSettings`, and `Profile`.
- Several methods use tenant database aliases from model state and must preserve
  `.using(db_alias)` behavior.
- Transaction boundaries are embedded in service methods and must move intact when
  extracted.

Mitigation: preserve method bodies exactly during extraction and move tests with each
domain gate.

### View Coupling

Known import surface includes:

- `manufacturing/views/api.py`
- `manufacturing/views/bom.py`
- `manufacturing/views/bulk.py`
- `manufacturing/views/dashboard.py`
- `manufacturing/views/reports.py`
- `manufacturing/views/schedule.py`
- `manufacturing/views/shop_floor.py`
- `manufacturing/views/store.py`
- `manufacturing/views/work_order.py`
- `manufacturing/views/worker_assignment.py`
- legacy `manufacturing/views_legacy.py`

Mitigation: keep `manufacturing.services` as the compatibility facade until all view
imports are migrated and tested.

### API Coupling

- `/manufacturing/api/bom/save/`
- `/manufacturing/update-bom-status/`
- `/manufacturing/api/create-work-order/`
- work order BOM-change decision APIs
- shop-floor production log APIs
- schedule/timeline APIs
- store material readiness APIs

Mitigation: each extraction must run the full `manufacturing accounts tenancy` gate,
plus targeted tests for the touched endpoint family.

## Recommended Extraction Order

1. `bom_service.py`
   - Lowest initial blast radius if `BOMService` and BOM helper exports stay stable.
   - Run `manufacturing.tests.test_bom_save_api` and enterprise BOM tests.

2. `inventory_service.py`
   - Extract material readiness helpers after BOM helper imports are stable.
   - Run material readiness, store workflow, and planner dashboard tests.

3. `quality_service.py`
   - Extract QC annotations and scrap helpers after inventory boundaries are clear.
   - Run quality, shop floor, and work order lifecycle tests.

4. `production_service.py`
   - Move lifecycle/cycle/log services once QC dependencies are explicit.
   - Run production-cycle edge tests and shop-floor tests.

5. `work_order_service.py`
   - Move split/combine/recommendation and quantity helpers after dependent services
     are already separated.
   - Run split/combine, lifecycle, routing, and work-order creation tests.

6. `scheduling_service.py`
   - Move route scheduling and timeline slot logic after work-order operations are
     stable.
   - Run stage route planning, machine shift scheduling, timeline snap/history tests.

7. `reporting_service.py`
   - Move dashboard/timeline/report read models last.
   - Run dashboard, reports, planner, supervisor, and full workflow tests.

## Compatibility Plan For Later Phase

1. Create a temporary compatibility facade for `manufacturing.services`.
2. Move one domain at a time into the future package.
3. Re-export moved names from `manufacturing.services` until all imports are migrated.
4. Update imports in views/tests by domain only after the facade is proven stable.
5. Remove facade exports only in a later cleanup phase.

## Phase 3B Output

- Runtime behavior unchanged.
- Active `manufacturing/services.py` unchanged.
- Non-runtime placeholder modules created under `manufacturing/services_blueprint/`.
- This document records the target map, risks, and extraction order.

## First Extraction Target

Recommended first extraction target: BOM logic.

Reason: `BOMService` is compact compared with `DashboardService` and
`WorkOrderService`, has clear tests, and is already consumed as a named service from
several callers. Start with `BOMService` only, then move BOM change helpers in a
separate follow-up.
