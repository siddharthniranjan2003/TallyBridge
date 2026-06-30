"""Unit tests for the voucher reconciliation-scope fix (AlterID-incremental wipe).

AlterID-incremental sync fetches ONLY the alter-id-changed vouchers but spans the
whole financial year. If the payload still carries a full-year reconciliation date
range, every cloud transport (the tb_ingest_vouchers RPC used by direct + hybrid,
and the render backend route) treats every UNCHANGED voucher in that range as
stale and deletes it — a near-full wipe that tb_guard blocks, leaving the sync
stuck retrying the same delta forever (new vouchers never land).

These tests pin the decision logic that suppresses the reconciliation range for an
alter-id-delta payload while keeping it for full + date-window incremental syncs.

Run: python3 tests/test_voucher_reconcile_scope.py   (or: pytest tests/...)
"""

import os
import sys
from unittest.mock import MagicMock

sys.modules.setdefault("requests", MagicMock())
sys.modules.setdefault("xmltodict", MagicMock())
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sync_main  # noqa: E402

R = sync_main.resolve_voucher_reconcile_dates


# ── resolve_voucher_reconcile_dates: the single source of truth ──────────────

def test_alterid_delta_suppresses_range():
    # The exact production bug: incremental mode + full-year from-date + a delta
    # (min_alter_id > 0). MUST send no reconcile range so the cloud can't wipe.
    plan = {
        "voucher_from_date": "20250401", "voucher_to_date": "20260331",
        "voucher_sync_mode": "incremental", "voucher_min_alter_id": 4521,
        "voucher_reconcile": False,
    }
    assert R(plan) == (None, None)


def test_date_window_incremental_keeps_range():
    # Narrow recent window, re-fetches ALL vouchers in it -> date-scoped delete is
    # correct, so the range is kept.
    plan = {
        "voucher_from_date": "20260610", "voucher_to_date": "20260331",
        "voucher_sync_mode": "incremental", "voucher_min_alter_id": 0,
        "voucher_reconcile": True,
    }
    assert R(plan) == ("20260610", "20260331")


def test_full_sync_keeps_range():
    plan = {
        "voucher_from_date": "20250401", "voucher_to_date": "20260331",
        "voucher_sync_mode": "full", "voucher_min_alter_id": 0,
        "voucher_reconcile": True,
    }
    assert R(plan) == ("20250401", "20260331")


def test_defensive_min_alter_id_overrides_flag():
    # Belt-and-suspenders: even if the flag wrongly says reconcile=True, an
    # alter-id delta (min_alter_id > 0) must still suppress the range.
    plan = {
        "voucher_from_date": "20250401", "voucher_to_date": "20260331",
        "voucher_min_alter_id": 99, "voucher_reconcile": True,
    }
    assert R(plan) == (None, None)


def test_defensive_flag_overrides_when_no_alter_id():
    plan = {
        "voucher_from_date": "20250401", "voucher_to_date": "20260331",
        "voucher_min_alter_id": 0, "voucher_reconcile": False,
    }
    assert R(plan) == (None, None)


def test_missing_keys_default_to_reconcile():
    # Legacy/full plans that don't set the new keys must keep reconciling.
    plan = {"voucher_from_date": "20250401", "voucher_to_date": "20260331"}
    assert R(plan) == ("20250401", "20260331")


def test_malformed_min_alter_id_is_treated_as_zero():
    plan = {
        "voucher_from_date": "20250401", "voucher_to_date": "20260331",
        "voucher_min_alter_id": "oops", "voucher_reconcile": True,
    }
    assert R(plan) == ("20250401", "20260331")


# ── build_sync_plan: end-to-end flag wiring ──────────────────────────────────

def _with_cache(cache: dict):
    sync_main.load_cached_ids = lambda: (dict(cache), False)


def test_build_plan_alterid_branch_sets_reconcile_false():
    sync_main.ENABLE_INCREMENTAL_VOUCHER_SYNC = True
    _with_cache({"alt_vch_id": "4521", "alter_id": "100", "alt_mst_id": "50",
                 "last_voucher_date": "2026-06-19"})
    current = {"alt_vch_id": "4530", "alter_id": "100", "alt_mst_id": "50",
               "last_voucher_date": "2026-06-20"}
    plan = sync_main.build_sync_plan(current, "20250401", "20260331")
    assert plan["voucher_sync_mode"] == "incremental"
    assert plan["voucher_min_alter_id"] == 4521
    assert plan["voucher_reconcile"] is False
    assert R(plan) == (None, None)  # no wipe


def test_build_plan_forced_full_keeps_reconcile():
    _with_cache({"alt_vch_id": "10"})
    plan = sync_main.build_sync_plan({"alt_vch_id": "11"}, "20250401", "20260331",
                                     force_full_sync=True)
    assert plan["voucher_sync_mode"] == "full"
    assert plan.get("voucher_min_alter_id", 0) == 0
    assert R(plan) == ("20250401", "20260331")


def test_build_plan_date_window_keeps_reconcile():
    sync_main.ENABLE_INCREMENTAL_VOUCHER_SYNC = True
    sync_main.VOUCHER_OVERLAP_DAYS = 3
    # No cached alt_vch_id (0) but last_voucher_date advanced -> date-window branch.
    _with_cache({"alt_vch_id": "0", "alter_id": "100", "alt_mst_id": "50",
                 "last_voucher_date": "2026-06-10"})
    current = {"alt_vch_id": "5", "alter_id": "100", "alt_mst_id": "50",
               "last_voucher_date": "2026-06-20"}
    plan = sync_main.build_sync_plan(current, "20250401", "20260331")
    assert plan["voucher_sync_mode"] == "incremental"
    assert plan["voucher_min_alter_id"] == 0
    assert plan["voucher_reconcile"] is True
    frm, to = R(plan)
    assert frm is not None and to == "20260331"


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok: {fn.__name__}")
    print(f"\nPASS: {len(fns)} voucher-reconcile-scope tests")


if __name__ == "__main__":
    try:
        _run_all()
    except AssertionError as exc:
        print(f"\nFAIL: {exc}")
        sys.exit(1)
