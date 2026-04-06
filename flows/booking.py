"""
flows/booking.py - Appointment booking conversation flow.
Main AI-driven flow for all non-followup messages.
"""

from __future__ import annotations
import logging
import agent
import whatsapp
import database as db
from config import settings

logger = logging.getLogger(__name__)


async def handle_booking_flow(phone: str, name: str, text: str) -> None:
    logger.info("[Booking] Processing message from %s: %s", phone, text[:60])
    reply_text, appt_row = await agent.get_agent_reply(phone, text)
    await whatsapp.send_text(phone, reply_text)
    if appt_row:
        await _send_booking_confirmation(phone, appt_row)


async def _send_booking_confirmation(phone: str, appt: dict) -> None:
    name = appt.get("patient_name", "Patient")
    date_str = appt.get("appointment_date", "")
    slot = appt.get("slot_time", "")
    try:
        from datetime import datetime
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        date_display = dt.strftime("%A, %d %B %Y")
    except Exception:
        date_display = date_str
    confirmation = (
        f"Appointment Confirmed!\n\n"
        f"Patient: {name}\n"
        f"Date: {date_display}\n"
        f"Time: {slot}\n"
        f"Doctor: {settings.DOCTOR_NAME}\n"
        f"Location: {settings.CLINIC_ADDRESS}\n\n"
        f"A reminder will be sent 24 hours before your appointment.\n"
        f"To cancel or reschedule, please call the clinic."
    )
    await whatsapp.send_text(phone, confirmation)
    logger.info("[Booking] Confirmation sent to %s for appt %s", phone, appt.get("id"))
