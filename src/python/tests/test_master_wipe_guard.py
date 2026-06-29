"""Unit tests for the master-section wipe guard (review finding tally-dep-01 / C2).

A degraded TallyPrime (busy, modal/license popup, no company loaded) can return
0 groups/ledgers/stock that look like a genuinely empty company. Pushing that
empty set as the authoritative snapshot wipes the cloud chart of accounts /
stock master. These tests pin the decision logic that refuses such a push.

Run: python3 tests/test_master_wipe_guard.py   (or: pytest tests/test_master_wipe_guard.py)
"""

import os
import sys
from unittest.mock import MagicMock

sys.modules.setdefault("requests", MagicMock())
sys.modules.setdefault("xmltodict", MagicMock())
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sync_main  # noqa: E402


def _with_baseline(**counts):
    """Patch load_cached_ids() to return a fixed baseline cache."""
    sync_main.load_cached_ids = lambda: (dict(counts), False)


def test_no_baseline_allows():
    _with_baseline()  # empty cache, no known-good counts yet
    assert sync_main.evaluate_master_wipe_guard("ledgers", 0) is None


def test_tiny_company_not_policed():
    _with_baseline(last_ledgers_count=5)  # below MIN_BASELINE (10)
    assert sync_main.evaluate_master_wipe_guard("ledgers", 0) is None


def test_collapse_to_zero_is_blocked():
    _with_baseline(last_ledgers_count=500)
    reason = sync_main.evaluate_master_wipe_guard("ledgers", 0)
    assert reason is not None and "Refusing to push ledgers" in reason


def test_collapse_below_half_is_blocked():
    _with_baseline(last_groups_count=200)
    assert sync_main.evaluate_master_wipe_guard("groups", 80) is not None  # 80 < 100


def test_legitimate_full_resync_is_allowed():
    _with_baseline(last_stock_count=500)
    assert sync_main.evaluate_master_wipe_guard("stock_items", 500) is None
    assert sync_main.evaluate_master_wipe_guard("stock_items", 480) is None  # > 50%


def test_disable_flag_allows(monkeypatch=None):
    _with_baseline(last_ledgers_count=500)
    original = sync_main.DISABLE_MASTER_WIPE_GUARD
    sync_main.DISABLE_MASTER_WIPE_GUARD = True
    try:
        assert sync_main.evaluate_master_wipe_guard("ledgers", 0) is None
    finally:
        sync_main.DISABLE_MASTER_WIPE_GUARD = original


def test_cold_start_uses_cloud_baseline():
    # No local baseline (fresh install / cache wipe), hybrid mode: the guard
    # must fall back to the cloud row count so a degraded first sync can't wipe.
    _with_baseline()  # empty cache
    orig_mode = sync_main.SYNC_INGEST_MODE
    orig_fetch = sync_main.fetch_remote_master_count
    sync_main.SYNC_INGEST_MODE = "hybrid"
    try:
        sync_main.fetch_remote_master_count = lambda section: (500, "ok")
        assert sync_main.evaluate_master_wipe_guard("ledgers", 0) is not None  # cloud has 500 -> block
        # A genuinely-new company (cloud also empty) must NOT false-trip.
        sync_main.fetch_remote_master_count = lambda section: (0, "ok")
        assert sync_main.evaluate_master_wipe_guard("ledgers", 0) is None
        # Cloud unreadable -> fall back to (absent) local baseline -> allow.
        sync_main.fetch_remote_master_count = lambda section: (None, "company_not_found")
        assert sync_main.evaluate_master_wipe_guard("ledgers", 0) is None
    finally:
        sync_main.SYNC_INGEST_MODE = orig_mode
        sync_main.fetch_remote_master_count = orig_fetch


def test_baseline_persistence():
    _with_baseline(last_groups_count=42)
    ids = {}
    # Fetched this run -> record the new count.
    sync_main.update_master_count_baseline(ids, "groups", [{}] * 7)
    assert ids["last_groups_count"] == 7
    # Not fetched this run (None) -> carry the prior baseline forward.
    ids2 = {}
    sync_main.update_master_count_baseline(ids2, "groups", None)
    assert ids2["last_groups_count"] == 42


def test_hold_master_markers_for_retry():
    # Voucher markers advance; master markers are held at cached values so masters
    # re-detect next run (A6: stop one tripped section blocking the whole cache).
    saved = {"alt_mst_id": "B", "alter_id": "Y", "alt_vch_id": "V2", "vch_id": "9"}
    cached = {"alt_mst_id": "A", "alter_id": "X", "alt_vch_id": "V1", "vch_id": "4"}
    out = sync_main.hold_master_markers_for_retry(saved, cached)
    assert out["alt_mst_id"] == "A" and out["alter_id"] == "X"   # held -> masters re-detect
    assert out["alt_vch_id"] == "V2" and out["vch_id"] == "9"     # voucher markers advanced
    # If the cache had no master marker yet, it must be removed (not left advanced).
    saved2 = {"alt_mst_id": "B", "alter_id": "Y"}
    sync_main.hold_master_markers_for_retry(saved2, {})
    assert "alt_mst_id" not in saved2 and "alter_id" not in saved2


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok: {fn.__name__}")
    print(f"\nPASS: {len(fns)} master-wipe-guard tests")


if __name__ == "__main__":
    try:
        _run_all()
    except AssertionError as exc:
        print(f"\nFAIL: {exc}")
        sys.exit(1)
