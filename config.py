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
    # Default 2 UTC = 7:30 AM IST — morning schedule arrives before clinic opens.
    # Set DAILY_SCHEDULE_HOUR=1 for 6:30 AM IST or =3 for 8:30 AM IST.
    DAILY_SCHEDULE_HOUR: int = int(os.getenv("DAILY_SCHEDULE_HOUR", "2"))  # 2 UTC = 7:30 AM IST

    # ── Recurring schedule rules ──────────────────────────────────────────────
    # Comma-separated day names when clinic is closed (e.g. "Sunday" or "Saturday,Sunday")
    WEEKLY_OFF_DAYS: list = [
        d.strip()
        for d in os.getenv("WEEKLY_OFF_DAYS", "Sunday").split(",")
        if d.strip()
    ]

    # ── Server / public URL ───────────────────────────────────────────────────
    # Used to build invoice links sent via WhatsApp.
    # Set to your Railway domain: e.g. "https://clinic-agent-production.up.railway.app"
    SERVER_URL: str = os.getenv("SERVER_URL", "https://clinic-agent-production.up.railway.app")

    # ── Subscription plan pricing (INR per month) ─────────────────────────────
    PRICE_STARTER: int = int(os.getenv("PRICE_STARTER", "999"))
    PRICE_PRO:     int = int(os.getenv("PRICE_PRO",     "1999"))
    PRICE_SUITE:   int = int(os.getenv("PRICE_SUITE",   "2999"))

    # ── Invoice settings ──────────────────────────────────────────────────────
    # Days after 1st of month before invoice is considered overdue
    INVOICE_DUE_DAYS: int = int(os.getenv("INVOICE_DUE_DAYS", "5"))
    # UPI ID shown on invoice for direct payment
    INVOICE_UPI_ID: str = os.getenv("INVOICE_UPI_ID", "yourname@upi")
    # Business name on invoice header
    INVOICE_BUSINESS_NAME: str = os.getenv("INVOICE_BUSINESS_NAME", "Clinic AI Agent Services")
    INVOICE_BUSINESS_ADDRESS: str = os.getenv("INVOICE_BUSINESS_ADDRESS", "Mumbai, Maharashtra, India")
    INVOICE_GSTIN: str = os.getenv("INVOICE_GSTIN", "")   # leave blank if not GST-registered

    # ── Scheduler ─────────────────────────────────────────────────────────────
    FOLLOWUP_DAYS: int = 7          # days after appointment to send follow-up
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
