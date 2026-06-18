"""Future home for work order orchestration logic.

TODO(phase3-service-split): Extract ``WorkOrderService`` and work-order helper
functions from ``manufacturing.services`` in small, behavior-preserving steps.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from manufacturing.models import WorkOrder

