CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS companies (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name TEXT NOT NULL,
  books_from DATE,
  books_to DATE,
  books_from_raw TEXT,
  books_to_raw TEXT,
  gstin TEXT,
  address TEXT,
  guid TEXT,
  master_id INTEGER,
  state TEXT,
  country TEXT,
  pincode TEXT,
  email TEXT,
  phone TEXT,
  gst_type TEXT,
  pan TEXT,
  alter_id TEXT,
  alt_vch_id TEXT,
  alt_mst_id TEXT,
  last_voucher_date DATE,
  last_synced_at TIMESTAMPTZ,
  last_outstanding_synced_at TIMESTAMPTZ,
  last_profit_loss_synced_at TIMESTAMPTZ,
  last_balance_sheet_synced_at TIMESTAMPTZ,
  last_trial_balance_synced_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS groups (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id UUID NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  parent TEXT,
  master_id INTEGER,
  is_revenue TEXT,
  affects_stock TEXT,
  is_subledger TEXT,
  synced_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(company_id, name)
);

CREATE TABLE IF NOT EXISTS ledgers (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id UUID NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  group_name TEXT,
  opening_balance NUMERIC DEFAULT 0,
  closing_balance NUMERIC DEFAULT 0,
  master_id INTEGER,
  email TEXT,
  phone TEXT,
  mobile TEXT,
  pincode TEXT,
  gstin TEXT,
  state TEXT,
  country TEXT,
  credit_period TEXT,
  credit_limit NUMERIC DEFAULT 0,
  bank_account TEXT,
  ifsc_code TEXT,
  pan TEXT,
  mailing_name TEXT,
  guid TEXT,
  synced_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(company_id, name)
);

CREATE TABLE IF NOT EXISTS vouchers (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id UUID NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  tally_guid TEXT NOT NULL,
  alter_id INTEGER,
  master_id INTEGER,
  voucher_number TEXT,
  voucher_type TEXT,
  date DATE,
  party_name TEXT,
  amount NUMERIC DEFAULT 0,
  narration TEXT,
  is_cancelled BOOLEAN DEFAULT false,
  is_optional BOOLEAN DEFAULT false,
  reference TEXT,
  is_invoice BOOLEAN DEFAULT false,
  view TEXT,
  synced_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(company_id, tally_guid)
);

CREATE TABLE IF NOT EXISTS voucher_items (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  voucher_id UUID NOT NULL REFERENCES vouchers(id) ON DELETE CASCADE,
  stock_item_name TEXT NOT NULL,
  quantity NUMERIC DEFAULT 0,
  unit TEXT,
  rate NUMERIC DEFAULT 0,
  discount_pct NUMERIC DEFAULT 0,
  amount NUMERIC DEFAULT 0,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS voucher_ledger_entries (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  voucher_id UUID NOT NULL REFERENCES vouchers(id) ON DELETE CASCADE,
  ledger_name TEXT NOT NULL,
  amount NUMERIC DEFAULT 0,
  is_party_ledger BOOLEAN DEFAULT false,
  is_deemed_positive BOOLEAN DEFAULT false,
  bill_allocations JSONB NOT NULL DEFAULT '[]'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS purchases (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id UUID NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  voucher_id UUID NOT NULL REFERENCES vouchers(id) ON DELETE CASCADE,
  tally_guid TEXT NOT NULL,
  voucher_number TEXT,
  voucher_type TEXT,
  date DATE,
  party_name TEXT,
  amount NUMERIC DEFAULT 0,
  narration TEXT,
  reference TEXT,
  is_cancelled BOOLEAN DEFAULT false,
  is_invoice BOOLEAN DEFAULT false,
  synced_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(company_id, tally_guid)
);

CREATE TABLE IF NOT EXISTS stock_items (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id UUID NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  group_name TEXT,
  unit TEXT,
  closing_qty NUMERIC DEFAULT 0,
  closing_value NUMERIC DEFAULT 0,
  rate NUMERIC DEFAULT 0,
  synced_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(company_id, name)
);

CREATE TABLE IF NOT EXISTS outstanding (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id UUID NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  party_name TEXT NOT NULL,
  type TEXT NOT NULL CHECK (type IN ('receivable', 'payable')),
  voucher_number TEXT,
  voucher_date DATE,
  due_date DATE,
  original_amount NUMERIC DEFAULT 0,
  pending_amount NUMERIC DEFAULT 0,
  days_overdue INTEGER DEFAULT 0,
  synced_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS profit_loss (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id UUID NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  particulars TEXT NOT NULL,
  amount NUMERIC DEFAULT 0,
  is_debit BOOLEAN DEFAULT false,
  synced_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS balance_sheet (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id UUID NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  particulars TEXT NOT NULL,
  amount NUMERIC DEFAULT 0,
  side TEXT NOT NULL DEFAULT 'asset' CHECK (side IN ('asset', 'liability')),
  synced_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS trial_balance (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id UUID NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  particulars TEXT NOT NULL,
  debit NUMERIC DEFAULT 0,
  credit NUMERIC DEFAULT 0,
  synced_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS sync_log (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id UUID NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  synced_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  status TEXT NOT NULL,
  records_synced JSONB NOT NULL DEFAULT '{}'::jsonb,
  error_message TEXT,
  sync_meta JSONB
);

-- PUSH PHASE 1: outbound Sales/Purchase voucher queue
CREATE TABLE IF NOT EXISTS push_queue (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id UUID NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  voucher_payload JSONB NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending', 'pushed', 'failed')),
  error_message TEXT,
  tally_response JSONB,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  pushed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_groups_company ON groups(company_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_companies_guid_unique
  ON companies(guid)
  WHERE guid IS NOT NULL AND guid <> '';
CREATE INDEX IF NOT EXISTS idx_companies_name ON companies(name);
CREATE INDEX IF NOT EXISTS idx_ledgers_company ON ledgers(company_id);
CREATE INDEX IF NOT EXISTS idx_ledgers_company_group ON ledgers(company_id, group_name);
CREATE INDEX IF NOT EXISTS idx_vouchers_company_guid ON vouchers(company_id, tally_guid);
CREATE INDEX IF NOT EXISTS idx_vouchers_company_date ON vouchers(company_id, date DESC);
CREATE INDEX IF NOT EXISTS idx_vouchers_company_party ON vouchers(company_id, party_name);
CREATE INDEX IF NOT EXISTS idx_vouchers_company_master_id ON vouchers(company_id, master_id);
CREATE INDEX IF NOT EXISTS idx_voucher_items_voucher_id ON voucher_items(voucher_id);
CREATE INDEX IF NOT EXISTS idx_voucher_ledger_entries_vid ON voucher_ledger_entries(voucher_id);
CREATE INDEX IF NOT EXISTS idx_purchases_company_guid ON purchases(company_id, tally_guid);
CREATE INDEX IF NOT EXISTS idx_purchases_company_date ON purchases(company_id, date DESC);
CREATE INDEX IF NOT EXISTS idx_purchases_company_party ON purchases(company_id, party_name);
CREATE INDEX IF NOT EXISTS idx_stock_items_company ON stock_items(company_id);
CREATE INDEX IF NOT EXISTS idx_outstanding_company_synced_at ON outstanding(company_id, synced_at DESC);
CREATE INDEX IF NOT EXISTS idx_outstanding_company_party ON outstanding(company_id, party_name);
CREATE INDEX IF NOT EXISTS idx_profit_loss_company_synced_at ON profit_loss(company_id, synced_at DESC);
CREATE INDEX IF NOT EXISTS idx_balance_sheet_company_synced_at ON balance_sheet(company_id, synced_at DESC);
CREATE INDEX IF NOT EXISTS idx_trial_balance_company_synced_at ON trial_balance(company_id, synced_at DESC);
CREATE INDEX IF NOT EXISTS idx_sync_log_company_synced_at ON sync_log(company_id, synced_at DESC);
CREATE INDEX IF NOT EXISTS idx_push_queue_company_status ON push_queue(company_id, status, created_at);
