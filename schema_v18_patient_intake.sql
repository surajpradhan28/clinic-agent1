-- schema_v18_patient_intake.sql
-- Creates the patient_intake table used for new-patient intake collection.
-- Run once in Supabase SQL Editor.
--
-- The agent collects age, gender, and chief complaint before the first visit.
-- This table stores those responses keyed by appointment so the bot knows
-- not to ask again on subsequent messages.

CREATE TABLE IF NOT EXISTS patient_intake (
    id              BIGSERIAL PRIMARY KEY,
    client_id       INTEGER       NOT NULL,
    patient_phone   TEXT          NOT NULL,
    appointment_id  BIGINT        NOT NULL,
    age             INTEGER,
    gender          TEXT,
    chief_complaint TEXT,
    created_at      TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);

-- Fast lookup: does an intake record exist for a given appointment?
CREATE INDEX IF NOT EXISTS idx_patient_intake_appt
    ON patient_intake (client_id, patient_phone, appointment_id);

-- Fast lookup: all intake records for a patient
CREATE INDEX IF NOT EXISTS idx_patient_intake_patient
    ON patient_intake (client_id, patient_phone);

SELECT 'schema_v18_patient_intake applied' AS status;
