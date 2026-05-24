-- ============================================================
-- fix_doctor_supabase_sql.sql
-- Run this in Supabase Dashboard → SQL Editor
-- This fixes: doctor phone not recognized + doctor name display
-- ============================================================

-- STEP 1: Update the clients table
-- Sets contact_phone (used for doctor routing) and doctor_name
-- Replace client_id = 1 if your clinic has a different ID
UPDATE clients
SET
    contact_phone = '919326376895',
    doctor_name   = 'Dr. Shweta Gupta'
WHERE id = 1;

-- STEP 2: Update clinic_settings (doctor name shown in patient messages)
INSERT INTO clinic_settings (client_id, key, value)
VALUES (1, 'doctor_name', 'Dr. Shweta Gupta')
ON CONFLICT (client_id, key)
DO UPDATE SET value = EXCLUDED.value;

-- STEP 3: Verify the changes
SELECT
    id,
    name AS clinic_name,
    doctor_name,
    contact_phone,
    status
FROM clients
WHERE id = 1;

-- Expected output:
--  id | clinic_name | doctor_name       | contact_phone | status
-- ----+-------------+-------------------+---------------+--------
--   1 | ...         | Dr. Shweta Gupta  | 919326376895  | active
