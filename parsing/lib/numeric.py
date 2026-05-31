from __future__ import annotations

from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from lib.text import normalize_space


def decimal_value(value: Any) -> Decimal:
    if value in (None, ""):
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    text = str(value).replace(",", "").strip()
    if not text or text.lower() == "null":
        return Decimal("0")
    return Decimal(text)


def round2(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def pretty_number(value: str | Decimal | float | int) -> str:
    number = decimal_value(value)
    text = format(number.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if text.startswith("0.") and number < 1:
        return text[1:]
    if text == "-0":
        return "0"
    return text or "0"


def normalize_decimal_token(raw: str) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    if "." in text:
        return pretty_number(text)
    if not text.isdigit():
        return normalize_space(text)
    if len(text) == 1:
        return text
    if text.startswith("0"):
        return f".{text[1:]}"
    return f"{text[0]}.{text[1:]}"


def parse_date_to_iso(value: str) -> str:
    raw = normalize_space(value)
    if not raw:
        return ""
    for pattern in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d/%m/%y", "%d.%m.%Y", "%d.%m.%y", "%d-%b-%y", "%d-%b-%Y"):
        try:
            return datetime.strptime(raw, pattern).strftime("%Y-%m-%d")
        except ValueError:
            continue
    raise ValueError(f"Unsupported date format: {raw}")
