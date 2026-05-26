-- schema_v13_razorpay.sql
-- Add Razorpay payment-link columns to invoices table.
-- Run once in Supabase SQL Editor.
--
-- Flow after this migration:
--   1. Scheduler creates invoice → calls Razorpay API → stores link_id + short_url
--   2. Doctor opens /invoice/<token> → sees "Pay Now" button (Razorpay link)
--   3. Doctor pays (UPI/card/netbanking) on Razorpay's hosted page
--   4. Razorpay fires POST /razorpay/webhook (event: payment_link.paid)
--   5. We verify HMAC-SHA256 signature, find invoice by razorpay_payment_link_id,
--      mark paid, record payment, update subscription, WhatsApp doctor "✅ Payment received"

ALTER TABLE invoices
    ADD COLUMN IF NOT EXISTS razorpay_payment_link_id  TEXT,      -- plink_xxx
    ADD COLUMN IF NOT EXISTS razorpay_payment_link_url TEXT,      -- https://rzp.io/xxx
    ADD COLUMN IF NOT EXISTS razorpay_payment_id       TEXT;      -- pay_xxx (set on payment)

-- Fast webhook lookup: find invoice from Razorpay's plink_id
CREATE UNIQUE INDEX IF NOT EXISTS idx_invoices_razorpay_link
    ON invoices (razorpay_payment_link_id)
    WHERE razorpay_payment_link_id IS NOT NULL;

SELECT 'schema_v13_razorpay applied' AS status;
