"""
config.py - All environment variables for the Clinic AI Agent.
Load from .env file locally; set as Railway environment variables in production.
"""

import os
from dotenv import load_dotenv

load_dotenv()


class Settings:
    # WhatsApp / Meta Cloud API
    WHATSAPP_TOKEN: str = os.getenv("WHATSAPP_TOKEN", "")
    WHATSAPP_PHONE_ID: str = os.getenv("WHATSAPP_PHONE_ID", "")
    WHATSAPP_VERIFY_TOKEN: str = os.getenv("WHATSAPP_VERIFY_TOKEN", "clinic_secret_abc")

    # OpenAI
    OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")
    OPENAI_MODEL: str = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

    # Supabase
    SUPABASE_URL: str = os.getenv("SUPABASE_URL", "")
    SUPABASE_KEY: str = os.getenv("SUPABASE_KEY", "")

    # Clinic Info
    CLINIC_NAME: str = os.getenv("CLINIC_NAME", "Dr. Sharma's Clinic")
    DOCTOR_NAME: str = os.getenv("DOCTOR_NAME", "Dr. Sharma")
    CLINIC_ADDRESS: str = os.getenv("CLINIC_ADDRESS", "123 MG Road, Mumbai")
    GOOGLE_REVIEW_LINK: str = os.getenv("GOOGLE_REVIEW_LINK", "https://g.page/r/YOUR_CLINIC_ID/review")

    # Clinic Schedule
    MORNING_START: str = "10:00"
    MORNING_END: str = "13:00"
    EVENING_START: str = "17:00"
    EVENING_END: str = "20:00"
    SLOT_DURATION_MIN: int = 30

    # Scheduler
    FOLLOWUP_DAYS: int = 7
    REMINDER_HOURS_BEFORE: int = 24
    JOB_INTERVAL_HOURS: int = 1

    def validate(self) -> None:
        required = {
            "WHATSAPP_TOKEN": self.WHATSAPP_TOKEN,
            "WHATSAPP_PHONE_ID": self.WHATSAPP_PHONE_ID,
            "OPENAI_API_KEY": self.OPENAI_API_KEY,
            "SUPABASE_URL": self.SUPABASE_URL,
            "SUPABASE_KEY": self.SUPABASE_KEY,
        }
        missing = [k for k, v in required.items() if not v]
        if missing:
            raise EnvironmentError(f"Missing required environment variables: {', '.join(missing)}")


settings = Settings()
