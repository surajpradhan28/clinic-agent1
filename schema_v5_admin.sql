-- =============================================================================
-- schema_v5_admin.sql — Admin features migration (v5)
--
-- Run this AFTER schema_v4_multitenant.sql
--
-- Adds:
--   1. Grace period columns to clients table
--   2. Expiry warning flags to subscriptions table
--   3. Usage tracking table (monthly booking counts)
--   4. v_monthly_usage view for analytics dashboard
--   5. Updated check_subscription_expiry() with grace period logic
-- =============================================================================


-- ── 1. Grace period columns on clients ───────────────────────────────────────

ALTER TABLE clients
  ADD COLUMN IF NOT EXISTS grace_until DATE DEFAULT NULL,
  ADD COLUMN IF NOT EXISTS suspended_at TIMESTAMPTZ DEFAULT NULL;


-- ── 2. Expiry warning flags on subscriptions ──────────────────────────────────

ALTER TABLE subscriptions
  ADD COLUMN IF NOT EXISTS warning_7d_sent BOOLEAN NOT NULL DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS warning_3d_sent BOOLEAN NOT NULL DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS grace_warning_sent BOOLEAN NOT NULL DEFAULT FALSE;


-- ── 3. Usage / analytics table ────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS usage_log (
    id          BIGSERIAL PRIMARY KEY,
    client_id   INT NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    month       DATE NOT NULL,              -- first day of the month (e.g. 2025-05-01)
    bookings    INT NOT NULL DEFAULT 0,
    cancels     INT NOT NULL DEFAULT 0,
    reschedules INT NOT NULL DEFAULT 0,
    followups   INT NOT NULL DEFAULT 0,
    reviews     INT NOT NULL DEFAULT 0,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (client_id, month)
);

-- Trigger: bump updated_at automatically
CREATE OR REPLACE FUNCTION _set_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN NEW.updated_at = NOW(); RETURN NEW; END;
$$;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
    WHERE tgname = 'usage_log_updated_at'
  ) THEN
    CREATE TRIGGER usage_log_updated_at
    BEFORE UPDATE ON usage_log
    FOR EACH ROW EXECUTE FUNCTION _set_updated_at();
  END IF;
END;
$$;


-- ── 4. v_monthly_usage view ───────────────────────────────────────────────────

CREATE OR REPLACE VIEW v_monthly_usage AS
SELECT
    ul.client_id,
    c.name           AS clinic_name,
    ul.month,
    TO_CHAR(ul.month, 'Mon YYYY') AS month_label,
    ul.bookings,
    ul.cancels,
    ul.reschedules,
    ul.followups,
    ul.reviews
FROM usage_log ul
JOIN clients c ON c.id = ul.client_id
ORDER BY ul.client_id, ul.month DESC;


-- ── 5. v_client_status view (replace v4 version with grace info) ──────────────

CREATE OR REPLACE VIEW v_client_status AS
SELECT
    c.id,
    c.name,
    c.contact_phone,
    c.whatsapp_phone_id,
    c.status,
    c.grace_until,
    c.plan,
    c.created_at,
    s.start_date,
    s.end_date,
    s.warning_7d_sent,
    s.warning_3d_sent,
    s.grace_warning_sent,
    p.amount        AS last_payment_amount,
    p.paid_at       AS last_payment_date,
    p.method        AS last_payment_method,
    -- current month usage
    ul.bookings     AS this_month_bookings,
    ul.cancels      AS this_month_cancels,
    ul.followups    AS this_month_followups,
    ul.reviews      AS this_month_reviews
FROM clients c
LEFT JOIN subscriptions s
    ON s.client_id = c.id
    AND s.status IN ('active', 'grace')
    ORDER BY s.end_date DESC
    LIMIT 1
LEFT JOIN payments p
    ON p.client_id = c.id
    ORDER BY p.paid_at DESC
    LIMIT 1
LEFT JOIN usage_log ul
    ON ul.client_id = c.id
    AND ul.month = DATE_TRUNC('month', CURRENT_DATE)::DATE;


-- ── 6. Updated check_subscription_expiry() ────────────────────────────────────
-- Grace period: 5 days after expiry before full suspension
-- Status flow: active → grace → expired → (manually reactivated)

CREATE OR REPLACE FUNCTION check_subscription_expiry()
RETURNS void LANGUAGE plpgsql AS $$
DECLARE
    grace_days INT := 5;
BEGIN
    -- Step 1: Active subscriptions whose end_date has passed → move to grace
    UPDATE subscriptions s
    SET    status = 'grace'
    WHERE  s.status = 'active'
    AND    s.end_date < CURRENT_DATE;

    -- Update clients whose active subscription just moved to grace
    UPDATE clients c
    SET    status     = 'grace',
           grace_until = (
               SELECT end_date + grace_days
               FROM   subscriptions
               WHERE  client_id = c.id
               AND    status    = 'grace'
               ORDER  BY end_date DESC
               LIMIT  1
           )
    WHERE  c.status = 'active'
    AND    EXISTS (
        SELECT 1 FROM subscriptions
        WHERE  client_id = c.id AND status = 'grace'
    );

    -- Step 2: Grace period over → fully expire
    UPDATE clients c
    SET    status       = 'expired',
           suspended_at = NOW()
    WHERE  c.status = 'grace'
    AND    c.grace_until IS NOT NULL
    AND    c.grace_until < CURRENT_DATE;

    UPDATE subscriptions s
    SET    status = 'expired'
    WHERE  s.status = 'grace'
    AND    EXISTS (
        SELECT 1 FROM clients
        WHERE  id = s.client_id AND status = 'expired'
    );
END;
$$;


-- ── 7. Helper: upsert usage counters ─────────────────────────────────────────
-- Call this from Python: db.increment_usage(client_id, "bookings")

CREATE OR REPLACE FUNCTION increment_usage(
    p_client_id INT,
    p_column    TEXT         -- 'bookings' | 'cancels' | 'reschedules' | 'followups' | 'reviews'
)
RETURNS void LANGUAGE plpgsql AS $$
DECLARE
    month_start DATE := DATE_TRUNC('month', CURRENT_DATE)::DATE;
    sql TEXT;
BEGIN
    -- Ensure row exists
    INSERT INTO usage_log (client_id, month)
    VALUES (p_client_id, month_start)
    ON CONFLICT (client_id, month) DO NOTHING;

    -- Increment the requested column (dynamic, whitelist-checked)
    IF p_column NOT IN ('bookings','cancels','reschedules','followups','reviews') THEN
        RAISE EXCEPTION 'Invalid column: %', p_column;
    END IF;
    sql := FORMAT(
        'UPDATE usage_log SET %I = %I + 1 WHERE client_id = $1 AND month = $2',
        p_column, p_column
    );
    EXECUTE sql USING p_client_id, month_start;
END;
$$;
