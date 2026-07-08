from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

try:
    from rapidfuzz import fuzz
except Exception:  # pragma: no cover - optional acceleration
    fuzz = None

from lib.env import make_env_loader
from lib.numeric import decimal_value, normalize_decimal_token, parse_date_to_iso, pretty_number, round2
from lib.text import normalize_space
from purchase.models import PurchaseRawItem, StockMatch

_env = make_env_loader(Path(__file__).resolve().parents[1] / ".env")
DEFAULT_PURCHASE_LEDGER = _env("MINICPM_PURCHASE_LEDGER", "PURCHASE GST") or "PURCHASE GST"
# Supplier-recovered freight/courier/cartage is booked to this ledger. It must
# exist in the target Tally company, set to "Appropriate for: GST" so the amount
# folds into the assessable value. Override per deployment via MINICPM_FREIGHT_LEDGER.
FREIGHT_LEDGER = _env("MINICPM_FREIGHT_LEDGER", "CARTAGE INWARD") or "CARTAGE INWARD"


LEDGER_NAME_HINTS = {
    "ADDISON": "ADDISON & COMPANY LTD",
    "ET": "EMKAY TOOLS LIMITED",
    "GNL": "GRINDWELL NORTON LIMITED",
    "RR": "R.R.TOOLS & EQUIPMENTS",
    "TOTEM": "FORBES PRECISION TOOLS AND MACHINE PARTS LTD",
    "CP": "CP GRAT-EX MANUFACTURING CO. LTD.",
    "PIDILITE": "PIDILITE INDUSTRIES LIMITED",
    "STANLEY": "STANLEY BLACK & DECKER INDIA PRIVATE LIMITED",
    "WIKUS": "WIKUS INDIA PRIVATE LIMITED",
}
LEDGER_NAME_ALIASES = {
    "CP": [
        "CP GRAT-EX MANUFACTURING COMPANY",
        "CP GRAT-EX MANUFACTURING CO LTD",
        "CP GRAT-EX MANUFACTURING CO. LTD.",
        "CP GRAT-EX MANUFACTURING CO",
        "CP GRAT-EX",
    ],
}
BEST_EFFORT_LEDGER_NAMES = {
    "ADDISON": "ADDISON & COMPANY LTD",
    "ET": "EMKAY TOOLS LIMITED",
    "GNL": "GRINDWELL NORTON LIMITED",
    "RR": "R.R.TOOLS & EQUIPMENTS",
    "TOTEM": "FORBES PRECISION TOOLS AND MACHINE PARTS LTD",
    "CP": "CP GRAT-EX MANUFACTURING COMPANY",
    "PIDILITE": "PIDILITE INDUSTRIES LIMITED",
    "STANLEY": "STANLEY BLACK & DECKER INDIA PRIVATE LIMITED",
    "WIKUS": "WIKUS INDIA PRIVATE LIMITED",
}


CP_EXACT_ITEM_QUERIES = {
    "DT-2": ["DT-2 DEBURRING TOOL HANDLE"],
    "DT-3": ["DT-3 DEBURING TOOL HANDLE"],
    "C-10": ["C-10 DEBURING BLADE", "C-10 DEBURRING BLADE"],
    "C-10-TIN": ["C-10 TIN DEBURRING BLADES", "C-10 TIN DEBURING BLADE"],
    "C-15": ["C-15 DEBURING BLADE"],
    "C-20": ["C-20 DEBURING BLADE"],
    "R-15": ["R-15 (S) DEBURRING BLADE"],
    "T-SD": ["T-SD HANDLES", "T-SD HANDLE"],
    "CS-30": ["CS-30 3 FLS COUNTERSINK TOOL"],
    "CSH": ["CSH HANDLE"],
    "CS-R": ["CS-R HOLDER COUNTERSINK TOOL"],
    "HEX HOLD KIT": ["HEX HOLD KIT"],
    "C BURR SET 2": ["C BURR SET 2"],
}
CP_CANONICAL_QUERY_SET = {
    normalize_space(query)
    for query_list in CP_EXACT_ITEM_QUERIES.values()
    for query in query_list
}


def clean_item_code(value: str) -> str:
    text = normalize_space(value).upper().replace(" ", "")
    text = text.replace("—", "-")
    if text.startswith("F0"):
        text = "F-" + text[2:]
    if text.startswith("FO"):
        text = "F-" + text[2:]
    if text.startswith("F-0") or text.startswith("F-"):
        return text
    if text.startswith("F") and len(text) > 1 and text[1].isdigit():
        return f"F-{text[1:]}"
    return text


def clean_numeric_unit(value: str) -> str:
    unit = normalize_space(value).upper()
    if not unit:
        return "NOS"
    return {
        "NO": "NOS",
        "NOS.": "NOS",
        "PCS": "NOS",
        "PIECES": "NOS",
    }.get(unit, unit)


def normalized_item_code_key(value: str) -> str:
    text = normalize_space(value).upper()
    text = text.replace("–", "-").replace("—", "-")
    text = re.sub(r"\s+", " ", text)
    return text


def compact_metric_token(value: str) -> str:
    return pretty_number(value).replace("0.", ".") if str(value).startswith("0.") else pretty_number(value)


def titleless_inches(value: str) -> str:
    token = normalize_space(value).replace('"', "")
    token = token.replace(".", "-")
    token = token.replace(" ", "")
    if token and not token.endswith('"'):
        token = f'{token}"'
    return token


def extract_first_metric_diameter(text: str) -> str | None:
    match = re.search(r"\b(\d+(?:\.\d+)?)\s*(?:MMDIA|MM|DIA)\b", text)
    if not match:
        match = re.search(r"-\s*(\d+(?:\.\d+)?)\s*(?:MMDIA|MM|DIA)", text)
    if not match:
        return None
    return pretty_number(match.group(1))


def extract_all_number_tokens(text: str) -> list[str]:
    return re.findall(r'\d+(?:\.\d+)?|\d+(?:/\d+)', text)


def extract_bare_size_pitch(text: str) -> tuple[str, str] | None:
    match = re.search(r"\b(\d+(?:\.\d+)?)\s*X\s*(\d+(?:\.\d+)?)\b", text)
    if not match:
        return None
    return pretty_number(match.group(1)), pretty_number(match.group(2))


def repair_et_description(text: str) -> str:
    cleaned = normalize_space(text).upper()
    cleaned = cleaned.replace("FS-IIII", "F IS-III")
    cleaned = cleaned.replace("FS-III", "F IS-III")
    cleaned = cleaned.replace("FS III", "F IS-III")
    cleaned = cleaned.replace("FS B949", "F BS 949")
    cleaned = cleaned.replace("BS F949", "F BS 949")
    cleaned = cleaned.replace("D-F371", "D-371")
    cleaned = cleaned.replace("D-F374", "D-374")
    cleaned = cleaned.replace("D-F376", "D-376")
    cleaned = cleaned.replace("SP.PLT", "SP.FLUTE")
    cleaned = cleaned.replace("SP.PLUTE", "SP.FLUTE")
    cleaned = cleaned.replace("SP.PLT.", "SP.FLUTE")
    cleaned = cleaned.replace("SPFLUTE", "SP.FLUTE")
    cleaned = cleaned.replace("SP.PT(", "SP.PT. (")
    cleaned = cleaned.replace("SP PT", "SP.PT")
    cleaned = cleaned.replace("SP.PTTIN", "SP.PT TIN")
    cleaned = cleaned.replace("SP.PT.", "SP.PT")
    cleaned = cleaned.replace("SP.FLUTE.", "SP.FLUTE")
    cleaned = cleaned.replace("FLUTTELESS", "FLUTELESS")
    cleaned = cleaned.replace("ROLL O/G", "ROLL O.G.")
    cleaned = cleaned.replace("OG.", "O.G.")
    cleaned = cleaned.replace("0.G.", "O.G.")
    cleaned = cleaned.replace("BRIG", "BRIGHT")
    cleaned = cleaned.replace("BRIGHI", "BRIGHT")
    cleaned = cleaned.replace("PROFILE TI", "PROFILE TIN")
    cleaned = cleaned.replace("PROFILE TIC", "PROFILE TICN")
    cleaned = re.sub(r"\bM(\d+(?:\.\d+)?)\s*X\s*(\d{2,3})\b", lambda match: f"M{match.group(1)} X {normalize_decimal_token(match.group(2))}", cleaned)
    cleaned = re.sub(r"\bM\s+0\.75\s+X\s+6G\b", "M6 X 0.75 6G", cleaned)
    cleaned = re.sub(r"\bM\s+0\.75\s+X\s+7G\b", "M7 X 0.75 7G", cleaned)
    cleaned = re.sub(r"\bD-371\s+M35\s+M\s+0\.75\s+6G\b", "D-371 M35 M6 X 0.75 6G", cleaned)
    cleaned = re.sub(r"\bM\s+0\.7\s+X\s+4\b", "M4 X 0.7", cleaned)
    cleaned = re.sub(r"\b6H\.?\b", "6H", cleaned)
    cleaned = re.sub(r"\bM35 X M(\d+(?:\.\d+)?)\s+(\d+(?:\.\d+)?)\b", r"M35 M\1 X \2", cleaned)
    cleaned = cleaned.replace('1/4"- -', '1/4" -')
    cleaned = cleaned.replace('5/16" "-', '5/16" -')
    cleaned = cleaned.replace("4 FL", "")
    cleaned = cleaned.replace("4FLT", "")
    cleaned = cleaned.replace("2B", "")
    cleaned = re.sub(r"(?<=[A-Z0-9])M35", " M35", cleaned)
    cleaned = re.sub(r"M35(?=[A-Z1-9])", "M35 ", cleaned)
    return normalize_space(cleaned)


def repair_rr_description(text: str) -> str:
    cleaned = normalize_space(text).upper()
    cleaned = cleaned.replace("DR JOBBER", "DR-JOBBER")
    cleaned = cleaned.replace("DR LONG", "DR.LONG")
    cleaned = cleaned.replace("DR TS", "DR-TS")
    cleaned = cleaned.replace("TYPE -A", "TYPE-A")
    cleaned = cleaned.replace("FARMING", "FORMING")
    cleaned = cleaned.replace("*", " X ")
    return normalize_space(cleaned)


def repair_totem_description(text: str) -> str:
    cleaned = normalize_space(text).upper()
    cleaned = cleaned.replace("CENTER", "CENTRE")
    cleaned = cleaned.replace("ST. SHK", "STRAIGHT SHANK")
    cleaned = cleaned.replace("ST SHK", "STRAIGHT SHANK")
    cleaned = cleaned.replace("UNIF DISC", "UNIFIED DISC")
    cleaned = cleaned.replace("1.1/4", "1-1/4")
    cleaned = cleaned.replace("1.1/2", "1-1/2")
    cleaned = re.sub(r"(\d)X(\d)", r"\1 X \2", cleaned)
    return normalize_space(cleaned)


