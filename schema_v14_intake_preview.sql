-- schema_v14_intake_preview.sql
-- Add intake_preview_sent column to appointments table.
-- Run once in Supabase SQL Editor.
--
-- This flag prevents the 30-min pre-appointment intake preview
-- from being sent more than once per appointment.

ALTER TABLE appointments
    ADD COLUMN IF NOT EXISTS intake_preview_sent BOOLEAN DEFAULT FALSE;

-- Index for fast scheduler lookup (confirmed, not yet sent)
CREATE INDEX IF NOT EXISTS idx_appointments_intake_preview
    ON appointments (client_id, status, intake_preview_sent)
    WHERE status = 'confirmed' AND intake_preview_sent = FALSE;

SELECT 'schema_v14_intake_preview applied' AS status;
