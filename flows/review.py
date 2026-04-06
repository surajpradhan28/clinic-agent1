"""
flows/review.py - Google review request sender (idempotent).
Triggered only after positive follow-up sentiment.
"""

from __future__ import annotations
import logging
import database as db
import whatsapp
from config import settings

logger = logging.getLogger(__name__)


async def send_review_request(phone: str, name: str, appt_id) -> None:
    if not appt_id:
        logger.warning("[Review] No appointment ID - skipping review request for %s", phone)
        return

    if db.has_review_been_requested(phone, appt_id):
        logger.info("[Review] Already requested for %s appt %s - skipping", phone, appt_id)
        return

    message = (
        f"Thank you, {name}!\n\n"
        f"We are so happy you are feeling better. Your feedback means the world to us.\n\n"
        f"If {settings.DOCTOR_NAME} helped you, please take 30 seconds to leave us a "
        f"Google review. It helps other patients find us!\n\n"
        f"{settings.GOOGLE_REVIEW_LINK}\n\n"
        f"Thank you for choosing {settings.CLINIC_NAME}!"
    )

    success = await whatsapp.send_text(phone, message)
    if success:
        db.log_review_request(phone, appt_id)
        logger.info("[Review] Review request sent to %s for appt %s", phone, appt_id)
    else:
        logger.error("[Review] Failed to send review request to %s", phone)
