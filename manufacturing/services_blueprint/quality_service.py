"""Future home for quality and QC workflow service logic.

TODO(phase3-service-split): Extract ``QualityService`` plus QC metric helpers
currently coupled to ``WorkOrderService`` annotations.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from manufacturing.models import QualityCheck, WorkOrder

