from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass
class PurchaseRawItem:
    item_code: str
    raw_description: str
    quantity: Decimal
    amount: Decimal
    rate: Decimal
    unit: str


@dataclass
class StockMatch:
    stock_item_name: str
    unit: str
    score: float
    group_name: str
    stock_rate: Decimal
    canonical_query: str
