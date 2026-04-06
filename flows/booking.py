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

    # Format date nicely  e.g. "Wednesday, 02 April 2026"
    try:
        from datetime import datetime
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        date_display = dt.strftime("%A, %d %B %Y")
    except Exception:
        date_display = date_str

    confirmation = (
        f"✅ *Appointment Confirmed!*\n\n"
        f"👤 *Patient:* {name}\n"
        f"📅 *Date:* {date_display}\n"
        f"⏰ *Time:* {slot}\n"
        f"👨‍⚕️ *Doctor:* {settings.DOCTOR_NAME}\n"
        f"📍 *Location:* {settings.CLINIC_ADDRESS}\n\n"
        f"A reminder will be sent 24 hours before your appointment.\n\n"
        f"_To cancel or reschedule, please call the clinic._"
    )
    await whatsapp.send_text(phone, confirmation)
    logger.info(
        "[Booking] Confirmation sent to %s for appt %s", phone, appt.get("id")
    )
