"""
scheduler.py - APScheduler background jobs for follow-ups and reminders.
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


async def _run_followups() -> None:
    logger.info("[Scheduler] Running follow-up job")
    try:
        due = db.get_pending_followups()
        for row in due:
            appt = row.get("appointments") or {}
            phone = appt.get("patient_phone") or ""
            name = appt.get("patient_name") or "there"
            followup_id = row["id"]
            if not phone:
                continue
            message = (
                f"Hello {name}! Hope you are feeling better.\n\n"
                f"It has been 7 days since your visit to {settings.CLINIC_NAME}. "
                f"How are you feeling?\n\nReply:\n"
                f"1 Better - I am feeling great!\n"
                f"2 Same - About the same\n"
                f"3 Worse - Not feeling well"
            )
            success = await whatsapp.send_text(phone, message)
            if success:
                db.mark_followup_sent(followup_id)
    except Exception as exc:
        logger.error("[Scheduler] Follow-up job error: %s", exc, exc_info=True)


async def _run_reminders() -> None:
    logger.info("[Scheduler] Running reminder job")
    try:
        due = db.get_appointments_for_reminder()
        for appt in due:
            phone = appt["patient_phone"]
            name = appt["patient_name"]
            date = appt["appointment_date"]
            slot = appt["slot_time"]
            appt_id = appt["id"]
            try:
                from datetime import datetime
                dt = datetime.strptime(date, "%Y-%m-%d")
                date_display = dt.strftime("%d %B %Y")
            except Exception:
                date_display = date
            message = (
                f"Appointment Reminder\n\n"
                f"Hello {name}! Reminder from {settings.CLINIC_NAME}.\n\n"
                f"Your appointment with {settings.DOCTOR_NAME} is tomorrow at {slot}.\n\n"
                f"{settings.CLINIC_ADDRESS}\n\nPlease arrive 5-10 minutes early!"
            )
            success = await whatsapp.send_text(phone, message)
            if success:
                db.mark_reminder_sent(appt_id)
    except Exception as exc:
        logger.error("[Scheduler] Reminder job error: %s", exc, exc_info=True)


def start() -> None:
    scheduler.add_job(_run_followups, trigger=IntervalTrigger(hours=settings.JOB_INTERVAL_HOURS), id="send_followups", replace_existing=True, misfire_grace_time=300)
    scheduler.add_job(_run_reminders, trigger=IntervalTrigger(hours=settings.JOB_INTERVAL_HOURS), id="send_reminders", replace_existing=True, misfire_grace_time=300)
    scheduler.start()
    logger.info("[Scheduler] Started")


def stop() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("[Scheduler] Stopped")
