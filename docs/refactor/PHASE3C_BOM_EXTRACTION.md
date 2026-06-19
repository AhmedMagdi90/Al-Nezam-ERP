# Phase 3C BOM Extraction

Date: 2026-06-18

## Objective

Extract BOM-specific service logic from `manufacturing/services.py` into
`manufacturing/services_blueprint/bom_service.py` while preserving
`manufacturing.services` as the runtime compatibility facade.

## Runtime Compatibility

No caller imports were changed. Existing views, APIs, tests, analytics, and
integrations continue to import from `manufacturing.services`.

`manufacturing/services.py` now re-exports the extracted names from
`manufacturing.services_blueprint.bom_service`.

## Classes Moved

- `BOMService`

## Functions Moved

- `resolve_bom_for_work_order`
- `get_latest_active_bom_for_work_order`
- `_work_order_db_alias`
- `_work_order_family_ids`
- `get_workorder_production_totals`
- `work_order_has_started_production`
- `get_workorder_bom_change_payload`
- `clear_work_order_assignment_for_bom_change`
- `flag_bom_change_impact`
- `get_apply_latest_bom_eligibility`
- `apply_latest_bom_to_work_order`
- `decide_bom_change_archive_and_replace`
- `decide_bom_change_scrap_and_apply`
- `decide_bom_change_continue_old`

## Lines Moved

- 623 lines moved out of `manufacturing/services.py`
- `manufacturing/services.py` line count changed from 6,594 to 5,988
- `manufacturing/services_blueprint/bom_service.py` now contains the extracted
  BOM module and compatibility export list

## Dependency Analysis

The extracted module depends on:

- `django.db.transaction`
- `django.db.models.Sum`
- `django.utils.timezone`
- `manufacturing.models.BillOfMaterial`
- `manufacturing.models.Product`
- `manufacturing.models.ProductionLog`
- `manufacturing.models.WorkOrder`
- `manufacturing.units.UnitService`

The moved code does not import `manufacturing.services`, which avoids a circular
dependency with the compatibility facade.

## Remaining Coupling

- `get_workorder_execution_readiness` in `manufacturing/services.py` still calls
  extracted BOM helpers through the facade import.
- `DashboardService` still consumes `get_workorder_bom_change_payload`.
- Scheduling and API views still import BOM helpers from `manufacturing.services`.
- BOM change decisions still mutate `WorkOrder` state and therefore remain coupled
  to work-order lifecycle fields.
- `_work_order_db_alias`, `_work_order_family_ids`, and
  `get_workorder_production_totals` moved with BOM-change payload helpers because
  they are private/supporting implementation details for BOM change decisions and
  avoiding a back-import into `manufacturing.services` was safer than introducing a
  new shared utility module in this phase.

## Public Import Comparison

Before:

```python
from manufacturing.services import BOMService, flag_bom_change_impact
```

After:

```python
from manufacturing.services import BOMService, flag_bom_change_impact
```

No public import change is required.

## Behavior Comparison

- No model changes
- No URL changes
- No API response changes
- No permission changes
- No migrations
- Existing service names remain importable from `manufacturing.services`

## Risk Assessment

Risk level: low to medium.

The extraction is mostly mechanical, but BOM change helpers are coupled to
work-order status, production logs, and tenant database aliases. The compatibility
facade reduces caller risk; the main remaining risk is future circular imports if
new code imports from `manufacturing.services` inside `bom_service.py`.

## Follow-Up Guidance

Keep `bom_service.py` independent from `manufacturing.services`. Future extraction
targets should avoid importing the facade and should use direct model/service module
imports only after dependencies are clearly one-directional.
