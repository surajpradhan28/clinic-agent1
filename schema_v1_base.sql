-- ============================================================
-- schema_v1_base.sql
-- MyWhatsApp Clinic — Base Schema (all core tables)
-- Run this on a FRESH Supabase project before any v2+ migrations.
-- Safe to re-run: uses CREATE TABLE IF NOT EXISTS throughout.
-- ============================================================
-- Tables defined here:
--   appointments, conversations, clinic_settings, clinic_notes,
--   followups, custom_schedule, review_requests, patients,
--   blocked_slots
-- (clients, subscriptions, payments, waitlist, patient_intake,
--  invoices, oauth_tokens, referral_rewards are in later migrations)
-- ============================================================


-- ── 1. patients ───────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS patients (
    id          BIGSERIAL    PRIMARY KEY,
    client_id   INTEGER      NOT NULL,
    phone       TEXT         NOT NULL,
    name        TEXT,
    language    TEXT         NOT NULL DEFAULT 'en',
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    UNIQUE (client_id, phone)
);
CREATE INDEX IF NOT EXISTS idx_patients_client_phone
    ON patients (client_id, phone);


-- ── 2. appointments ───────────────────────────────────────────
CREATE TABLE IF NOT EXISTS appointments (
    id                  BIGSERIAL    PRIMARY KEY,
    client_id           INTEGER      NOT NULL,
    patient_phone       TEXT         NOT NULL,
    patient_name        TEXT         NOT NULL DEFAULT '',
    appointment_date    DATE         NOT NULL,
    slot_time           TEXT         NOT NULL,   -- "HH:MM" 24-hour format
    status              TEXT         NOT NULL DEFAULT 'confirmed',
    -- status values: confirmed | completed | cancelled
    visit_notes         TEXT,
    followup_days       INTEGER,
    reminder_sent       BOOLEAN      NOT NULL DEFAULT FALSE,
    reminder_1h_sent    BOOLEAN      NOT NULL DEFAULT FALSE,
    intake_preview_sent BOOLEAN      NOT NULL DEFAULT FALSE,
    cancelled_at        TIMESTAMPTZ,
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_appointments_client_date
    ON appointments (client_id, appointment_date);
CREATE INDEX IF NOT EXISTS idx_appointments_phone_status
    ON appointments (patient_phone, status, appointment_date);
CREATE INDEX IF NOT EXISTS idx_appointments_status
    ON appointments (status);
CREATE INDEX IF NOT EXISTS idx_appointments_client_slot
    ON appointments (client_id, appointment_date, slot_time, status);


-- ── 3. conversations ──────────────────────────────────────────
-- Stores the last N messages per patient for AI context.
CREATE TABLE IF NOT EXISTS conversations (
    id            BIGSERIAL    PRIMARY KEY,
    client_id     INTEGER      NOT NULL,
    patient_phone TEXT         NOT NULL,
    role          TEXT         NOT NULL,   -- 'user' | 'assistant'
    content       TEXT         NOT NULL,
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_conversations_client_phone
    ON conversations (client_id, patient_phone, created_at DESC);


-- ── 4. clinic_settings ────────────────────────────────────────
-- Key-value store for per-clinic configuration.
-- Keys include: clinic_name, doctor_name, clinic_address, clinic_phone,
--               weekly_off_days, custom_notes, etc.
CREATE TABLE IF NOT EXISTS clinic_settings (
    id          BIGSERIAL    PRIMARY KEY,
    client_id   INTEGER      NOT NULL,
    key         TEXT         NOT NULL,
    value       TEXT,
    updated_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    UNIQUE (client_id, key)
);
CREATE INDEX IF NOT EXISTS idx_clinic_settings_client_key
    ON clinic_settings (client_id, key);


-- ── 5. clinic_notes ───────────────────────────────────────────
-- Doctor's private notes about the clinic / operational notes.
CREATE TABLE IF NOT EXISTS clinic_notes (
    id          BIGSERIAL    PRIMARY KEY,
    client_id   INTEGER      NOT NULL,
    note        TEXT         NOT NULL,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_clinic_notes_client
    ON clinic_notes (client_id, created_at DESC);


-- ── 6. followups ──────────────────────────────────────────────
-- Scheduled post-appointment follow-up messages.
CREATE TABLE IF NOT EXISTS followups (
    id               BIGSERIAL    PRIMARY KEY,
    client_id        INTEGER      NOT NULL,
    appointment_id   BIGINT       NOT NULL REFERENCES appointments(id) ON DELETE CASCADE,
    scheduled_at     TIMESTAMPTZ  NOT NULL,
    sent_at          TIMESTAMPTZ,
    status           TEXT         NOT NULL DEFAULT 'pending',
    -- status values: pending | sent | responded | skipped
    patient_response TEXT,
    sentiment        TEXT,
    responded_at     TIMESTAMPTZ,
    created_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_followups_client_scheduled
    ON followups (client_id, scheduled_at, status);
CREATE INDEX IF NOT EXISTS idx_followups_appointment
    ON followups (appointment_id);


-- ── 7. custom_schedule ────────────────────────────────────────
-- Per-date slot overrides (special hours, holidays with custom hours).
CREATE TABLE IF NOT EXISTS custom_schedule (
    id                BIGSERIAL   PRIMARY KEY,
    client_id         INTEGER     NOT NULL,
    schedule_date     DATE        NOT NULL,
    morning_start     TEXT,       -- "HH:MM" or NULL to skip morning
    morning_end       TEXT,
    evening_start     TEXT,       -- "HH:MM" or NULL to skip evening
    evening_end       TEXT,
    slot_duration_min INTEGER     NOT NULL DEFAULT 30,
    note              TEXT        NOT NULL DEFAULT '',
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (client_id, schedule_date)
);
CREATE INDEX IF NOT EXISTS idx_custom_schedule_client_date
    ON custom_schedule (client_id, schedule_date);


-- ── 8. blocked_slots ──────────────────────────────────────────
-- Manually blocked time slots (doctor out, lunch break, etc.)
-- slot_time = NULL means the entire day is blocked.
CREATE TABLE IF NOT EXISTS blocked_slots (
    id          BIGSERIAL   PRIMARY KEY,
    client_id   INTEGER     NOT NULL,
    block_date  DATE        NOT NULL,
    slot_time   TEXT,       -- NULL = entire day blocked
    reason      TEXT        NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (client_id, block_date, slot_time)
);
CREATE INDEX IF NOT EXISTS idx_blocked_slots_client_date
    ON blocked_slots (client_id, block_date);


-- ── 9. review_requests ────────────────────────────────────────
-- Tracks which patients have been sent a Google review request.
CREATE TABLE IF NOT EXISTS review_requests (
    id              BIGSERIAL   PRIMARY KEY,
    client_id       INTEGER     NOT NULL,
    patient_phone   TEXT        NOT NULL,
    appointment_id  BIGINT      REFERENCES appointments(id) ON DELETE SET NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (client_id, patient_phone, appointment_id)
);
CREATE INDEX IF NOT EXISTS idx_review_requests_client
    ON review_requests (client_id, patient_phone);


SELECT 'schema_v1_base applied — all core tables ready' AS status;
