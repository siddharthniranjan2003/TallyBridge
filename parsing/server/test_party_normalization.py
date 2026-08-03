#!/usr/bin/env python3
"""
Standalone tester for normalize_party_name. Pure string work -- no network.

Covers the abbreviation expansion added for 'P. T ENT.', which was rejected at
54.56 against a 78 threshold despite being a 90% string match for the real
customer 'P. T. ENTERPRISES'.

Why expansion rather than a scoring change: strong_party_tokens drops tokens
under 3 characters, so P and T vanish and only the generic ENTERPRISES survives
-- nothing identifying is left to score on. 63 of the client's 1,490 customers
are named this way (A K TOOLS, S R ENTERPRISES, OM TRADERS...). Two alternatives
were measured and rejected:

  - Adding SALES to PARTY_GENERIC_TOKENS changes nothing: both penalties are
    gated on strong_tokens being non-empty, so a probe of purely generic words
    gets NO penalty and raw fuzzy still clears 78. The matcher is most
    permissive exactly when it knows least.
  - Requiring a strong token would reject all 63 of those customers, including
    the one it was meant to fix.

Measured on the client's 1,490 real party names: abbreviated reads matched
1459/1490 -> 1488/1490, with clean reads unchanged at 1489/1490.

The case worth staring at is SUNRISE. The client has BOTH 'SUNRISE ENTERPRISE'
and 'SUNRISE ENTERPRISES', so an abbreviated 'SUNRISE ENT.' is genuinely
ambiguous -- the abbreviation erases the only character that separates them.
Baseline resolved it correctly by luck; this resolves it to the other one by
luck. Collapsing singular into plural to "fix" that is a trap: it makes a
PERFECTLY READ 'SUNRISE ENTERPRISES' resolve to 'SUNRISE ENTERPRISE', breaking a
clean read to patch an unresolvable one. Deliberately not done.

Usage:
    python server/test_party_normalization.py
"""
from __future__ import annotations

import sys
from pathlib import Path

# Make the top-level parsing/ modules importable when run from anywhere.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server import handler  # noqa: E402

FAILURES: list[str] = []


def check(label: str, actual: object, expected: object) -> None:
    if actual == expected:
        print(f"  PASS  {label}")
        return
    FAILURES.append(label)
    print(f"  FAIL  {label}\n          expected: {expected!r}\n          actual:   {actual!r}")


norm = handler.normalize_party_name

# ── The expansions ─────────────────────────────────────────────────────────────
print("abbreviation expansion")
check("ENT. -> ENTERPRISES", norm("P. T ENT."), "P T ENTERPRISES")
check("ENT (no dot) -> ENTERPRISES", norm("S R ENT"), "S R ENTERPRISES")
check("ENTP -> ENTERPRISES", norm("A K ENTP."), "A K ENTERPRISES")
check("ENTR -> ENTERPRISES", norm("A K ENTR."), "A K ENTERPRISES")
check("ENTPR -> ENTERPRISES", norm("A K ENTPR."), "A K ENTERPRISES")
check("AGY -> AGENCY", norm("MUNDHARA AGY"), "MUNDHARA AGENCY")
check("CORP. -> CORPORATION", norm("BHARAT SALES CORP."), "BHARAT SALES CORPORATION")

print()
print("the probe and the ledger name converge")
# The whole point: an abbreviated read and the real ledger entry must normalize
# to the same string, whichever side carries the abbreviation.
check("P. T ENT. == P. T. ENTERPRISES", norm("P. T ENT."), norm("P. T. ENTERPRISES"))
check("BHARAT SALES CORP. == ...CORPORATION", norm("BHARAT SALES CORP."), norm("BHARAT SALES CORPORATION"))
check("MUNDHARA AGY == MUNDHARA AGENCIES", norm("MUNDHARA AGY"), norm("MUNDHARA AGENCIES"))

print()
print("expansions must not fire inside longer words")
check("ENTERPRISES is left alone", norm("MOHIT ENTERPRISES"), "MOHIT ENTERPRISES")
check("ENGINEERING is not an ENT", norm("AM TECH ENGINEERING"), "AM TECH ENGINEERING")
check("CORPORATION is left alone", norm("GOYAL SALES CORPORATION"), "GOYAL SALES CORPORATION")
check("CORPORATE is not a CORP", norm("EXCEL CORPORATE SERVICES"), "EXCEL CORPORATE SERVICES")
check("ENTRY is not an ENTR", norm("ENTRY POINT TOOLS"), "ENTRY POINT TOOLS")

print()
print("pre-existing rules still hold")
check("H/W -> HARDWARE", norm("BALAJI H/W AGENCIES"), "BALAJI HARDWARE AGENCY")
check("HW -> HARDWARE", norm("SHREE SHYAM HW"), "SHREE SHYAM HARDWARE")
check("HARDW -> HARDWARE", norm("SHREE HARDW STORE"), "SHREE HARDWARE STORE")
check("AGENCIES -> AGENCY", norm("CPM AGENCIES"), "CPM AGENCY")
check("& -> AND", norm("A & B TOOLS"), "A AND B TOOLS")
check("punctuation stripped, spaces collapsed", norm("  S.M.   CORPORATION  "), "S M CORPORATION")

print()
print("the singular/plural split is NOT collapsed (deliberate)")
# Collapsing these would make a perfectly-read plural resolve to the singular.
check(
    "ENTERPRISE and ENTERPRISES stay distinct",
    norm("SUNRISE ENTERPRISE") != norm("SUNRISE ENTERPRISES"),
    True,
)


# ── End to end through the scorer ──────────────────────────────────────────────
print()
print("scoring, against a stand-in for the real candidate list")
CANDIDATES = [
    "P. T. ENTERPRISES",
    "S R ENTERPRISES",
    "BHARAT SALES CORP.",
    "MUNDHARA AGENCIES",
    "BALAJI H/W AGENCIES",
    "SUNRISE ENTERPRISE",
    "SUNRISE ENTERPRISES",
    "AMBEY TOOLS",
]


def best(probe: str) -> tuple[str | None, float]:
    ranked = handler.rank_party_candidates(probe, CANDIDATES)
    if not ranked:
        return None, 0.0
    top = ranked[0]
    ok = top["score"] >= handler.PARTY_MATCH_THRESHOLD or top["strong_ratio"] >= 0.99
    return (top["party_name"] if ok else None), top["score"]


check("P. T ENT. now matches (was rejected at 54.56)", best("P. T ENT.")[0], "P. T. ENTERPRISES")
check("clears the threshold with room", best("P. T ENT.")[1] >= handler.PARTY_MATCH_THRESHOLD, True)
check("S R ENT matches", best("S R ENT")[0], "S R ENTERPRISES")
check("CORP. read as CORPORATION matches", best("BHARAT SALES CORPORATION")[0], "BHARAT SALES CORP.")
check("MUNDHRA (transposed) still matches", best("MUNDHRA AGENCIES")[0], "MUNDHARA AGENCIES")

print()
print("clean reads are not disturbed")
for name in CANDIDATES:
    check(f"{name!r} still self-matches", best(name)[0], name)

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
    sys.exit(1)
print("all party normalization checks passed")
