-- ============================================================
-- TallyBridge — New Supabase Tables
-- Run this in your Supabase SQL Editor (Dashboard → SQL Editor)
-- ============================================================

-- 1. Profit & Loss
CREATE TABLE IF NOT EXISTS profit_loss (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id UUID REFERENCES companies(id) ON DELETE CASCADE,
  particulars TEXT NOT NULL,
  amount NUMERIC DEFAULT 0,
  is_debit BOOLEAN DEFAULT false,
  synced_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_profit_loss_company_synced_at
  ON profit_loss(company_id, synced_at DESC);


-- 2. Balance Sheet
CREATE TABLE IF NOT EXISTS balance_sheet (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id UUID REFERENCES companies(id) ON DELETE CASCADE,
  particulars TEXT NOT NULL,
  amount NUMERIC DEFAULT 0,
  side TEXT DEFAULT 'asset',  -- 'asset' or 'liability'
  synced_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_balance_sheet_company_synced_at
  ON balance_sheet(company_id, synced_at DESC);


-- 3. Trial Balance
CREATE TABLE IF NOT EXISTS trial_balance (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id UUID REFERENCES companies(id) ON DELETE CASCADE,
  particulars TEXT NOT NULL,
  debit NUMERIC DEFAULT 0,
  credit NUMERIC DEFAULT 0,
  synced_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_trial_balance_company_synced_at
  ON trial_balance(company_id, synced_at DESC);


-- ============================================================
-- Enable Row Level Security (optional but recommended)
-- ============================================================

ALTER TABLE profit_loss ENABLE ROW LEVEL SECURITY;
ALTER TABLE balance_sheet ENABLE ROW LEVEL SECURITY;
ALTER TABLE trial_balance ENABLE ROW LEVEL SECURITY;

-- Service role policy (your backend uses the service key)
CREATE POLICY "Service role full access" ON profit_loss
  FOR ALL USING (true) WITH CHECK (true);

CREATE POLICY "Service role full access" ON balance_sheet
  FOR ALL USING (true) WITH CHECK (true);

CREATE POLICY "Service role full access" ON trial_balance
  FOR ALL USING (true) WITH CHECK (true);


-- 4. PUSH PHASE 1: outbound Sales/Purchase voucher queue
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

CREATE INDEX IF NOT EXISTS idx_push_queue_company_status
  ON push_queue(company_id, status, created_at);

ALTER TABLE push_queue ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Service role full access" ON push_queue
  FOR ALL USING (true) WITH CHECK (true);
