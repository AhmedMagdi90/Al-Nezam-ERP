# Phase 3D Work Order Service Extraction

## Summary

Phase 3D moved work-order orchestration logic out of `manufacturing/services.py` and into `manufacturing/services_blueprint/work_order_service.py` while preserving the public compatibility facade:

```python
from manufacturing.services import WorkOrderService
```

No models, URLs, migrations, permissions, API responses, or call sites were changed.

## Files Changed

- `manufacturing/services.py`
  - Now re-exports work-order service symbols from `manufacturing.services_blueprint.work_order_service`.
  - Line count reduced from 5,346 to 2,582 lines.
- `manufacturing/services_blueprint/work_order_service.py`
  - Replaced the Phase 3B placeholder with the extracted work-order implementation.
  - Current line count: 2,794 lines.

## Classes And Functions Moved

- `WorkOrderService`
- `WorkOrderLifecycle`
- `WorkOrderLifecycleError`
- `get_workorder_quantity_breakdown`
- `request_store_receipt_for_work_order`
- `normalize_operation_flow_mode`
- `get_company_default_operation_flow_mode`
- `get_work_order_operation_flow_mode`

Mechanical extraction size: 3,156 source lines.

## Compatibility

Existing imports continue to work through `manufacturing/services.py`:

```python
from manufacturing.services import WorkOrderService
from manufacturing.services import WorkOrderLifecycle, WorkOrderLifecycleError
from manufacturing.services import get_work_order_operation_flow_mode
```

The extracted module keeps cross-domain notification access behind a small lazy adapter so the facade can continue to own `NotificationService` until a later extraction phase.

## Dependency Analysis

Direct dependencies in the extracted module:

- Django transaction and ORM utilities.
- `accounts.models.Profile`.
- `manufacturing.models.ProductionLog`, `SystemSettings`, and `WorkOrder`.
- Local model imports inside methods for scheduling, QC, production, and change-log workflows.
- `manufacturing.shift_utils.machine_shift_configuration`.
- Existing `NotificationService` through a lazy compatibility adapter.

## Remaining Coupling

- Scheduling logic is still embedded inside `WorkOrderService`.
- Production-stage release and QC-compensation paths still live in the work-order service.
- Notification delivery remains in `manufacturing/services.py` and is called lazily.
- Dashboard, production log, quality, and material readiness helpers still depend on work-order service methods.

## Risk Notes

- The main runtime risk is import order. This was mitigated by keeping `manufacturing/services.py` as the public facade and avoiding direct call-site changes.
- The notification adapter intentionally avoids importing `manufacturing.services` at module import time to prevent circular imports.
- Further extractions should avoid changing scheduling or production-cycle behavior in the same phase as file movement.

## Recommended Next Extraction Target

Extract scheduling helpers from `WorkOrderService` into `manufacturing/services_blueprint/scheduling_service.py` after a dedicated scheduling test pass. The best candidates are slot finding, machine selection, snapping, and route rescheduling methods because they are a cohesive subset but still heavily tested by timeline and machine-shift tests.
