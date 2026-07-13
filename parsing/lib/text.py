import re


def normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def audit_trail_key(text: str) -> str:
    """Canonical lookup key for the Audit_Trail_Purchase raw-OCR map.

    Deliberately case-insensitive, unlike the Purchase_Matching key: a line's
    raw_description is upper-cased on the recognized-vendor path but case-preserved
    on the alien path, so the same invoice line would otherwise miss the trail it
    just taught. The app must canonicalize identically before writing a row.
    """
    return normalize_space(text).casefold()
