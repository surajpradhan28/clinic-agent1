"""
flows/review.py — Google review request sender (multi-tenant v4).

Rules:
  - Triggered ONLY after a positive follow-up sentiment.
  - Sent at most ONCE per appointment (idempotency guard via review_requests table).
"""

from __future__ import annotations

import logging

import database as db
import whatsapp
from config import settings

logger = logging.getLogger(__name__)


async def send_review_request(
    phone: str,
    name: str,
    appt_id: int | None,
    client: dict,
    review_link: str = "",
) -> None:
    """
    Send a Google review request to a happy patient.
    client: full client row (for phone_id and clinic info).
    review_link: from clinic_settings, passed in from followup.py.
    """
    if not appt_id:
        logger.warning("[Review] No appointment ID — skipping for %s", phone)
        return

    client_id  = client["id"]
    client_pid = client.get("whatsapp_phone_id") or settings.WHATSAPP_PHONE_ID

    if db.has_review_been_requested(client_id, phone, appt_id):
        logger.info("[Review] Already sent (client=%s, appt=%s) — skipping", client_id, appt_id)
        return

    info        = db.get_all_clinic_settings(client_id)
    clinic_name = info.get("clinic_name")  or client.get("name", "the clinic")
    doctor_name = info.get("doctor_name")  or client.get("doctor_name", "your doctor")
    link        = review_link or info.get("google_review_link") or ""

    if not link:
        logger.info("[Review] No review link set for client=%s — skipping", client_id)
        return

    message = (
        f"🙏 *Thank you, {name}!*\n\n"
        f"We're so happy you're feeling better. Your feedback means the world to us.\n\n"
        f"If {doctor_name} helped you, please take 30 seconds to leave us a "
        f"Google review. It helps other patients find us! ⭐⭐⭐⭐⭐\n\n"
        f"👉 {link}\n\n"
        f"Thank you for choosing {clinic_name}! 💚"
    )

    success = await whatsapp.send_text(phone, message, phone_id=client_pid)
    if success:
        db.log_review_request(client_id, phone, appt_id)
        logger.info("[Review] Request sent (client=%s, appt=%s)", client_id, appt_id)
    else:
        logger.error("[Review] Failed to send (client=%s, phone=%s)", client_id, phone)
