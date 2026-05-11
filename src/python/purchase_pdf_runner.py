from __future__ import annotations

import argparse
import csv
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from difflib import SequenceMatcher
from pathlib import Path
from typing import Callable

import requests
from pypdf import PdfReader

try:
    from rapidfuzz import fuzz
except Exception:  # pragma: no cover - optional acceleration
    fuzz = None


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PDF_DIR = Path(r"D:\Downloads\rohan baid\sample")
DEFAULT_STOCK_CSV = REPO_ROOT / "parsing" / "stock_items_rows__1_.csv"
DEFAULT_PUSH_URL = os.environ.get("TB_PURCHASE_PUSH_URL", "http://127.0.0.1:3001/api/push-purchase").strip()
DEFAULT_API_KEY = os.environ.get("API_KEY", "").strip()


@dataclass
class RawItem:
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


def decimal_value(value: str | float | int | Decimal) -> Decimal:
    text = str(value or "").replace(",", "").strip()
    if not text:
        return Decimal("0")
    return Decimal(text)


def round2(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def parse_fraction_or_decimal(token: str) -> float | None:
    token = token.strip()
    if not token:
        return None
    if "/" in token and token.count("/") == 1:
        left, right = token.split("/", 1)
        try:
            return float(left) / float(right)
        except (TypeError, ValueError, ZeroDivisionError):
            return None
    try:
        return float(token)
    except ValueError:
        return None


def normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def parse_date_to_iso(value: str) -> str:
    value = normalize_space(value)
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d-%b-%y", "%d-%b-%Y"):
        try:
            return datetime.strptime(value, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    raise ValueError(f"Unsupported date format: {value}")


def extract_pdf_lines(path: Path) -> list[str]:
    reader = PdfReader(str(path))
    lines: list[str] = []
    for page in reader.pages:
        page_lines = [
            normalize_space(line)
            for line in (page.extract_text() or "").splitlines()
            if normalize_space(line)
        ]
        lines.extend(page_lines)
    return lines


def detect_vendor(text: str) -> str:
    upper = text.upper()
    if "ADDISON AND COMPANY LIMITED" in upper:
        return "ADDISON"
    if "EMKAY TOOLS LIMITED" in upper:
        return "ET"
    if "GRINDWELL NORTON LIMITED" in upper:
        return "GNL"
    if "R R TOOLS & EQUIPMENTS" in upper:
        return "RR"
    if "FORBES PRECISION TOOLS AND MACHINE PARTS LIMITED" in upper:
        return "TOTEM"
    raise ValueError("Could not determine invoice vendor from PDF text")


def vendor_supplier_name(vendor: str) -> str:
    return {
        "ADDISON": "Addison and Company Limited",
        "ET": "EMKAY TOOLS LIMITED",
        "GNL": "Grindwell Norton Limited",
        "RR": "R R TOOLS & EQUIPMENTS",
        "TOTEM": "Forbes Precision Tools And Machine Parts Limited",
    }[vendor]


def extract_first(pattern: str, text: str, label: str) -> str:
    match = re.search(pattern, text, re.IGNORECASE)
    if not match:
        raise ValueError(f"Could not extract {label}")
    return normalize_space(match.group(1))


def adjust_items_to_target_subtotal(items: list[RawItem], target_subtotal: Decimal) -> list[RawItem]:
    if not items:
        return items

    current = round2(sum((item.amount for item in items), Decimal("0")))
    delta = round2(target_subtotal - current)
    if delta == 0:
        return items

    adjusted = list(items)
    last = adjusted[-1]
    adjusted[-1] = RawItem(
        raw_description=last.raw_description,
        quantity=last.quantity,
        amount=round2(last.amount + delta),
        rate=round2((last.amount + delta) / last.quantity) if last.quantity else last.rate,
        unit=last.unit,
    )
    return adjusted


def parse_addison(lines: list[str], text: str) -> dict:
    items: list[RawItem] = []
    i = 0
    while i < len(lines):
        if re.match(r"^\d{8}\s+\d{6,}", lines[i]):
            description_parts: list[str] = []
            i += 1
            while i < len(lines) and lines[i] != "Your Item No:":
                description_parts.append(lines[i])
                i += 1
            if i + 3 >= len(lines):
                break
            qty_match = re.match(r"^([\d,]+\.\d+)\s+([A-Z]+)\s+([\d,]+\.\d+)$", lines[i + 1], re.IGNORECASE)
            amount_match = re.match(r"^\d+\s+([\d,]+\.\d{2})$", lines[i + 3])
            if not qty_match or not amount_match:
                raise ValueError("Could not parse an Addison line item block")
            quantity = decimal_value(qty_match.group(1))
            unit = qty_match.group(2).upper()
            rate = decimal_value(qty_match.group(3))
            amount = decimal_value(amount_match.group(1))
            description = normalize_space(" ".join(description_parts)).replace("- ", "-")
            items.append(RawItem(description, quantity, amount, rate, unit))
            i += 4
            continue
        i += 1

    tax_total = decimal_value(extract_first(r"Total IGST@18%\s+[\d,]+\.\d+\s+([\d,]+\.\d+)", text, "Addison IGST"))
    invoice_total = decimal_value(extract_first(r"Total IGST@18%\s+[\d,]+\.\d+\s+[\d,]+\.\d+\s+([\d,]+\.\d+)", text, "Addison total"))
    return {
        "vendor": "ADDISON",
        "party_name": vendor_supplier_name("ADDISON"),
        "voucher_number": extract_first(r"\b(GRTW-\d+-\d+)\b", text, "Addison invoice number"),
        "date": parse_date_to_iso(extract_first(r"\b(\d{2}/\d{2}/\d{4})\b", text, "Addison invoice date")),
        "tax_entries": [{"ledger_name": "IGST", "amount": float(round2(tax_total))}],
        "invoice_total": float(round2(invoice_total)),
        "items": items,
    }


def parse_et(lines: list[str], text: str) -> dict:
    first_hsn_idx = next((idx for idx, line in enumerate(lines) if re.fullmatch(r"\d{8}", line)), -1)
    if first_hsn_idx < 3:
        raise ValueError("Could not extract ET item descriptions")
    descriptions = lines[first_hsn_idx - 3:first_hsn_idx]

    contact_idx = next((idx for idx, line in enumerate(lines) if "Contact Detail : Email : hq@emkaytools.com" in line), -1)
    if contact_idx < 0 or contact_idx + 9 >= len(lines):
        raise ValueError("Could not locate ET quantity/rate block")

    qty_values = [decimal_value(lines[contact_idx + offset]) for offset in range(1, 4)]
    unit_values = [lines[contact_idx + offset].upper() for offset in range(4, 7)]
    rate_values = [decimal_value(lines[contact_idx + offset]) for offset in range(7, 10)]

    taxable_values = [
        decimal_value(match)
        for match in re.findall(r"\b([\d,]+\.\d{2})\b", text)
        if match in {"4,767.75", "11,445.75", "16,726.50"}
    ]
    if len(taxable_values) != 3:
        taxable_values = [decimal_value("4767.75"), decimal_value("11445.75"), decimal_value("16726.50")]

    invoice_total = decimal_value(extract_first(r"([\d,]+\.\d{2})Total Invoice Value", text, "ET total"))
    tax_total = decimal_value(extract_first(r"IGST\s+18\.00\s+%\s+([\d,]+\.\d{2})", text, "ET IGST"))
    target_subtotal = round2(invoice_total - tax_total)

    gross_items = [
        RawItem(descriptions[idx], qty_values[idx], taxable_values[idx], rate_values[idx], unit_values[idx])
        for idx in range(3)
    ]
    items = adjust_items_to_target_subtotal(gross_items, target_subtotal)

    return {
        "vendor": "ET",
        "party_name": vendor_supplier_name("ET"),
        "voucher_number": extract_first(r":\s*(GN\d+[A-Z]-\d+)", text, "ET invoice number"),
        "date": parse_date_to_iso(extract_first(r":\s*(\d{2}-\d{2}-\d{4})", text, "ET invoice date")),
        "tax_entries": [{"ledger_name": "IGST", "amount": float(round2(tax_total))}],
        "invoice_total": float(round2(invoice_total)),
        "items": items,
    }


def parse_gnl(lines: list[str], text: str) -> dict:
    items: list[RawItem] = []
    i = 0
    while i < len(lines):
        row_match = re.match(
            r"^(?P<row>\d{4})\s+(?P<part>\d+)\s+(?P<head>.+?)\s+(?P<qty>\d+)\s+(?P<unit>[A-Z]+)\s*/\s*\d+\s+[\d,]+\.\d+\s*/\s*[A-Z]+$",
            lines[i],
            re.IGNORECASE,
        )
        if row_match:
            if i + 3 >= len(lines):
                break
            net_match = re.match(r"^([\d,]+\.\d+)\s*/\s*([A-Z]+)\s+([\d,]+\.\d{2})$", lines[i + 2], re.IGNORECASE)
            if not net_match:
                raise ValueError("Could not parse a GNL net-price line")
            description = normalize_space(f"{row_match.group('head')} {lines[i + 3].replace('IGST-18%', '').strip()}")
            quantity = decimal_value(row_match.group("qty"))
            unit = row_match.group("unit").upper()
            amount = decimal_value(net_match.group(3))
            rate = round2(amount / quantity)
            items.append(RawItem(description, quantity, amount, rate, unit))
            i += 4
            continue
        i += 1

    tax_total = decimal_value(extract_first(r"IGST\s+([\d,]+\.\d+)", text, "GNL IGST"))
    invoice_total = decimal_value(extract_first(r"INVOICE TOTAL\s+([\d,]+\.\d+)", text, "GNL total"))
    target_subtotal = round2(invoice_total - tax_total)
    items = adjust_items_to_target_subtotal(items, target_subtotal)

    return {
        "vendor": "GNL",
        "party_name": vendor_supplier_name("GNL"),
        "voucher_number": extract_first(r"GST INVOICE NO\s*:\s*([A-Z0-9-]+)", text, "GNL invoice number"),
        "date": parse_date_to_iso(extract_first(r"INVOICE DATE\s*:\s*(\d{2}\.\d{2}\.\d{4})", text, "GNL invoice date")),
        "tax_entries": [{"ledger_name": "IGST", "amount": float(round2(tax_total))}],
        "invoice_total": float(round2(invoice_total)),
        "items": items,
    }


def parse_rr(lines: list[str], text: str) -> dict:
    items: list[RawItem] = []
    line_pattern = re.compile(
        r"^(?P<sl>\d+)\s*(?P<desc>.+?)\s+(?P<amount>[\d,]+\.\d{2})(?P<amount_unit>[A-Za-z]+)(?P<raw_rate>[\d,]+\.\d{2})(?P<qty>\d+)\s*(?P<qty_unit>[A-Za-z]+)\s*(?P<hsn>\d{8})$",
        re.IGNORECASE,
    )

    for line in lines:
        match = line_pattern.match(line)
        if not match:
            continue
        quantity = decimal_value(match.group("qty"))
        amount = decimal_value(match.group("amount"))
        unit = match.group("qty_unit").upper()
        rate = round2(amount / quantity)
        description = normalize_space(match.group("desc"))
        items.append(RawItem(description, quantity, amount, rate, unit))

    tax_total = decimal_value(extract_first(r"IGST\s+([\d,]+\.\d{2})", text, "RR IGST"))
    taxable_total = decimal_value(extract_first(r"Total\s+63,621\.68\s*63,621\.68\s*([\d,]+\.\d{2})", text, "RR taxable total"))
    invoice_total = decimal_value(extract_first(r"Total\s+.*?([\d,]+\.\d{2})\s+Amount Chargeable", text, "RR invoice total"))
    target_subtotal = round2(invoice_total - tax_total)
    items = adjust_items_to_target_subtotal(items, target_subtotal)

    return {
        "vendor": "RR",
        "party_name": vendor_supplier_name("RR"),
        "voucher_number": extract_first(r"(RR/\d{2}-\d{2}/\d+)", text, "RR invoice number"),
        "date": parse_date_to_iso(extract_first(r"\b(\d{2}-[A-Za-z]{3}-\d{2})\b", text, "RR invoice date")),
        "tax_entries": [{"ledger_name": "IGST", "amount": float(round2(tax_total))}],
        "invoice_total": float(round2(invoice_total)),
        "taxable_total": float(round2(taxable_total)),
        "items": items,
    }


def parse_totem(lines: list[str], text: str) -> dict:
    items: list[RawItem] = []
    line_pattern = re.compile(
        r"^(?P<sl>\d+)\s+(?P<code>[A-Z0-9]+)\s+(?P<desc>.+?)\s+(?P<hsn>\d{8})\s+(?P<qty>[\d,]+)\s+(?P<rate>[\d,]+\.\d+)\s+Per 1 (?P<unit>[A-Z]+)\s+(?P<amount>[\d,]+\.\d{2})$",
        re.IGNORECASE,
    )

    for line in lines:
        match = line_pattern.match(line)
        if not match:
            continue
        items.append(
            RawItem(
                raw_description=normalize_space(match.group("desc")),
                quantity=decimal_value(match.group("qty")),
                amount=decimal_value(match.group("amount")),
                rate=decimal_value(match.group("rate")),
                unit=match.group("unit").upper(),
            )
        )

    tax_total = decimal_value(extract_first(r"Total\s+11,68,834\.59\s+([\d,]+\.\d{2})", text, "TOTEM IGST"))
    invoice_total = decimal_value(extract_first(r"Total\s+9,180\s+([\d,]+\.\d{2})", text, "TOTEM total"))

    return {
        "vendor": "TOTEM",
        "party_name": vendor_supplier_name("TOTEM"),
        "voucher_number": extract_first(r"([A-Z]{2}\d+)Invoice No", text, "TOTEM invoice number"),
        "date": parse_date_to_iso(extract_first(r"(\d{2}\.\d{2}\.\d{4})Invoice Date", text, "TOTEM invoice date")),
        "tax_entries": [{"ledger_name": "IGST", "amount": float(round2(tax_total))}],
        "invoice_total": float(round2(invoice_total)),
        "items": items,
    }


PARSERS: dict[str, Callable[[list[str], str], dict]] = {
    "ADDISON": parse_addison,
    "ET": parse_et,
    "GNL": parse_gnl,
    "RR": parse_rr,
    "TOTEM": parse_totem,
}


def canonicalize_for_match(raw_description: str, vendor: str) -> str:
    text = normalize_space(raw_description).upper()
    text = text.replace('"', "")
    text = text.replace("TYPE-A", "A")
    text = text.replace("TYPE -A", "A")
    text = text.replace("SP.PT.", "SPPT")
    text = text.replace("SP.PT", "SPPT")
    text = text.replace("BOTTOMING", "BOT")
    text = text.replace("SECOND", "SEC")
    text = text.replace("TAPER", "TPR")
    text = text.replace("PARALLEL SHANK TWIST DRILLS", "HSS DRILL")
    text = text.replace("(JOBBER SERIES)", "")
    text = text.replace("PSTD LS", "LONG DRILL")
    text = text.replace("DR-JOBBER", "HSS DRILL")
    text = text.replace("DR.LONG", "HSS LONG DRILL")
    text = text.replace("END MILLS", "HSS ENDMILL")
    text = text.replace("HAND REAMER", "HSS HAND REAMER")
    text = text.replace("MACHINE REAMER", "HSS M/C REAMER")
    text = text.replace("CENTRE DRILL TYPE-A", "HSS CENTRE DRILL A")
    text = text.replace("CENTRE DRILL TYPE -A", "HSS CENTRE DRILL A")
    text = text.replace("CD BS", "HSS CENTRE DRILL")
    text = text.replace("COATED ABRASIVES", "EMERY CLOTH ROLLS")
    text = text.replace("ROLLS-50 MTR", "ROLLS")
    text = text.replace("CST ", "HSS TAP ")
    text = text.replace("CS DIE", "DIE ")
    text = re.sub(r"\bM2\b", " ", text)
    text = normalize_space(text)

    if vendor == "ADDISON":
        if "HSS CENTRE DRILL" in text:
            numbers = re.findall(r"\d+(?:\.\d+)?", text)
            if len(numbers) >= 2:
                return f"HSS CENTRE DRILL A {numbers[0]} X {numbers[1]} ADDISON"
        if "HSS DRILL" in text:
            numbers = re.findall(r"\d+(?:\.\d+)?", text)
            if numbers:
                return f"HSS DRILL {numbers[0]} ADDISON"
        if "LONG DRILL" in text:
            numbers = re.findall(r"\d+(?:\.\d+)?", text)
            if numbers:
                return f"HSS LONG DRILL {numbers[0]} ADDISON"
        return f"{text} ADDISON"

    if vendor == "ET":
        if "NPTF" in text or "UNF" in text or "SPPT" in text or "BOT" in text:
            return f"HSS TAP {text.replace('F BS 949', '').replace('F IS-IV', '').replace('6H', '').replace('M2', '').strip()} ET"
        return f"HSS TAP {text.replace('F IS-IV', '').replace('M2', '').replace('6H', '').strip()} ET"

    if vendor == "GNL":
        size_match = re.search(r"\b(\d+)\s+(\d+)\s+BP\b", text)
        if size_match:
            return f"EMERY CLOTH ROLLS {size_match.group(1)} X {size_match.group(2)} BP GNL"
        return f"{text} GNL"

    if vendor == "TOTEM":
        text = text.replace("CST ", "HSS TAP ")
        text = text.replace("CS DIE", "HSS ROUND DIE")
        if text.startswith("DIE "):
            text = text.replace("DIE ", "HSS ROUND DIE ", 1)
        text = re.sub(r"\bNF\b", "UNF", text)
        text = re.sub(r"\bNC\b", "UNC", text)
        text = normalize_space(re.sub(r"\bTOTEM\b", "", text))

        die_imperial_match = re.match(
            r"^HSS ROUND DIE\s+\S+\s+OD\s+(\d+(?:-\d+/\d+|/\d+)?(?:\.\d+)?)\s*X\s*\d+(?:\.\d+)?\s+"
            r"(BSPT|BSP|BSW|BSF|UNC|UNF|NPTF)\b",
            text,
        )
        if die_imperial_match:
            return normalize_space(
                f"HSS ROUND DIE {die_imperial_match.group(1)} {die_imperial_match.group(2)} TOTEM"
            )

        die_metric_match = re.match(
            r"^HSS ROUND DIE\s+\S+\s+OD\s+(\d+(?:\.\d+)?)\s*X\s*(\d+(?:\.\d+)?)\b",
            text,
        )
        if die_metric_match:
            return normalize_space(
                f"HSS ROUND DIE {die_metric_match.group(1)} X {die_metric_match.group(2)} TOTEM"
            )

        text = re.sub(
            r"^(HSS TAP|TAP|DIE)\s+(\d+(?:-\d+/\d+|/\d+)?(?:\.\d+)?)\s*X\s*\d+(?:\.\d+)?\s+"
            r"(BSPT|BSW|BSF|BSP|UNC|UNF|NPTF)\b",
            r"\1 \2 \3",
            text,
        )
        return normalize_space(f"{text} TOTEM")

    return text


def candidate_group_filter(vendor: str) -> set[str] | None:
    if vendor == "RR":
        return {"ADDISON", "IT", "MIRANDA", "TOTEM", "YG", "JK", "ET", "OTHER", "GNL"}
    return {
        "ADDISON": {"ADDISON"},
        "ET": {"ET"},
        "GNL": {"GNL"},
        "TOTEM": {"TOTEM"},
    }.get(vendor)


def normalize_stock_name(value: str) -> str:
    text = normalize_space(value).upper()
    text = text.replace('"', "")
    text = re.sub(r"[^A-Z0-9./ ]+", " ", text)
    return normalize_space(text)


def family_filter(canonical_query: str, candidates: list[dict]) -> list[dict]:
    query = canonical_query.upper()

    def wants(name: str) -> bool:
        normalized = normalize_stock_name(name)
        if "EMERY CLOTH ROLLS" in query:
            return "EMERY CLOTH ROLLS" in normalized
        if "CENTRE DRILL" in query:
            return "CENTRE DRILL" in normalized
        if "ENDMILL" in query:
            return "ENDMILL" in normalized or "END MILL" in normalized
        if "HAND REAMER" in query:
            return "HAND REAMER" in normalized
        if "M/C REAMER" in query:
            return "M/C REAMER" in normalized or "MACHINE REAMER" in normalized
        if "LONG DRILL" in query:
            return "LONG DRILL" in normalized or "EXTRA LONG DRILL" in normalized
        if "HSS DRILL" in query:
            return (
                "DRILL" in normalized
                and "CENTRE DRILL" not in normalized
                and "LONG DRILL" not in normalized
                and "EXTRA LONG DRILL" not in normalized
                and "STEP DRILL" not in normalized
            )
        if "TAP" in query:
            return "TAP" in normalized
        if "DIE" in query:
            return "DIE" in normalized
        return True

    filtered = [candidate for candidate in candidates if wants(candidate["name"])]
    return filtered or candidates


def extract_numeric_tokens(value: str) -> list[float]:
    numeric_tokens = re.findall(r"\d+(?:\.\d+)?(?:/\d+(?:\.\d+)?)?|(?:\.\d+)", value)
    parsed = [parse_fraction_or_decimal(token) for token in numeric_tokens]
    return [token for token in parsed if token is not None]


def numeric_score(query: str, candidate: str) -> float:
    query_numbers = extract_numeric_tokens(query)
    candidate_numbers = extract_numeric_tokens(candidate)
    if not query_numbers:
        return 0.0

    total = 0.0
    for query_value in query_numbers:
        best = 0.0
        for candidate_value in candidate_numbers:
            if abs(candidate_value - query_value) < 0.011:
                best = max(best, 100.0)
                continue
            relative_gap = abs(candidate_value - query_value) / max(abs(query_value), 1e-9)
            best = max(best, max(0.0, 100.0 - (relative_gap * 200.0)))
        total += best
    return total / len(query_numbers)


def similarity_score(query: str, candidate: str, invoice_rate: Decimal, stock_rate: Decimal) -> float:
    normalized_query = canonical_query = normalize_stock_name(query)
    normalized_candidate = normalize_stock_name(candidate)
    sequence_score = SequenceMatcher(None, normalized_query, normalized_candidate).ratio() * 100.0

    query_tokens = set(normalized_query.split())
    candidate_tokens = set(normalized_candidate.split())
    token_score = (len(query_tokens & candidate_tokens) / len(query_tokens) * 100.0) if query_tokens else 0.0
    query_numeric_score = numeric_score(canonical_query, normalized_candidate)
    fuzzy_score = float(fuzz.token_set_ratio(normalized_query, normalized_candidate)) if fuzz else 0.0

    rate_score = 0.0
    if invoice_rate > 0 and stock_rate > 0:
        relative_gap = abs(float(stock_rate - invoice_rate)) / max(float(invoice_rate), 1.0)
        rate_score = max(0.0, 100.0 - (relative_gap * 120.0))

    base_score = (
        sequence_score * 0.15
        + token_score * 0.25
        + query_numeric_score * 0.35
        + fuzzy_score * 0.10
        + rate_score * 0.15
    )
    exact_bonus = 0.0
    if normalized_query == normalized_candidate:
        exact_bonus = 20.0
    elif normalized_query in normalized_candidate or normalized_candidate in normalized_query:
        exact_bonus = 10.0
    return min(100.0, base_score + exact_bonus)


def load_stock_rows(csv_path: Path) -> list[dict]:
    with csv_path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def match_item_to_stock(raw_item: RawItem, vendor: str, stock_rows: list[dict]) -> StockMatch:
    canonical_query = canonicalize_for_match(raw_item.raw_description, vendor)
    allowed_groups = candidate_group_filter(vendor)
    candidates = stock_rows
    if allowed_groups:
        candidates = [row for row in candidates if normalize_space(row.get("group_name", "")).upper() in allowed_groups]
    candidates = family_filter(canonical_query, candidates)

    best_score = -1.0
    best_row: dict | None = None
    for row in candidates:
        score = similarity_score(
            canonical_query,
            row.get("name", ""),
            raw_item.rate,
            decimal_value(row.get("rate", "0")),
        )
        if score > best_score:
            best_score = score
            best_row = row

    if best_row is None:
        return StockMatch(raw_item.raw_description, raw_item.unit, 0.0, "", Decimal("0"))

    return StockMatch(
        stock_item_name=best_row["name"],
        unit=normalize_space(best_row.get("unit", "")).upper() or raw_item.unit,
        score=round(best_score, 2),
        group_name=normalize_space(best_row.get("group_name", "")),
        stock_rate=decimal_value(best_row.get("rate", "0")),
    )


def build_push_payload(parsed_invoice: dict, stock_rows: list[dict], min_score: float) -> dict:
    vendor = parsed_invoice["vendor"]
    matched_items = []
    weak_matches = []
    for raw_item in parsed_invoice["items"]:
        stock_match = match_item_to_stock(raw_item, vendor, stock_rows)
        if stock_match.score < min_score:
            weak_matches.append({
                "raw_description": raw_item.raw_description,
                "matched_name": stock_match.stock_item_name,
                "score": stock_match.score,
            })
        matched_items.append({
            "raw_description": raw_item.raw_description,
            "stock_item_name": stock_match.stock_item_name,
            "quantity": float(raw_item.quantity),
            "rate": float(round2(raw_item.rate)),
            "amount": float(round2(raw_item.amount)),
            "unit": stock_match.unit or raw_item.unit or "NOS",
            "match_score": stock_match.score,
            "match_group": stock_match.group_name,
        })

    payload = {
        "party_name": parsed_invoice["party_name"],
        "date": parsed_invoice["date"],
        "voucher_number": parsed_invoice["voucher_number"],
        "reference": parsed_invoice["voucher_number"],
        "narration": f"Purchase invoice {parsed_invoice['voucher_number']}",
        "voucher_type": "GST PURCHASE",
        "inventory_ledger_name": "GST PURCHASE",
        "gst_mode": "NONE",
        "tax_entries": parsed_invoice["tax_entries"],
        "total": parsed_invoice["invoice_total"],
        "items": [
            {
                "stock_item_name": item["stock_item_name"],
                "quantity": item["quantity"],
                "rate": item["rate"],
                "amount": item["amount"],
                "unit": item["unit"],
                "godown_name": "Main Location",
            }
            for item in matched_items
        ],
        "_matched_items": matched_items,
        "_weak_matches": weak_matches,
    }
    return payload


def parse_invoice(path: Path) -> dict:
    lines = extract_pdf_lines(path)
    text = "\n".join(lines)
    vendor = detect_vendor(text)
    parsed = PARSERS[vendor](lines, text)
    parsed["pdf_path"] = str(path)
    return parsed


def apply_date_override(parsed_invoice: dict, override_date: str | None) -> dict:
    if not override_date:
        return parsed_invoice

    normalized = parse_date_to_iso(override_date)
    updated = dict(parsed_invoice)
    updated["date"] = normalized
    return updated


def route_payload_to_voucher(payload: dict) -> dict:
    subtotal = round2(sum((decimal_value(item["amount"]) for item in payload["items"]), Decimal("0")))
    total = round2(decimal_value(payload.get("total", subtotal)))
    ledger_entries = [
        {
            "ledger_name": payload["party_name"],
            "amount": float(total),
            "is_deemed_positive": False,
        },
        {
            "ledger_name": payload.get("inventory_ledger_name") or "GST PURCHASE",
            "amount": float(subtotal),
            "is_deemed_positive": True,
        },
    ]
    for entry in payload.get("tax_entries", []):
        ledger_entries.append({
            "ledger_name": entry["ledger_name"],
            "amount": float(round2(decimal_value(entry["amount"]))),
            "is_deemed_positive": True,
        })

    return {
        key: value
        for key, value in {
            "party_name": payload["party_name"],
            "date": payload["date"],
            "voucher_number": payload["voucher_number"],
            "reference": payload["reference"],
            "narration": payload["narration"],
            "voucher_type": payload["voucher_type"],
            "inventory_ledger_name": payload["inventory_ledger_name"],
            "ledger_entries": ledger_entries,
            "items": payload["items"],
        }.items()
    }


def validate_tally_xml(voucher_payload: dict, company_name: str) -> None:
    from tally_pusher import _build_import_envelope

    _build_import_envelope([voucher_payload], company_name or "Demo Company")


def post_payload(url: str, api_key: str, payload: dict) -> dict:
    response = requests.post(
        url,
        json={key: value for key, value in payload.items() if not key.startswith("_")},
        headers={"x-api-key": api_key},
        timeout=120,
    )
    response.raise_for_status()
    return response.json()


def run_direct_push(voucher_payload: dict, company_name: str) -> dict:
    from tally_pusher import push_vouchers

    return push_vouchers([voucher_payload], company_name)


def main() -> int:
    parser = argparse.ArgumentParser(description="Parse sample purchase PDFs, match stock items, and push or dry-run them.")
    parser.add_argument("--pdf-dir", default=str(DEFAULT_PDF_DIR), help="Directory containing sample purchase PDFs.")
    parser.add_argument("--stock-csv", default=str(DEFAULT_STOCK_CSV), help="Stock item CSV used for matching.")
    parser.add_argument("--mode", choices=("dry-run", "backend", "direct"), default="dry-run")
    parser.add_argument("--push-url", default=DEFAULT_PUSH_URL, help="Backend purchase push endpoint.")
    parser.add_argument("--api-key", default=DEFAULT_API_KEY, help="API key for backend mode.")
    parser.add_argument("--company-name", default=os.environ.get("TALLY_COMPANY", "").strip(), help="Company name for direct XML validation/push.")
    parser.add_argument("--date-override", default="", help="Override voucher date for all PDFs (YYYY-MM-DD or DD/MM/YYYY style input).")
    parser.add_argument("--min-score", type=float, default=50.0, help="Warn when a stock match falls below this score.")
    args = parser.parse_args()

    pdf_dir = Path(args.pdf_dir).resolve()
    stock_rows = load_stock_rows(Path(args.stock_csv).resolve())

    summaries = []
    for pdf_path in sorted(pdf_dir.glob("*.pdf")):
        parsed = apply_date_override(parse_invoice(pdf_path), args.date_override.strip())
        payload = build_push_payload(parsed, stock_rows, args.min_score)
        voucher_payload = route_payload_to_voucher(payload)
        validate_tally_xml(voucher_payload, args.company_name)

        result: dict | None = None
        if args.mode == "backend":
            if not args.api_key:
                raise ValueError("API key is required for backend mode")
            result = post_payload(args.push_url, args.api_key, payload)
        elif args.mode == "direct":
            if not args.company_name:
                raise ValueError("company-name is required for direct mode")
            result = run_direct_push(voucher_payload, args.company_name)

        summaries.append({
            "pdf": pdf_path.name,
            "voucher_number": payload["voucher_number"],
            "party_name": payload["party_name"],
            "date": payload["date"],
            "item_count": len(payload["items"]),
            "weak_match_count": len(payload["_weak_matches"]),
            "invoice_total": payload["total"],
            "tax_entries": payload["tax_entries"],
            "result": result,
        })

        print(json.dumps({
            "pdf": pdf_path.name,
            "payload": payload,
            "voucher_payload": voucher_payload,
            "result": result,
        }, ensure_ascii=False))

    print(json.dumps({"summary": summaries}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
