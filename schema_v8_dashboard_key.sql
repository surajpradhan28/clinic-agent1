-- schema_v8_dashboard_key.sql
-- Adds a unique per-client dashboard key used to protect /clinic?key=<key>
-- Each client gets a random UUID as their private dashboard link.
-- Run once in Supabase SQL Editor.

ALTER TABLE clients
  ADD COLUMN IF NOT EXISTS dashboard_key TEXT UNIQUE;

-- Generate a key for every existing client that doesn't have one yet
UPDATE clients
  SET dashboard_key = gen_random_uuid()::text
  WHERE dashboard_key IS NULL;

-- Index for fast lookup on each page load
CREATE INDEX IF NOT EXISTS idx_clients_dashboard_key
  ON clients (dashboard_key);
