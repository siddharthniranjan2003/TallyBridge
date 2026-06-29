"""Memory regression test for the sync payload byte-size estimation.

Context: pressing "Sync All Now" forces a FULL sync that pulls the entire
financial year of vouchers into memory. Before this fix, the engine then
`json.dumps`-ed that whole voucher list 2-3 extra times *just to log byte
sizes* (build_section_metric, build_payload_section_sizes, and a whole-payload
estimate). Each of those transiently allocated a full JSON copy of every
voucher — a sequence of memory spikes that pushes a 4GB / i3 machine into swap
and OOM-kills the engine, so the first sync never completes.

This test proves the measurement path no longer materializes a full JSON copy
of a large voucher list, while still returning a byte estimate that is accurate
enough for logging.

Runnable two ways:
    python3 tests/test_payload_memory.py      # prints a report, exits non-zero on failure
    pytest tests/test_payload_memory.py
"""

import json
import os
import sys
import tracemalloc
from unittest.mock import MagicMock

# sync_main pulls in requests/xmltodict transitively; the functions under test
# touch neither, so stub them so the test runs without the bundled engine deps.
sys.modules.setdefault("requests", MagicMock())
sys.modules.setdefault("xmltodict", MagicMock())

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sync_main  # noqa: E402


def make_voucher(i: int) -> dict:
    """A voucher shaped like Tally output: nested items + ledger entries with
    bill allocations. ~1-2 KB serialized — realistic for an active SME."""
    item_count = 3 + (i % 4)
    return {
        "date": f"2025{(i % 12) + 1:02d}{(i % 28) + 1:02d}",
        "voucher_number": f"SAL/{i}",
        "tally_guid": f"abc123-{i:08d}-0000000000000000",
        "voucher_type": "Sales" if i % 2 else "Purchase",
        "party_name": f"Party Number {i % 500} Trading Co. Pvt Ltd",
        "narration": "Being goods sold vide invoice " * (1 + i % 3),
        "amount": round(1000 + (i % 9999) * 1.5, 2),
        "items": [
            {
                "name": f"Stock Item {(i + j) % 800}",
                "quantity": (j + 1) * 2,
                "rate": 12.5 * (j + 1),
                "amount": 25.0 * (j + 1) * (j + 1),
                "godown": "Main Location",
                "hsn": f"{1000 + ((i + j) % 8000)}",
            }
            for j in range(item_count)
        ],
        "ledger_entries": [
            {
                "ledger_name": f"Ledger {(i + k) % 300}",
                "amount": round((k + 1) * 100.25, 2),
                "is_debit": k % 2 == 0,
                "bill_allocations": [
                    {"name": f"Bill/{i}/{k}", "amount": (k + 1) * 50.0, "bill_type": "New Ref"}
                ],
            }
            for k in range(2 + (i % 3))
        ],
    }


def peak_bytes_of(fn) -> int:
    tracemalloc.reset_peak()
    fn()
    return tracemalloc.get_traced_memory()[1]


def run(n: int = 30000) -> None:
    vouchers = [make_voucher(i) for i in range(n)]

    tracemalloc.start()

    # Old behavior: serialize the entire list (what the metric path used to do).
    holder = {}

    def full_serialize():
        holder["bytes"] = len(
            json.dumps(vouchers, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
        )

    full_peak = peak_bytes_of(full_serialize)
    true_bytes = holder["bytes"]

    # New behavior: the production estimate function (sampled for large lists).
    est_holder = {}

    def sampled_estimate():
        est_holder["bytes"] = sync_main.estimate_payload_bytes(vouchers)

    sampled_peak = peak_bytes_of(sampled_estimate)
    estimated_bytes = est_holder["bytes"]

    tracemalloc.stop()

    accuracy = estimated_bytes / true_bytes if true_bytes else 0
    peak_ratio = sampled_peak / full_peak if full_peak else 0

    print(f"vouchers:            {n:,}")
    print(f"true serialized size: {true_bytes/1e6:.1f} MB")
    print(f"estimated size:       {estimated_bytes/1e6:.1f} MB  (accuracy {accuracy*100:.1f}% of true)")
    print(f"peak alloc  full serialize: {full_peak/1e6:.1f} MB")
    print(f"peak alloc  sampled estimate: {sampled_peak/1e6:.2f} MB")
    print(f"peak reduction: {(1 - peak_ratio)*100:.1f}%  (sampled is {peak_ratio*100:.2f}% of full)")

    # The estimate must stay useful for logging (within 20% of the real size).
    assert 0.80 <= accuracy <= 1.20, f"estimate off by too much: {accuracy:.3f}"
    # The whole point: the estimate must NOT allocate a full JSON copy of the
    # list. Sampled peak should be a small fraction of the full-serialize peak.
    assert peak_ratio < 0.10, f"sampled estimate still allocates too much: {peak_ratio:.3f}"
    print("\nPASS: byte estimation is accurate and no longer materializes the full voucher list.")


def test_estimate_is_cheap_and_accurate():
    run(n=20000)


if __name__ == "__main__":
    try:
        run()
    except AssertionError as exc:
        print(f"\nFAIL: {exc}")
        sys.exit(1)
