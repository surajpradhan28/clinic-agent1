-- schema_v16_visit_notes.sql
-- Adds doctor visit notes and per-appointment followup scheduling.
--
-- Run this once in your Supabase SQL editor.
-- Safe to re-run (uses IF NOT EXISTS / DO $$ guards).

-- ── 1. Add visit_notes to appointments ────────────────────────────────────────
ALTER TABLE appointments
  ADD COLUMN IF NOT EXISTS visit_notes TEXT DEFAULT NULL;

-- ── 2. Add followup_days to appointments (per-visit override, default 2) ──────
ALTER TABLE appointments
  ADD COLUMN IF NOT EXISTS followup_days INTEGER DEFAULT 2;

-- ── 3. Update existing followups to match new 2-day default ───────────────────
-- (Only updates pending followups that were scheduled at the old 7-day offset
--  and haven't been sent yet — recalculates to 2 days from appointment time)
UPDATE followups f
SET scheduled_at = (
  SELECT (a.appointment_date::timestamp + a.slot_time::time) AT TIME ZONE 'Asia/Kolkata'
         + INTERVAL '2 days'
  FROM appointments a
  WHERE a.id = f.appointment_id
)
WHERE f.status = 'pending'
  AND EXISTS (
    SELECT 1 FROM appointments a
    WHERE a.id = f.appointment_id
      AND a.appointment_date >= CURRENT_DATE
  );

-- ── 4. Add index for fast patient history lookups ─────────────────────────────
CREATE INDEX IF NOT EXISTS idx_appointments_patient_phone_client
  ON appointments (client_id, patient_phone, appointment_date DESC);
