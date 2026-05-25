"""
scheduler.py — APScheduler background jobs (multi-tenant v5).

Jobs run on the same interval as before, but now loop over ALL active clients.
Each job fetches pending work per client_id and sends messages via that
client's WhatsApp phone_number_id so replies come from the correct clinic number.

Jobs:
  1. send_followups        — 7-day post-visit check (every hour)
  2. send_reminders        — 24h appointment reminder (every hour)
  3. send_1h_reminders     — 1-hour appointment reminder (every 15 min)
  4. daily_doctor_schedule — Morning schedule to doctor (daily cron)
  5. check_expiry          — Grace period + expiry warnings (daily cron at 2am UTC)
  6. intake_previews       — Patient intake card to doctor 30 min before appt (every 15 min)
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

                fallback = (
                    f"Hi *{name}!* 👋\n\n"
                    f"It's been a week since your visit with *{doctor_name}*. "
                    f"How are you feeling now?\n\n"
                    f"1️⃣  *Better / Recovered* 😊\n"
                    f"2️⃣  *Same as before* 😐\n"
                    f"3️⃣  *Not well / Getting worse* 😔"
                )
                success = await whatsapp.send_template_or_text(
                    phone,
                    template_name="clinic_followup_checkup",
                    body_params=[name, doctor_name],
                    fallback_text=fallback,
                    phone_id=client_pid, token=client_token,
                )
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

                fallback = (
                    f"⏰ *Appointment Reminder!*\n\n"
                    f"Hi *{name}!* This is a reminder for your appointment tomorrow.\n\n"
                    f"🏥 *{clinic_name}*\n"
                    f"👨‍⚕️ {doctor_name}\n"
                    f"📅 *{date_display}*\n"
                    f"⏰ *{slot}*\n"
                    f"📍 {clinic_address}\n\n"
                    f"Please arrive 5-10 minutes early. See you tomorrow! 🙏"
                )
                success = await whatsapp.send_template_or_text(
                    phone,
                    template_name="clinic_appt_reminder_24h",
                    body_params=[name, clinic_name, date_display, slot, clinic_address],
                    fallback_text=fallback,
                    phone_id=client_pid, token=client_token,
                )
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

                fallback = (
                    f"⏰ *Appointment in 1 Hour!*\n\n"
                    f"Hi *{name}!* Just a quick reminder — your appointment is *in about 1 hour*.\n\n"
                    f"🏥 *{clinic_name}*\n"
                    f"👨‍⚕️ {doctor_name}\n"
                    f"📅 {date_display}\n"
                    f"⏰ *{slot}*\n"
                    f"📍 {clinic_address}\n\n"
                    f"Please leave now to arrive on time. See you soon! 🙏"
                )
                success = await whatsapp.send_template_or_text(
                    phone,
                    template_name="clinic_appt_reminder_1h",
                    body_params=[name, clinic_name, date_display, slot, clinic_address],
                    fallback_text=fallback,
                    phone_id=client_pid, token=client_token,
                )
                if success:
                    db.mark_1h_reminder_sent(appt_id)
                    logger.info("[Scheduler] 1h reminder sent (client=%s, appt=%s)", client_id, appt_id)
                else:
                    logger.error("[Scheduler] Failed to send 1h reminder (client=%s, phone=%s)", client_id, phone)

    except Exception as exc:
        logger.error("[Scheduler] 1h reminder job error: %s", exc, exc_info=True)


# ── Job 2c: Send patient intake card to doctor 30 min before appointment ──────

async def _run_intake_previews() -> None:
    """
    Every 15 minutes, check for appointments 25–35 min away.
    If the patient submitted an intake form at first booking,
    send a summary card to the doctor so they can prepare.
    """
    logger.info("[Scheduler] Running intake preview job…")
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
                continue

            due = db.get_appointments_for_intake_preview(client_id)
            for appt in due:
                # Always mark as sent first — so we don't re-process on next tick
                db.mark_intake_preview_sent(appt["id"])

                intake = db.get_patient_intake(client_id, appt["patient_phone"])
                if not intake:
                    # No intake collected (e.g., returning patient) — nothing to send
                    continue

                try:
                    date_display = datetime.strptime(
                        appt["appointment_date"], "%Y-%m-%d"
                    ).strftime("%d %B %Y")
                except Exception:
                    date_display = appt["appointment_date"]

                age_str     = str(intake.get("age"))     if intake.get("age")     else "Not provided"
                gender_str  = intake.get("gender")       or "Not provided"
                complaint   = intake.get("chief_complaint") or "Not provided"

                msg = (
                    f"📋 *Patient Intake — Appointment in ~30 min*\n\n"
                    f"👤 *{appt['patient_name']}*\n"
                    f"📱 {appt['patient_phone']}\n"
                    f"📅 {date_display} at *{appt['slot_time']}*\n\n"
                    f"🔢 Age      : {age_str}\n"
                    f"⚧  Gender   : {gender_str}\n"
                    f"🩺 Complaint: _{complaint}_\n\n"
                    f"_Collected via WhatsApp at first booking._"
                )
                success = await whatsapp.send_text(
                    doctor_phone, msg, phone_id=client_pid, token=client_token
                )
                if success:
                    logger.info(
                        "[Scheduler] Intake preview sent (client=%s, appt=%s)",
                        client_id, appt["id"],
                    )
                else:
                    logger.error(
                        "[Scheduler] Failed to send intake preview (client=%s, appt=%s)",
                        client_id, appt["id"],
                    )

    except Exception as exc:
        logger.error("[Scheduler] Intake preview job error: %s", exc, exc_info=True)


# ── Job 2d: Monthly invoices on the 1st of each month ────────────────────────

async def _run_monthly_invoices() -> None:
    """
    Runs on the 1st of every month at 8:30 AM IST.
    For every active client:
      1. Determine the billing period (current month).
      2. Skip if invoice already exists for this period.
      3. Create invoice record with unique token.
      4. Send WhatsApp message to the clinic's contact phone with the invoice link.
    Also marks any overdue invoices during the daily expiry check.
    """
    logger.info("[Scheduler] Running monthly invoice job…")
    now      = datetime.now(_IST)
    year     = now.year
    month    = now.month

    # Billing period = this calendar month
    from calendar import monthrange as _mr
    last_day = _mr(year, month)[1]
    period_start = f"{year:04d}-{month:02d}-01"
    period_end   = f"{year:04d}-{month:02d}-{last_day:02d}"
    due_date     = (now + timedelta(days=settings.INVOICE_DUE_DAYS)).strftime("%Y-%m-%d")

    plan_prices = {
        "starter": settings.PRICE_STARTER,
        "pro":     settings.PRICE_PRO,
        "suite":   settings.PRICE_SUITE,
    }

    try:
        clients = db.get_all_active_clients()
        sent_count = 0

        for client in clients:
            client_id    = client["id"]
            client_pid   = client.get("whatsapp_phone_id") or settings.WHATSAPP_PHONE_ID
            client_token = client.get("whatsapp_token") or None

            # Contact phone for invoice delivery (prefer contact_phone, fall back to doctor phone)
            contact_phone = (
                (client.get("contact_phone") or "").strip()
                or (settings.DOCTOR_PHONE if client_id == 1 else "")
            )
            if not contact_phone:
                logger.warning("[Invoices] No contact phone for client=%s — skipping", client_id)
                continue

            # Skip if already generated for this period (idempotent)
            if db.invoice_exists(client_id, period_start):
                logger.info("[Invoices] Invoice already exists for client=%s %s — skip", client_id, period_start)
                continue

            plan   = (client.get("plan") or "starter").lower()
            amount = float(plan_prices.get(plan, settings.PRICE_STARTER))

            clinic_name = (
                db.get_all_clinic_settings(client_id).get("clinic_name")
                or client.get("clinic_name", "Your Clinic")
            )

            try:
                invoice = db.create_invoice(
                    client_id   = client_id,
                    period_start= period_start,
                    period_end  = period_end,
                    due_date    = due_date,
                    amount      = amount,
                    plan        = plan,
                )
            except Exception as db_exc:
                logger.error("[Invoices] DB error for client=%s: %s", client_id, db_exc)
                continue

            invoice_url = f"{settings.SERVER_URL}/invoice/{invoice['invoice_token']}"
            plan_label  = plan.title()
            month_name  = now.strftime("%B %Y")
            due_display = datetime.strptime(due_date, "%Y-%m-%d").strftime("%d %B %Y")
            amount_str  = f"₹{amount:,.0f}"

            fallback_msg = (
                f"🧾 *Invoice for {month_name}*\n\n"
                f"Hi! Here is your monthly invoice for *{clinic_name}*.\n\n"
                f"📋 Invoice No.: *{invoice['invoice_number']}*\n"
                f"📦 Plan: *{plan_label}*\n"
                f"💰 Amount: *{amount_str}*\n"
                f"📅 Due by: *{due_display}*\n\n"
                f"🔗 View invoice:\n{invoice_url}\n\n"
                f"Please pay via UPI to *{settings.INVOICE_UPI_ID}* and send us the screenshot to confirm renewal. "
                f"Thank you! 🙏"
            )

            success = await whatsapp.send_template_or_text(
                contact_phone,
                template_name="clinic_invoice_monthly",
                body_params=[
                    clinic_name,
                    invoice["invoice_number"],
                    amount_str,
                    due_display,
                    invoice_url,
                ],
                fallback_text=fallback_msg,
                phone_id=client_pid, token=client_token,
            )
            if success:
                sent_count += 1
                logger.info(
                    "[Invoices] Sent %s to client=%s (%s)",
                    invoice["invoice_number"], client_id, contact_phone,
                )
            else:
                logger.error("[Invoices] Failed to send to client=%s", client_id)

        # Mark any overdue invoices
        overdue_count = db.mark_overdue_invoices()
        logger.info(
            "[Invoices] Monthly job done — sent=%d, newly_overdue=%d",
            sent_count, overdue_count,
        )

    except Exception as exc:
        logger.error("[Invoices] Monthly job error: %s", exc, exc_info=True)


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

        # ── A0. 14-day advance warning — renewal offer with early-bird discount ──
        warn14_date = (today + timedelta(days=14)).isoformat()
        subs14 = (
            db_conn.table("subscriptions")
            .select("id, client_id, end_date")
            .eq("status", "active")
            .eq("end_date", warn14_date)
            .eq("warning_14d_sent", False)
            .execute()
        ).data or []

        for sub in subs14:
            client = db.get_client_by_id(sub["client_id"])
            if not client:
                continue
            doctor_phone = (client.get("contact_phone") or "").strip()
            client_pid   = client.get("whatsapp_phone_id") or settings.WHATSAPP_PHONE_ID
            client_token = client.get("whatsapp_token") or None
            db_settings  = db.get_all_clinic_settings(sub["client_id"])
            doctor_name  = (db_settings.get("doctor_name") or client.get("doctor_name") or "Doctor").split()[-1]
            if not doctor_phone:
                continue
            end_date_str = str(sub["end_date"])[:10]
            # Early-renewal offer: renew within 7 days to get 1 free month
            early_deadline = (today + timedelta(days=7)).strftime("%d %B %Y")
            msg = (
                f"🌟 *Time to Renew Your Clinic AI Agent!*\n\n"
                f"Hi Dr. {doctor_name}! 👋\n\n"
                f"Your subscription expires in *14 days* (on {end_date_str}).\n\n"
                f"✨ *Early Renewal Offer* — Renew before *{early_deadline}* and get *1 month FREE* added to your next plan!\n\n"
                f"Here's what you keep when you renew:\n"
                f"📲 24/7 WhatsApp appointment booking\n"
                f"⏰ Automated 24h + 1h reminders\n"
                f"📋 Daily schedule to your WhatsApp\n"
                f"📢 Patient broadcast messages\n"
                f"📊 Follow-up & recovery tracking\n\n"
                f"Reply *RENEW* or contact your account manager to lock in the early offer. 🙏"
            )
            success = await whatsapp.send_text(doctor_phone, msg, phone_id=client_pid, token=client_token)
            if success:
                db_conn.table("subscriptions").update({"warning_14d_sent": True})\
                    .eq("id", sub["id"]).execute()
                logger.info("[Scheduler] 14-day renewal offer sent to client=%s", sub["client_id"])

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
            db_settings_7  = db.get_all_clinic_settings(sub["client_id"])
            doctor_name_7  = (db_settings_7.get("doctor_name") or client.get("doctor_name") or "Doctor").split()[-1]
            msg = (
                f"⚠️ *Last Chance — Renew in 7 Days!*\n\n"
                f"Hi Dr. {doctor_name_7}! Your Clinic AI Agent subscription expires on *{sub['end_date']}*.\n\n"
                f"⏳ After expiry you get a *{settings.GRACE_PERIOD_DAYS}-day grace period*, "
                f"then your patients *won't be able to book* via WhatsApp.\n\n"
                f"👉 Reply *RENEW* or contact your account manager today to keep the bot running without interruption. 🙏"
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
        _run_intake_previews,
        trigger=IntervalTrigger(minutes=15),   # Same cadence as 1h reminders
        id="intake_previews", replace_existing=True, misfire_grace_time=120,
    )
    # ── Monthly invoice: 1st of every month at 8:30 AM IST (3:00 UTC) ──
    scheduler.add_job(
        _run_monthly_invoices,
        trigger=CronTrigger(day=1, hour=3, minute=0, timezone="UTC"),
        id="monthly_invoices", replace_existing=True, misfire_grace_time=3600,
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