def repair_addison_description(text: str) -> str:
    cleaned = normalize_space(text).upper()
    cleaned = cleaned.replace("TYPE -A", "TYPE-A")
    cleaned = cleaned.replace("TSTD", "TAPER SHANK TWIST DRILL")
    cleaned = cleaned.replace("T/S DRILL", "TAPER SHANK TWIST DRILL")
    cleaned = cleaned.replace("PARALLEL SHANK TWIST DRILLS", "PARALLEL SHANK TWIST DRILL")
    cleaned = cleaned.replace("PSTD (JS)", "PSTD")
    cleaned = cleaned.replace("PSTD (LONG SERIES)", "PSTD LONG SERIES")
    cleaned = cleaned.replace("PSTD (LS)", "PSTD LONG SERIES")
    cleaned = cleaned.replace("M35 HSS PSTD", "M35 HSS PSTD")
    return normalize_space(cleaned)


def repair_gnl_description(text: str) -> str:
    cleaned = normalize_space(text).upper()
    cleaned = cleaned.replace("*", " X ")
    cleaned = cleaned.replace("SPILFIRE", "SPITFIRE")
    cleaned = re.sub(r"(\d)X(\d)", r"\1 X \2", cleaned)
    return normalize_space(cleaned)


def repair_cp_description(text: str) -> str:
    cleaned = normalize_space(text).upper()
    cleaned = cleaned.replace("C-BURR", "C BURR")
    cleaned = cleaned.replace("C-BURR", "C BURR")
    cleaned = cleaned.replace("HEX HOLD KIT", "HEX HOLD KIT")
    return cleaned


def repair_pidilite_description(text: str) -> str:
    return normalize_space(text).upper()


def repair_stanley_description(text: str) -> str:
    cleaned = normalize_space(text).upper()
    cleaned = cleaned.replace(" X ", " X ")
    cleaned = re.sub(r"(\d)X(\d)", r"\1 X \2", cleaned)
    return normalize_space(cleaned)


def repair_wikus_description(text: str) -> str:
    cleaned = normalize_space(text).upper()
    cleaned = cleaned.replace(",", ".")
    cleaned = cleaned.replace(" X ", " X ")
    cleaned = re.sub(r"(\d)\s*X\s*(\d)", r"\1 X \2", cleaned)
    return normalize_space(cleaned)


def repair_description(text: str, vendor: str) -> str:
    if vendor == "ET":
        return repair_et_description(text)
    if vendor == "RR":
        return repair_rr_description(text)
    if vendor == "TOTEM":
        return repair_totem_description(text)
    if vendor == "ADDISON":
        return repair_addison_description(text)
    if vendor == "GNL":
        return repair_gnl_description(text)
    if vendor == "CP":
        return repair_cp_description(text)
    if vendor == "PIDILITE":
        return repair_pidilite_description(text)
    if vendor == "STANLEY":
        return repair_stanley_description(text)
    if vendor == "WIKUS":
        return repair_wikus_description(text)
    return normalize_space(text).upper()


def extract_metric_size_pitch(text: str) -> tuple[str, str] | None:
    match = re.search(r"\bM\s*(\d+(?:\.\d+)?)\s*X\s*(\d+(?:\.\d+)?)\b", text)
    if not match:
        return None
    return pretty_number(match.group(1)), pretty_number(match.group(2))


def extract_imperial_thread(text: str) -> tuple[str, str] | None:
    match = re.search(r'(\d+(?:-\d+/\d+|/\d+)?)\s*"?(?:\s*-\s*\d+)?\s*(UNF|UNC|BSW|BSF|BSPT|BSP|NPTF|NPT|UNEF|UNS|NPSF|NPSM)', text)
    if not match:
        return None
    return f'{match.group(1)}"', match.group(2)


def normalize_stock_name(value: str) -> str:
    text = normalize_space(value).upper().replace('"', "")
    text = text.replace("&", " AND ")
    text = re.sub(r"[^A-Z0-9./ -]+", " ", text)
    return normalize_space(text)


def rr_brand_hint(raw_description: str, item_code: str = "") -> str:
    upper = f"{item_code} {raw_description}".upper()
    if " ADD" in f" {upper} " or "ADDISON" in upper:
        return "ADDISON"
    if any(token in upper for token in ("ISO529", "GUN POINT", "COLD FORMING", "COLD FARMING", "CARBIDE JOB SS DRILL", "K-2 CARBIDE")):
        return "YG"
    if re.search(r"\b(TD|TS|TY)\d{3}\b", upper):
        return "YG"
    return "MIRANDA"


def candidate_vendor_tokens(vendor: str, raw_description: str, item_code: str = "") -> list[str]:
    if vendor == "ET":
        return [" ET", "ET"]
    if vendor == "GNL":
        return [" GNL", "GNL"]
    if vendor == "TOTEM":
        return []
    if vendor == "ADDISON":
        return [" ADDISON", "ADDISON"]
    if vendor == "STANLEY":
        return [" LENOX", "LENOX"]
    if vendor == "WIKUS":
        return [" WIKUS", "WIKUS"]
    if vendor == "PIDILITE":
        return ["STEELGRIP", " TAPE "]
    if vendor == "RR":
        brand = rr_brand_hint(raw_description, item_code)
        if brand == "ADDISON":
            return [" ADDISON", "ADDISON"]
        if brand == "YG":
            return [" YG", "YG"]
        return [" MIRANDA", "MIRANDA"]
    return []


def candidate_group_filter(vendor: str, raw_description: str = "", item_code: str = "") -> set[str] | None:
    if vendor == "RR":
        brand = rr_brand_hint(raw_description, item_code)
        if brand == "ADDISON":
            return {"ADDISON"}
        if brand == "YG":
            return {"YG"}
        return {"MIRANDA", "IT", "OTHER"}
    return {
        "ADDISON": {"ADDISON"},
        "CP": {"CP"},
        "ET": {"ET"},
        "GNL": {"GNL"},
        "TOTEM": {"TOTEM"},
        "STANLEY": {"LENOX", "STANLEY"},
        "WIKUS": {"WIKUS"},
    }.get(vendor)


