"""
scheduler.py — APScheduler background jobs.

Two jobs run every hour:
  Job 1 (send_followups)  — Finds pending follow-ups due ±30 min from now
                             and sends 7-day post-visit WhatsApp messages.
  Job 2 (send_reminders)  — Finds appointments ~24h away and sends reminder.

Start the scheduler by calling start() from main.py on app startup.
"""

from __future__ import annotations

import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

import database as db
import whatsapp
from config import settings

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()


# ── Job 1: Send 7-day follow-ups ──────────────────────────────────────────────

async def _run_followups() -> None:
    """
    Scan followups table for rows that are:
      - status = 'pending'
      - scheduled_at within ±30 min of now

    Send the follow-up message and mark as 'sent'.
    """
    logger.info("[Scheduler] Running follow-up job…")
    try:
        due = db.get_pending_followups()
        logger.info("[Scheduler] %d follow-up(s) due", len(due))

        for row in due:
            appt = row.get("appointments") or {}
            phone = appt.get("patient_phone") or ""
            name = appt.get("patient_name") or "there"
            followup_id = row["id"]

            if not phone:
                logger.warning("Follow-up %s has no phone — skipping", followup_id)
                continue

            message = (
                f"Hello {name}! 😊 Hope you're feeling better.\n\n"
                f"It's been 7 days since your visit to {settings.CLINIC_NAME}. "
                f"How are you feeling?\n\n"
                f"Reply:\n"
                f"1️⃣  *Better* — I'm feeling great!\n"
                f"2️⃣  *Same* — About the same\n"
                f"3️⃣  *Worse* — Not feeling well"
            )

            success = await whatsapp.send_text(phone, message)
            if success:
                db.mark_followup_sent(followup_id)
                logger.info(
                    "[Scheduler] Follow-up sent to %s (followup_id=%s)", phone, followup_id
                )
            else:
                logger.error(
                    "[Scheduler] Failed to send follow-up to %s", phone
                )

    except Exception as exc:
        logger.error("[Scheduler] Follow-up job error: %s", exc, exc_info=True)


# ── Job 2: Send 24-hour appointment reminders ─────────────────────────────────

async def _run_reminders() -> None:
    """
    Scan appointments where:
      - status = 'confirmed'
      - reminder_sent = False
      - appointment is ~24h away (within 23–25h window)

    Send reminder message and mark reminder_sent = True.
    """
    logger.info("[Scheduler] Running reminder job…")
    try:
        due = db.get_appointments_for_reminder()
        logger.info("[Scheduler] %d reminder(s) due", len(due))

        for appt in due:
            phone = appt["patient_phone"]
            name = appt["patient_name"]
            date = appt["appointment_date"]
            slot = appt["slot_time"]
            appt_id = appt["id"]

            # Format date nicely
            try:
                from datetime import datetime
                dt = datetime.strptime(date, "%Y-%m-%d")
                date_display = dt.strftime("%d %B %Y")  # e.g. 02 April 2026
            except Exception:
                date_display = date

            message = (
                f"📅 *Appointment Reminder*\n\n"
                f"Hello {name}! This is a reminder from {settings.CLINIC_NAME}.\n\n"
                f"Your appointment with {settings.DOCTOR_NAME} is *tomorrow* at *{slot}*.\n\n"
                f"📍 {settings.CLINIC_ADDRESS}\n\n"
                f"Please arrive 5-10 minutes early. See you tomorrow! 😊"
            )

            success = await whatsapp.send_text(phone, message)
            if success:
                db.mark_reminder_sent(appt_id)
                logger.info(
                    "[Scheduler] Reminder sent to %s for appt %s", phone, appt_id
                )
            else:
                logger.error(
                    "[Scheduler] Failed to send reminder to %s", phone
                )

    except Exception as exc:
        logger.error("[Scheduler] Reminder job error: %s", exc, exc_info=True)


# ── Scheduler lifecycle ───────────────────────────────────────────────────────

def start() -> None:
    """Register jobs and start the scheduler. Call once on app startup."""
    scheduler.add_job(
        _run_followups,
        trigger=IntervalTrigger(hours=settings.JOB_INTERVAL_HOURS),
        id="send_followups",
        replace_existing=True,
        misfire_grace_time=300,  # 5 min grace if scheduler was down
    )
    scheduler.add_job(
        _run_reminders,
        trigger=IntervalTrigger(hours=settings.JOB_INTERVAL_HOURS),
        id="send_reminders",
        replace_existing=True,
        misfire_grace_time=300,
    )
    scheduler.start()
    logger.info(
        "[Scheduler] Started — follow-up + reminder jobs every %dh",
        settings.JOB_INTERVAL_HOURS,
    )


def stop() -> None:
    """Gracefully shut down the scheduler on app shutdown."""
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("[Scheduler] Stopped")
