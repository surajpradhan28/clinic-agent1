"""
config.py — All environment variables for the Clinic AI Agent.
Load from .env file locally; set as Railway environment variables in production.
"""

import os
from dotenv import load_dotenv

load_dotenv()


class Settings:
    # ── WhatsApp / Meta Cloud API ─────────────────────────────────────────────
    WHATSAPP_TOKEN: str = os.getenv("WHATSAPP_TOKEN", "")
    WHATSAPP_PHONE_ID: str = os.getenv("WHATSAPP_PHONE_ID", "")
    WHATSAPP_VERIFY_TOKEN: str = os.getenv("WHATSAPP_VERIFY_TOKEN", "clinic_secret_abc")
    # App Secret from Meta Developer Console → Your App → Settings → Basic.
    # Used to verify X-Hub-Signature-256 on incoming webhooks.
    # Leave blank to skip verification (not recommended for production).
    WHATSAPP_APP_SECRET: str = os.getenv("WHATSAPP_APP_SECRET", "")

    # ── OpenAI ────────────────────────────────────────────────────────────────
    OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")
    OPENAI_MODEL: str = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

    # ── Supabase ──────────────────────────────────────────────────────────────
    SUPABASE_URL: str = os.getenv("SUPABASE_URL", "")
    SUPABASE_KEY: str = os.getenv("SUPABASE_KEY", "")

    # ── Clinic Info ───────────────────────────────────────────────────────────
    CLINIC_NAME: str = os.getenv("CLINIC_NAME", "Dr. Sharma's Clinic")
    DOCTOR_NAME: str = os.getenv("DOCTOR_NAME", "Dr. Sharma")
    CLINIC_ADDRESS: str = os.getenv("CLINIC_ADDRESS", "123 MG Road, Mumbai")
    GOOGLE_REVIEW_LINK: str = os.getenv(
        "GOOGLE_REVIEW_LINK", "https://g.page/r/YOUR_CLINIC_ID/review"
    )

    # ── Clinic Schedule ───────────────────────────────────────────────────────
    # Morning slots: 10:00 – 13:00 every 30 min
    MORNING_START: str = "10:00"
    MORNING_END: str = "13:00"
    # Evening slots: 17:00 – 20:00 every 30 min
    EVENING_START: str = "17:00"
    EVENING_END: str = "20:00"
    SLOT_DURATION_MIN: int = 30

    # ── Plan Tier ─────────────────────────────────────────────────────────────
    # starter : booking + 24h reminders
    # pro     : + cancellation + reschedule
    # suite   : + daily doctor schedule WhatsApp
    PLAN_TIER: str = os.getenv("PLAN_TIER", "starter")  # starter | pro | suite

    # ── Super-Admin ───────────────────────────────────────────────────────────
    # WhatsApp number of the super-admin (you / service owner).
    # Messages from this number to ANY clinic's WhatsApp are routed to admin.py.
    # Format: country code + number, no '+' or spaces (e.g. "919999999999")
    ADMIN_PHONE: str = os.getenv("ADMIN_PHONE", "")

    # Secret key to access the web admin dashboard at /admin?key=<ADMIN_SECRET>
    ADMIN_SECRET: str = os.getenv("ADMIN_SECRET", "change_me_in_production")

    # Grace period (days) between subscription expiry and full suspension
    GRACE_PERIOD_DAYS: int = int(os.getenv("GRACE_PERIOD_DAYS", "5"))

    # ── Doctor WhatsApp (Suite plan — daily schedule) ─────────────────────────
    DOCTOR_PHONE: str = os.getenv("DOCTOR_PHONE", "")   # e.g. "919876543210"
    # Default 1:30 UTC = 7:00 AM IST — morning schedule arrives before clinic opens.
    # Override with DAILY_SCHEDULE_HOUR + DAILY_SCHEDULE_MINUTE env vars.
    DAILY_SCHEDULE_HOUR: int = int(os.getenv("DAILY_SCHEDULE_HOUR", "1"))   # 1 UTC = 6:30 AM IST base
    DAILY_SCHEDULE_MINUTE: int = int(os.getenv("DAILY_SCHEDULE_MINUTE", "30"))  # +30 min → 7:00 AM IST

    # ── Recurring schedule rules ──────────────────────────────────────────────
    # Comma-separated day names when clinic is closed (e.g. "Sunday" or "Saturday,Sunday")
    WEEKLY_OFF_DAYS: list = [
        d.strip()
        for d in os.getenv("WEEKLY_OFF_DAYS", "Sunday").split(",")
        if d.strip()
    ]

    # ── Server / public URL ───────────────────────────────────────────────────
    # Used to build invoice links sent via WhatsApp.
    # Set to your Railway domain: e.g. "https://clinic-agent1-production-9d21.up.railway.app"
    SERVER_URL: str = os.getenv("SERVER_URL", "https://clinic-agent1-production-9d21.up.railway.app")

    # ── Google Calendar OAuth ─────────────────────────────────────────────────
    # Create at: Google Cloud Console → APIs & Services → Credentials → OAuth 2.0 Client ID
    # Authorized redirect URI must include: <SERVER_URL>/calendar/callback
    GOOGLE_CLIENT_ID:     str = os.getenv("GOOGLE_CLIENT_ID",     "")
    GOOGLE_CLIENT_SECRET: str = os.getenv("GOOGLE_CLIENT_SECRET", "")

    # ── Subscription plan pricing (INR per month) ─────────────────────────────
    PRICE_STARTER: int = int(os.getenv("PRICE_STARTER", "999"))
    PRICE_PRO:     int = int(os.getenv("PRICE_PRO",     "1999"))
    PRICE_SUITE:   int = int(os.getenv("PRICE_SUITE",   "2999"))
    # Annual pricing = 10 months price (pay 10, get 12 — ~17% off)
    PRICE_STARTER_ANNUAL: int = int(os.getenv("PRICE_STARTER_ANNUAL", "9990"))
    PRICE_PRO_ANNUAL:     int = int(os.getenv("PRICE_PRO_ANNUAL",     "19990"))
    PRICE_SUITE_ANNUAL:   int = int(os.getenv("PRICE_SUITE_ANNUAL",   "29990"))
    # One-time setup fee charged on trial-to-paid conversion
    SETUP_FEE:     int = int(os.getenv("SETUP_FEE",     "1499"))

    # ── Razorpay (UPI auto-pay) ───────────────────────────────────────────────
    # Create at https://dashboard.razorpay.com → Settings → API Keys
    # Leave blank to skip Razorpay and fall back to manual UPI.
    RAZORPAY_KEY_ID:         str = os.getenv("RAZORPAY_KEY_ID",         "")
    RAZORPAY_KEY_SECRET:     str = os.getenv("RAZORPAY_KEY_SECRET",     "")
    # Webhook secret set at Razorpay Dashboard → Webhooks → Secret
    RAZORPAY_WEBHOOK_SECRET: str = os.getenv("RAZORPAY_WEBHOOK_SECRET", "")

    # ── Invoice settings ──────────────────────────────────────────────────────
    # Days after 1st of month before invoice is considered overdue
    INVOICE_DUE_DAYS: int = int(os.getenv("INVOICE_DUE_DAYS", "5"))
    # UPI ID shown on invoice for direct payment
    INVOICE_UPI_ID: str = os.getenv("INVOICE_UPI_ID", "yourname@upi")
    # Business name on invoice header
    INVOICE_BUSINESS_NAME: str = os.getenv("INVOICE_BUSINESS_NAME", "Clinic AI Agent Services")
    INVOICE_BUSINESS_ADDRESS: str = os.getenv("INVOICE_BUSINESS_ADDRESS", "Mumbai, Maharashtra, India")
    INVOICE_GSTIN: str = os.getenv("INVOICE_GSTIN", "")   # leave blank if not GST-registered

    # ── Admin email notifications ─────────────────────────────────────────────
    # Email address to receive signup alerts (usually same as SMTP_USER)
    ADMIN_EMAIL: str = os.getenv("ADMIN_EMAIL", "")
    SMTP_HOST:   str = os.getenv("SMTP_HOST",   "smtp.gmail.com")
    SMTP_PORT:   int = int(os.getenv("SMTP_PORT", "587"))
    SMTP_USER:   str = os.getenv("SMTP_USER",   "")
    SMTP_PASS:   str = os.getenv("SMTP_PASS",   "")

    # ── Error monitoring ──────────────────────────────────────────────────────
    # Get DSN from https://sentry.io → Your Project → Settings → Client Keys
    # Leave blank to disable Sentry (no errors will be reported).
    SENTRY_DSN: str = os.getenv("SENTRY_DSN", "")

    # ── PostHog analytics ─────────────────────────────────────────────────────
    # Get from https://app.posthog.com → Project Settings → Project API Key
    # Leave blank to disable analytics (bot works fine without it).
    POSTHOG_API_KEY: str = os.getenv("POSTHOG_API_KEY", "")
    POSTHOG_HOST:    str = os.getenv("POSTHOG_HOST", "https://app.posthog.com")

    # ── Conversation history ───────────────────────────────────────────────────
    # How many past messages to send to OpenAI per turn.
    # Higher = better memory, slightly more tokens per request.
    # A typical booking takes ~10 messages; keep well above that.
    PATIENT_HISTORY_LIMIT: int = int(os.getenv("PATIENT_HISTORY_LIMIT", "20"))
    DOCTOR_HISTORY_LIMIT:  int = int(os.getenv("DOCTOR_HISTORY_LIMIT",  "16"))

    # ── Scheduler ─────────────────────────────────────────────────────────────
    FOLLOWUP_DAYS: int = 2          # days after appointment to send follow-up (default 2)
    REMINDER_HOURS_BEFORE: int = 24  # hours before appointment to send reminder
    JOB_INTERVAL_HOURS: int = 1     # scheduler poll interval

    def validate(self) -> None:
        """Raise if critical env vars are missing."""
        required = {
            "WHATSAPP_TOKEN": self.WHATSAPP_TOKEN,
            "WHATSAPP_PHONE_ID": self.WHATSAPP_PHONE_ID,
            "OPENAI_API_KEY": self.OPENAI_API_KEY,
            "SUPABASE_URL": self.SUPABASE_URL,
            "SUPABASE_KEY": self.SUPABASE_KEY,
        }
        missing = [k for k, v in required.items() if not v]
        if missing:
            raise EnvironmentError(
                f"Missing required environment variables: {', '.join(missing)}"
            )


settings = Settings()
