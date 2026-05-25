-- ============================================================
-- schema_v11_trial_signup.sql
-- Features: Self-Serve Signup + Trial Management
-- Run in Supabase SQL Editor
-- ============================================================

-- ── 1. New columns on clients ────────────────────────────────

-- Contact email for invoices and notifications
ALTER TABLE clients ADD COLUMN IF NOT EXISTS contact_email  TEXT;

-- City for the signup form display
ALTER TABLE clients ADD COLUMN IF NOT EXISTS city           TEXT;

-- Trial window (set automatically on signup)
ALTER TABLE clients ADD COLUMN IF NOT EXISTS trial_started_at TIMESTAMPTZ;
ALTER TABLE clients ADD COLUMN IF NOT EXISTS trial_ends_at    TIMESTAMPTZ;

-- Where the signup came from (web, referral, manual)
ALTER TABLE clients ADD COLUMN IF NOT EXISTS signup_source  TEXT DEFAULT 'manual';

-- Referral tracking
ALTER TABLE clients ADD COLUMN IF NOT EXISTS referred_by    TEXT;   -- referral code used
ALTER TABLE clients ADD COLUMN IF NOT EXISTS referral_code  TEXT UNIQUE;  -- this client's own code


-- ── 2. Signups log table ─────────────────────────────────────
-- Every form submission is logged here for admin visibility,
-- even before a client row is created (handles duplicates gracefully).

CREATE TABLE IF NOT EXISTS signups (
    id              BIGSERIAL PRIMARY KEY,
    clinic_name     TEXT NOT NULL,
    doctor_name     TEXT NOT NULL,
    contact_phone   TEXT NOT NULL,
    contact_email   TEXT,
    city            TEXT,
    plan            TEXT NOT NULL DEFAULT 'starter',
    referred_by     TEXT,
    client_id       INTEGER REFERENCES clients(id) ON DELETE SET NULL,
    ip_address      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_signups_phone
    ON signups (contact_phone, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_signups_created
    ON signups (created_at DESC);


-- ── 3. Referral codes: generate for existing clients ─────────
-- Run once to back-fill referral codes for existing clients
UPDATE clients
SET referral_code = UPPER(SUBSTRING(MD5(id::text || clinic_name), 1, 6))
WHERE referral_code IS NULL;
