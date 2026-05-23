"""
flows/followup.py — 7-day post-visit follow-up handler (multi-tenant v4).

Triggered when a patient replies to a follow-up message.
Classifies their response as positive/neutral/negative and responds
accordingly. For positive responses, triggers the Google review flow.
"""

from __future__ import annotations

import asyncio
import logging

import database as db
import whatsapp
from config import settings
from flows.review import send_review_request

logger = logging.getLogger(__name__)

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
    lower = text.lower().strip()
    for kw in _POSITIVE_KEYWORDS:
        if kw in lower:
            return "positive"
    for kw in _NEGATIVE_KEYWORDS:
        if kw in lower:
            return "negative"
    return "neutral"


async def is_followup_response(client_id: int, phone: str) -> bool:
    """Return True if there is an active (sent, not responded) follow-up for this patient."""
    followup = db.get_active_followup_for_phone(client_id, phone)
    return followup is not None


async def handle_followup_response(phone: str, name: str, text: str, client: dict) -> None:
    """
    Process a patient's reply to a follow-up message.
    - Classify sentiment → send appropriate reply → save to DB
    - If positive → trigger Google review request (3s delay)
    """
    client_id    = client["id"]
    client_pid   = client.get("whatsapp_phone_id") or settings.WHATSAPP_PHONE_ID
    client_token = client.get("whatsapp_token") or None

    # Read live clinic info from DB
    info        = db.get_all_clinic_settings(client_id)
    clinic_name = info.get("clinic_name")  or client.get("name", "the clinic")
    doctor_name = info.get("doctor_name")  or client.get("doctor_name", "your doctor")
    review_link = info.get("google_review_link") or ""

    followup = db.get_active_followup_for_phone(client_id, phone)
    if not followup:
        logger.warning("[Followup] No active follow-up found (client=%s, phone=%s)", client_id, phone)
        return

    followup_id = followup["id"]
    appt        = followup.get("appointments") or {}
    appt_id     = appt.get("id") or followup.get("appointment_id")

    sentiment = _classify_sentiment(text)
    logger.info("[Followup] client=%s | %s → sentiment=%s", client_id, text[:40], sentiment)

    db.save_followup_response(followup_id, text, sentiment)

    if sentiment == "positive":
        reply = (
            f"Wonderful! 🎉 So glad to hear you're feeling better, {name}!\n\n"
            f"Thank you for trusting {clinic_name} with your health. "
            f"Take care and stay well! 💚"
        )
        await whatsapp.send_text(phone, reply, phone_id=client_pid, token=client_token)
        await asyncio.sleep(3)
        await send_review_request(phone, name, appt_id, client=client, review_link=review_link)

    elif sentiment == "negative":
        reply = (
            f"Sorry to hear that, {name}. 😔 Your health is our priority.\n\n"
            f"We strongly recommend scheduling a follow-up visit with "
            f"{doctor_name} so they can re-evaluate your condition.\n\n"
            f"Reply *appointment* to book a follow-up, or call us directly."
        )
        await whatsapp.send_text(phone, reply, phone_id=client_pid, token=client_token)

    else:
        reply = (
            f"Thank you for the update, {name}! 😊\n\n"
            f"Sometimes recovery takes a little more time — give it a few more days. "
            f"If you notice any discomfort or your condition worsens, please don't hesitate "
            f"to book a follow-up with {doctor_name}.\n\n"
            f"Reply *appointment* anytime to book. Take care! 🌿"
        )
        await whatsapp.send_text(phone, reply, phone_id=client_pid, token=client_token)
