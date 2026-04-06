"""
flows/followup.py — 7-day post-visit follow-up handler.

Triggered when a patient replies to a follow-up message.
Classifies their response as positive/neutral/negative and responds
accordingly. For positive responses, triggers the Google review flow
after a short delay.
"""

from __future__ import annotations

import asyncio
import logging

import database as db
import whatsapp
from config import settings
from flows.review import send_review_request

logger = logging.getLogger(__name__)

# Keywords for sentiment classification
_POSITIVE_KEYWORDS = {
    "better", "great", "good", "well", "excellent", "fine",
    "recovered", "healthy", "wonderful", "fantastic", "1",
    "bahut accha", "theek", "achi", "ठीक", "अच्छा",
}
_NEGATIVE_KEYWORDS = {
    "worse", "bad", "sick", "pain", "not well", "3",
    "bura", "dard", "takleef", "बुरा", "दर्द", "तकलीफ",
}


def _classify_sentiment(text: str) -> str:
    """
    Classify patient response as positive | neutral | negative.
    Simple keyword-based classifier (no API call needed).
    """
    lower = text.lower().strip()

    # Check positive keywords
    for kw in _POSITIVE_KEYWORDS:
        if kw in lower:
            return "positive"

    # Check negative keywords
    for kw in _NEGATIVE_KEYWORDS:
        if kw in lower:
            return "negative"

    return "neutral"


async def is_followup_response(phone: str) -> bool:
    """Return True if there is an active (sent, not responded) follow-up for this patient."""
    followup = db.get_active_followup_for_phone(phone)
    return followup is not None


async def handle_followup_response(phone: str, name: str, text: str) -> None:
    """
    Process a patient's reply to a follow-up message.

      - Classify sentiment
      - Send appropriate reply
      - Save response to DB
      - If positive → trigger review request (after 3s delay)
    """
    followup = db.get_active_followup_for_phone(phone)
    if not followup:
        logger.warning("[Followup] No active follow-up found for %s", phone)
        return

    followup_id = followup["id"]
    appt = followup.get("appointments") or {}
    appt_id = appt.get("id") or followup.get("appointment_id")

    sentiment = _classify_sentiment(text)
    logger.info(
        "[Followup] %s responded '%s' → sentiment=%s", phone, text[:40], sentiment
    )

    # Save response
    db.save_followup_response(followup_id, text, sentiment)

    # Reply based on sentiment
    if sentiment == "positive":
        reply = (
            f"Wonderful! 🎉 So glad to hear you're feeling better, {name}!\n\n"
            f"Thank you for trusting {settings.CLINIC_NAME} with your health. "
            f"Take care and stay well! 💚"
        )
        await whatsapp.send_text(phone, reply)

        # Trigger Google review request after a short delay
        await asyncio.sleep(3)
        await send_review_request(phone, name, appt_id)

    elif sentiment == "negative":
        reply = (
            f"Sorry to hear that, {name}. 😔 Your health is our priority.\n\n"
            f"We strongly recommend scheduling a follow-up visit with "
            f"{settings.DOCTOR_NAME} so they can re-evaluate your condition.\n\n"
            f"Reply *appointment* to book a follow-up, or call us directly."
        )
        await whatsapp.send_text(phone, reply)

    else:  # neutral
        reply = (
            f"Thank you for the update, {name}! 😊\n\n"
            f"Sometimes recovery takes a little more time — give it a few more days. "
            f"If you notice any discomfort or your condition worsens, please don't hesitate "
            f"to book a follow-up with {settings.DOCTOR_NAME}.\n\n"
            f"Reply *appointment* anytime to book. Take care! 🌿"
        )
        await whatsapp.send_text(phone, reply)
