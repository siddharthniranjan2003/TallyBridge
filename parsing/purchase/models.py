from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any


@dataclass
class PurchaseRawItem:
    item_code: str
    raw_description: str
    quantity: Decimal
    amount: Decimal
    rate: Decimal
    unit: str
    discount_pct: Decimal = Decimal("0")


@dataclass
class StockMatch:
    stock_item_name: str
    unit: str
    score: float
    group_name: str
    stock_rate: Decimal
    canonical_query: str
    trace: dict[str, Any] | None = None
