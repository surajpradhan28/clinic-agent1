"""
flows/followup.py - 7-day post-visit follow-up handler.
Classifies patient response as positive/neutral/negative.
"""

from __future__ import annotations
import asyncio
import logging
import database as db
import whatsapp
from config import settings
from flows.review import send_review_request

logger = logging.getLogger(__name__)

_POSITIVE_KEYWORDS = {"better", "great", "good", "well", "excellent", "fine", "recovered", "healthy", "wonderful", "fantastic", "1", "bahut accha", "theek", "achi"}
_NEGATIVE_KEYWORDS = {"worse", "bad", "sick", "pain", "not well", "3", "bura", "dard", "takleef"}


def _classify_sentiment(text: str) -> str:
    lower = text.lower().strip()
    for kw in _POSITIVE_KEYWORDS:
        if kw in lower:
            return "positive"
    for kw in _NEGATIVE_KEYWORDS:
        if kw in lower:
            return "negative"
    return "neutral"


async def is_followup_response(phone: str) -> bool:
    followup = db.get_active_followup_for_phone(phone)
    return followup is not None


async def handle_followup_response(phone: str, name: str, text: str) -> None:
    followup = db.get_active_followup_for_phone(phone)
    if not followup:
        logger.warning("[Followup] No active follow-up found for %s", phone)
        return

    followup_id = followup["id"]
    appt = followup.get("appointments") or {}
    appt_id = appt.get("id") or followup.get("appointment_id")
    sentiment = _classify_sentiment(text)
    logger.info("[Followup] %s responded '%s' -> sentiment=%s", phone, text[:40], sentiment)

    db.save_followup_response(followup_id, text, sentiment)

    if sentiment == "positive":
        reply = (
            f"Wonderful! So glad to hear you're feeling better, {name}!\n\n"
            f"Thank you for trusting {settings.CLINIC_NAME} with your health. Take care! "
        )
        await whatsapp.send_text(phone, reply)
        await asyncio.sleep(3)
        await send_review_request(phone, name, appt_id)

    elif sentiment == "negative":
        reply = (
            f"Sorry to hear that, {name}. Your health is our priority.\n\n"
            f"We recommend scheduling a follow-up visit with {settings.DOCTOR_NAME}.\n\n"
            f"Reply 'appointment' to book a follow-up, or call us directly."
        )
        await whatsapp.send_text(phone, reply)

    else:
        reply = (
            f"Thank you for the update, {name}!\n\n"
            f"Recovery sometimes takes a little more time. If you feel worse, please book a follow-up.\n\n"
            f"Reply 'appointment' anytime to book. Take care!"
        )
        await whatsapp.send_text(phone, reply)
