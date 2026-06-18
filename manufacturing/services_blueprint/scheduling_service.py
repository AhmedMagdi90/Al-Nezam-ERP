"""Future home for scheduling and timeline planning logic.

TODO(phase3-service-split): Extract scheduling, route planning, shift, and
timeline helpers currently embedded in ``DashboardService`` and
``WorkOrderService``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from manufacturing.models import Machine, ProductionStage, WorkOrder

