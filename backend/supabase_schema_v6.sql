-- ============================================================
-- TallyBridge - Voucher schema updates for ERP 9 two-pass sync
-- Run this in your Supabase SQL Editor
-- ============================================================

ALTER TABLE public.vouchers
ADD COLUMN IF NOT EXISTS alter_id INTEGER;

ALTER TABLE public.vouchers
ADD COLUMN IF NOT EXISTS master_id INTEGER;

ALTER TABLE public.vouchers
ADD COLUMN IF NOT EXISTS is_optional BOOLEAN DEFAULT false;

CREATE INDEX IF NOT EXISTS idx_vouchers_company_master_id
  ON public.vouchers(company_id, master_id);
