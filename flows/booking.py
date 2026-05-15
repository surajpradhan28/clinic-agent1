"""
flows/booking.py — Appointment booking conversation flow (multi-tenant v4).

handle_booking_flow now accepts a `client` dict so it reads clinic info
from the correct tenant's DB rows and sends WhatsApp from the right phone_id.
"""

from __future__ import annotations

import logging
from datetime import datetime

import agent
import whatsapp
import database as db
from config import settings

logger = logging.getLogger(__name__)


async def handle_booking_flow(phone: str, name: str, text: str, client: dict) -> None:
    """
    Run the AI booking agent for an incoming message.

    Steps:
      1. Get AI reply (may include function calls for slots/booking)
      2. Send the reply via WhatsApp (using this client's phone_id)
      3. If a booking was just created, send a structured confirmation card
    """
    logger.info("[Booking] client=%s | from=%s: %s", client["id"], phone, text[:60])

    reply_text, appt_row = await agent.get_agent_reply(phone, text, client)

    client_pid = client.get("whatsapp_phone_id") or settings.WHATSAPP_PHONE_ID
    await whatsapp.send_text(phone, reply_text, phone_id=client_pid)

    if appt_row:
        await _send_booking_confirmation(phone, appt_row, client)


async def _send_booking_confirmation(phone: str, appt: dict, client: dict) -> None:
    """Send a structured appointment confirmation message."""
    client_id  = client["id"]
    client_pid = client.get("whatsapp_phone_id") or settings.WHATSAPP_PHONE_ID

    # Read live clinic info from DB (doctor may have updated via WhatsApp)
    info           = db.get_all_clinic_settings(client_id)
    clinic_name    = info.get("clinic_name")    or client.get("name", "")
    doctor_name    = info.get("doctor_name")    or client.get("doctor_name", "")
    clinic_address = info.get("clinic_address") or ""

    name     = appt.get("patient_name", "Patient")
    date_str = appt.get("appointment_date", "")
    slot     = appt.get("slot_time", "")
    appt_id  = appt.get("id", "")

    try:
        date_display = datetime.strptime(date_str, "%Y-%m-%d").strftime("%A, %d %B %Y")
    except Exception:
        date_display = date_str

    try:
        booking_id = f"BK-{date_str.replace('-', '')}-{int(appt_id):04d}"
    except Exception:
        booking_id = str(appt_id)

    plan = client.get("plan", "starter").lower()
    if plan in ("pro", "suite"):
        footer = "_To cancel or reschedule, just reply 'cancel' or 'reschedule' anytime._"
    else:
        footer = "_To cancel or reschedule, please call the clinic._"

    confirmation = (
        f"✅ *Appointment Confirmed!*\n"
        f"_{clinic_name}_\n\n"
        f"👤 *Patient:* {name}\n"
        f"👨‍⚕️ *Doctor:* {doctor_name}\n"
        f"📅 *Date:* {date_display}\n"
        f"⏰ *Time:* {slot}\n"
        f"📍 *Location:* {clinic_address}\n\n"
        f"🔖 *Booking ID:* `{booking_id}`\n\n"
        f"A reminder will be sent 24 hours before your appointment. 🙏\n\n"
        f"{footer}"
    )
    await whatsapp.send_text(phone, confirmation, phone_id=client_pid)
    logger.info("[Booking] Confirmation sent (client=%s, appt=%s)", client_id, appt_id)
