-- schema_v7_14d_warning.sql
-- Add 14-day renewal warning flag to subscriptions table.
-- Run once in Supabase SQL Editor before deploying the updated scheduler.

ALTER TABLE subscriptions
  ADD COLUMN IF NOT EXISTS warning_14d_sent BOOLEAN NOT NULL DEFAULT FALSE;

-- Index for fast daily check (only unsent 14d warnings on active subscriptions)
CREATE INDEX IF NOT EXISTS idx_subs_14d_warning
  ON subscriptions (client_id, end_date, status, warning_14d_sent)
  WHERE status = 'active' AND warning_14d_sent = FALSE;
