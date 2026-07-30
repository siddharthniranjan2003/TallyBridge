-- ============================================================
-- Audit_Trail_Purchase
--
-- The self-learning record of how a purchase invoice line was actually booked:
-- raw_ocr (the line's OCR text) -> actual_item (the stock item the user committed
-- to TallyPrime). The web app appends a row for every line of every voucher it
-- pushes, so the table grows itself from human decisions.
--
-- The parser reads it BEFORE the curated Purchase_Matching table and before the
-- vendor rule engine, which makes it the primary source of item conversion: once a
-- line has been booked correctly, the next scan of that line resolves straight to
-- the same item, tagged source="Audit_Trail" in push_queue.source_payload.
--
-- Append-only. The same raw_ocr may appear many times; the newest row (highest id)
-- wins on read, and the earlier rows remain as the audit history.
--
-- Mixed-case identifier, like the existing Purchase_Matching table: it MUST be
-- double-quoted everywhere, because Postgres folds unquoted identifiers to lower
-- case.
--
-- Tenancy: one Supabase project per client, so run this on EVERY client project
-- (see the tenancy model). The parser and the app must read/write the SAME project
-- or the learning loop never closes.
-- ============================================================

CREATE TABLE IF NOT EXISTS "Audit_Trail_Purchase" (
  id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  raw_ocr     TEXT NOT NULL,
  actual_item TEXT NOT NULL,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The parser pages the whole table ordered by id and folds it into a lookup map,
-- so the read is a sequential scan by design. This index serves the "what has this
-- OCR line been booked as before?" query from SQL/support tooling.
CREATE INDEX IF NOT EXISTS idx_audit_trail_purchase_raw_ocr
  ON "Audit_Trail_Purchase" (raw_ocr);

-- A brand-new table ships with RLS OFF, so it must be enabled explicitly.
ALTER TABLE "Audit_Trail_Purchase" ENABLE ROW LEVEL SECURITY;

-- Mirror push_queue's / scan_jobs' permissive single-policy access: the anon client
-- (mobile + web) inserts on Push to Tally; the service-keyed parsing service reads.
-- One policy, all roles.
DROP POLICY IF EXISTS "Audit_Trail_Purchase full access" ON "Audit_Trail_Purchase";
CREATE POLICY "Audit_Trail_Purchase full access" ON "Audit_Trail_Purchase"
  FOR ALL USING (true) WITH CHECK (true);
