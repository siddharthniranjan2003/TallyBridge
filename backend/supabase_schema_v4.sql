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

CREATE INDEX IF NOT EXISTS idx_purchases_company_guid ON purchases(company_id, tally_guid);
CREATE INDEX IF NOT EXISTS idx_purchases_company_date ON purchases(company_id, date DESC);
CREATE INDEX IF NOT EXISTS idx_purchases_company_party ON purchases(company_id, party_name);
