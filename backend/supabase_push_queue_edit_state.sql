-- ============================================================
-- TallyBridge — push_queue.edit_state (queue edit lifecycle tag)
-- Run in your Supabase SQL Editor (Dashboard → SQL Editor).
-- Apply to every project the app reads from:
--   yynuuysvjeipawzfbeme  (testing / prod flavors)
-- ============================================================
--
-- Why this column exists
-- ----------------------
-- When a queued voucher is touched in the app's detail sheet, the queue row gets
-- a lifecycle tag shown under the party name + time:
--   under_edit  → the sheet was closed while still mid-edit (changes not saved).
--   edited      → edits were saved back to the row.
-- Both cases also bump created_at so the newest-first queue floats the row to the
-- top. The tag used to be a local-only overlay (lost on reload, invisible to
-- other clients); persisting it here keeps it across a cold start and on every
-- logged-in client — the app's rowToEntry reads it back. A Revert writes NULL.
--
-- No backend code change is needed: every backend push_queue write is a targeted
-- column update (status / error_message / tally_response / pushed_at) or the
-- enqueue INSERT (which omits edit_state → defaults to NULL), so none of them
-- touch this column. The mobile/web client writes it directly, same as it already
-- writes created_at and voucher_payload, under push_queue's existing all-roles RLS.

ALTER TABLE push_queue
  ADD COLUMN IF NOT EXISTS edit_state TEXT
  CHECK (edit_state IS NULL OR edit_state IN ('under_edit', 'edited'));