def build_candidate_queries(raw_description: str, vendor: str, item_code: str = "") -> list[str]:
    text = repair_description(raw_description, vendor)
    normalized_code = normalized_item_code_key(item_code)
    text = text.replace('"', "")
    text = text.replace("SP.PT", "SPPT")
    text = text.replace("SP.FLUTE", "SPFL")
    text = text.replace("BOTTOMING", "BOT")
    text = text.replace("SECOND", "SEC")
    text = text.replace("TAPER", "TPR")
    text = text.replace("PAIR", "PAIR")
    text = normalize_space(re.sub(r"\(E SERIES\)", "", text))

    if vendor == "ADDISON":
        queries = []
        if "CD" in text or "CENTRE DRILL" in text:
            numbers = re.findall(r"\d+(?:\.\d+)?", text)
            if len(numbers) >= 2:
                queries.append(f"HSS CENTRE DRILL A  {pretty_number(numbers[0])} X {pretty_number(numbers[1])} ADDISON")
                queries.append(f"HSS CENTRE DRILL A {pretty_number(numbers[0])} X {pretty_number(numbers[1])} ADDISON")
        diameter = extract_first_metric_diameter(text)
        if "M/C REAMER" in text or "MACHINE REAMER" in text:
            if diameter:
                queries.append(f"HSS M/C REAMER {diameter} ADDISON")
                queries.append(f"HSS MACHINE REAMER {diameter} ADDISON")
        elif "TAPER SHANK TWIST DRILL" in text or "TPR SHANK TWIST DRILL" in text:
            if diameter:
                queries.append(f"HSS T/S DRILL {diameter} ADDISON")
        elif "LONG SERIES" in text:
            if diameter:
                queries.append(f"HSS LONG DRILL {diameter} ADDISON")
        elif "PSTD" in text or "PARALLEL SHANK TWIST DRILL" in text or "DRILL" in text:
            if diameter:
                if "M35 HSS" in text:
                    queries.append(f"HSS M35 DRILL {diameter} ADDISON")
                queries.append(f"HSS DRILL {diameter} ADDISON")
        queries.append(f"{text} ADDISON")
        return dedupe_queries(queries)

    if vendor == "ET":
        brand = "HSS-E" if "M35" in text or "HSS-E" in text else "HSS"
        family = "TAP"
        if "IS-IV" in text or "LONG TAP" in text:
            family = "LONG TAP"
        elif "CIR" in text:
            family = "CIR TAP"
        elif "FLUTELESS" in text or "ROLL" in text:
            family = "ROLL TAP"
        qualifiers: list[str] = []
        if "D-371" in text or "D371" in text:
            qualifiers.append("D371")
        metric = extract_metric_size_pitch(text)
        imperial = extract_imperial_thread(text)
        size_part = ""
        if metric:
            size_part = f"{metric[0]} X {metric[1]}"
        elif imperial:
            size_part = f"{imperial[0]} {imperial[1]}"
        suffix_tokens: list[str] = []
        if "6G" in text:
            suffix_tokens.append("6G")
        if "7G" in text:
            suffix_tokens.append("7G")
        if "7H" in text:
            suffix_tokens.append("7H")
        if family == "LONG TAP" and "BOT" in text:
            suffix_tokens.append("TYPE C")
        elif "BOT" in text:
            suffix_tokens.append("BOT")
        if "TPR" in text:
            suffix_tokens.append("TPR")
        if "SEC" in text:
            suffix_tokens.append("SEC")
        if "SET" in text:
            suffix_tokens.append("SET")
        if "PAIR" in text:
            suffix_tokens.append("PAIR")
        if "SPFL" in text:
            suffix_tokens.append("SPFL")
        if "SPPT" in text:
            suffix_tokens.append("SPPT")
        if family == "ROLL TAP" or "O.G." in text or "O/G" in text:
            suffix_tokens.append("O/G")
        coating_candidates: list[str] = []
        for coating in ("FUTURA", "TICN", "TIN", "GOLD"):
            if coating in text:
                coating_candidates.append(coating)
        if family == "ROLL TAP" and not coating_candidates:
            coating_candidates.append("TIN")
        if text.endswith(" TI") or text.endswith(" T") or " TI " in f" {text} ":
            coating_candidates.extend(["TIN", "TICN"])
        base = f"{brand} {family}".strip()
        if qualifiers:
            base = f"{base} {' '.join(qualifiers)}"
        queries = []
        suffix_core = " ".join(suffix_tokens)
        if size_part:
            if coating_candidates:
                for coating in coating_candidates:
                    queries.append(normalize_space(f"{base} {size_part} {suffix_core} {coating} ET"))
            else:
                queries.append(normalize_space(f"{base} {size_part} {suffix_core} ET"))
            if family == "TAP" and imperial and imperial[1] in {"NPTF", "NPT", "BSP", "BSPT"}:
                if coating_candidates:
                    for coating in coating_candidates:
                        queries.append(normalize_space(f"{brand} TAP DIN {size_part} {suffix_core} {coating} ET"))
                else:
                    queries.append(normalize_space(f"{brand} TAP DIN {size_part} {suffix_core} ET"))
        queries.append(normalize_space(f"{base} {text} ET"))
        if not coating_candidates:
            queries.append(normalize_space(f"{brand} TAP {size_part} {suffix_core} ET"))
        return dedupe_queries(queries)

    if vendor == "GNL":
        queries = []
        source = text
        code_source = f"{normalized_code} {text}"
        has_prefix = lambda prefix: bool(re.search(rf"\b{re.escape(prefix)}\d*\b", code_source))
        if has_prefix("DX") or "THIN WHEELS" in source:
            dx_match = re.search(r"(\d+(?:\.\d+)?)\s*X\s*(\d+(?:\.\d+)?)\s*X\s*(\d+(?:\.\d+)?)", source)
            if dx_match:
                diameter = max(1, round(float(dx_match.group(1)) / 25.4))
                thickness = pretty_number(dx_match.group(2)).replace(".0", "")
                queries.append(f"AG-{diameter} ({thickness}MM) XPERT QUICK CUT GNL")
        if has_prefix("FA") and "MOPS" in source:
            mop_match = re.search(r"\b(\d+(?:\.\d+)?)\s+(\d+(?:\.\d+)?)\s+(\d+(?:\.\d+)?)\s+R\d+\s+(\d{2,3})\b", source)
            if mop_match:
                queries.append(
                    f"MOP WHEEL {pretty_number(mop_match.group(1))} X {pretty_number(mop_match.group(2))} X {pretty_number(mop_match.group(3))} G{mop_match.group(4)} GNL"
                )
        if has_prefix("FA") and "FW" in source:
            flap_match = re.search(r"\b(\d+(?:\.\d+)?)\s+(\d+(?:\.\d+)?)\s+(\d+(?:\.\d+)?)\s+R\d+\s+(\d{2,3})\b", source)
            if flap_match:
                x1 = pretty_number(flap_match.group(1))
                x2 = pretty_number(flap_match.group(2))
                x3 = pretty_number(flap_match.group(3))
                flap_grit = flap_match.group(4)
                queries.append(f"FLAP WHEEL {x1} X {x2} X {x3} G{flap_grit} GNL")
                queries.append(f"FLAP WHEEL {x1} X {x2} G{flap_grit} GNL")
                if "SPITFIRE" in source:
                    queries.append(f"FLAP WHEEL {x1} X {x2} G{flap_grit} SPITFIRE GNL")
            dotted_flap_match = re.search(r"\b(\d{2,4})\.(\d{1,2})\s+(\d+(?:\.\d+)?)\s+R\d+\s+(\d{2,3})\b", source)
            if dotted_flap_match:
                x1 = pretty_number(dotted_flap_match.group(1))
                raw_width = int(dotted_flap_match.group(2))
                common_widths = (25, 40, 50, 75)
                width = min(common_widths, key=lambda candidate: abs(candidate - raw_width))
                x3 = pretty_number(dotted_flap_match.group(3))
                grit_token = dotted_flap_match.group(4)
                grit_candidates = [grit_token]
                if len(grit_token) == 3 and grit_token.startswith("7"):
                    grit_candidates.append(f"2{grit_token[1:]}")
                for grit in dict.fromkeys(grit_candidates):
                    queries.append(f"FLAP WHEEL {x1} X {width} X {x3} G{grit} GNL")
                    queries.append(f"FLAP WHEEL {x1} X {width} G{grit} GNL")
                    if "SPITFIRE" in source:
                        queries.append(f"FLAP WHEEL {x1} X {width} X {x3} G{grit} SPITFIRE GNL")
                        queries.append(f"FLAP WHEEL {x1} X {width} G{grit} SPITFIRE GNL")
        if has_prefix("FP") or " FD" in f" {source} ":
            inch_match = re.search(r'(\d+(?:/\d+)?)"', source)
            grit_match = re.search(r"\b(\d{2,3})\s+(?:BEAR|PREMIUM|FD)\b", source)
            grit = grit_match.group(1) if grit_match else ""
            if inch_match and grit:
                queries.append(f'FLAP DISK {inch_match.group(1)}" G{grit} BEAR PREMIUM GNL')
                queries.append(f'FLAP DISK {inch_match.group(1)}" G{grit} GNL')
        if has_prefix("AB") or " N BELT" in source:
            belt_match = re.search(r"\b(\d+(?:\.\d+)?)\s+(\d+(?:\.\d+)?)\s+R\d+\s+(\d{2,3})\b", source)
            if belt_match:
                a, b = pretty_number(belt_match.group(1)), pretty_number(belt_match.group(2))
                belt_grit = belt_match.group(3)
                queries.append(f"EMERY BELT {a} X {b} G{belt_grit} ALKON PREMIUM GNL")
                queries.append(f"EMERY BELT {b} X {a} G{belt_grit} ALKON PREMIUM GNL")
                queries.append(f"EMERY BELT {a} X {b} G{belt_grit} ALKON PRE GNL")
        if has_prefix("VG2"):
            queries.append("EMERY PASTE 400 GM COARSE GNL")
        if has_prefix("VG3"):
            queries.append("EMERY PASTE 400 GM MEDIUM GNL")
        if has_prefix("VG4"):
            queries.append("EMERY PASTE 400 GM FINE GNL")
        if has_prefix("NES3"):
            if "MED" in source:
                queries.append("EMERY CLOTH MEDIUM GNL")
        if has_prefix("S1"):
            queries.append("COMBINATION STONE LIST NO 109 GNL")
        if has_prefix("R340") or has_prefix("R305") or "CLOTH ROLLS" in source:
            cloth_match = re.search(r"\b(\d+)\s+(\d+)\s+BP\b", source)
            if cloth_match:
                queries.append(f"EMERY CLOTH ROLLS {cloth_match.group(1)} X {cloth_match.group(2)} BP GNL")
            else:
                roll_match = re.search(r"\b(\d+)\s+(\d+)\b", source)
                if roll_match:
                    queries.append(f"EMERY CLOTH ROLLS {pretty_number(roll_match.group(1))} X {pretty_number(roll_match.group(2))} GNL")
        if re.search(r"\bV\d", source) and "BONDED ABRASIVES" in source:
            wheel_match = re.search(r"(\d+(?:\.\d+)?)\s*[\*X]\s*(\d+(?:\.\d+)?)\s*[\*X]\s*(\d+(?:\.\d+)?)", source)
            grade_match = re.search(r"\b([A-Z]{1,3}\d+(?:/\d+)?[A-Z0-9]*)\b", source)
            if wheel_match:
                grade = grade_match.group(1) if grade_match else ""
                queries.append(normalize_space(f"GRINDING WHEEL {pretty_number(wheel_match.group(1))} X {pretty_number(wheel_match.group(2))} X {pretty_number(wheel_match.group(3))} {grade} GNL"))
        if re.search(r"\bPX[M]?\d", source):
            dims_match = re.search(r"\b(\d+(?:\.\d+)?)\s+(\d+(?:\.\d+)?)(?:\s+(\d+(?:\.\d+)?))?\s+P-(\d)", source)
            if dims_match:
                prefix = "UNIFIED DISC" if "DISC" in source else "POLISHING WHEEL"
                dim_text = f"{pretty_number(dims_match.group(1))} X {pretty_number(dims_match.group(2))}"
                if prefix == "UNIFIED DISC" and dims_match.group(3):
                    dim_text += f" X {pretty_number(dims_match.group(3))}"
                queries.append(f"{prefix} {dim_text} P{dims_match.group(4)} GNL")
        if "DOUBLE SIDED PE FOAM TAPE" in source:
            dim_match = re.search(r"(\d+(?:\.\d+)?)\s*MM\s*X\s*(\d+(?:\.\d+)?)\s*M", source)
            if dim_match:
                queries.append(f"DOUBLE SIDED PE FOAM TAPE {pretty_number(dim_match.group(1))} X {pretty_number(dim_match.group(2))} GNL")
        queries.append(f"{text} GNL")
        return dedupe_queries(queries)

    if vendor == "RR":
        queries = []
        brand = rr_brand_hint(text, normalized_code)
        if brand == "YG":
            metric = extract_metric_size_pitch(text)
            designation_match = re.search(r"\b(TD703|TD723|TD711|TS232|TY283|TD227)\b", text)
            if "CARBIDE JOB SS DRILL" in text:
                numbers = re.findall(r"\d+(?:\.\d+)?", text)
                if numbers:
                    queries.append(f"SOLID CARBIDE DRILL {pretty_number(numbers[0])} YG")
            if "K-2 CARBIDE" in text and "BALL NOSE" in text:
                numbers = re.findall(r"\d+(?:\.\d+)?", text)
                if numbers:
                    queries.append(f"SOLID CARBIDE BALL NOSE {pretty_number(numbers[0])} K2 YG")
            if "K-2 CARBIDE" in text and ("EX-LONG E/M" in text or "EX LONG E/M" in text):
                numbers = re.findall(r"\d+(?:\.\d+)?", text)
                if len(numbers) >= 4:
                    queries.append(f"SOLID CARBIDE LONG ENDMILL {pretty_number(numbers[0])} X {pretty_number(numbers[3])} K2 YG")
            if "K-2 CARBIDE" in text and "SHORT E/M" in text:
                numbers = re.findall(r"\d+(?:\.\d+)?", text)
                if numbers:
                    queries.append(f"SOLID CARBIDE ENDMILL {pretty_number(numbers[0])} K2 YG")
            if "LONG SHANK STRAIGHT FLUTE TAP" in text or "LONG STRAIGHT FLUTE TAP" in text:
                if metric:
                    queries.append(f"HSS LONG TAP {metric[0]} X {metric[1]} TYPE C YG")
            if "ISO529" in text:
                chamfer = ""
                if "#1" in text:
                    chamfer = "TPR"
                elif "#2" in text:
                    chamfer = "SEC"
                elif "#3" in text:
                    chamfer = "BOT"
                elif "SET" in text:
                    chamfer = "SET"
                elif "GUN POINT" in text:
                    chamfer = "SPPT"
                elif "SPFL" in text:
                    chamfer = "SPFL"
                if metric:
                    queries.append(normalize_space(f"HSS TAP {metric[0]} X {metric[1]} {chamfer} YG"))
            if designation_match and metric:
                designation = designation_match.group(1)
                prefix = "HSS-E ROLL TAP" if designation == "TD703" else "HSS-E TAP"
                queries.append(f"{prefix} {designation} {metric[0]} X {metric[1]} YG")
            queries.append(f"{text} YG")
            return dedupe_queries(queries)
        if "DR-JOBBER" in text:
            numbers = re.findall(r"\d+(?:\.\d+)?|(?:\d+/\d+)", text)
            if numbers:
                queries.append(f"HSS DRILL {pretty_number(numbers[0])} {brand}")
        if "DR.LONG" in text or "DR-LONG" in text:
            numbers = re.findall(r"\d+(?:\.\d+)?|(?:\d+/\d+)", text)
            if numbers:
                queries.append(f"HSS LONG DRILL {pretty_number(numbers[0])} {brand}")
        if "DR-TS" in text or "DR.TS" in text:
            numbers = re.findall(r"\d+(?:\.\d+)?", text)
            if numbers:
                queries.append(f"HSS T/S DRILL {pretty_number(numbers[0])} {brand}")
        if "END MILL" in text or "ENDMILL" in text:
            numbers = re.findall(r"\d+(?:\.\d+)?", text)
            if numbers:
                queries.append(f"HSS ENDMILL {pretty_number(numbers[0])} {brand}")
        if "HAND REAMER" in text:
            numbers = re.findall(r"\d+(?:\.\d+)?", text)
            if numbers:
                queries.append(f"HSS HAND REAMER {pretty_number(numbers[0])} {brand}")
        if "MACHINE REAMER" in text:
            numbers = re.findall(r"\d+(?:\.\d+)?", text)
            if numbers:
                queries.append(f"HSS M/C REAMER {pretty_number(numbers[0])} {brand}")
        if "TOOLBIT" in text:
            queries.append(f"HSS TOOLBIT {text} {brand}")
        if "CENTRE DRILL" in text or "TYPE-A" in text or re.search(r"\bCD\b", text):
            numbers = re.findall(r"\d+(?:\.\d+)?|(?:\d+/\d+)", text)
            if len(numbers) >= 2:
                queries.append(f"HSS CENTRE DRILL A {pretty_number(numbers[0])} X {pretty_number(numbers[1])} {brand}")
        if "GOLD" in text and queries:
            queries.extend([f"{query} GOLD" for query in queries if "GOLD" not in query])
        queries.append(f"{text} {brand}")
        return dedupe_queries(queries)

    if vendor == "TOTEM":
        queries = []
        if text.startswith("TCRB"):
            payload = text.replace("MM", " mm")
            payload = payload.replace("  ", " ")
            queries.append(normalize_space(f"{payload} TOTEM"))
        if text.startswith("CST ") or text.startswith("HST ") or text.startswith("HPT ") or " TAP " in f" {text} ":
            prefixes = ["HSS TAP"] if text.startswith("HST") else ["TAP"]
            if text.startswith("HPT"):
                prefixes = ["HSS TAP", "HSS-E TAP"]
            metric = extract_metric_size_pitch(text) or extract_bare_size_pitch(text)
            imperial = extract_imperial_thread(text)
            suffix = []
            tolerance = "6G" if "6G" in text else "7G" if "7G" in text else "7H" if "7H" in text else ""
            if tolerance:
                suffix.append(tolerance)
            if "RHCUT" in text:
                suffix.append("RH")
            if "SBF2" in text:
                suffix.append("SBF2")
            oal_match = re.search(r"OAL\s*(\d+)", text)
            if oal_match:
                suffix.append(f"OAL {oal_match.group(1)}")
            for token in ("TPR", "SEC", "BOT", "SET", "PAIR", "GOLD"):
                if token in text:
                    suffix.append(token)
            for prefix in prefixes:
                actual_prefix = prefix
                if re.search(r"\bLH\b", text) and "LHHLX" not in text:
                    actual_prefix = actual_prefix.replace("HSS ", "HSS LH ")
                if imperial:
                    queries.append(normalize_space(f"{actual_prefix} {imperial[0]} {imperial[1]} {' '.join(suffix)} TOTEM"))
                if metric:
                    queries.append(normalize_space(f"{actual_prefix} {metric[0]} X {metric[1]} {' '.join(suffix)} TOTEM"))
        if "CS DIE" in text or " DIE" in f" {text} ":
            metric = extract_metric_size_pitch(text) or extract_bare_size_pitch(text)
            die_thread_match = re.search(
                r"\bOD\s+(?P<thread>\d+(?:-\d+/\d+|/\d+)?)\s*[Xx]\s*\d+\s+(?P<form>BSW|BSF|BSPT|BSP|NPTF|NPT|UNF|UNC|UNEF|UNS|NPSF|NPSM)\b",
                text,
            )
            imperial = None if die_thread_match else extract_imperial_thread(text)
            od_match = re.search(r'(\d+(?:-\d+/\d+|/\d+)?)"?\s+OD', text)
            if die_thread_match:
                thread_token = die_thread_match.group("thread")
                if thread_token == "58":
                    thread_token = "5/8"
                thread = f'{thread_token}"'
                form = die_thread_match.group("form")
                queries.append(normalize_space(f"DIE {thread} {form} TOTEM"))
                if od_match:
                    queries.append(normalize_space(f'DIE {thread} {form} OD{od_match.group(1)}"'))
                    queries.append(normalize_space(f'DIE {thread} {form} OD {od_match.group(1)}"'))
            if imperial:
                queries.append(normalize_space(f"DIE {imperial[0]} {imperial[1]} TOTEM"))
                if od_match:
                    queries.append(normalize_space(f'DIE {imperial[0]} {imperial[1]} OD{od_match.group(1)}"'))
                    queries.append(normalize_space(f'DIE {imperial[0]} {imperial[1]} OD {od_match.group(1)}"'))
            if metric:
                queries.append(normalize_space(f"DIE {metric[0]} X {metric[1]}"))
                if metric[1].startswith("0."):
                    queries.append(normalize_space(f"DIE {metric[0]} X {metric[1][1:]}"))
                if text.startswith("HS DIE") or "CS DIE" not in text:
                    queries.append(normalize_space(f"HSS ROUND DIE {metric[0]} X {metric[1]} TOTEM"))
        if text.startswith("HS DIE"):
            metric = extract_metric_size_pitch(text)
            if metric:
                queries.append(normalize_space(f"HSS ROUND DIE {metric[0]} X {metric[1]} TOTEM"))
        if "STRAIGHT SHANK DRILL" in text:
            numbers = re.findall(r"\d+(?:\.\d+)?", text)
            if numbers:
                queries.append(f"HSS DRILL {pretty_number(numbers[0])} TOTEM")
        if "LONG DRILL" in text:
            numbers = re.findall(r"\d+(?:\.\d+)?", text)
            if numbers:
                queries.append(f"HSS LONG DRILL {pretty_number(numbers[0])} TOTEM")
        bs_match = re.search(r"BS\s*([1-8])", text)
        if "CENTER DRILL" in text or "CENTRE DRILL" in text:
            if bs_match:
                queries.append(f"HSS CENTRE DRILL BS{bs_match.group(1)} TOTEM")
            else:
                numbers = re.findall(r"\d+(?:\.\d+)?", text)
                if len(numbers) >= 2:
                    queries.append(f"HSS CENTRE DRILL A {pretty_number(numbers[0])} X {pretty_number(numbers[1])} TOTEM")
        queries.append(f"{text} TOTEM")
        return dedupe_queries(queries)

    if vendor == "CP":
        queries = []
        code_key = normalized_code
        if "TIN COATED" in text and code_key.startswith("C-10"):
            queries.extend(CP_EXACT_ITEM_QUERIES["C-10-TIN"])
        if "CS-REVOLVING HANDLE" in text or "CS REVOLVING HANDLE" in text:
            queries.extend(CP_EXACT_ITEM_QUERIES["CS-R"])
        for key, values in CP_EXACT_ITEM_QUERIES.items():
            if key in code_key or key in text:
                queries.extend(values)
        if not queries and text:
            queries.append(text)
        return dedupe_queries(queries)

    if vendor == "PIDILITE":
        queries = []
        if "STEELGRIP" in text:
            color = "BLACK" if "BLK" in text or "BLACK" in text else ""
            queries.append(normalize_space(f'STEELGRIP TAPE 3/4" {color}'))
            queries.append("STEELGRIP TAPE")
        return dedupe_queries(queries or [text])

    if vendor == "STANLEY":
        queries = []
        source = f"{normalized_code} {text}"
        series_match = re.search(r"\b(CP|CL|LXP)-0*(\d{4,5})", source)
        if series_match:
            series_map = {"CP": "CL PRO", "CL": "CL", "LXP": "LXP PRO"}
            series = series_map[series_match.group(1)]
            length = str(int(series_match.group(2)))
            dim_match = re.search(r'X\s*(\d+(?:\.\d+)?)\s+(\d+(?:\.\d+)?)\s+(\d+/\d+)', source)
            if not dim_match:
                dim_match = re.search(r'-(\d+(?:\.\d+)?)X(\d+(?:\.\d+)?)X(\d+/\d+)', source)
            if dim_match:
                width = pretty_number(dim_match.group(1))
                thickness = pretty_number(dim_match.group(2))
                if thickness.startswith("0."):
                    thickness = thickness[1:]
                tpi = dim_match.group(3)
                queries.append(f"BIMETAL BANDSAW {length} X {width} X {thickness} {tpi} TPI {series} LENOX")
        queries.append(text)
        return dedupe_queries(queries)

    if vendor == "WIKUS":
        queries = []
        source = f"{normalized_code} {text}".replace(",", ".")
        series_match = re.search(r"\b(ECOFLEX|PRIMAR|NOVOFLEX)\b", source)
        size_match = re.search(r'(\d+(?:\.\d+)?)\s*X\s*(\d+(?:\.\d+)?)\s*MM.*?(\d+-\d+)\s*TPI', source)
        length_match = re.search(r'BLADE LENGTH\s*(\d+(?:\.\d+)?)\s*MM', source)
        if series_match and size_match and length_match:
            width = pretty_number(size_match.group(1))
            thickness = pretty_number(size_match.group(2))
            if thickness.startswith("0."):
                thickness = thickness[1:]
            tpi = size_match.group(3)
            tpi_variants = [tpi]
            if "-" in tpi:
                tpi_variants.append(tpi.replace("-", "/"))
            for tpi_token in tpi_variants:
                queries.append(
                    f"BIMETAL BANDSAW {pretty_number(length_match.group(1))} X {width} X {thickness} {tpi_token} TPI {series_match.group(1)} WIKUS"
                )
        queries.append(text)
        return dedupe_queries(queries)

    return [text]


