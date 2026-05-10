-- ============================================================
-- Clinic AI Agent — Schema v3 Migration
-- Adds: blocked_slots table for doctor slot management
-- Run this in Supabase Dashboard → SQL Editor
-- ============================================================

-- blocked_slots: doctor can block specific slots or entire days
CREATE TABLE IF NOT EXISTS blocked_slots (
    id          bigserial PRIMARY KEY,
    block_date  date NOT NULL,
    slot_time   text,               -- NULL means entire day is blocked
    reason      text,
    created_at  timestamptz DEFAULT now()
);

-- Unique constraint: one block row per date+slot combo
CREATE UNIQUE INDEX IF NOT EXISTS idx_blocked_slots_unique
    ON blocked_slots (block_date, COALESCE(slot_time, '__all__'));

-- Index for fast date lookups
CREATE INDEX IF NOT EXISTS idx_blocked_slots_date
    ON blocked_slots (block_date);
