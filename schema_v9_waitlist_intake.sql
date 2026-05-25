-- ============================================================
-- schema_v9_waitlist_intake.sql
-- Features: Appointment Waitlist + New Patient Intake Form
-- Run this in Supabase SQL editor
-- ============================================================

-- ── 1. Waitlist ──────────────────────────────────────────────
-- Stores patients waiting for a cancelled slot (FIFO per slot).

CREATE TABLE IF NOT EXISTS waitlist (
    id              BIGSERIAL PRIMARY KEY,
    client_id       INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    patient_phone   TEXT    NOT NULL,
    patient_name    TEXT    NOT NULL DEFAULT '',
    requested_date  DATE    NOT NULL,
    requested_slot  TEXT    NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (client_id, patient_phone, requested_date, requested_slot)
);

-- Index: fast lookup by slot (used on every cancellation)
CREATE INDEX IF NOT EXISTS idx_waitlist_client_slot
    ON waitlist (client_id, requested_date, requested_slot, created_at ASC);

-- Index: fast lookup by patient (used to show them their waitlist entries)
CREATE INDEX IF NOT EXISTS idx_waitlist_patient_phone
    ON waitlist (client_id, patient_phone);


-- ── 2. Patient Intake Form ───────────────────────────────────
-- Stores the brief intake collected at first booking: age, gender, chief complaint.

CREATE TABLE IF NOT EXISTS patient_intake (
    id               BIGSERIAL PRIMARY KEY,
    client_id        INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    patient_phone    TEXT    NOT NULL,
    appointment_id   BIGINT  REFERENCES appointments(id) ON DELETE SET NULL,
    age              INTEGER,
    gender           TEXT,
    chief_complaint  TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Index: latest intake per patient
CREATE INDEX IF NOT EXISTS idx_patient_intake_client_phone
    ON patient_intake (client_id, patient_phone, created_at DESC);

-- Index: lookup by appointment
CREATE INDEX IF NOT EXISTS idx_patient_intake_appointment
    ON patient_intake (appointment_id);


-- ── 3. New column on appointments ───────────────────────────
-- Tracks whether the 30-min pre-appointment intake card was sent to the doctor.

ALTER TABLE appointments
    ADD COLUMN IF NOT EXISTS intake_preview_sent BOOLEAN NOT NULL DEFAULT FALSE;
