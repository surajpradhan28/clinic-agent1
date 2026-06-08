-- schema_v17_annual.sql
-- Adds billing_cycle to clients table for annual plan support.
-- Run once in Supabase SQL editor.

ALTER TABLE clients
  ADD COLUMN IF NOT EXISTS billing_cycle text NOT NULL DEFAULT 'monthly'
  CHECK (billing_cycle IN ('monthly', 'annual'));

COMMENT ON COLUMN clients.billing_cycle IS
  'Billing cadence chosen at signup: monthly (default) or annual (10-month price, 12-month access).';
