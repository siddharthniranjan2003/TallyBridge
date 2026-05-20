from __future__ import annotations

from lib.text import normalize_space


def detect_vendor(value: str) -> str:
    upper = normalize_space(value).upper()
    if "ADDISON" in upper:
        return "ADDISON"
    if "EMKAY" in upper:
        return "ET"
    if "GRINDWELL" in upper or "NORTON" in upper:
        return "GNL"
    if "R R TOOLS" in upper or "R.R.TOOLS" in upper or "R.R. TOOLS" in upper:
        return "RR"
    if "FORBES" in upper or "TOTEM" in upper:
        return "TOTEM"
    if "CP GRAT-EX" in upper or "GRAT-EX" in upper:
        return "CP"
    if "PIDILITE" in upper or "STEELGRIP" in upper:
        return "PIDILITE"
    if "STANLEY" in upper or "BLACK & DECKER" in upper or "LENOX" in upper:
        return "STANLEY"
    if "WIKUS" in upper:
        return "WIKUS"
    raise ValueError(f"Could not determine purchase vendor from OCR header: {value}")
