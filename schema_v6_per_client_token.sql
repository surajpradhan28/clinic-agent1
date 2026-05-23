-- ─────────────────────────────────────────────────────────────────────────────
-- schema_v6_per_client_token.sql
--
-- Adds per-client WhatsApp token support.
-- Each clinic can now have its own Meta access token stored securely in the DB.
-- If whatsapp_token is NULL or empty, the system falls back to the global
-- WHATSAPP_TOKEN environment variable — no existing clients are affected.
--
-- Run once in Supabase SQL Editor.
-- ─────────────────────────────────────────────────────────────────────────────

ALTER TABLE clients
    ADD COLUMN IF NOT EXISTS whatsapp_token TEXT DEFAULT NULL;

COMMENT ON COLUMN clients.whatsapp_token IS
    'Optional per-client Meta WhatsApp access token. '
    'If NULL, the global WHATSAPP_TOKEN env var is used. '
    'Use this when a client has their own Meta Business Account.';
