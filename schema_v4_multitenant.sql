-- ============================================================
-- schema_v4_multitenant.sql
-- Multi-tenant migration: clients, subscriptions, payments
-- + adds client_id to all existing tables
--
-- HOW TO RUN:
--   1. Go to Supabase Dashboard → SQL Editor
--   2. Paste this entire file and click Run
--   3. Existing data is preserved under client_id = 1
--      (a default "Client 1" row is created from your current setup)
-- ============================================================


-- ────────────────────────────────────────────────────────────
-- STEP 1: New top-level tables
-- ────────────────────────────────────────────────────────────

-- Master clients table (one row per clinic you onboard)
CREATE TABLE IF NOT EXISTS clients (
    id                  bigserial PRIMARY KEY,
    name                text NOT NULL,              -- Clinic display name
    doctor_name         text NOT NULL,              -- e.g. "Dr. Sharma"
    contact_phone       text,                       -- Doctor's personal WhatsApp (for doctor mode)
    email               text,                       -- Billing / contact email
    whatsapp_phone_id   text UNIQUE,                -- Meta phone_number_id (routes incoming msgs)
    plan                text DEFAULT 'starter',     -- starter | pro | suite
    status              text DEFAULT 'trial',       -- trial | active | suspended | expired
    trial_ends_at       date DEFAULT (CURRENT_DATE + INTERVAL '14 days'),
    notes               text,                       -- Internal notes about this client
    created_at          timestamptz DEFAULT now(),
    updated_at          timestamptz DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_clients_whatsapp_phone_id ON clients (whatsapp_phone_id);
CREATE INDEX IF NOT EXISTS idx_clients_status ON clients (status);

-- Insert your existing single client as "Client 1"
-- Update these values to match your actual clinic
INSERT INTO clients (id, name, doctor_name, contact_phone, whatsapp_phone_id, plan, status)
VALUES (
    1,
    'Dr. Sharma''s Clinic',      -- ← update to your clinic name
    'Dr. Sharma',                 -- ← update to doctor name
    '',                           -- ← set to doctor's WhatsApp number e.g. '919876543210'
    ''                            -- ← set to your Meta phone_number_id from Railway env var
)
ON CONFLICT (id) DO NOTHING;

-- Reset the sequence so next INSERT uses id >= 2
SELECT setval('clients_id_seq', GREATEST((SELECT MAX(id) FROM clients), 1));


-- Subscriptions (one active row per client at a time)
CREATE TABLE IF NOT EXISTS subscriptions (
    id              bigserial PRIMARY KEY,
    client_id       bigint NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    plan_name       text NOT NULL,                  -- starter | pro | suite
    price           numeric(10,2),                  -- Amount in INR
    billing_cycle   text DEFAULT 'monthly',         -- monthly | yearly
    start_date      date NOT NULL,
    end_date        date NOT NULL,
    status          text DEFAULT 'active',          -- active | expired | cancelled
    created_at      timestamptz DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_subscriptions_client ON subscriptions (client_id, status);


-- Payments (record every payment received)
CREATE TABLE IF NOT EXISTS payments (
    id              bigserial PRIMARY KEY,
    client_id       bigint NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    subscription_id bigint REFERENCES subscriptions(id),
    amount          numeric(10,2) NOT NULL,
    currency        text DEFAULT 'INR',
    payment_date    date NOT NULL DEFAULT CURRENT_DATE,
    due_date        date,
    method          text,                           -- UPI | cash | bank | card
    status          text DEFAULT 'paid',            -- paid | pending | overdue
    notes           text,
    created_at      timestamptz DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_payments_client ON payments (client_id);
CREATE INDEX IF NOT EXISTS idx_payments_status ON payments (status, due_date);


-- ────────────────────────────────────────────────────────────
-- STEP 2: Add client_id to all existing tables
-- (safe to re-run — checks existence first)
-- ────────────────────────────────────────────────────────────

-- patients
ALTER TABLE patients ADD COLUMN IF NOT EXISTS client_id bigint REFERENCES clients(id);
UPDATE patients SET client_id = 1 WHERE client_id IS NULL;
ALTER TABLE patients ALTER COLUMN client_id SET NOT NULL;
-- Change unique constraint from (phone) to (client_id, phone)
ALTER TABLE patients DROP CONSTRAINT IF EXISTS patients_phone_key;
ALTER TABLE patients ADD CONSTRAINT patients_client_phone_key UNIQUE (client_id, phone);
CREATE INDEX IF NOT EXISTS idx_patients_client ON patients (client_id);

-- conversations
ALTER TABLE conversations ADD COLUMN IF NOT EXISTS client_id bigint REFERENCES clients(id);
UPDATE conversations SET client_id = 1 WHERE client_id IS NULL;
ALTER TABLE conversations ALTER COLUMN client_id SET NOT NULL;
CREATE INDEX IF NOT EXISTS idx_conversations_client ON conversations (client_id, patient_phone, created_at);

-- appointments
ALTER TABLE appointments ADD COLUMN IF NOT EXISTS client_id bigint REFERENCES clients(id);
UPDATE appointments SET client_id = 1 WHERE client_id IS NULL;
ALTER TABLE appointments ALTER COLUMN client_id SET NOT NULL;
CREATE INDEX IF NOT EXISTS idx_appointments_client ON appointments (client_id, appointment_date);

-- followups (inherits client scope via appointments, but add directly for clean queries)
ALTER TABLE followups ADD COLUMN IF NOT EXISTS client_id bigint REFERENCES clients(id);
UPDATE followups SET client_id = 1 WHERE client_id IS NULL;
ALTER TABLE followups ALTER COLUMN client_id SET NOT NULL;
CREATE INDEX IF NOT EXISTS idx_followups_client ON followups (client_id, status);

-- review_requests
ALTER TABLE review_requests ADD COLUMN IF NOT EXISTS client_id bigint REFERENCES clients(id);
UPDATE review_requests SET client_id = 1 WHERE client_id IS NULL;
ALTER TABLE review_requests ALTER COLUMN client_id SET NOT NULL;

-- blocked_slots
ALTER TABLE blocked_slots ADD COLUMN IF NOT EXISTS client_id bigint REFERENCES clients(id);
UPDATE blocked_slots SET client_id = 1 WHERE client_id IS NULL;
ALTER TABLE blocked_slots ALTER COLUMN client_id SET NOT NULL;
-- Update unique constraint to include client_id
ALTER TABLE blocked_slots DROP CONSTRAINT IF EXISTS blocked_slots_block_date_slot_time_key;
ALTER TABLE blocked_slots ADD CONSTRAINT blocked_slots_client_date_slot_key
    UNIQUE (client_id, block_date, slot_time);
CREATE INDEX IF NOT EXISTS idx_blocked_slots_client ON blocked_slots (client_id, block_date);

-- clinic_settings: PK was just (key), now (client_id, key)
ALTER TABLE clinic_settings ADD COLUMN IF NOT EXISTS client_id bigint REFERENCES clients(id);
UPDATE clinic_settings SET client_id = 1 WHERE client_id IS NULL;
ALTER TABLE clinic_settings ALTER COLUMN client_id SET NOT NULL;
ALTER TABLE clinic_settings DROP CONSTRAINT IF EXISTS clinic_settings_pkey;
ALTER TABLE clinic_settings ADD PRIMARY KEY (client_id, key);

-- clinic_notes
ALTER TABLE clinic_notes ADD COLUMN IF NOT EXISTS client_id bigint REFERENCES clients(id);
UPDATE clinic_notes SET client_id = 1 WHERE client_id IS NULL;
ALTER TABLE clinic_notes ALTER COLUMN client_id SET NOT NULL;
CREATE INDEX IF NOT EXISTS idx_clinic_notes_client ON clinic_notes (client_id);

-- custom_schedule: unique was (schedule_date), now (client_id, schedule_date)
ALTER TABLE custom_schedule ADD COLUMN IF NOT EXISTS client_id bigint REFERENCES clients(id);
UPDATE custom_schedule SET client_id = 1 WHERE client_id IS NULL;
ALTER TABLE custom_schedule ALTER COLUMN client_id SET NOT NULL;
ALTER TABLE custom_schedule DROP CONSTRAINT IF EXISTS custom_schedule_schedule_date_key;
ALTER TABLE custom_schedule ADD CONSTRAINT custom_schedule_client_date_key
    UNIQUE (client_id, schedule_date);
CREATE INDEX IF NOT EXISTS idx_custom_schedule_client ON custom_schedule (client_id, schedule_date);


-- ────────────────────────────────────────────────────────────
-- STEP 3: Seed default clinic_settings for client 1
-- (only inserts rows that don't exist yet)
-- ────────────────────────────────────────────────────────────
INSERT INTO clinic_settings (client_id, key, value) VALUES
    (1, 'clinic_name',        'Dr. Sharma''s Clinic'),
    (1, 'doctor_name',        'Dr. Sharma'),
    (1, 'clinic_address',     '123 MG Road, Mumbai'),
    (1, 'clinic_phone',       ''),
    (1, 'google_review_link', 'https://g.page/r/YOUR_CLINIC_ID/review')
ON CONFLICT (client_id, key) DO NOTHING;


-- ────────────────────────────────────────────────────────────
-- STEP 4: Helper view — active clients with subscription info
-- ────────────────────────────────────────────────────────────
CREATE OR REPLACE VIEW v_client_status AS
SELECT
    c.id,
    c.name,
    c.doctor_name,
    c.contact_phone,
    c.whatsapp_phone_id,
    c.plan,
    c.status,
    c.trial_ends_at,
    s.end_date AS subscription_ends,
    s.price    AS monthly_price,
    (
        SELECT COALESCE(SUM(p.amount), 0)
        FROM payments p
        WHERE p.client_id = c.id AND p.status = 'paid'
    ) AS total_paid
FROM clients c
LEFT JOIN subscriptions s
    ON s.client_id = c.id AND s.status = 'active'
ORDER BY c.id;


-- ────────────────────────────────────────────────────────────
-- STEP 5: Expiry check function (call from your cron job)
-- Suspends clients whose subscription ended; warns 3 days before
-- ────────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION check_subscription_expiry()
RETURNS TABLE(client_id bigint, action text) AS $$
BEGIN
    -- Expire trial clients whose trial ended
    UPDATE clients
    SET status = 'expired', updated_at = now()
    WHERE status = 'trial'
      AND trial_ends_at < CURRENT_DATE;

    -- Expire active clients with no valid subscription
    UPDATE clients
    SET status = 'expired', updated_at = now()
    WHERE status = 'active'
      AND id NOT IN (
          SELECT client_id FROM subscriptions
          WHERE status = 'active' AND end_date >= CURRENT_DATE
      );

    -- Return clients expiring in 3 days (for warning messages)
    RETURN QUERY
    SELECT s.client_id, 'expiring_soon'::text
    FROM subscriptions s
    WHERE s.status = 'active'
      AND s.end_date = CURRENT_DATE + INTERVAL '3 days';
END;
$$ LANGUAGE plpgsql;


-- ────────────────────────────────────────────────────────────
-- DONE — verify with:
--   SELECT * FROM v_client_status;
--   SELECT COUNT(*) FROM clients;
-- ────────────────────────────────────────────────────────────
