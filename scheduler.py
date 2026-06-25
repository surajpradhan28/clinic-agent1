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
  7. trial_automation      — Welcome, 3-day nudge, 1-day warning, auto-suspend (daily 8:30 AM IST)
  8. upsell_nudges         — Usage-based upgrade nudge (5th of month, 9 AM IST)
  9. gcal_sync             — Google Calendar busy → block slots (every 15 min)
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


# ── Client validation guard ───────────────────────────────────────────────────

def _valid_phone_id(client: dict) -> bool:
    """
    Return True only if this client has a usable WhatsApp phone_number_id.

    Meta phone_number_ids are 15+ digit numeric strings.
    A short or non-numeric value means the client was never properly onboarded —
    skip them instead of hammering Meta with guaranteed-400 requests.
    Falls back to the global phone_id if the client has no dedicated one set,
    which is fine (test/dev clients use the global number).
    """
    pid = (client.get("whatsapp_phone_id") or "").strip()
    if not pid:
        return True  # will use global WHATSAPP_PHONE_ID — that's fine
    if not pid.isdigit() or len(pid) < 10:
        logger.warning(
            "[Scheduler] Client %s skipped — invalid whatsapp_phone_id '%s' "
            "(must be a 10+ digit Meta phone_number_id). Fix in DB.",
            client.get("id"), pid,
        )
        return False
    return True


def _patient_opted_in(client_id: int, phone: str) -> bool:
    """
    Meta compliance guard — returns True only if this patient has opted in.

    Called before EVERY proactive outbound message to a patient.
    Patients who have never messaged us (optin=NULL) or who sent STOP (optin=False)
    must NOT receive any outbound messages — even approved templates.

    Note: Messages sent in direct response to a patient's own message within the
    Meta 24-hour session window are exempt, but we guard all sends here for
    simplicity and belt-and-suspenders compliance.
    """
    opted_in = db.check_patient_optin(client_id, phone)
    if not opted_in:
        logger.info(
            "[Compliance] Skipping outbound to %s (client=%s) — no opt-in on record",
            phone, client_id,
        )
    return opted_in


# ── Job 1: Send 7-day follow-ups ──────────────────────────────────────────────

async def _run_followups() -> None:
    logger.info("[Scheduler] Running follow-up job…")
    try:
        clients = db.get_all_active_clients()
        for client in clients:
            if not _valid_phone_id(client):
                continue
            client_id    = client["id"]
            client_pid   = client.get("whatsapp_phone_id") or settings.WHATSAPP_PHONE_ID
            client_token = client.get("whatsapp_token") or None
            # Read clinic settings (doctor name, review link)
            db_settings  = db.get_all_clinic_settings(client_id)
            doctor_name  = db_settings.get("doctor_name") or client.get("doctor_name") or "your doctor"
            # Per-clinic Google Review link; fall back to global config
            review_link  = (db_settings.get("google_review_link") or "").strip() \
                           or settings.GOOGLE_REVIEW_LINK.strip()

            due = db.get_pending_followups(client_id)
            if not due:
                continue

            logger.info("[Scheduler] Client %s: %d follow-up(s) due", client_id, len(due))
            for row in due:
                appt        = row.get("appointments") or {}
                phone       = appt.get("patient_phone") or ""
                name        = appt.get("patient_name") or "there"
                followup_id = row["id"]
                if not phone:
                    continue

                # ── Meta compliance: skip if patient has not opted in ─────────
                if not _patient_opted_in(client_id, phone):
                    db.mark_followup_sent(followup_id)  # mark sent so we don't retry
                    continue

                # ── Message 1: health check ───────────────────────────────────
                health_fallback = (
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
                    fallback_text=health_fallback,
                    phone_id=client_pid, token=client_token,
                )
                if not success:
                    logger.error("[Scheduler] Failed to send follow-up (client=%s, phone=%s)", client_id, phone)
                    continue

                db.mark_followup_sent(followup_id)
                logger.info("[Scheduler] Follow-up sent (client=%s, followup=%s)", client_id, followup_id)

                # ── Message 2: Google Review request (only if link configured) ─
                if review_link and "YOUR_CLINIC" not in review_link:
                    review_msg = (
                        f"⭐ *One small favour — takes 30 seconds!*\n\n"
                        f"If *{doctor_name}* helped you, a Google review makes a huge "
                        f"difference for other patients trying to find us. 🙏\n\n"
                        f"👉 *Tap here to leave a review:*\n"
                        f"{review_link}\n\n"
                        f"Thank you so much, {name}! 😊"
                    )
                    await whatsapp.send_template_or_text(
                        phone,
                        template_name="clinic_google_review_request",
                        body_params=[name, doctor_name, review_link],
                        fallback_text=review_msg,
                        phone_id=client_pid, token=client_token,
                    )
                    logger.info("[Scheduler] Review request sent (client=%s)", client_id)

    except Exception as exc:
        logger.error("[Scheduler] Follow-up job error: %s", exc, exc_info=True)


