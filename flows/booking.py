"""
flows/booking.py — Appointment booking conversation flow.

This is the main AI-driven flow. Every message that is NOT a pending
follow-up response lands here. The OpenAI agent handles the full
multi-turn booking conversation.
"""

from __future__ import annotations

import logging

import agent
import whatsapp
import database as db
from config import settings

logger = logging.getLogger(__name__)


async def handle_booking_flow(phone: str, name: str, text: str) -> None:
    """
    Run the AI booking agent for an incoming message.

    Steps:
      1. Get AI reply (may include function calls for slots/booking)
      2. Send the reply via WhatsApp
      3. If a booking was just created, send a structured confirmation card
    """
    logger.info("[Booking] Processing message from %s: %s", phone, text[:60])

    reply_text, appt_row = await agent.get_agent_reply(phone, text)

    # Send the AI's natural language reply
    await whatsapp.send_text(phone, reply_text)

    # If a new appointment was created, send a formatted confirmation card
    if appt_row:
        await _send_booking_confirmation(phone, appt_row)


async def _send_booking_confirmation(phone: str, appt: dict) -> None:
    """Send a structured appointment confirmation message."""
    name = appt.get("patient_name", "Patient")
    date_str = appt.get("appointment_date", "")
    slot = appt.get("slot_time", "")
    appt_id = appt.get("id", "")

    # Format date nicely  e.g. "Wednesday, 02 April 2026"
    try:
        from datetime import datetime
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        date_display = dt.strftime("%A, %d %B %Y")
    except Exception:
        date_display = date_str

    # Format booking ID  e.g. "CLN-20260402-0042"
    try:
        booking_id = f"BK-{date_str.replace('-', '')}-{int(appt_id):04d}"
    except Exception:
        booking_id = str(appt_id)

    # Cancel/reschedule footer — Pro/Suite patients can do it via WhatsApp
    tier = settings.PLAN_TIER.lower()
    if tier in ("pro", "suite"):
        footer = "_To cancel or reschedule, just reply 'cancel' or 'reschedule' anytime._"
    else:
        footer = "_To cancel or reschedule, please call the clinic._"

    confirmation = (
        f"✅ *Appointment Confirmed!*\n"
        f"_{settings.CLINIC_NAME}_\n\n"
        f"👤 *Patient:* {name}\n"
        f"👨‍⚕️ *Doctor:* {settings.DOCTOR_NAME}\n"
        f"📅 *Date:* {date_display}\n"
        f"⏰ *Time:* {slot}\n"
        f"📍 *Location:* {settings.CLINIC_ADDRESS}\n\n"
        f"🔖 *Booking ID:* `{booking_id}`\n\n"
        f"A reminder will be sent 24 hours before your appointment. 🙏\n\n"
        f"{footer}"
    )
    await whatsapp.send_text(phone, confirmation)
    logger.info(
        "[Booking] Confirmation sent to %s for appt %s", phone, appt.get("id")
    )
