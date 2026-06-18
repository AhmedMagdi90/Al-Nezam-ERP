"""Future home for inventory and material readiness service logic.

TODO(phase3-service-split): Extract material readiness, shortage, store receipt,
and BOM material availability helpers from ``manufacturing.services``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from manufacturing.models import BillOfMaterial, MaterialUsage, WorkOrder