def dedupe_queries(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = normalize_space(value)
        if not normalized:
            continue
        key = normalize_stock_name(normalized)
        if key in seen:
            continue
        seen.add(key)
        result.append(normalized)
    return result


def family_filter(canonical_query: str, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    query = canonical_query.upper()

    def wants(name: str) -> bool:
        normalized = normalize_stock_name(name)
        if "BIMETAL BANDSAW" in query:
            return "BANDSAW" in normalized
        if "SOLID CARBIDE" in query:
            return "SOLID CARBIDE" in normalized or "CARBIDE" in normalized
        if "BALL NOSE" in query:
            return "BALL NOSE" in normalized
        if "POLISHING WHEEL" in query:
            return "POLISHING WHEEL" in normalized
        if "UNIFIED DISC" in query:
            return "UNIFIED DISC" in normalized
        if "MOP WHEEL" in query:
            return "MOP WHEEL" in normalized
        if "FLAP WHEEL" in query:
            return "FLAP WHEEL" in normalized
        if "FLAP DISK" in query:
            return "FLAP DISK" in normalized
        if "EMERY BELT" in query:
            return "EMERY BELT" in normalized
        if "EMERY PASTE" in query:
            return "EMERY PASTE" in normalized
        if "EMERY CLOTH ROLLS" in query:
            return "EMERY CLOTH ROLLS" in normalized
        if "EMERY CLOTH" in query:
            return "EMERY CLOTH" in normalized
        if "COMBINATION STONE" in query:
            return "COMBINATION STONE" in normalized
        if "QUICK CUT" in query:
            return "QUICK CUT" in normalized or "AG-" in normalized
        if "COUNTERSINK" in query:
            return "COUNTERSINK" in normalized
        if "DEBUR" in query or "HEX HOLD KIT" in query or "C BURR" in query:
            return any(token in normalized for token in ("DEBUR", "HEX HOLD KIT", "C BURR", "HANDLE"))
        if "STEELGRIP TAPE" in query:
            return "STEELGRIP" in normalized or "TAPE" in normalized
        if "TCRB" in query:
            return "TCRB" in normalized
        if "EMERY CLOTH ROLLS" in query:
            return "EMERY CLOTH ROLLS" in normalized
        if "CENTRE DRILL" in query:
            return "CENTRE DRILL" in normalized
        if "ENDMILL" in query or "END MILL" in query:
            return "ENDMILL" in normalized or "END MILL" in normalized
        if "HAND REAMER" in query:
            return "HAND REAMER" in normalized
        if "M/C REAMER" in query:
            return "M/C REAMER" in normalized or "MACHINE REAMER" in normalized
        if "T/S DRILL" in query:
            return "T/S DRILL" in normalized
        if "LONG DRILL" in query:
            return "LONG DRILL" in normalized or "EXTRA LONG DRILL" in normalized
        if "DRILL" in query:
            return "DRILL" in normalized and "CENTRE DRILL" not in normalized and "LONG DRILL" not in normalized
        if "ROLL TAP" in query:
            return "ROLL TAP" in normalized or "FLUTELESS" in normalized
        if "CIR TAP" in query:
            return "CIR TAP" in normalized
        if "LONG TAP" in query:
            return "LONG TAP" in normalized
        if "ROUND DIE" in query or " DIE " in f" {query} ":
            return "DIE" in normalized
        if "TAP" in query:
            return "TAP" in normalized
        return True

    filtered = [candidate for candidate in candidates if wants(candidate["name"])]
    return filtered or candidates


def extract_numeric_tokens(value: str) -> list[float]:
    tokens = re.findall(r'\d+(?:\.\d+)?(?:/\d+(?:\.\d+)?)?|(?:\.\d+)', value)
    result: list[float] = []
    for token in tokens:
        if "/" in token and token.count("/") == 1:
            left, right = token.split("/", 1)
            try:
                result.append(float(left) / float(right))
            except (TypeError, ValueError, ZeroDivisionError):
                continue
            continue
        try:
            result.append(float(token))
        except ValueError:
            continue
    return result


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
    normalized_query = normalize_stock_name(query)
    normalized_candidate = normalize_stock_name(candidate)
    sequence_score = SequenceMatcher(None, normalized_query, normalized_candidate).ratio() * 100.0
    query_tokens = set(normalized_query.split())
    candidate_tokens = set(normalized_candidate.split())
    token_score = (len(query_tokens & candidate_tokens) / len(query_tokens) * 100.0) if query_tokens else 0.0
    query_numeric_score = numeric_score(normalized_query, normalized_candidate)
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
    if normalized_query == normalized_candidate:
        return min(100.0, base_score + 20.0)
    if normalized_query in normalized_candidate or normalized_candidate in normalized_query:
        return min(100.0, base_score + 10.0)
    return min(100.0, base_score)


def classify_match_reason(query: str, candidate_name: str, score: float) -> str:
    normalized_query = normalize_stock_name(query)
    normalized_candidate = normalize_stock_name(candidate_name)
    if normalized_query and normalized_query == normalized_candidate:
        return "exact_normalized_match"
    if normalized_query and (normalized_query in normalized_candidate or normalized_candidate in normalized_query):
        return "substring_match"
    if score >= 90:
        return "high_confidence_score"
    if score >= 70:
        return "score_match"
    return "weak_score_match"


def best_query_score(queries: list[str], candidate_name: str, invoice_rate: Decimal, stock_rate: Decimal) -> tuple[float, str]:
    best_score = -1.0
    best_query = queries[0] if queries else ""
    for query in queries:
        score = similarity_score(query, candidate_name, invoice_rate, stock_rate)
        if score > best_score:
            best_score = score
            best_query = query
    return best_score, best_query


def match_preference(vendor: str, raw_description: str, candidate_name: str, best_query: str) -> int:
    raw_text = repair_description(raw_description, vendor)
    candidate = normalize_stock_name(candidate_name)
    query = normalize_stock_name(best_query)
    preference = 0

    if vendor == "ET":
        # Chamfer/style tokens are material for taps; a high fuzzy score must not
        # allow SPPT/SPFL to beat a printed BOTTOMING/BOT description.
        style_source = (
            raw_text.replace("SP.PT", "SPPT")
            .replace("SP.FLUTE", "SPFL")
            .replace("BOTTOMING", "BOT")
        )
        style_tokens = ("BOT", "SPPT", "SPFL", "TPR", "SEC", "SET", "O/G")
        for token in style_tokens:
            raw_has_token = token in style_source
            candidate_has_token = token in candidate
            if raw_has_token and candidate_has_token:
                preference += 30
            elif raw_has_token and not candidate_has_token:
                preference -= 20
            elif not raw_has_token and candidate_has_token and token in {"BOT", "SPPT", "SPFL"}:
                preference -= 10
        if ("BOTTOMING" in raw_text or "BOT" in raw_text or "BOT" in query) and "BOT" not in candidate:
            preference -= 60
        if "STI" in candidate and "STI" not in raw_text and "STI" not in query:
            preference -= 80
        return preference

    if vendor == "GNL":
        source_mentions_spitfire = "SPITFIRE" in raw_text or "SPITFIRE" in query
        candidate_mentions_spitfire = "SPITFIRE" in candidate
        if source_mentions_spitfire and candidate_mentions_spitfire:
            preference += 10
        elif not source_mentions_spitfire and candidate_mentions_spitfire:
            preference -= 5
        return preference

    if vendor != "ADDISON":
        return 0

    if "M35" in raw_text:
        if "M35" in candidate:
            preference += 20
        else:
            preference -= 5

    if "LONG SERIES" in raw_text:
        if "LONG DRILL" in candidate:
            preference += 15
        elif "DRILL" in candidate:
            preference -= 5

    if "TAPER SHANK TWIST DRILL" in raw_text:
        if "T/S DRILL" in candidate:
            preference += 15
        elif "TOOLBIT" in candidate:
            preference -= 10

    if "PSTD" in raw_text and "LONG SERIES" not in raw_text:
        if "HSS DRILL" in candidate or "M35 DRILL" in candidate:
            preference += 10

    if "M35" in query and "M35" in candidate:
        preference += 5

    return preference


def resolve_party_ledger_name(vendor: str, ledgers: list[dict[str, Any]]) -> str:
    preferred = LEDGER_NAME_HINTS[vendor]
    exact = next((row["name"] for row in ledgers if row.get("name", "").casefold() == preferred.casefold()), "")
    if exact:
        return exact
    aliases = [preferred, *LEDGER_NAME_ALIASES.get(vendor, [])]
    for alias in aliases:
        exact_alias = next((row["name"] for row in ledgers if row.get("name", "").casefold() == alias.casefold()), "")
        if exact_alias:
            return exact_alias
    upper_preferred = preferred.upper()
    for row in ledgers:
        name = normalize_space(row.get("name", ""))
        if not name:
            continue
        if all(token in name.upper() for token in upper_preferred.split() if len(token) > 2):
            return name
    for alias in aliases:
        alias_tokens = [token for token in normalize_space(alias).upper().replace(".", "").split() if len(token) > 2]
        for row in ledgers:
            name = normalize_space(row.get("name", ""))
            normalized_name = name.upper().replace(".", "")
            if name and all(token in normalized_name for token in alias_tokens):
                return name
    raise ValueError(f"Could not find supplier ledger for vendor {vendor} in Supabase master data")


def resolve_purchase_ledger_name(ledgers: list[dict[str, Any]]) -> str:
    exact = next((row["name"] for row in ledgers if row.get("name", "").casefold() == DEFAULT_PURCHASE_LEDGER.casefold()), "")
    if exact:
        return exact
    for row in ledgers:
        name = normalize_space(row.get("name", ""))
        group_name = normalize_space(row.get("group_name", ""))
        if "PURCHASE" in name.upper() and "GST" in name.upper():
            return name
        if group_name.upper() == "PURCHASE ACCOUNTS" and "PURCHASE" in name.upper():
            return name
    raise ValueError("Could not find a purchase ledger in Supabase master data")


def best_effort_party_ledger_name(vendor: str, header_data: dict[str, Any] | None = None) -> str:
    vendor_name = normalize_space((header_data or {}).get("vendor_name", ""))
    if vendor == "CP" and "CP GRAT-EX" in vendor_name.upper():
        return "CP GRAT-EX MANUFACTURING COMPANY"
    if vendor == "ADDISON" and "ADDISON" in vendor_name.upper():
        return "ADDISON & COMPANY LTD"
    return BEST_EFFORT_LEDGER_NAMES.get(vendor, vendor_name or vendor)


def best_effort_purchase_ledger_name() -> str:
    return DEFAULT_PURCHASE_LEDGER


def candidate_rows_for_item(
    vendor: str,
    raw_description: str,
    stock_rows: list[dict[str, Any]],
    item_code: str = "",
) -> list[dict[str, Any]]:
    allowed_groups = candidate_group_filter(vendor, raw_description, item_code)
    rows = stock_rows
    if allowed_groups:
        grouped = [row for row in rows if normalize_space(row.get("group_name", "")).upper() in allowed_groups]
        if grouped:
            rows = grouped
    vendor_tokens = candidate_vendor_tokens(vendor, raw_description, item_code)
    if vendor_tokens:
        filtered = []
        for row in rows:
            upper_name = f" {row.get('name', '').upper()} "
            if any(token.upper() in upper_name for token in vendor_tokens):
                filtered.append(row)
        if filtered:
            rows = filtered
    return rows


def match_item_to_live_stock(raw_item: PurchaseRawItem, vendor: str, stock_rows: list[dict[str, Any]]) -> StockMatch:
    queries = build_candidate_queries(raw_item.raw_description, vendor, raw_item.item_code)
    candidates = candidate_rows_for_item(vendor, raw_item.raw_description, stock_rows, raw_item.item_code)

    filtered_candidates: list[dict[str, Any]] = candidates
    for query in queries:
        filtered_candidates = family_filter(query, filtered_candidates)
        if filtered_candidates:
            break
    if not filtered_candidates:
        filtered_candidates = candidates or stock_rows

    best_score = -1.0
    best_selection_score = -1.0
    best_query = queries[0] if queries else raw_item.raw_description
    best_row: dict[str, Any] | None = None
    best_preference = -10_000
    candidate_scores: list[dict[str, Any]] = []
    for row in filtered_candidates:
        score, query = best_query_score(
            queries or [raw_item.raw_description],
            row.get("name", ""),
            raw_item.rate,
            decimal_value(row.get("rate", "0")),
        )
        preference = match_preference(vendor, raw_item.raw_description, row.get("name", ""), query)
        if vendor == "ET":
            selection_score = max(0.0, min(100.0, score + min(preference, 0)))
        elif vendor == "GNL":
            selection_score = max(0.0, min(100.0, score + preference))
        else:
            selection_score = score
        candidate_scores.append(
            {
                "candidate_name": row.get("name", ""),
                "score": round(score, 2),
                "selection_score": round(selection_score, 2),
                "best_query": query,
                "group_name": normalize_space(row.get("group_name", "")),
                "unit": clean_numeric_unit(row.get("unit", "")),
                "rate": float(round2(decimal_value(row.get("rate", "0")))),
                "preference": preference,
            }
        )
        if selection_score > best_selection_score or (
            abs(selection_score - best_selection_score) < 1e-9
            and (score > best_score or (abs(score - best_score) < 1e-9 and preference > best_preference))
        ):
            best_score = score
            best_selection_score = selection_score
            best_query = query
            best_row = row
            best_preference = preference

    candidate_scores.sort(
        key=lambda item: (item.get("selection_score", item["score"]), item["score"], item.get("preference", 0)),
        reverse=True,
    )
    top_candidates = candidate_scores[:3]

    if best_row is None:
        return StockMatch(
            stock_item_name=raw_item.raw_description,
            unit=raw_item.unit,
            score=0.0,
            group_name="",
            stock_rate=Decimal("0"),
            canonical_query=best_query,
            trace={
                "reason": "no_candidate_found",
                "query_count": len(queries or [raw_item.raw_description]),
                "candidate_pool_size": len(filtered_candidates),
                "top_candidates": top_candidates,
            },
        )

    rounded_score = round(best_score, 2)
    if vendor == "CP" and rounded_score < 56:
        preferred_cp_query = next(
            (normalize_space(query) for query in queries if normalize_space(query) in CP_CANONICAL_QUERY_SET),
            "",
        )
        if preferred_cp_query:
            return StockMatch(
                stock_item_name=preferred_cp_query,
                unit=clean_numeric_unit(raw_item.unit),
                score=rounded_score,
                group_name="CP",
                stock_rate=Decimal("0"),
                canonical_query=preferred_cp_query,
                trace={
                    "reason": "cp_canonical_query_fallback",
                    "query_count": len(queries or [raw_item.raw_description]),
                    "candidate_pool_size": len(filtered_candidates),
                    "top_candidates": top_candidates,
                },
            )
    return StockMatch(
        stock_item_name=best_row["name"],
        unit=clean_numeric_unit(best_row.get("unit", "")) or raw_item.unit,
        score=rounded_score,
        group_name=normalize_space(best_row.get("group_name", "")),
        stock_rate=decimal_value(best_row.get("rate", "0")),
        canonical_query=best_query,
        trace={
            "reason": classify_match_reason(best_query, best_row.get("name", ""), rounded_score),
            "query_count": len(queries or [raw_item.raw_description]),
            "candidate_pool_size": len(filtered_candidates),
            "top_candidates": top_candidates,
        },
    )


def generic_text_score(query: str, candidate: str, invoice_rate: Decimal, stock_rate: Decimal) -> float:
    """Text-forward similarity for vendor-neutral (alien) matching.

    similarity_score() is tuned for the vendor rule engine (numeric tokens weighted
    heavily, queries pre-engineered into canonical form), so it under-scores raw OCR
    text — a near-identical name can land in the 50s. Here plain string similarity
    (token-set / weighted ratio) dominates, with only a light rate-agreement nudge.
    """
    normalized_query = normalize_stock_name(query)
    normalized_candidate = normalize_stock_name(candidate)
    if not normalized_query or not normalized_candidate:
        return 0.0
    if normalized_query == normalized_candidate:
        return 100.0
    # Tokenize on non-alphanumeric boundaries so "TYPE-A" == "TYPE A" (OCR punctuation
    # should not split a shared token and depress the overlap score).
    query_tokens = set(re.findall(r"[A-Za-z0-9]+", normalized_query))
    candidate_tokens = set(re.findall(r"[A-Za-z0-9]+", normalized_candidate))
    if not query_tokens or not candidate_tokens:
        return 0.0
    # Token overlap from BOTH sides: how much of the OCR text the candidate explains AND
    # how much of the candidate the OCR text covers. Averaging both punishes a candidate
    # that merely shares one incidental common word (e.g. a colour) with the OCR line —
    # which is what makes token_set_ratio over-match. Blended 50/50 with an order-aware
    # full-string ratio so a true match (mostly-shared tokens) still scores high.
    shared = len(query_tokens & candidate_tokens)
    query_coverage = shared / len(query_tokens)
    candidate_coverage = shared / len(candidate_tokens)
    overlap_score = (query_coverage + candidate_coverage) / 2.0 * 100.0
    if fuzz is not None:
        ratio_score = float(fuzz.token_sort_ratio(normalized_query, normalized_candidate))
    else:
        ratio_score = SequenceMatcher(None, normalized_query, normalized_candidate).ratio() * 100.0
    text_score = 0.5 * ratio_score + 0.5 * overlap_score
    # Light rate-agreement nudge to break ties between textually similar names.
    if invoice_rate > 0 and stock_rate > 0:
        gap = abs(float(stock_rate - invoice_rate)) / max(float(invoice_rate), 1.0)
        if gap <= 0.15:
            text_score = min(100.0, text_score + 3.0)
    return round(text_score, 2)


def match_item_to_live_stock_generic(raw_item: PurchaseRawItem, stock_rows: list[dict[str, Any]]) -> StockMatch:
    """Vendor-neutral catalog match for alien invoices.

    Pure text similarity of the plain OCR description against every stock row — NO
    vendor query rules, group/token filters, or match_preference. Used to suggest an
    existing stock item; the caller keeps the raw OCR text when the best score is below
    threshold. Party ledger is never inferred here (alien party stays the header name).
    """
    query = raw_item.raw_description
    best_score = -1.0
    best_row: dict[str, Any] | None = None
    candidate_scores: list[dict[str, Any]] = []
    for row in stock_rows:
        score = generic_text_score(query, row.get("name", ""), raw_item.rate, decimal_value(row.get("rate", "0")))
        candidate_scores.append(
            {
                "candidate_name": row.get("name", ""),
                "score": round(score, 2),
                "group_name": normalize_space(row.get("group_name", "")),
                "unit": clean_numeric_unit(row.get("unit", "")),
                "rate": float(round2(decimal_value(row.get("rate", "0")))),
            }
        )
        if score > best_score:
            best_score = score
            best_row = row
    candidate_scores.sort(key=lambda item: item["score"], reverse=True)
    top_candidates = candidate_scores[:3]

    if best_row is None:
        return StockMatch(
            stock_item_name=raw_item.raw_description,
            unit=raw_item.unit or "NOS",
            score=0.0,
            group_name="",
            stock_rate=Decimal("0"),
            canonical_query=query,
            trace={"reason": "no_candidate_found", "candidate_pool_size": 0, "top_candidates": []},
        )
    rounded_score = round(best_score, 2)
    return StockMatch(
        stock_item_name=best_row["name"],
        unit=clean_numeric_unit(best_row.get("unit", "")) or raw_item.unit,
        score=rounded_score,
        group_name=normalize_space(best_row.get("group_name", "")),
        stock_rate=decimal_value(best_row.get("rate", "0")),
        canonical_query=query,
        trace={
            "reason": classify_match_reason(query, best_row.get("name", ""), rounded_score),
            "candidate_pool_size": len(stock_rows),
            "top_candidates": top_candidates,
        },
    )


def stock_row_for_name(stock_item_name: str, stock_rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    normalized_target = normalize_space(stock_item_name)
    if not normalized_target:
        return None

    exact = next((row for row in stock_rows if normalize_space(row.get("name", "")) == normalized_target), None)
    if exact is not None:
        return exact

    lowered_target = normalized_target.casefold()
    return next((row for row in stock_rows if normalize_space(row.get("name", "")).casefold() == lowered_target), None)


def match_item_via_purchase_matching(
    raw_item: PurchaseRawItem,
    stock_rows: list[dict[str, Any]],
    purchase_matching_exact_map: dict[str, str] | None,
) -> StockMatch | None:
    if not purchase_matching_exact_map:
        return None

    invoice_description = normalize_space(raw_item.raw_description)
    if not invoice_description:
        return None

    tally_item_name = purchase_matching_exact_map.get(invoice_description)
    if not tally_item_name:
        return None

    stock_row = stock_row_for_name(tally_item_name, stock_rows)
    return StockMatch(
        stock_item_name=tally_item_name,
        unit=clean_numeric_unit(stock_row.get("unit", "")) if stock_row else (raw_item.unit or "NOS"),
        score=100.0,
        group_name=normalize_space(stock_row.get("group_name", "")) if stock_row else "",
        stock_rate=decimal_value(stock_row.get("rate", "0")) if stock_row else Decimal("0"),
        canonical_query=raw_item.raw_description,
        trace={
            "reason": "purchase_matching_exact_lookup",
            "lookup_table": "Purchase_Matching",
            "invoice_item_description": invoice_description,
            "matched_name": tally_item_name,
            "stock_row_found": bool(stock_row),
        },
    )


def combine_ocr_items(
    vendor: str,
    description_rows: list[dict[str, Any]],
    numeric_rows: list[dict[str, Any]],
    *,
    repair_descriptions: bool = True,
    preserve_raw: bool = False,
) -> tuple[list[PurchaseRawItem], list[str]]:
    warnings: list[str] = []
    if len(description_rows) != len(numeric_rows):
        warnings.append(
            f"Description row count ({len(description_rows)}) did not match numeric row count ({len(numeric_rows)})."
        )
    row_count = min(len(description_rows), len(numeric_rows))
    items: list[PurchaseRawItem] = []
    for index in range(row_count):
        description_row = description_rows[index] or {}
        numeric_row = numeric_rows[index] or {}
        raw_text = description_row.get("raw_description", "")
        # preserve_raw keeps the plain OCR text verbatim (alien passthrough); the
        # default non-repair path upper-cases for the vendor matching algorithm.
        if preserve_raw:
            raw_description = normalize_space(raw_text)
        elif repair_descriptions:
            raw_description = repair_description(raw_text, vendor)
        else:
            raw_description = normalize_space(raw_text).upper()
        if not raw_description:
            continue
        quantity = decimal_value(numeric_row.get("quantity", 0))
        rate = decimal_value(numeric_row.get("rate", 0))
        amount = decimal_value(numeric_row.get("amount", 0))
        if quantity <= 0 and rate <= 0 and amount <= 0:
            continue
        if amount <= 0 and quantity > 0 and rate > 0:
            amount = round2(quantity * rate)
        items.append(
            PurchaseRawItem(
                item_code=clean_item_code(description_row.get("item_code", "")),
                raw_description=raw_description,
                quantity=quantity,
                amount=amount,
                rate=rate if rate > 0 else (round2(amount / quantity) if quantity > 0 else Decimal("0")),
                unit=clean_numeric_unit(numeric_row.get("unit", "")),
                discount_pct=decimal_value(numeric_row.get("discount_pct", 0)),
            )
        )
    return items, warnings


def adjust_items_to_target_subtotal(items: list[PurchaseRawItem], target_subtotal: Decimal) -> list[PurchaseRawItem]:
    if not items:
        return items
    current = round2(sum((item.amount for item in items), Decimal("0")))
    if current <= 0:
        return items

    scale = target_subtotal / current
    adjusted: list[PurchaseRawItem] = []
    running_total = Decimal("0")
    for item in items:
        new_amount = round2(item.amount * scale)
        running_total += new_amount
        adjusted.append(
            PurchaseRawItem(
                item_code=item.item_code,
                raw_description=item.raw_description,
                quantity=item.quantity,
                amount=new_amount,
                rate=round2(new_amount / item.quantity) if item.quantity > 0 else item.rate,
                unit=item.unit,
                discount_pct=item.discount_pct,
            )
        )

    delta = round2(target_subtotal - running_total)
    if adjusted and abs(delta) >= Decimal("0.01"):
        last = adjusted[-1]
        new_amount = round2(last.amount + delta)
        adjusted[-1] = PurchaseRawItem(
            item_code=last.item_code,
            raw_description=last.raw_description,
            quantity=last.quantity,
            amount=new_amount,
            rate=round2(new_amount / last.quantity) if last.quantity > 0 else last.rate,
            unit=last.unit,
            discount_pct=last.discount_pct,
        )
    return adjusted


def normalize_tax_entries(raw_entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for entry in raw_entries or []:
        ledger_name = normalize_space(entry.get("ledger_name", "")).upper()
        amount = round2(decimal_value(entry.get("amount", 0)))
        if not ledger_name or amount <= 0:
            continue
        if "IGST" in ledger_name:
            entries.append({"ledger_name": "IGST", "amount": float(amount)})
        elif "CGST" in ledger_name:
            entries.append({"ledger_name": "CGST", "amount": float(amount)})
        elif "SGST" in ledger_name:
            entries.append({"ledger_name": "SGST", "amount": float(amount)})
    return entries


def format_match_score_percent(score: float) -> str:
    rounded = round(float(score), 2)
    if abs(rounded - round(rounded)) < 0.001:
        return f"{int(round(rounded))}%"
    return f"{rounded:.2f}%"


def build_source_payload_items(
    voucher_items: list[dict[str, Any]],
    matched_items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    source_items: list[dict[str, Any]] = []
    for index, voucher_item in enumerate(voucher_items):
        matched_item = matched_items[index] if index < len(matched_items) else {}
        reason = str((matched_item.get("match_trace") or {}).get("reason", ""))
        source_items.append(
            {
                **voucher_item,
                "source": "Purchase_Matching" if reason == "purchase_matching_exact_lookup" else "Matching_Algorithem",
                "score": format_match_score_percent(float(matched_item.get("match_score", 0) or 0)),
            }
        )
    return source_items


def compute_item_discounts(
    items: list[PurchaseRawItem],
    header_data: dict[str, Any],
) -> list[Decimal]:
    """Absolute per-item discount value (in rupees) for the three invoice cases:

    1. No discount               -> 0 for every item.
    2. Per-item discount percent -> gross * pct / 100, where gross = rate * qty
       (e.g. CP / RR 'Disc. %' column; the table amount is already net).
    3. One total discount value stated outside the table (e.g. EMKAY / GNL,
       a number following 'DISCOUNT' / 'TOTAL DISCOUNT') -> distributed across
       items in proportion to each item's taxable amount:
           item_discount = (item_amount / sum_of_item_amounts) * total_discount
    """
    count = len(items)
    discounts = [Decimal("0")] * count
    if count == 0:
        return discounts

    # --- Case 2: any item carries a per-line discount percentage ---
    per_item_pct = [decimal_value(getattr(item, "discount_pct", 0) or 0) for item in items]
    if any(pct > 0 for pct in per_item_pct):
        for index, item in enumerate(items):
            gross = round2(decimal_value(item.rate) * decimal_value(item.quantity))
            discounts[index] = round2(gross * per_item_pct[index] / Decimal("100"))
        return discounts

    # --- Case 3: a single total discount value stated outside the table ---
    total_discount = round2(decimal_value(header_data.get("discount_amount", 0) or 0))
    if total_discount <= 0:
        # Fall back to a total discount percentage if that is all we have.
        disc_pct = decimal_value(header_data.get("discount_percent", 0) or 0)
        if disc_pct > 0:
            base = round2(sum((decimal_value(item.amount) for item in items), Decimal("0")))
            total_discount = round2(base * disc_pct / Decimal("100"))
    if total_discount > 0:
        grand_total = round2(sum((decimal_value(item.amount) for item in items), Decimal("0")))
        if grand_total > 0:
            running = Decimal("0")
            for index, item in enumerate(items):
                share = round2(decimal_value(item.amount) / grand_total * total_discount)
                discounts[index] = share
                running += share
            # Push any rounding remainder onto the last item so the parts sum exactly.
            remainder = round2(total_discount - running)
            if abs(remainder) >= Decimal("0.01"):
                discounts[-1] = round2(discounts[-1] + remainder)

    # --- Case 1 falls through as all zeros ---
    return discounts


def build_voucher_payload(
    company_name: str,
    vendor: str,
    header_data: dict[str, Any],
    raw_items: list[PurchaseRawItem],
    stock_rows: list[dict[str, Any]],
    ledgers: list[dict[str, Any]],
    min_score: float,
    purchase_matching_exact_map: dict[str, str] | None = None,
) -> dict[str, Any]:
    tax_entries = normalize_tax_entries(header_data.get("tax_entries", []))
    tax_total = round2(sum((decimal_value(entry["amount"]) for entry in tax_entries), Decimal("0")))
    invoice_total = round2(decimal_value(header_data.get("invoice_total", 0)))

    # A discount is present if any line carries a % or a single total discount is
    # stated in the header. When discounted, we make each item amount NET below and
    # recompute GST on that net, so we must NOT anchor item amounts to the printed
    # invoice_total (that would re-scale the rate and hide the discount).
    has_discount = (
        any(decimal_value(getattr(item, "discount_pct", 0) or 0) > 0 for item in raw_items)
        or round2(decimal_value(header_data.get("discount_amount", 0) or 0)) != 0
        or decimal_value(header_data.get("discount_percent", 0) or 0) > 0
    )
    # The document's pre-tax taxable base, used to infer each tax line's effective
    # rate so GST can be recomputed on our net subtotal.
    doc_taxable = round2(invoice_total - tax_total) if invoice_total > 0 else Decimal("0")

    items = raw_items
    if not has_discount and invoice_total > 0 and tax_total >= 0:
        target_subtotal = round2(invoice_total - tax_total)
        items = adjust_items_to_target_subtotal(items, target_subtotal)

    matched_items: list[dict[str, Any]] = []
    weak_matches: list[dict[str, Any]] = []
    for raw_item in items:
        stock_match = match_item_via_purchase_matching(raw_item, stock_rows, purchase_matching_exact_map)
        if stock_match is None:
            stock_match = match_item_to_live_stock(raw_item, vendor, stock_rows)
        if stock_match.score < min_score:
            weak_matches.append(
                {
                    "item_code": raw_item.item_code,
                    "raw_description": raw_item.raw_description,
                    "canonical_query": stock_match.canonical_query,
                    "matched_name": stock_match.stock_item_name,
                    "score": stock_match.score,
                    "match_trace": stock_match.trace,
                }
            )
        matched_items.append(
            {
                "item_code": raw_item.item_code,
                "raw_description": raw_item.raw_description,
                "canonical_query": stock_match.canonical_query,
                "stock_item_name": stock_match.stock_item_name,
                "quantity": float(raw_item.quantity),
                "rate": float(round2(raw_item.rate)),
                "amount": float(round2(raw_item.amount)),
                "unit": stock_match.unit or raw_item.unit or "NOS",
                "match_score": stock_match.score,
                "match_group": stock_match.group_name,
                "match_trace": stock_match.trace,
            }
        )

    party_name = resolve_party_ledger_name(vendor, ledgers)
    inventory_ledger_name = resolve_purchase_ledger_name(ledgers)

    voucher_number = normalize_space(header_data.get("invoice_number", ""))
    voucher_date = parse_date_to_iso(str(header_data.get("invoice_date", "")))
    item_discounts = compute_item_discounts(items, header_data)
    discount_total = round2(sum(item_discounts, Decimal("0")))

    voucher_items = []
    net_amounts: list[Decimal] = []
    for index, item in enumerate(matched_items):
        discount_value = item_discounts[index] if index < len(item_discounts) else Decimal("0")
        # Derived effective percent: rupee discount over gross (rate x qty). Recovers
        # the original column percent in the per-line case and yields a sensible
        # effective percent when a single total was distributed pro-rata.
        gross = round2(decimal_value(item["rate"]) * decimal_value(item["quantity"]))
        discount_pct = round2(discount_value / gross * Decimal("100")) if gross > 0 else Decimal("0")
        # Make the amount NET. Detect whether the parsed amount is already net
        # (leave it), still gross (deduct the discount), or neither (trust parsed).
        parsed_amount = round2(decimal_value(item["amount"]))
        if discount_value <= 0:
            net_amount = parsed_amount
        else:
            expected_net = round2(gross - discount_value)
            tol = max(Decimal("1"), round2(gross * Decimal("0.01")))
            if abs(parsed_amount - expected_net) <= tol:
                net_amount = parsed_amount            # already net -> leave
            elif abs(parsed_amount - gross) <= tol:
                net_amount = expected_net             # still gross -> deduct
            else:
                net_amount = parsed_amount            # neither -> trust parsed
        net_amounts.append(net_amount)
        voucher_items.append(
            {
                "stock_item_name": item["stock_item_name"],
                "quantity": item["quantity"],
                "rate": item["rate"],
                "amount": float(net_amount),
                "unit": item["unit"],
                "discount": float(discount_value),
                "discount_pct": float(discount_pct),
                "godown_name": "Main Location",
            }
        )

    # Net subtotal, then GST recomputed on the net (GST is charged after discount).
    subtotal = round2(sum(net_amounts, Decimal("0")))
    doc_tax_total = round2(sum((decimal_value(e["amount"]) for e in tax_entries), Decimal("0")))

    # Supplier-recovered freight/courier/cartage. The OCR usually orphans the amount
    # from its label, but it is reliably the gap between the document's pre-tax
    # taxable base (grand total - tax, both from the GST summary) and the items'
    # subtotal. When a freight/courier label is present and that gap is sane, book it
    # to CARTAGE INWARD and include it in the assessable value, so the single GST
    # line (charged on items + freight) reconciles. Falls back to a same-line amount
    # if one was captured; otherwise no charge is invented.
    charge_amount = Decimal("0")
    if header_data.get("charges"):
        gap = round2(doc_taxable - subtotal) if doc_taxable > 0 else Decimal("0")
        same_line = round2(
            sum(
                (decimal_value(c.get("amount")) for c in header_data["charges"]
                 if isinstance(c, dict) and c.get("amount")),
                Decimal("0"),
            )
        )
        cap = round2(subtotal * Decimal("0.5"))
        if Decimal("1") <= gap <= cap:
            charge_amount = gap
        elif Decimal("1") <= same_line <= cap:
            charge_amount = same_line
    taxable_base = round2(subtotal + charge_amount)

    # The document's tax line is often unreliable (some vendors print the *rate*,
    # e.g. "18", instead of the rupee amount). When the printed grand total is
    # available we trust (grand_total - taxable_base) as the real tax and split it
    # across the document's tax ledgers (CGST/SGST split equally; a lone IGST takes
    # it all). taxable_base includes any freight booked above so GST stays on
    # items + freight.
    implied_tax = round2(invoice_total - taxable_base) if invoice_total > 0 else Decimal("-1")
    if (
        tax_entries
        and implied_tax > 0
        and (doc_tax_total <= 0 or abs(doc_tax_total - implied_tax) > implied_tax * Decimal("0.2"))
    ):
        count = len(tax_entries)
        per = round2(implied_tax / count)
        recomputed_tax_entries = []
        running = Decimal("0")
        for idx, entry in enumerate(tax_entries):
            amt = per if idx < count - 1 else round2(implied_tax - running)
            running += amt
            recomputed_tax_entries.append({"ledger_name": entry["ledger_name"], "amount": float(amt)})
    elif has_discount and doc_taxable > 0:
        # Printed total missing/uninformative: keep each tax line's document rate
        # (inferred from the doc's taxable base) and scale to our taxable base.
        scale = taxable_base / doc_taxable
        recomputed_tax_entries = [
            {"ledger_name": e["ledger_name"], "amount": float(round2(decimal_value(e["amount"]) * scale))}
            for e in tax_entries
        ]
    else:
        recomputed_tax_entries = [
            {"ledger_name": e["ledger_name"], "amount": float(round2(decimal_value(e["amount"])))}
            for e in tax_entries
        ]
    tax_total = round2(sum((decimal_value(e["amount"]) for e in recomputed_tax_entries), Decimal("0")))
    invoice_total = round2(taxable_base + tax_total)

    ledger_entries = [
        {
            "ledger_name": party_name,
            "amount": float(invoice_total),
            "is_deemed_positive": False,
        },
        {
            "ledger_name": inventory_ledger_name,
            "amount": float(subtotal),
            "is_deemed_positive": True,
        },
    ]
    if charge_amount > 0:
        ledger_entries.append(
            {
                "ledger_name": FREIGHT_LEDGER,
                "amount": float(charge_amount),
                "is_deemed_positive": True,
            }
        )
    for entry in recomputed_tax_entries:
        ledger_entries.append(
            {
                "ledger_name": entry["ledger_name"],
                "amount": float(round2(decimal_value(entry["amount"]))),
                "is_deemed_positive": True,
            }
        )

    voucher_payload = {
        "party_name": party_name,
        "date": voucher_date,
        "voucher_number": voucher_number,
        "reference": voucher_number,
        "narration": f"Purchase invoice {voucher_number}",
        "discount_total": float(discount_total),
        "voucher_type": "Purchase",
        "inventory_ledger_name": inventory_ledger_name,
        "ledger_entries": ledger_entries,
        "items": voucher_items,
    }
    return {
        "company_name": company_name,
        "vendor": vendor,
        "party_name": party_name,
        "voucher_payload": voucher_payload,
        "source_payload": {
            "items": build_source_payload_items(voucher_items, matched_items),
        },
        "matched_items": matched_items,
        "weak_matches": weak_matches,
        "subtotal": float(subtotal),
        "invoice_total": float(invoice_total),
        "tax_total": float(tax_total),
    }


def _alien_invoice_date(value: Any) -> str:
    # Aliens must still produce a backend-valid date. parse_date_to_iso returns "" for
    # an empty input and can raise on exotic tokens; in either case fall back to today
    # (a visible placeholder the user corrects) so the voucher always enqueues.
    try:
        parsed = parse_date_to_iso(str(value or ""))
    except Exception:  # noqa: BLE001 - never let an odd date format break the passthrough
        parsed = ""
    return parsed or date.today().isoformat()


def build_alien_voucher_payload(
    company_name: str,
    header_data: dict[str, Any],
    raw_items: list[PurchaseRawItem],
    stock_rows: list[dict[str, Any]] | None = None,
    min_score: float = 0.0,
    purchase_matching_exact_map: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Voucher for an unrecognized ("alien") supplier.

    No vendor rule engine. When a live stock catalog is supplied, each plain OCR
    description is matched VENDOR-NEUTRALLY against it (exact prior match, then pure
    fuzzy similarity — no vendor query rules, group filters, or party assumptions); a
    match at/above ``min_score`` becomes the canonical stock_item_name, otherwise the
    raw OCR text is kept. With no catalog it is a plain passthrough. Party + purchase
    ledgers fall back to header/default names (never a vendor ledger). Flagged
    ``alien: True`` so the app marks it for review (the user edits) before push.
    """
    # Party + date must still satisfy the backend's voucher-level guards (which are not
    # relaxed for aliens), so fall back to visible placeholders the user corrects in the
    # app rather than emitting empty values that would 400/500 the enqueue.
    party_name = best_effort_party_ledger_name("", header_data) or "Unknown Supplier"
    inventory_ledger_name = best_effort_purchase_ledger_name()
    tax_entries = normalize_tax_entries(header_data.get("tax_entries", []))
    stock_rows = stock_rows or []

    voucher_items: list[dict[str, Any]] = []
    matched_items: list[dict[str, Any]] = []
    weak_matches: list[dict[str, Any]] = []
    for item in raw_items:
        amount = round2(decimal_value(item.amount))
        rate = round2(decimal_value(item.rate))
        if rate <= 0 and item.quantity > 0 and amount > 0:
            rate = round2(amount / decimal_value(item.quantity))

        # Vendor-neutral catalog match: exact prior match first, then pure fuzzy. A
        # below-threshold match leaves the plain OCR text in place for the user to edit.
        stock_item_name = item.raw_description
        unit = item.unit or "NOS"
        match_score = 0.0
        match_trace: dict[str, Any] | None = None
        matched = False
        if stock_rows:
            stock_match = match_item_via_purchase_matching(item, stock_rows, purchase_matching_exact_map)
            if stock_match is None:
                stock_match = match_item_to_live_stock_generic(item, stock_rows)
            match_score = stock_match.score
            match_trace = stock_match.trace
            if stock_match.score >= min_score:
                stock_item_name = stock_match.stock_item_name
                unit = stock_match.unit or unit
                matched = True
            else:
                weak_matches.append(
                    {
                        "raw_description": item.raw_description,
                        "matched_name": stock_match.stock_item_name,
                        "score": stock_match.score,
                        "match_trace": stock_match.trace,
                    }
                )
        matched_items.append(
            {
                "raw_description": item.raw_description,
                "stock_item_name": stock_item_name,
                "quantity": float(item.quantity),
                "rate": float(rate),
                "amount": float(amount),
                "unit": unit,
                "match_score": match_score,
                "matched": matched,
                "match_trace": match_trace,
            }
        )
        voucher_items.append(
            {
                "stock_item_name": stock_item_name,
                "quantity": float(item.quantity),
                "rate": float(rate),
                "amount": float(amount),
                "unit": unit,
                "discount": 0.0,
                "discount_pct": 0.0,
                "godown_name": "Main Location",
            }
        )

    subtotal = round2(sum((decimal_value(it["amount"]) for it in voucher_items), Decimal("0")))
    tax_total = round2(sum((decimal_value(e["amount"]) for e in tax_entries), Decimal("0")))
    invoice_total = round2(decimal_value(header_data.get("invoice_total", 0)))
    if invoice_total <= 0:
        invoice_total = round2(subtotal + tax_total)

    ledger_entries = [
        {"ledger_name": party_name, "amount": float(invoice_total), "is_deemed_positive": False},
        {"ledger_name": inventory_ledger_name, "amount": float(subtotal), "is_deemed_positive": True},
    ]
    for entry in tax_entries:
        ledger_entries.append(
            {
                "ledger_name": entry["ledger_name"],
                "amount": float(round2(decimal_value(entry["amount"]))),
                "is_deemed_positive": True,
            }
        )

    voucher_number = normalize_space(header_data.get("invoice_number", ""))
    voucher_date = _alien_invoice_date(header_data.get("invoice_date", ""))
    voucher_payload = {
        "party_name": party_name,
        "date": voucher_date,
        "voucher_number": voucher_number,
        "reference": voucher_number,
        "narration": (f"Purchase invoice {voucher_number}").strip(),
        "discount_total": 0.0,
        "voucher_type": "Purchase",
        "inventory_ledger_name": inventory_ledger_name,
        "alien": True,
        "ledger_entries": ledger_entries,
        "items": voucher_items,
    }
    source_items = [
        {
            **it,
            "source": "Alien_Matched" if mi["matched"] else "Alien_Passthrough",
            "score": format_match_score_percent(mi["match_score"]) if mi["match_score"] else "",
        }
        for it, mi in zip(voucher_items, matched_items)
    ]
    return {
        "company_name": company_name,
        "vendor": "",
        "party_name": party_name,
        "voucher_payload": voucher_payload,
        "source_payload": {"items": source_items, "alien": True},
        "matched_items": matched_items,
        "weak_matches": weak_matches,
        "subtotal": float(subtotal),
        "invoice_total": float(invoice_total),
        "tax_total": float(tax_total),
    }


def build_purchase_queue_payload(
    company_name: str,
    voucher_payload: dict[str, Any],
    source_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = {
        "company_name": company_name,
        "voucher_payload": voucher_payload,
    }
    if isinstance(source_payload, dict):
        payload["source_payload"] = source_payload
    return payload
