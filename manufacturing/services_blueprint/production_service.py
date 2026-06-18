"""Future home for production cycle and production log logic.

TODO(phase3-service-split): Extract ``WorkOrderLifecycle``,
``WorkOrderCycleService``, and ``ProductionLogService`` once work order helpers
have stable module boundaries.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from manufacturing.models import ProductionLog, WorkOrder

