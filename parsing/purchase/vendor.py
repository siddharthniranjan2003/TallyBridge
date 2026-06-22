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
    if "PIDIL" in upper or "STEELGRIP" in upper:
        # "PIDIL" prefix tolerates OCR variants like "PIDILLITE" (double-L).
        return "PIDILITE"
    if "STANLEY" in upper or "BLACK & DECKER" in upper or "LENOX" in upper:
        return "STANLEY"
    if "WIKUS" in upper:
        return "WIKUS"
    # Unrecognized ("alien") supplier: return an empty code instead of raising so the
    # pipeline can fall back to a generic, rule-engine-free passthrough that returns
    # the items exactly as OCR'd. detect_vendor("") == "" is the alien sentinel.
    return ""