# ── Job 2: Send 24-hour appointment reminders ─────────────────────────────────

async def _run_reminders() -> None:
    logger.info("[Scheduler] Running reminder job…")
    try:
        clients = db.get_all_active_clients()
        for client in clients:
            if not _valid_phone_id(client):
                continue
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

                # ── Meta compliance: skip if patient has not opted in ─────────
                if not _patient_opted_in(client_id, phone):
                    db.mark_reminder_sent(appt_id)
                    continue

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
            if not _valid_phone_id(client):
                continue
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

                # ── Meta compliance: skip if patient has not opted in ─────────
                if not _patient_opted_in(client_id, phone):
                    db.mark_1h_reminder_sent(appt_id)
                    continue

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
            if not _valid_phone_id(client):
                continue
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
            if not _valid_phone_id(client):
                continue
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

            # ── Create Razorpay payment link (if configured) ──────────────────
            razorpay_url = None
            try:
                from main import _create_razorpay_link
                client_row_for_rz = db.get_client_by_id(client_id) or {}
                razorpay_url = await _create_razorpay_link(invoice, client_row_for_rz)
            except Exception as rz_exc:
                logger.warning("[Invoices] Razorpay link failed for client=%s: %s", client_id, rz_exc)

            # Payment CTA: Razorpay link if available, else raw UPI
            pay_line = (
                f"\n\n💳 Pay now (UPI / Card / Netbanking):\n{razorpay_url}"
                if razorpay_url
                else f"\n\nPlease pay via UPI to *{settings.INVOICE_UPI_ID}* and send the screenshot to confirm."
            )

            fallback_msg = (
                f"🧾 *Invoice for {month_name}*\n\n"
                f"Hi! Here is your monthly invoice for *{clinic_name}*.\n\n"
                f"📋 Invoice No.: *{invoice['invoice_number']}*\n"
                f"📦 Plan: *{plan_label}*\n"
                f"💰 Amount: *{amount_str}*\n"
                f"📅 Due by: *{due_display}*\n\n"
                f"🔗 View invoice:\n{invoice_url}"
                f"{pay_line}\n\n"
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
            if not _valid_phone_id(client):
                continue
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


# ── Job 2e: Monthly usage-based upsell nudge ─────────────────────────────────

# Thresholds: if a clinic crosses these appointments in the last 30 days, nudge.
_UPSELL_THRESHOLD_STARTER = 40   # Starter → nudge about Pro
_UPSELL_THRESHOLD_PRO     = 80   # Pro     → nudge about Suite


async def _run_upsell_nudges() -> None:
    """
    Runs on the 5th of every month at 9:00 AM IST (3:30 UTC).
    For Starter/Pro clinics that hit the appointment volume threshold
    last month, send the doctor a WhatsApp upgrade nudge.

    Keeps it to one nudge per month per clinic (only fires if last_upsell_sent
    is NULL or > 30 days ago — tracked in clinic_settings as 'last_upsell_sent').
    """
    logger.info("[Scheduler] Running upsell nudges job…")
    try:
        from calendar import monthrange as _mr
        now       = datetime.now(_IST)
        # Count appointments in the last 30 days
        since_dt  = now - timedelta(days=30)
        since_str = since_dt.strftime("%Y-%m-%d")

        upgrade_url = f"{settings.SERVER_URL}/signup"

        clients = db.get_all_active_clients()
        for client in clients:
            if not _valid_phone_id(client):
                continue
            try:
                plan         = (client.get("plan") or "starter").lower()
                client_id    = client["id"]
                doctor_phone = (client.get("contact_phone") or "").strip()
                client_pid   = client.get("whatsapp_phone_id") or settings.WHATSAPP_PHONE_ID
                client_token = client.get("whatsapp_token") or None

                if plan not in ("starter", "pro"):
                    continue   # Suite is top-tier — nothing to upsell
                if not doctor_phone:
                    continue

                # Check if we already nudged this month
                _settings = db.get_all_clinic_settings(client_id)
                last_nudge_str = _settings.get("last_upsell_sent", "")
                if last_nudge_str:
                    try:
                        last_nudge = datetime.fromisoformat(last_nudge_str)
                        if (now - last_nudge).days < 28:
                            continue   # Already nudged this month
                    except ValueError:
                        pass

                # Count appointments in last 30 days
                appt_count = db.count_appointments_since(client_id, since_str)

                threshold = _UPSELL_THRESHOLD_STARTER if plan == "starter" else _UPSELL_THRESHOLD_PRO
                if appt_count < threshold:
                    continue

                # Build nudge message
                clinic_name = _settings.get("clinic_name") or client.get("clinic_name") or "your clinic"
                if plan == "starter":
                    msg = (
                        f"🎉 *{clinic_name}* is growing fast!\n\n"
                        f"You've had *{appt_count} appointments* in the last 30 days — "
                        f"that's brilliant! 🚀\n\n"
                        f"With this volume, your patients would benefit from:\n"
                        f"  ✅ Self-cancel & reschedule via WhatsApp\n"
                        f"  ✅ Auto waitlist when slots fill up\n"
                        f"  ✅ Post-visit follow-up messages\n"
                        f"  ✅ Broadcast to all patients\n\n"
                        f"*Pro Plan — ₹{settings.PRICE_PRO:,}/mo*\n"
                        f"👉 Upgrade: {upgrade_url}\n\n"
                        f"Reply *UPGRADE* for more details."
                    )
                else:  # pro
                    msg = (
                        f"🎉 *{clinic_name}* is at Pro level — and beyond!\n\n"
                        f"*{appt_count} appointments* in 30 days — you're running a serious practice. 🏆\n\n"
                        f"Suite unlocks the full power:\n"
                        f"  ✅ Custom clinic hours per day\n"
                        f"  ✅ AI knowledge notes for the bot\n"
                        f"  ✅ Daily morning schedule on WhatsApp\n"
                        f"  ✅ Monthly automated invoice\n\n"
                        f"*Suite Plan — ₹{settings.PRICE_SUITE:,}/mo*\n"
                        f"👉 Upgrade: {upgrade_url}\n\n"
                        f"Reply *UPGRADE* for a full plan comparison."
                    )

                success = await whatsapp.send_template_or_text(
                    phone=doctor_phone,
                    template_name="clinic_upsell_nudge",
                    body_params=[clinic_name, str(appt_count), upgrade_url],
                    fallback_text=msg,
                    phone_id=client_pid,
                    token=client_token,
                )
                if success:
                    # Record nudge timestamp in clinic_settings
                    db.update_clinic_setting(client_id, "last_upsell_sent", now.isoformat())
                    logger.info("[Scheduler] Upsell nudge sent to client=%s (plan=%s, appts=%d)",
                                client_id, plan, appt_count)

            except Exception as exc:
                logger.error("[Scheduler] Upsell nudge error (client=%s): %s", client.get("id"), exc)

    except Exception as exc:
        logger.error("[Scheduler] Upsell nudge job error: %s", exc, exc_info=True)


# ── Job 8: Trial automation ───────────────────────────────────────────────────

async def _get_or_create_trial_payment_url(client: dict, cli_settings: dict) -> str:
    """
    Returns a payment URL for trial-to-paid conversion.

    On first call: creates an invoice (setup fee + first month) and a Razorpay
    payment link, stores the URL in clinic_settings as 'trial_payment_url'.
    On subsequent calls: returns the stored URL.

    Falls back to the /signup page if Razorpay is not configured.
    """
    # Return cached URL if we already created one
    cached = cli_settings.get("trial_payment_url", "")
    if cached:
        return cached

    client_id     = client["id"]
    plan          = (client.get("plan") or "starter").lower()
    billing_cycle = (client.get("billing_cycle") or "monthly").lower()
    is_annual     = billing_cycle == "annual"

    plan_prices_monthly = {
        "starter": settings.PRICE_STARTER,
        "pro":     settings.PRICE_PRO,
        "suite":   settings.PRICE_SUITE,
    }
    plan_prices_annual = {
        "starter": settings.PRICE_STARTER_ANNUAL,
        "pro":     settings.PRICE_PRO_ANNUAL,
        "suite":   settings.PRICE_SUITE_ANNUAL,
    }
    base_price   = float(plan_prices_annual.get(plan, settings.PRICE_STARTER_ANNUAL) if is_annual
                         else plan_prices_monthly.get(plan, settings.PRICE_STARTER))
    total_amount = float(settings.SETUP_FEE) + base_price
    period_days  = 365 if is_annual else 30
    cycle_label  = f"1-year {plan.title()}" if is_annual else f"first month {plan.title()}"

    now          = datetime.now(_IST)
    period_start = now.strftime("%Y-%m-%d")
    period_end   = (now + timedelta(days=period_days)).strftime("%Y-%m-%d")
    due_date     = (now + timedelta(days=settings.INVOICE_DUE_DAYS)).strftime("%Y-%m-%d")

    try:
        invoice = db.create_invoice(
            client_id    = client_id,
            period_start = period_start,
            period_end   = period_end,
            due_date     = due_date,
            amount       = total_amount,
            plan         = plan,
            notes        = f"One-time setup fee ₹{settings.SETUP_FEE:,} + {cycle_label} ₹{base_price:,.0f}",
        )
    except Exception as exc:
        logger.warning("[TrialAuto] Invoice creation failed for client %s: %s", client_id, exc)
        return f"{settings.SERVER_URL}/signup"

    invoice_url = f"{settings.SERVER_URL}/invoice/{invoice['invoice_token']}"

    # Try to get a Razorpay link (nicer UX — UPI / card / netbanking in one click)
    pay_url = invoice_url  # fallback
    try:
        from main import _create_razorpay_link
        client_row = db.get_client_by_id(client_id) or {}
        rzp_url = await _create_razorpay_link(invoice, client_row)
        if rzp_url:
            pay_url = rzp_url
    except Exception as exc:
        logger.warning("[TrialAuto] Razorpay link failed for client %s: %s", client_id, exc)

    # Cache the URL so we reuse it in 1-day + expiry messages
    db.update_clinic_setting(client_id, "trial_payment_url", pay_url)
    logger.info("[TrialAuto] Trial invoice created for client %s — amount=₹%.0f, url=%s",
                client_id, total_amount, pay_url)
    return pay_url


async def _run_trial_automation() -> None:
    """
    Daily job at 8:30 AM IST (3:00 UTC).

    For every clinic with status='trial' (and whatsapp_phone_id set):
      1. Welcome message    — sent once when trial is first detected (trial_welcome_sent)
      2. 3-day nudge        — plan comparison + payment link created (trial_nudge_3d_sent)
      3. 1-day warning      — urgency message + reuse payment link (trial_warning_1d_sent)
      4. Auto-suspend       — trial expired → status=suspended + payment link (trial_ended_sent)

    State is tracked via clinic_settings keys so each message fires exactly once.
    A Razorpay invoice for setup fee + first month is created at the 3-day mark
    and reused in subsequent messages (stored as trial_payment_url).
    """
    logger.info("[TrialAuto] Running trial automation…")
    try:
        all_clients  = db.list_all_clients()
        trial_clients = [
            c for c in all_clients
            if c.get("status") == "trial" and c.get("trial_ends_at")
        ]
        if not trial_clients:
            logger.info("[TrialAuto] No trial clients found.")
            return

        now_utc = datetime.now(timezone.utc)

        for client in trial_clients:
            if not _valid_phone_id(client):
                continue
            client_id    = client["id"]
            doctor_phone = client.get("contact_phone") or ""
            pid          = client.get("whatsapp_phone_id") or ""
            token        = client.get("whatsapp_token") or None

            # Skip clients not yet fully activated (no WhatsApp phone ID)
            if not doctor_phone or not pid:
                logger.debug("[TrialAuto] Client %s skipped — no phone/pid yet", client_id)
                continue

            cli_settings = db.get_all_clinic_settings(client_id)
            doctor_name  = (
                cli_settings.get("doctor_name")
                or client.get("doctor_name")
                or "Doctor"
            )
            plan = (client.get("plan") or "starter").lower()
            plan_prices = {
                "starter": settings.PRICE_STARTER,
                "pro":     settings.PRICE_PRO,
                "suite":   settings.PRICE_SUITE,
            }
            monthly_price = plan_prices.get(plan, settings.PRICE_STARTER)

            try:
                trial_ends = datetime.fromisoformat(
                    client["trial_ends_at"].replace("Z", "+00:00")
                )
            except Exception:
                logger.warning("[TrialAuto] Bad trial_ends_at for client %s", client_id)
                continue

            days_left  = (trial_ends - now_utc).days
            hours_left = (trial_ends - now_utc).total_seconds() / 3600

            # ── Auto-suspend: trial has expired ──────────────────────────────
            if hours_left < 0:
                if not cli_settings.get("trial_ended_sent"):
                    db.update_client_status(client_id, "suspended")

                    # Get or create payment link
                    pay_url = await _get_or_create_trial_payment_url(client, cli_settings)
                    total   = settings.SETUP_FEE + monthly_price

                    msg = (
                        f"😔 Hi Dr. {doctor_name},\n\n"
                        f"Your 7-day free trial has ended and your Clinic AI Agent has been paused.\n\n"
                        f"Don't worry — all your patient data and appointments are safely stored. "
                        f"You can continue right where you left off in under 2 minutes.\n\n"
                        f"💳 *Complete your payment to reactivate:*\n"
                        f"   Setup fee: ₹{settings.SETUP_FEE:,} (one-time)\n"
                        f"   {plan.title()} plan: ₹{monthly_price:,}/mo\n"
                        f"   *Total today: ₹{total:,}*\n\n"
                        f"👉 Pay now: {pay_url}\n\n"
                        f"Your bot will reactivate automatically once payment is confirmed. 🙏"
                    )
                    try:
                        await whatsapp.send_text(doctor_phone, msg, phone_id=pid, token=token)
                    except Exception as exc:
                        logger.warning("[TrialAuto] Suspend notify failed for client %s: %s",
                                       client_id, exc)
                    db.update_clinic_setting(client_id, "trial_ended_sent", "true")
                    logger.info("[TrialAuto] Client %s trial expired → suspended", client_id)
                continue

            # ── Welcome message (first run after activation) ─────────────────
            if not cli_settings.get("trial_welcome_sent"):
                trial_end_str = trial_ends.astimezone(_IST).strftime("%-d %b %Y")
                msg = (
                    f"🎉 Welcome to Clinic AI Agent, Dr. {doctor_name}!\n\n"
                    f"Your *7-day free trial* is now active — full access, no credit card needed.\n\n"
                    f"*What you can do right now:*\n"
                    f"  📅 Book appointments for patients\n"
                    f"  💬 Patients self-book via WhatsApp 24/7\n"
                    f"  🔔 Automatic 24h & 1h patient reminders\n"
                    f"  📋 Morning schedule every day at 7 AM\n"
                    f"  🩺 Patient intake forms before appointments\n\n"
                    f"Your trial ends on *{trial_end_str}*.\n\n"
                    f"Type *HELP* to see all doctor commands. Let's go! 🚀"
                )
                try:
                    await whatsapp.send_text(doctor_phone, msg, phone_id=pid, token=token)
                    db.update_clinic_setting(client_id, "trial_welcome_sent", "true")
                    logger.info("[TrialAuto] Welcome sent to client %s", client_id)
                except Exception as exc:
                    logger.warning("[TrialAuto] Welcome failed for client %s: %s", client_id, exc)
                continue  # Only one message per run per client

            # ── 1-day warning + payment link ─────────────────────────────────
            if hours_left <= 28 and not cli_settings.get("trial_warning_1d_sent"):
                time_desc = "today" if days_left == 0 else "tomorrow"
                pay_url   = await _get_or_create_trial_payment_url(client, cli_settings)
                # Refresh cli_settings after potential invoice creation
                cli_settings = db.get_all_clinic_settings(client_id)
                total = settings.SETUP_FEE + monthly_price

                msg = (
                    f"🔔 *Last chance, Dr. {doctor_name}!*\n\n"
                    f"Your free trial ends *{time_desc}*. After that, your clinic bot pauses.\n\n"
                    f"💳 *Pay now to keep running without any gap:*\n"
                    f"   Setup fee (one-time): ₹{settings.SETUP_FEE:,}\n"
                    f"   {plan.title()} plan: ₹{monthly_price:,}/mo\n"
                    f"   *Total: ₹{total:,}*\n\n"
                    f"👉 {pay_url}\n\n"
                    f"UPI / Credit card / Netbanking accepted. "
                    f"Bot reactivates automatically on payment. 🙏"
                )
                try:
                    await whatsapp.send_text(doctor_phone, msg, phone_id=pid, token=token)
                    db.update_clinic_setting(client_id, "trial_warning_1d_sent", "true")
                    logger.info("[TrialAuto] 1-day warning + payment link sent to client %s", client_id)
                except Exception as exc:
                    logger.warning("[TrialAuto] 1d warning failed for client %s: %s", client_id, exc)
                continue

            # ── 3-day nudge + payment link ───────────────────────────────────
            if days_left <= 3 and not cli_settings.get("trial_nudge_3d_sent"):
                day_str   = f"{days_left} day{'s' if days_left != 1 else ''}"
                end_str   = trial_ends.astimezone(_IST).strftime("%-d %b")
                pay_url   = await _get_or_create_trial_payment_url(client, cli_settings)
                total     = settings.SETUP_FEE + monthly_price

                msg = (
                    f"⏳ Dr. {doctor_name}, your free trial ends in *{day_str}* ({end_str}).\n\n"
                    f"Here's what you unlock when you subscribe:\n\n"
                    f"🟢 *Starter — ₹{settings.PRICE_STARTER:,}/mo*\n"
                    f"   AI booking + patient reminders\n\n"
                    f"🔵 *Pro — ₹{settings.PRICE_PRO:,}/mo*\n"
                    f"   + Self-cancel/reschedule + broadcast messages\n\n"
                    f"🟣 *Suite — ₹{settings.PRICE_SUITE:,}/mo*\n"
                    f"   + Custom AI notes + waitlist + intake forms\n\n"
                    f"💳 *Pay now and never lose a booking:*\n"
                    f"   Setup fee (one-time): ₹{settings.SETUP_FEE:,}\n"
                    f"   {plan.title()} plan: ₹{monthly_price:,}/mo\n"
                    f"   *Total today: ₹{total:,}*\n\n"
                    f"👉 {pay_url}\n\n"
                    f"Secure payment — UPI / Card / Netbanking. "
                    f"Bot stays live the moment payment clears. 🚀"
                )
                try:
                    await whatsapp.send_text(doctor_phone, msg, phone_id=pid, token=token)
                    db.update_clinic_setting(client_id, "trial_nudge_3d_sent", "true")
                    logger.info("[TrialAuto] 3-day nudge + payment link sent to client %s", client_id)
                except Exception as exc:
                    logger.warning("[TrialAuto] 3d nudge failed for client %s: %s", client_id, exc)

    except Exception as exc:
        logger.error("[TrialAuto] Job failed: %s", exc, exc_info=True)


# ── Job 10: Nightly visit-notes reminder to doctor (11 PM IST) ───────────────

async def _run_notes_reminder() -> None:
    """
    Every night at 11 PM IST, check each clinic's appointments for today.
    If any patient still has no visit notes, send the doctor a WhatsApp reminder
    listing those patients so they can add notes before end of day.
    Skips silently if all patients already have notes or there were no appointments.
    """
    logger.info("[Scheduler] Running nightly notes reminder job…")
    today_str = datetime.now(_IST).strftime("%Y-%m-%d")

    try:
        clients = db.get_all_active_clients()
        for client in clients:
            if not _valid_phone_id(client):
                continue
            client_id    = client["id"]
            client_pid   = client.get("whatsapp_phone_id") or settings.WHATSAPP_PHONE_ID
            client_token = client.get("whatsapp_token") or None
            doctor_phone = (client.get("contact_phone") or "").strip() or (
                settings.DOCTOR_PHONE if client_id == 1 else ""
            )
            if not doctor_phone:
                continue

            db_settings = db.get_all_clinic_settings(client_id)
            doctor_name = db_settings.get("doctor_name") or client.get("doctor_name") or "Doctor"
            first_name  = doctor_name.split()[-1]

            missing = db.get_appointments_without_notes(client_id, today_str)
            if not missing:
                logger.debug(
                    "[NotesReminder] Client %s: all notes done or no appointments today.",
                    client_id,
                )
                continue

            # Build patient list
            lines = []
            for appt in missing:
                try:
                    slot_display = datetime.strptime(
                        appt["slot_time"], "%H:%M"
                    ).strftime("%-I:%M %p")
                except Exception:
                    slot_display = appt["slot_time"]
                lines.append(f"  • {appt['patient_name']} ({slot_display})")

            patient_list = "\n".join(lines)
            count = len(missing)

            msg = (
                f"📋 *Good Evening, Dr. {first_name}!*\n\n"
                f"You have *{count} patient(s)* from today without visit notes yet:\n\n"
                f"{patient_list}\n\n"
                f"To add notes, just send me something like:\n"
                f'_"Notes for {missing[0]["patient_name"]}: [your notes here]"_\n\n'
                f"Notes help you track patient history and auto-schedule their follow-up. 🩺"
            )

            success = await whatsapp.send_text(
                doctor_phone, msg, phone_id=client_pid, token=client_token
            )
            if success:
                logger.info(
                    "[NotesReminder] Reminder sent to client=%s doctor (%d patient(s) without notes)",
                    client_id, count,
                )
            else:
                logger.error(
                    "[NotesReminder] Failed to send reminder (client=%s)", client_id
                )

    except Exception as exc:
        logger.error("[NotesReminder] Job error: %s", exc, exc_info=True)


# ── Job 9: Google Calendar sync ──────────────────────────────────────────────

async def _run_gcal_sync() -> None:
    """
    Runs every 15 minutes.
    For every clinic that has Google Calendar connected (oauth_tokens row exists),
    fetches busy slots for the next 7 days and keeps blocked_slots in sync.
    """
    if not settings.GOOGLE_CLIENT_ID:
        return  # Google Calendar not configured — skip silently

    try:
        from gcal import sync_calendar_blocks
        connected = db.get_clients_with_gcal()
        if not connected:
            return

        logger.info("[GCal] Syncing %d clinic(s)…", len(connected))
        for row in connected:
            client_id = row["client_id"]
            try:
                summary = await sync_calendar_blocks(client_id)
                if summary["blocked_new"] or summary["unblocked"]:
                    logger.info(
                        "[GCal] client=%s → +%d blocked, -%d unblocked",
                        client_id, summary["blocked_new"], summary["unblocked"],
                    )
            except Exception as exc:
                logger.warning("[GCal] Sync error for client=%s: %s", client_id, exc)

    except Exception as exc:
        logger.error("[GCal] Job failed: %s", exc, exc_info=True)


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
        trigger=CronTrigger(hour=settings.DAILY_SCHEDULE_HOUR, minute=0, timezone="UTC"),
        id="daily_doctor_schedule", replace_existing=True, misfire_grace_time=7200,
        # misfire_grace_time=7200s (2h): if server restarts near cron time, still fires on recovery
    )
    # On startup: run daily schedule immediately so doctor gets today's list
    # even if the server was down at the scheduled cron time
    scheduler.add_job(
        _run_daily_doctor_schedule,
        trigger=None,  # run once immediately
        id="daily_doctor_schedule_startup", replace_existing=True,
    )
    scheduler.add_job(
        _run_expiry_check,
        trigger=CronTrigger(hour=2, minute=0),   # 2am UTC daily (7:30am IST)
        id="expiry_check", replace_existing=True, misfire_grace_time=1800,
    )
    # ── Upsell nudge: 5th of every month at 9:00 AM IST (3:30 UTC) ──
    scheduler.add_job(
        _run_upsell_nudges,
        trigger=CronTrigger(day=5, hour=3, minute=30, timezone="UTC"),
        id="upsell_nudges", replace_existing=True, misfire_grace_time=3600,
    )
    # ── Trial automation: daily at 8:30 AM IST (3:00 UTC) ──
    scheduler.add_job(
        _run_trial_automation,
        trigger=CronTrigger(hour=3, minute=0, timezone="UTC"),
        id="trial_automation", replace_existing=True, misfire_grace_time=1800,
    )

    # ── Google Calendar sync: every 15 minutes ──
    scheduler.add_job(
        _run_gcal_sync,
        trigger=IntervalTrigger(minutes=15),
        id="gcal_sync", replace_existing=True, misfire_grace_time=120,
    )

    # ── Nightly notes reminder: 11:00 PM IST = 17:30 UTC ──
    scheduler.add_job(
        _run_notes_reminder,
        trigger=CronTrigger(hour=17, minute=30, timezone="UTC"),
        id="notes_reminder", replace_existing=True, misfire_grace_time=1800,
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
