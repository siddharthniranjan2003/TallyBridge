-- ============================================================
-- TallyBridge — Core Supabase Schema Updates (Phase 2)
-- Run this in your Supabase SQL Editor
-- ============================================================

-- 1. Extend Companies Table
ALTER TABLE companies ADD COLUMN IF NOT EXISTS books_from DATE;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS books_to DATE;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS gstin TEXT;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS address TEXT;

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

-- Enable RLS for new tables
ALTER TABLE groups ENABLE ROW LEVEL SECURITY;
ALTER TABLE voucher_ledger_entries ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Service role full access" ON groups
  FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "Service role full access" ON voucher_ledger_entries
  FOR ALL USING (true) WITH CHECK (true);
