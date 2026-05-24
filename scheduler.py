"""
scheduler.py — APScheduler background jobs (multi-tenant v5).

Jobs run on the same interval as before, but now loop over ALL active clients.
Each job fetches pending work per client_id and sends messages via that
client's WhatsApp phone_number_id so replies come from the correct clinic number.

Jobs:
  1. send_followups        — 7-day post-visit check (every hour)
  2. send_reminders        — 24h appointment reminder (every hour)
  3. daily_doctor_schedule — Morning schedule to doctor (daily cron)
  4. check_expiry          — Grace period + expiry warnings (daily cron at 2am UTC)
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta, timezone

# Indian Standard Time constant — used wherever local clinic date/time is needed
_IST = timezone(timedelta(hours=5, minutes=30))

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
    logger.info("[Scheduler] Running follow-up job…")
    try:
        clients = db.get_all_active_clients()
        for client in clients:
            client_id    = client["id"]
            client_pid   = client.get("whatsapp_phone_id") or settings.WHATSAPP_PHONE_ID
            client_token = client.get("whatsapp_token") or None
            # Read doctor name from clinic_settings (doctor may have updated it via WhatsApp)
            db_settings = db.get_all_clinic_settings(client_id)
            doctor_name = db_settings.get("doctor_name") or client.get("doctor_name") or "your doctor"

            due = db.get_pending_followups(client_id)
            if not due:
                continue

            logger.info("[Scheduler] Client %s: %d follow-up(s) due", client_id, len(due))
            for row in due:
                appt       = row.get("appointments") or {}
                phone      = appt.get("patient_phone") or ""
                name       = appt.get("patient_name") or "there"
                followup_id = row["id"]
                if not phone:
                    continue

                message = (
                    f"Hi *{name}!* 👋\n\n"
                    f"It's been a week since your visit with *{doctor_name}*. "
                    f"How are you feeling now?\n\n"
                    f"1️⃣  *Better / Recovered* 😊\n"
                    f"2️⃣  *Same as before* 😐\n"
                    f"3️⃣  *Not well / Getting worse* 😔"
                )
                success = await whatsapp.send_text(phone, message, phone_id=client_pid, token=client_token)
                if success:
                    db.mark_followup_sent(followup_id)
                    logger.info("[Scheduler] Follow-up sent (client=%s, followup=%s)", client_id, followup_id)
                else:
                    logger.error("[Scheduler] Failed to send follow-up (client=%s, phone=%s)", client_id, phone)

    except Exception as exc:
        logger.error("[Scheduler] Follow-up job error: %s", exc, exc_info=True)


# ── Job 2: Send 24-hour appointment reminders ─────────────────────────────────

async def _run_reminders() -> None:
    logger.info("[Scheduler] Running reminder job…")
    try:
        clients = db.get_all_active_clients()
        for client in clients:
            client_id    = client["id"]
            client_pid   = client.get("whatsapp_phone_id") or settings.WHATSAPP_PHONE_ID
            client_token = client.get("whatsapp_token") or None
            db_settings = db.get_all_clinic_settings(client_id)
            clinic_name    = db_settings.get("clinic_name")    or client.get("name", "")
            doctor_name    = db_settings.get("doctor_name")    or client.get("doctor_name", "")
            clinic_address = db_settings.get("clinic_address") or ""

            due = db.get_appointments_for_reminder(client_id)
            if not due:
                continue

            logger.info("[Scheduler] Client %s: %d reminder(s) due", client_id, len(due))
            for appt in due:
                phone   = appt["patient_phone"]
                name    = appt["patient_name"]
                appt_date = appt["appointment_date"]
                slot    = appt["slot_time"]
                appt_id = appt["id"]

                try:
                    from datetime import datetime
                    date_display = datetime.strptime(appt_date, "%Y-%m-%d").strftime("%d %B %Y")
                except Exception:
                    date_display = appt_date

                message = (
                    f"⏰ *Appointment Reminder!*\n\n"
                    f"Hi *{name}!* This is a reminder for your appointment tomorrow.\n\n"
                    f"🏥 *{clinic_name}*\n"
                    f"👨‍⚕️ {doctor_name}\n"
                    f"📅 *{date_display}*\n"
                    f"⏰ *{slot}*\n"
                    f"📍 {clinic_address}\n\n"
                    f"Please arrive 5-10 minutes early. See you tomorrow! 🙏"
                )
                success = await whatsapp.send_text(phone, message, phone_id=client_pid, token=client_token)
                if success:
                    db.mark_reminder_sent(appt_id)
                    logger.info("[Scheduler] Reminder sent (client=%s, appt=%s)", client_id, appt_id)
                else:
                    logger.error("[Scheduler] Failed to send reminder (client=%s, phone=%s)", client_id, phone)

    except Exception as exc:
        logger.error("[Scheduler] Reminder job error: %s", exc, exc_info=True)


# ── Job 2b: Send 1-hour appointment reminders ────────────────────────────────

async def _run_1h_reminders() -> None:
    logger.info("[Scheduler] Running 1-hour reminder job…")
    try:
        clients = db.get_all_active_clients()
        for client in clients:
            client_id    = client["id"]
            client_pid   = client.get("whatsapp_phone_id") or settings.WHATSAPP_PHONE_ID
            client_token = client.get("whatsapp_token") or None
            db_settings  = db.get_all_clinic_settings(client_id)
            clinic_name    = db_settings.get("clinic_name")    or client.get("name", "")
            doctor_name    = db_settings.get("doctor_name")    or client.get("doctor_name", "")
            clinic_address = db_settings.get("clinic_address") or ""

            due = db.get_appointments_for_1h_reminder(client_id)
            if not due:
                continue

            logger.info("[Scheduler] Client %s: %d 1h-reminder(s) due", client_id, len(due))
            for appt in due:
                phone     = appt["patient_phone"]
                name      = appt["patient_name"]
                appt_date = appt["appointment_date"]
                slot      = appt["slot_time"]
                appt_id   = appt["id"]

                try:
                    from datetime import datetime as _dt
                    date_display = _dt.strptime(appt_date, "%Y-%m-%d").strftime("%d %B %Y")
                except Exception:
                    date_display = appt_date

                message = (
                    f"⏰ *Appointment in 1 Hour!*\n\n"
                    f"Hi *{name}!* Just a quick reminder — your appointment is *in about 1 hour*.\n\n"
                    f"🏥 *{clinic_name}*\n"
                    f"👨‍⚕️ {doctor_name}\n"
                    f"📅 {date_display}\n"
                    f"⏰ *{slot}*\n"
                    f"📍 {clinic_address}\n\n"
                    f"Please leave now to arrive on time. See you soon! 🙏"
                )
                success = await whatsapp.send_text(phone, message, phone_id=client_pid, token=client_token)
                if success:
                    db.mark_1h_reminder_sent(appt_id)
                    logger.info("[Scheduler] 1h reminder sent (client=%s, appt=%s)", client_id, appt_id)
                else:
                    logger.error("[Scheduler] Failed to send 1h reminder (client=%s, phone=%s)", client_id, phone)

    except Exception as exc:
        logger.error("[Scheduler] 1h reminder job error: %s", exc, exc_info=True)


# ── Job 3: Send daily appointment schedule to each doctor ─────────────────────

async def _run_daily_doctor_schedule() -> None:
    """
    Every morning, send each active doctor the day's appointment list.
    Uses client.contact_phone as the doctor's WhatsApp number.
    Falls back to env DOCTOR_PHONE for the first client (legacy).
    """
    logger.info("[Scheduler] Running daily doctor schedule job…")
    # Use IST date — Railway runs in UTC, but clinic day boundaries are IST
    today_ist     = datetime.now(_IST)
    today_str     = today_ist.strftime("%Y-%m-%d")
    today_display = today_ist.strftime("%d %B %Y (%A)")

    try:
        clients = db.get_all_active_clients()
        for client in clients:
            client_id    = client["id"]
            client_pid   = client.get("whatsapp_phone_id") or settings.WHATSAPP_PHONE_ID
            client_token = client.get("whatsapp_token") or None
            doctor_phone = (client.get("contact_phone") or "").strip() or (
                settings.DOCTOR_PHONE if client_id == 1 else ""
            )
            if not doctor_phone:
                logger.debug("[Scheduler] Client %s: no contact_phone — skipping daily schedule", client_id)
                continue

            db_settings = db.get_all_clinic_settings(client_id)
            doctor_name = db_settings.get("doctor_name") or client.get("doctor_name", "Doctor")
            first_name  = doctor_name.split()[-1]

            appointments = db.get_appointments_for_date(client_id, today_str)

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

            success = await whatsapp.send_text(doctor_phone, message, phone_id=client_pid, token=client_token)
            if success:
                logger.info(
                    "[Scheduler] Daily schedule sent to client=%s doctor (%d appts)",
                    client_id, len(appointments),
                )
            else:
                logger.error("[Scheduler] Failed to send daily schedule (client=%s)", client_id)

    except Exception as exc:
        logger.error("[Scheduler] Daily schedule job error: %s", exc, exc_info=True)


# ── Job 4: Check subscription expiry + grace period warnings ──────────────────

async def _run_expiry_check() -> None:
    """
    Daily job (2am UTC) that:
    1. Sends 7-day expiry warning to doctors (first time only)
    2. Sends 3-day expiry warning to doctors (first time only)
    3. Runs check_subscription_expiry() SQL fn → active→grace→expired transitions
    4. Notifies doctors entering grace period
    """
    logger.info("[Scheduler] Running subscription expiry check…")
    try:
        from datetime import date as _date
        db_conn = db.get_db()
        today   = _date.today()

        # ── A. 7-day advance warning ──────────────────────────────────────────
        warn7_date = (today + timedelta(days=7)).isoformat()
        subs7 = (
            db_conn.table("subscriptions")
            .select("id, client_id, end_date")
            .eq("status", "active")
            .eq("end_date", warn7_date)
            .eq("warning_7d_sent", False)
            .execute()
        ).data or []

        for sub in subs7:
            client = db.get_client_by_id(sub["client_id"])
            if not client:
                continue
            doctor_phone = (client.get("contact_phone") or "").strip()
            client_pid   = client.get("whatsapp_phone_id") or settings.WHATSAPP_PHONE_ID
            client_token = client.get("whatsapp_token") or None
            if not doctor_phone:
                continue
            msg = (
                f"📅 *Subscription Renewal Reminder*\n\n"
                f"Your Clinic AI Agent subscription expires in *7 days* "
                f"(on {sub['end_date']}).\n\n"
                f"After expiry you'll have a {settings.GRACE_PERIOD_DAYS}-day grace period, "
                f"then service pauses.\n\n"
                f"Please renew now to avoid interruption. Contact support. 🙏"
            )
            success = await whatsapp.send_text(doctor_phone, msg, phone_id=client_pid, token=client_token)
            if success:
                db_conn.table("subscriptions").update({"warning_7d_sent": True})\
                    .eq("id", sub["id"]).execute()
                logger.info("[Scheduler] 7-day warning sent to client=%s", sub["client_id"])

        # ── B. 3-day advance warning ──────────────────────────────────────────
        warn3_date = (today + timedelta(days=3)).isoformat()
        subs3 = (
            db_conn.table("subscriptions")
            .select("id, client_id, end_date")
            .eq("status", "active")
            .eq("end_date", warn3_date)
            .eq("warning_3d_sent", False)
            .execute()
        ).data or []

        for sub in subs3:
            client = db.get_client_by_id(sub["client_id"])
            if not client:
                continue
            doctor_phone = (client.get("contact_phone") or "").strip()
            client_pid   = client.get("whatsapp_phone_id") or settings.WHATSAPP_PHONE_ID
            client_token = client.get("whatsapp_token") or None
            if not doctor_phone:
                continue
            msg = (
                f"⚠️ *Subscription Expiring in 3 Days!*\n\n"
                f"Your Clinic AI Agent subscription expires on *{sub['end_date']}*.\n\n"
                f"After that you'll have a {settings.GRACE_PERIOD_DAYS}-day grace period, "
                f"then the bot will stop responding to patients.\n\n"
                f"Please renew NOW. Contact support immediately. 🙏"
            )
            success = await whatsapp.send_text(doctor_phone, msg, phone_id=client_pid, token=client_token)
            if success:
                db_conn.table("subscriptions").update({"warning_3d_sent": True})\
                    .eq("id", sub["id"]).execute()
                logger.info("[Scheduler] 3-day warning sent to client=%s", sub["client_id"])

        # ── C. Run expiry transitions (active→grace→expired) ──────────────────
        db_conn.rpc("check_subscription_expiry").execute()
        logger.info("[Scheduler] check_subscription_expiry() executed")

        # ── D. Notify doctors who just entered grace period ───────────────────
        grace_clients = (
            db_conn.table("clients")
            .select("id, contact_phone, whatsapp_phone_id, name, grace_until")
            .eq("status", "grace")
            .execute()
        ).data or []

        for client in grace_clients:
            cid = client["id"]
            # Check if grace_warning already sent (use subscriptions flag)
            sub_row = (
                db_conn.table("subscriptions")
                .select("id, grace_warning_sent, end_date")
                .eq("client_id", cid)
                .eq("status", "grace")
                .order("end_date", desc=True)
                .limit(1)
                .execute()
            ).data or []
            if not sub_row or sub_row[0].get("grace_warning_sent"):
                continue

            doctor_phone = (client.get("contact_phone") or "").strip()
            client_pid   = client.get("whatsapp_phone_id") or settings.WHATSAPP_PHONE_ID
            client_token = client.get("whatsapp_token") or None
            grace_until  = client.get("grace_until") or "unknown"

            if not doctor_phone:
                continue

            msg = (
                f"🔴 *Subscription Expired — Grace Period Active*\n\n"
                f"Your Clinic AI Agent subscription has expired.\n\n"
                f"✅ Bot is still running until *{grace_until}* (grace period).\n\n"
                f"Please renew before {grace_until} to keep service uninterrupted.\n"
                f"Contact support to renew. 🙏"
            )
            success = await whatsapp.send_text(doctor_phone, msg, phone_id=client_pid, token=client_token)
            if success:
                db_conn.table("subscriptions").update({"grace_warning_sent": True})\
                    .eq("id", sub_row[0]["id"]).execute()
                logger.info("[Scheduler] Grace period warning sent to client=%s", cid)

    except Exception as exc:
        logger.error("[Scheduler] Expiry check error: %s", exc, exc_info=True)


# ── Scheduler lifecycle ───────────────────────────────────────────────────────

def start() -> None:
    scheduler.add_job(
        _run_followups,
        trigger=IntervalTrigger(hours=settings.JOB_INTERVAL_HOURS),
        id="send_followups", replace_existing=True, misfire_grace_time=300,
    )
    scheduler.add_job(
        _run_reminders,
        trigger=IntervalTrigger(hours=settings.JOB_INTERVAL_HOURS),
        id="send_reminders", replace_existing=True, misfire_grace_time=300,
    )
    scheduler.add_job(
        _run_1h_reminders,
        trigger=IntervalTrigger(minutes=15),   # Check every 15 min for precision
        id="send_1h_reminders", replace_existing=True, misfire_grace_time=120,
    )
    scheduler.add_job(
        _run_daily_doctor_schedule,
        trigger=CronTrigger(hour=settings.DAILY_SCHEDULE_HOUR, minute=0),
        id="daily_doctor_schedule", replace_existing=True, misfire_grace_time=600,
    )
    scheduler.add_job(
        _run_expiry_check,
        trigger=CronTrigger(hour=2, minute=0),   # 2am UTC daily (7:30am IST)
        id="expiry_check", replace_existing=True, misfire_grace_time=1800,
    )

    scheduler.start()
    logger.info(
        "[Scheduler] Started — followups + 24h-reminders every %dh | "
        "1h-reminders every 15min | daily schedule at %02d:00 UTC | expiry check at 02:00 UTC",
        settings.JOB_INTERVAL_HOURS,
        settings.DAILY_SCHEDULE_HOUR,
    )


def stop() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("[Scheduler] Stopped")
