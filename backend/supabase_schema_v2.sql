-- ============================================================
-- TallyBridge — Core Supabase Schema Updates (Phase 2)
-- Run this in your Supabase SQL Editor
-- ============================================================

-- 1. Extend Companies Table
ALTER TABLE companies ADD COLUMN IF NOT EXISTS books_from DATE;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS books_to DATE;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS books_from_raw TEXT;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS books_to_raw TEXT;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS gstin TEXT;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS address TEXT;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS guid TEXT;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS master_id INTEGER;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS state TEXT;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS country TEXT;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS pincode TEXT;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS email TEXT;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS phone TEXT;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS gst_type TEXT;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS pan TEXT;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS alter_id TEXT;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS alt_vch_id TEXT;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS alt_mst_id TEXT;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS last_voucher_date DATE;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS last_outstanding_synced_at TIMESTAMPTZ;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS last_profit_loss_synced_at TIMESTAMPTZ;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS last_balance_sheet_synced_at TIMESTAMPTZ;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS last_trial_balance_synced_at TIMESTAMPTZ;
ALTER TABLE companies DROP CONSTRAINT IF EXISTS companies_name_key;
DROP INDEX IF EXISTS companies_name_key;
UPDATE companies SET guid = NULL WHERE guid = '';
CREATE UNIQUE INDEX IF NOT EXISTS idx_companies_guid_unique
  ON companies(guid)
  WHERE guid IS NOT NULL AND guid <> '';
CREATE INDEX IF NOT EXISTS idx_companies_name ON companies(name);

-- 2. Extend Ledgers Table
ALTER TABLE ledgers ADD COLUMN IF NOT EXISTS master_id INTEGER;
ALTER TABLE ledgers ADD COLUMN IF NOT EXISTS email TEXT;
ALTER TABLE ledgers ADD COLUMN IF NOT EXISTS phone TEXT;
ALTER TABLE ledgers ADD COLUMN IF NOT EXISTS mobile TEXT;
ALTER TABLE ledgers ADD COLUMN IF NOT EXISTS pincode TEXT;
ALTER TABLE ledgers ADD COLUMN IF NOT EXISTS gstin TEXT;
ALTER TABLE ledgers ADD COLUMN IF NOT EXISTS state TEXT;
ALTER TABLE ledgers ADD COLUMN IF NOT EXISTS country TEXT;
ALTER TABLE ledgers ADD COLUMN IF NOT EXISTS credit_period TEXT;
ALTER TABLE ledgers ADD COLUMN IF NOT EXISTS credit_limit NUMERIC;
ALTER TABLE ledgers ADD COLUMN IF NOT EXISTS bank_account TEXT;
ALTER TABLE ledgers ADD COLUMN IF NOT EXISTS ifsc_code TEXT;
ALTER TABLE ledgers ADD COLUMN IF NOT EXISTS pan TEXT;
ALTER TABLE ledgers ADD COLUMN IF NOT EXISTS mailing_name TEXT;
ALTER TABLE ledgers ADD COLUMN IF NOT EXISTS guid TEXT;

-- 3. Groups Table (Chart of Accounts hierarchy)
CREATE TABLE IF NOT EXISTS groups (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id UUID REFERENCES companies(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  parent TEXT,
  master_id INTEGER,
  is_revenue TEXT,
  affects_stock TEXT,
  is_subledger TEXT,
  synced_at TIMESTAMPTZ DEFAULT now(),
  UNIQUE(company_id, name)
);
CREATE INDEX IF NOT EXISTS idx_groups_company ON groups(company_id);

-- 4. Extend Vouchers Table
ALTER TABLE vouchers ADD COLUMN IF NOT EXISTS reference TEXT;
ALTER TABLE vouchers ADD COLUMN IF NOT EXISTS is_invoice BOOLEAN DEFAULT false;
ALTER TABLE vouchers ADD COLUMN IF NOT EXISTS view TEXT;

-- 5. Voucher Ledger Entries (Accounting Dr/Cr detail for vouchers)
CREATE TABLE IF NOT EXISTS voucher_ledger_entries (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  voucher_id UUID REFERENCES vouchers(id) ON DELETE CASCADE,
  ledger_name TEXT NOT NULL,
  amount NUMERIC DEFAULT 0,
  is_party_ledger BOOLEAN DEFAULT false,
  is_deemed_positive BOOLEAN DEFAULT false, -- typically true = Debit, false = Credit
  bill_allocations JSONB DEFAULT '[]'::jsonb,
  created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_voucher_ledger_entries_vid ON voucher_ledger_entries(voucher_id);
CREATE INDEX IF NOT EXISTS idx_outstanding_company_synced_at ON outstanding(company_id, synced_at DESC);

-- Enable RLS for new tables
ALTER TABLE groups ENABLE ROW LEVEL SECURITY;
ALTER TABLE voucher_ledger_entries ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Service role full access" ON groups
  FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "Service role full access" ON voucher_ledger_entries
  FOR ALL USING (true) WITH CHECK (true);
