-- schema_v12_referrals.sql
-- Referral reward tracking: "refer a clinic, get 1 month free"
--
-- Flow:
--   1. Each clinic already has a unique referral_code in the clients table (from schema_v11).
--   2. When a referred clinic makes their first payment, admin records it via
--      `payment: <id>|...` — the system auto-creates a referral_reward row.
--   3. Referrer gets a WhatsApp congratulation + the reward is queued.
--   4. When admin next renews the referrer's subscription, the reward is applied
--      (subscription end_date extended by 30 days) and status → 'applied'.
--
-- Run this in Supabase SQL Editor once.

-- ── referral_rewards table ────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS referral_rewards (
    id              SERIAL PRIMARY KEY,
    referrer_id     INT NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    referred_id     INT NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    reward_months   INT NOT NULL DEFAULT 1,
    status          TEXT NOT NULL DEFAULT 'pending'  -- pending | applied | cancelled
        CHECK (status IN ('pending', 'applied', 'cancelled')),
    triggered_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),   -- when referred clinic paid
    applied_at      TIMESTAMPTZ,                          -- when reward was applied
    notes           TEXT,
    UNIQUE (referrer_id, referred_id)    -- one reward per referral pair
);

-- Index for fast lookup by referrer
CREATE INDEX IF NOT EXISTS idx_referral_rewards_referrer
    ON referral_rewards(referrer_id);

-- Index for checking if a reward already exists for a referred client
CREATE INDEX IF NOT EXISTS idx_referral_rewards_referred
    ON referral_rewards(referred_id);

-- ── Ensure referral_code is backfilled for ALL existing clients ───────────────
-- (safe to re-run — only updates rows where referral_code is NULL)
UPDATE clients
SET referral_code = UPPER(SUBSTRING(MD5(id::text || COALESCE(name, '') || COALESCE(clinic_name, '')), 1, 6))
WHERE referral_code IS NULL;

-- ── Verify ────────────────────────────────────────────────────────────────────
SELECT 'referral_rewards table ready' AS status,
       COUNT(*) AS existing_rewards
FROM referral_rewards;
