-- ============================================================
-- Clinic AI Agent — Schema v2 Migration
-- Run this in Supabase SQL Editor if you already ran schema v1
-- Safe to run multiple times (uses IF NOT EXISTS / DO blocks)
-- ============================================================

-- 1. Add cancelled_at column to appointments (new in v2)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name='appointments' AND column_name='cancelled_at'
    ) THEN
        ALTER TABLE appointments ADD COLUMN cancelled_at timestamptz;
    END IF;
END$$;

-- 2. Update status check — allow 'cancelled' as valid status
--    (no constraint was added in v1, so this is informational only)
--    Status values: confirmed | completed | cancelled

-- 3. Add index on status for faster cancellation queries
CREATE INDEX IF NOT EXISTS idx_appointments_status
    ON appointments (status);

-- 4. Add index on patient_phone + status for get_upcoming_appointment
CREATE INDEX IF NOT EXISTS idx_appointments_phone_status
    ON appointments (patient_phone, status, appointment_date);

-- ============================================================
-- New Railway Environment Variables required for v2 features
-- Add these in Railway > Service > Variables
-- ============================================================
-- PLAN_TIER          = starter | pro | suite      (default: starter)
-- DOCTOR_PHONE       = 919876543210               (Suite plan only — doctor's WhatsApp)
-- DAILY_SCHEDULE_HOUR = 7                          (Suite plan only — hour in UTC, default 7)
-- ============================================================
