-- SAFETY GUARD: make it physically impossible for one sync/operation to wipe a
-- company's vouchers.
--
-- Background: tb_ingest_vouchers' reconciliation runs "DELETE FROM vouchers WHERE
-- synced_at <> this_run" on a full sync. That's correct only when a full sync
-- actually carries all the vouchers. A full-tagged sync that arrives with an
-- incomplete payload (e.g. 1 voucher) deletes everything else. This trigger blocks
-- any single DELETE that removes >1000 rows AND leaves the company with fewer
-- vouchers than it just removed (i.e. wipes more than half). The whole transaction
-- rolls back, so NO data is lost; the sync fails loudly instead.
--
-- Normal syncs are unaffected: a real full re-sync upserts rows in place, so its
-- reconciliation deletes only the few vouchers genuinely removed in Tally.

CREATE OR REPLACE FUNCTION public.tb_guard_voucher_mass_delete()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
  v_company UUID;
  v_companies INTEGER;
  v_deleted INTEGER;
  v_remaining INTEGER;
BEGIN
  SELECT count(*), count(DISTINCT company_id)
    INTO v_deleted, v_companies
  FROM deleted_rows;

  -- Only police substantial, single-company deletions. Normal reconciliation
  -- deletes are small; leave them (and any multi-company op) alone.
  IF v_deleted < 1000 OR v_companies <> 1 THEN
    RETURN NULL;
  END IF;

  SELECT company_id INTO v_company FROM deleted_rows LIMIT 1;
  IF v_company IS NULL THEN
    RETURN NULL;
  END IF;

  -- Allow legitimate company removal (ON DELETE CASCADE): if the company row is
  -- gone, this delete is a cascade, not a reconciliation — don't block it.
  IF NOT EXISTS (SELECT 1 FROM public.companies WHERE id = v_company) THEN
    RETURN NULL;
  END IF;

  SELECT count(*) INTO v_remaining
  FROM public.vouchers WHERE company_id = v_company;

  -- Refuse if this delete removed more than half the company's vouchers at once.
  IF v_remaining < v_deleted THEN
    RAISE EXCEPTION
      'tb_guard: refusing to delete % vouchers (only % would remain) for company % — looks like a broken/partial full sync. To override intentionally: ALTER TABLE public.vouchers DISABLE TRIGGER tb_guard_voucher_mass_delete_trg;',
      v_deleted, v_remaining, v_company
      USING ERRCODE = 'raise_exception';
  END IF;

  RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS tb_guard_voucher_mass_delete_trg ON public.vouchers;
CREATE TRIGGER tb_guard_voucher_mass_delete_trg
AFTER DELETE ON public.vouchers
REFERENCING OLD TABLE AS deleted_rows
FOR EACH STATEMENT
EXECUTE FUNCTION public.tb_guard_voucher_mass_delete();
