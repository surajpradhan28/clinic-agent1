-- ============================================================
-- schema_v10_invoices.sql
-- Feature: Automated Monthly Invoice Generation
-- Run in Supabase SQL Editor
-- ============================================================

-- ── Invoices table ───────────────────────────────────────────
CREATE TABLE IF NOT EXISTS invoices (
    id              BIGSERIAL PRIMARY KEY,
    client_id       INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,

    -- Human-readable number e.g. INV-2025-06-001
    invoice_number  TEXT NOT NULL,

    -- Unique URL token (UUID) — used in /invoice/<token>
    invoice_token   TEXT NOT NULL UNIQUE DEFAULT gen_random_uuid()::text,

    -- Billing period
    period_start    DATE NOT NULL,          -- first day of billed month
    period_end      DATE NOT NULL,          -- last day of billed month
    due_date        DATE NOT NULL,          -- typically period_start + 5 days

    -- Amount
    amount          NUMERIC(10,2) NOT NULL,
    currency        TEXT NOT NULL DEFAULT 'INR',
    plan            TEXT NOT NULL,          -- starter | pro | suite

    -- Status lifecycle: sent → paid | overdue | cancelled
    status          TEXT NOT NULL DEFAULT 'sent',

    -- Timestamps
    sent_at         TIMESTAMPTZ DEFAULT NOW(),
    paid_at         TIMESTAMPTZ,
    notes           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- One invoice per client per billing month
    UNIQUE (client_id, period_start)
);

-- Fast token lookup (webhook + invoice page)
CREATE UNIQUE INDEX IF NOT EXISTS idx_invoices_token
    ON invoices (invoice_token);

-- Client invoice history (newest first)
CREATE INDEX IF NOT EXISTS idx_invoices_client
    ON invoices (client_id, created_at DESC);

-- Overdue check job
CREATE INDEX IF NOT EXISTS idx_invoices_status_due
    ON invoices (status, due_date);

-- ── Mark overdue invoices automatically ─────────────────────
-- Run via pg_cron or call manually; also called in the scheduler
-- UPDATE invoices SET status = 'overdue'
-- WHERE status = 'sent' AND due_date < NOW()::DATE;
