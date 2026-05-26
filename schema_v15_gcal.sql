-- schema_v15_gcal.sql
-- Google Calendar OAuth integration
-- Run once in Supabase SQL Editor.

-- ── OAuth tokens per clinic ──────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS oauth_tokens (
    id             BIGSERIAL PRIMARY KEY,
    client_id      INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    provider       TEXT NOT NULL DEFAULT 'google',
    access_token   TEXT NOT NULL,
    refresh_token  TEXT,
    token_expiry   TIMESTAMPTZ,
    calendar_id    TEXT NOT NULL DEFAULT 'primary',
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (client_id, provider)
);

-- ── Add source column to blocked_slots ──────────────────────────────────────
-- 'manual'  = doctor blocked via WhatsApp command
-- 'gcal'    = auto-blocked from Google Calendar busy event
ALTER TABLE blocked_slots
    ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'manual';

-- Fast lookup: find all gcal-sourced blocks for cleanup during sync
CREATE INDEX IF NOT EXISTS idx_blocked_slots_source
    ON blocked_slots (client_id, source, block_date)
    WHERE source = 'gcal';

SELECT 'schema_v15_gcal applied' AS status;
