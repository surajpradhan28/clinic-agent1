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

from datetime import date, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
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
                f"Hi *{name}!* 👋\n\n"
                f"It's been a week since your visit with *{settings.DOCTOR_NAME}.* "
                f"How are you feeling now?\n\n"
                f"1️⃣  *Better / Recovered* 😊\n"
                f"2️⃣  *Same as before* 😐\n"
                f"3️⃣  *Not well / Getting worse* 😔"
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
                f"⏰ *Appointment Reminder!*\n\n"
                f"Hi *{name}!* This is a reminder for your appointment tomorrow.\n\n"
                f"🏥 *{settings.CLINIC_NAME}*\n"
                f"👨‍⚕️ {settings.DOCTOR_NAME}\n"
                f"📅 *{date_display}*\n"
                f"⏰ *{slot}*\n"
                f"📍 {settings.CLINIC_ADDRESS}\n\n"
                f"Please arrive 5-10 minutes early. See you tomorrow! 🙏"
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


# ── Job 3: Send daily appointment schedule to doctor (Suite plan only) ────────

async def _run_daily_doctor_schedule() -> None:
    """
    Every morning at DAILY_SCHEDULE_HOUR (UTC), send the doctor's WhatsApp
    a formatted list of today's confirmed appointments.
    Only active on Suite plan with DOCTOR_PHONE set.
    """
    if settings.PLAN_TIER.lower() != "suite":
        return
    if not settings.DOCTOR_PHONE:
        logger.warning("[Scheduler] Daily schedule: DOCTOR_PHONE not set — skipping")
        return

    today_str = date.today().strftime("%Y-%m-%d")
    today_display = date.today().strftime("%d %B %Y (%A)")

    logger.info("[Scheduler] Sending daily schedule for %s to doctor", today_str)
    try:
        appointments = db.get_appointments_for_date(today_str)

        # Extract doctor's first name for a warm greeting
        first_name = settings.DOCTOR_NAME.split()[-1]  # e.g. "Dr. Nishat Shaikh" → "Shaikh"

        if not appointments:
            message = (
                f"☀️ *Good Morning, Dr. {first_name}!*\n\n"
                f"No appointments scheduled for today.\n"
                f"Have a relaxing day! 😊"
            )
        else:
            lines = [
                f"☀️ *Good Morning, Dr. {first_name}!*",
                f"Your schedule for *{today_display}*\n",
            ]
            for appt in appointments:
                lines.append(f"⏰ *{appt['slot_time']}*  —  {appt['patient_name']}")
            lines.append(f"\n_{len(appointments)} appointment(s) today. Have a great day! 🏥_")
            message = "\n".join(lines)

        success = await whatsapp.send_text(settings.DOCTOR_PHONE, message)
        if success:
            logger.info("[Scheduler] Daily schedule sent to doctor (%s appts)", len(appointments))
        else:
            logger.error("[Scheduler] Failed to send daily schedule to doctor")

    except Exception as exc:
        logger.error("[Scheduler] Daily schedule job error: %s", exc, exc_info=True)


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

    # Daily doctor schedule — Suite plan only
    if settings.PLAN_TIER.lower() == "suite" and settings.DOCTOR_PHONE:
        scheduler.add_job(
            _run_daily_doctor_schedule,
            trigger=CronTrigger(hour=settings.DAILY_SCHEDULE_HOUR, minute=0),
            id="daily_doctor_schedule",
            replace_existing=True,
            misfire_grace_time=600,
        )
        logger.info(
            "[Scheduler] Daily doctor schedule job registered — fires at %02d:00 UTC",
            settings.DAILY_SCHEDULE_HOUR,
        )

    scheduler.start()
    logger.info(
        "[Scheduler] Started — follow-up + reminder jobs every %dh (plan: %s)",
        settings.JOB_INTERVAL_HOURS,
        settings.PLAN_TIER,
    )


def stop() -> None:
    """Gracefully shut down the scheduler on app shutdown."""
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("[Scheduler] Stopped")
