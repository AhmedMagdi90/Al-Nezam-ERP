"""Future home for BOM-related service logic.

TODO(phase3-service-split): Extract BOM helpers and ``BOMService`` from
``manufacturing.services`` after a compatibility package for
``manufacturing.services`` is introduced.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from manufacturing.models import BillOfMaterial, Product, WorkOrder

