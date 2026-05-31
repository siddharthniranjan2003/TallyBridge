import re


def normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()
