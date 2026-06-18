"""Future home for dashboard, reporting, and analytics service logic.

TODO(phase3-service-split): Extract reporting read models from
``DashboardService`` after write-side work order and production dependencies
are isolated.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from manufacturing.models import Company, WorkOrder

