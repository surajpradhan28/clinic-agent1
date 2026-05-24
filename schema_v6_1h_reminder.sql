-- schema_v6_1h_reminder.sql
-- Add 1-hour reminder flag to appointments table.
-- Run this once in Supabase SQL Editor.

ALTER TABLE appointments
  ADD COLUMN IF NOT EXISTS reminder_1h_sent BOOLEAN NOT NULL DEFAULT FALSE;

-- Index for fast scheduler lookup (only unsent reminders on confirmed future appts)
CREATE INDEX IF NOT EXISTS idx_appts_1h_reminder
  ON appointments (client_id, appointment_date, status, reminder_1h_sent)
  WHERE status = 'confirmed' AND reminder_1h_sent = FALSE;
