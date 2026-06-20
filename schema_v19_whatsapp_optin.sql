-- ============================================================
-- schema_v19_whatsapp_optin.sql
-- Meta WhatsApp Compliance: Patient Opt-In Tracking
--
-- Meta requires explicit patient consent before sending ANY
-- outbound WhatsApp message to a patient who has not first
-- messaged the clinic (Business Initiated messages).
--
-- This migration adds:
--   patients.whatsapp_optin      — TRUE = consented, FALSE = opted out, NULL = unknown
--   patients.optin_timestamp     — when they first messaged (implicit opt-in)
--   patients.optout_timestamp    — when they sent STOP/UNSUBSCRIBE
-- ============================================================

ALTER TABLE patients
    ADD COLUMN IF NOT EXISTS whatsapp_optin     BOOLEAN,
    ADD COLUMN IF NOT EXISTS optin_timestamp    TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS optout_timestamp   TIMESTAMPTZ;

-- Index for fast opt-in lookups (scheduler needs this on every send)
CREATE INDEX IF NOT EXISTS idx_patients_optin
    ON patients (client_id, phone, whatsapp_optin);

-- Backfill: any patient who already has appointments is implicitly opted in
-- (they must have messaged us to book). Mark them as consented at their
-- earliest appointment date as a conservative estimate.
UPDATE patients p
SET
    whatsapp_optin  = TRUE,
    optin_timestamp = COALESCE(
        (SELECT MIN(created_at)
         FROM appointments a
         WHERE a.client_id = p.client_id
           AND a.patient_phone = p.phone),
        p.created_at
    )
WHERE whatsapp_optin IS NULL
  AND EXISTS (
      SELECT 1 FROM appointments a
      WHERE a.client_id = p.client_id
        AND a.patient_phone = p.phone
  );

COMMENT ON COLUMN patients.whatsapp_optin IS
    'TRUE = patient messaged us first (implicit opt-in per Meta policy). '
    'FALSE = patient sent STOP/UNSUBSCRIBE. NULL = no contact yet (do not send outbound).';
COMMENT ON COLUMN patients.optin_timestamp IS
    'Timestamp of first inbound message from patient (when implicit opt-in was captured).';
COMMENT ON COLUMN patients.optout_timestamp IS
    'Timestamp when patient sent STOP or UNSUBSCRIBE to opt out.';
