"""
admin.py — Super-admin WhatsApp command handler + web dashboard (v5).

WhatsApp commands (sent from ADMIN_PHONE to any clinic number):
  help                            → command list
  clients                         → list all clients and status
  new client: Name|Doctor|Phone|PhoneId|plan
                                  → onboard a new clinic
  suspend: <client_id>            → manually suspend a client
  activate: <client_id>           → reactivate a suspended client
  payment: <client_id>|amount|method|notes
                                  → record a payment
  usage                           → this-month usage across all clients
  usage: <client_id>              → usage for one client (last 3 months)
  info: <client_id>               → detailed client info
  delete: <client_id>             → permanently delete a client (asks for confirm)

Web dashboard:
  GET /admin?key=<ADMIN_SECRET>   → HTML dashboard (all clients, payments, usage)
"""

from __future__ import annotations

import logging
import os
from datetime import date, timedelta
from typing import Optional

import database as db
import whatsapp
from config import settings

logger = logging.getLogger(__name__)

# ── Helpers ───────────────────────────────────────────────────────────────────

def _fmt_date(d) -> str:
    if not d:
        return "—"
    try:
        return str(d)[:10]
    except Exception:
        return str(d)

def _status_emoji(status: str) -> str:
    return {
        "active":   "🟢",
        "grace":    "🟡",
        "expired":  "🔴",
        "suspended":"🔴",
        "pending":  "⚪",
    }.get((status or "").lower(), "⚪")


# ── WhatsApp command router ────────────────────────────────────────────────────

async def handle_admin_message(
    phone: str,
    text: str,
    phone_id: str,
) -> None:
    """
    Route a WhatsApp message from the super-admin phone to the right handler.
    `phone_id` is the clinic's WhatsApp phone_number_id (we reply from it,
    but the content is super-admin-level regardless of which clinic received it).
    """
    cmd = text.strip()
    lower = cmd.lower()
    pid = phone_id or settings.WHATSAPP_PHONE_ID

    logger.info("[Admin] Command from %s: %s", phone, cmd[:80])

    # ── help ──────────────────────────────────────────────────────────────────
    if lower in ("help", "admin help", "?"):
        await whatsapp.send_text(phone, _help_text(), phone_id=pid)

    # ── clients ───────────────────────────────────────────────────────────────
    elif lower in ("clients", "list", "list clients"):
        await whatsapp.send_text(phone, _list_clients(), phone_id=pid)

    # ── usage (all) ───────────────────────────────────────────────────────────
    elif lower == "usage":
        await whatsapp.send_text(phone, _usage_all(), phone_id=pid)

    # ── usage: <client_id> ────────────────────────────────────────────────────
    elif lower.startswith("usage:"):
        cid = _parse_int(cmd, "usage:")
        if cid is None:
            await whatsapp.send_text(phone, "❌ Usage: `usage: <client_id>`", phone_id=pid)
        else:
            await whatsapp.send_text(phone, _usage_one(cid), phone_id=pid)

    # ── info: <client_id> ─────────────────────────────────────────────────────
    elif lower.startswith("info:"):
        cid = _parse_int(cmd, "info:")
        if cid is None:
            await whatsapp.send_text(phone, "❌ Usage: `info: <client_id>`", phone_id=pid)
        else:
            await whatsapp.send_text(phone, _client_info(cid), phone_id=pid)

    # ── suspend: <client_id> ──────────────────────────────────────────────────
    elif lower.startswith("suspend:"):
        cid = _parse_int(cmd, "suspend:")
        if cid is None:
            await whatsapp.send_text(phone, "❌ Usage: `suspend: <client_id>`", phone_id=pid)
        else:
            db.update_client_status(cid, "suspended")
            await whatsapp.send_text(phone, f"🔴 Client {cid} suspended.", phone_id=pid)

    # ── activate: <client_id> ─────────────────────────────────────────────────
    elif lower.startswith("activate:"):
        cid = _parse_int(cmd, "activate:")
        if cid is None:
            await whatsapp.send_text(phone, "❌ Usage: `activate: <client_id>`", phone_id=pid)
        else:
            client_row = db.get_client_by_id(cid)
            if not client_row:
                await whatsapp.send_text(phone, f"❌ Client {cid} not found.", phone_id=pid)
            else:
                # Web-signup clinics have trial_ends_at set — activate as 'trial'
                # Admin-created clinics without trial window go straight to 'active'
                new_status = "trial" if client_row.get("trial_ends_at") else "active"
                db.update_client_status(cid, new_status)
                status_label = "🟡 trial started" if new_status == "trial" else "🟢 activated"
                await whatsapp.send_text(phone, f"{status_label} — Client {cid}.", phone_id=pid)

                # Send instant welcome WhatsApp to doctor if not already sent
                if new_status == "trial":
                    doctor_phone = client_row.get("contact_phone") or ""
                    client_pid   = client_row.get("whatsapp_phone_id") or ""
                    client_token = client_row.get("whatsapp_token") or None
                    if doctor_phone and client_pid:
                        cli_settings = db.get_all_clinic_settings(cid)
                        doctor_name  = (
                            cli_settings.get("doctor_name")
                            or client_row.get("doctor_name")
                            or "Doctor"
                        )
                        if not cli_settings.get("trial_welcome_sent"):
                            from datetime import datetime, timezone, timedelta
                            _IST = timezone(timedelta(hours=5, minutes=30))
                            try:
                                trial_ends = datetime.fromisoformat(
                                    client_row["trial_ends_at"].replace("Z", "+00:00")
                                )
                                trial_end_str = trial_ends.astimezone(_IST).strftime("%-d %b %Y")
                            except Exception:
                                trial_end_str = "in 7 days"
                            upgrade_url = f"{settings.SERVER_URL}/signup"
                            welcome_msg = (
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
                                await whatsapp.send_text(
                                    doctor_phone, welcome_msg,
                                    phone_id=client_pid, token=client_token,
                                )
                                db.update_clinic_setting(cid, "trial_welcome_sent", "true")
                            except Exception as _we:
                                logger.warning("Trial welcome failed for client %s: %s", cid, _we)

    # ── payment: <client_id>|amount|method|notes ──────────────────────────────
    elif lower.startswith("payment:"):
        await _handle_payment(phone, cmd, pid)

    # ── new client: Name|Doctor|Phone|PhoneId|plan ────────────────────────────
    elif lower.startswith("new client:"):
        await _handle_new_client(phone, cmd, pid)

    # ── renewal template: <client_id>  (or shorthand: renewal: <id>) ─────────
    elif lower.startswith("renewal template:") or lower.startswith("renewal:"):
        prefix = "renewal template:" if lower.startswith("renewal template:") else "renewal:"
        cid = _parse_int(cmd, prefix)
        if cid is None:
            await whatsapp.send_text(
                phone,
                "❌ Usage: `renewal template: <client_id>`\n"
                "Example: `renewal template: 3`\n\n"
                "Sends you a ready-to-forward renewal offer for that client.",
                phone_id=pid,
            )
        else:
            await whatsapp.send_text(phone, _renewal_template(cid), phone_id=pid)

    # ── dashboard ─────────────────────────────────────────────────────────────────────────
    elif lower.startswith("dashboard:"):
        cid = _parse_int(cmd, "dashboard:")
        if cid is None:
            await whatsapp.send_text(
                phone,
                "❌ Usage: `dashboard: <client_id>`\nExample: `dashboard: 3`",
                phone_id=pid,
            )
        else:
            await whatsapp.send_text(phone, await _dashboard_link(cid), phone_id=pid)

    # ── referrals / apply reward ──────────────────────────────────────────────
    elif lower == "referrals":
        await whatsapp.send_text(phone, _referrals_summary(), phone_id=pid)

    elif lower.startswith("apply reward:"):
        rid = _parse_int(cmd, "apply reward:")
        if rid is None:
            await whatsapp.send_text(
                phone,
                "❌ Usage: `apply reward: <reward_id>`\nExample: `apply reward: 3`\n\n"
                "Extends the referrer's subscription by 1 month and marks reward applied.",
                phone_id=pid,
            )
        else:
            success = db.apply_referral_reward(rid)
            if success:
                await whatsapp.send_text(phone, f"✅ Reward #{rid} applied — subscription extended by 1 month.", phone_id=pid)
            else:
                await whatsapp.send_text(phone, f"❌ Reward #{rid} not found or already applied.", phone_id=pid)

    # ── invoice ───────────────────────────────────────────────────────────────
    elif lower.startswith("invoice:"):
        cid = _parse_int(cmd, "invoice:")
        if cid is None:
            await whatsapp.send_text(
                phone,
                "❌ Usage: `invoice: <client_id>`\nExample: `invoice: 3`\n\n"
                "Generates and sends an invoice for the current month to the clinic.",
                phone_id=pid,
            )
        else:
            await _send_invoice_now(phone, cid, pid)

    # ── unknown ───────────────────────────────────────────────────────────────
    else:
        await whatsapp.send_text(
            phone,
            "❓ Unknown admin command. Send *help* for the full list.",
            phone_id=pid,
        )


# ── Command handlers ──────────────────────────────────────────────────────────

def _help_text() -> str:
    return (
        "🛠️ *Clinic AI Admin Commands*\n\n"
        "*clients* — list all clinics\n"
        "*info: <id>* — detailed info for one client\n"
        "*usage* — this-month usage (all)\n"
        "*usage: <id>* — usage for one client\n\n"
        "*new client: Name|Doctor|Phone|PhoneId|plan*\n"
        "_plan: starter / pro / suite_\n\n"
        "*payment: <id>|amount|method|notes*\n"
        "_method: cash / upi / bank / card_\n\n"
        "*suspend: <id>* — suspend a client\n"
        "*activate: <id>* — reactivate a client\n\n"
        "💌 *renewal template: <id>*\n"
        "_Get a ready-to-forward renewal offer message_\n\n"
        "\U0001f517 *dashboard: <id>* — get clinic dashboard URL\n\n"
        "🤝 *referrals* — referral leaderboard + pending rewards\n"
        "*apply reward: <reward_id>* — credit 1 free month to referrer\n\n"
        "🧾 *invoice: <id>* — generate & send invoice now\n\n"
        "📊 Web dashboard:\n"
        f"/admin?key=YOUR_SECRET"
    )


async def _send_invoice_now(admin_phone: str, client_id: int, pid: str) -> None:
    """
    Admin command: generate and send an invoice for the current month to the clinic.
    Works like the monthly scheduler job but is triggered on-demand.
    """
    from calendar import monthrange as _mr
    from datetime import datetime as _dt

    try:
        client = db.get_client_by_id(client_id)
        if not client:
            await whatsapp.send_text(admin_phone, f"❌ Client #{client_id} not found.", phone_id=pid)
            return

        now        = _dt.now()
        year, month = now.year, now.month
        last_day   = _mr(year, month)[1]
        period_start = f"{year:04d}-{month:02d}-01"
        period_end   = f"{year:04d}-{month:02d}-{last_day:02d}"
        due_date     = (now + timedelta(days=settings.INVOICE_DUE_DAYS)).strftime("%Y-%m-%d")

        plan_prices = {
            "starter": settings.PRICE_STARTER,
            "pro":     settings.PRICE_PRO,
            "suite":   settings.PRICE_SUITE,
        }

        plan   = (client.get("plan") or "starter").lower()
        amount = float(plan_prices.get(plan, settings.PRICE_STARTER))

        # Check if already exists for this period
        if db.invoice_exists(client_id, period_start):
            # Fetch existing invoice details instead of creating a duplicate
            existing = db.get_invoices_for_client(client_id, limit=1)
            inv_num = existing[0]["invoice_number"] if existing else "?"
            await whatsapp.send_text(
                admin_phone,
                f"⚠️ Invoice already exists for client #{client_id} this month.\n"
                f"Invoice #: *{inv_num}*\n"
                f"Use `/invoice/<token>` URL to view it.",
                phone_id=pid,
            )
            return

        clinic_name = (
            db.get_all_clinic_settings(client_id).get("clinic_name")
            or client.get("clinic_name", "Your Clinic")
        )

        invoice = db.create_invoice(
            client_id    = client_id,
            period_start = period_start,
            period_end   = period_end,
            due_date     = due_date,
            amount       = amount,
            plan         = plan,
        )

        invoice_url = f"{settings.SERVER_URL}/invoice/{invoice['invoice_token']}"
        plan_label  = plan.title()
        month_name  = now.strftime("%B %Y")
        due_display = _dt.strptime(due_date, "%Y-%m-%d").strftime("%d %B %Y")
        amount_str  = f"₹{amount:,.0f}"

        # Try Razorpay link
        razorpay_url = None
        try:
            from main import _create_razorpay_link
            razorpay_url = await _create_razorpay_link(invoice, client)
        except Exception as rz_exc:
            logger.warning("[Admin/Invoice] Razorpay link failed for client=%s: %s", client_id, rz_exc)

        pay_line = (
            f"\n\n💳 Pay now:\n{razorpay_url}"
            if razorpay_url
            else f"\n\nUPI: *{settings.INVOICE_UPI_ID}*"
        )

        # Send to clinic contact phone
        contact_phone = (client.get("contact_phone") or "").strip()
        clinic_msg = (
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
        client_pid = client.get("whatsapp_phone_id") or settings.WHATSAPP_PHONE_ID

        if contact_phone:
            await whatsapp.send_text(contact_phone, clinic_msg, phone_id=client_pid)

        # Confirm to admin
        await whatsapp.send_text(
            admin_phone,
            f"✅ Invoice generated for *{clinic_name}* (#{client_id})\n\n"
            f"📋 Invoice No.: *{invoice['invoice_number']}*\n"
            f"💰 Amount: *{amount_str}*  |  Plan: *{plan_label}*\n"
            f"📅 Due by: *{due_display}*\n"
            f"{'💳 Razorpay link created ✓' if razorpay_url else '⚠️ Razorpay link not created'}\n\n"
            f"🔗 {invoice_url}",
            phone_id=pid,
        )

    except Exception as exc:
        logger.error("[Admin/Invoice] Unexpected error for client=%s: %s", client_id, exc, exc_info=True)
        await whatsapp.send_text(
            admin_phone,
            f"❌ Failed to generate invoice for client #{client_id}: {exc}",
            phone_id=pid,
        )


def _referrals_summary() -> str:
    """Admin: all referral rewards (pending first, then applied)."""
    try:
        supabase = db.get_db()
        rewards = (
            supabase.table("referral_rewards")
            .select("id, referrer_id, referred_id, reward_months, status, triggered_at")
            .order("triggered_at", desc=True)
            .limit(50)
            .execute()
        ).data or []
    except Exception as exc:
        return f"❌ Could not fetch referrals: {exc}"

    if not rewards:
        return "🤝 *Referrals*\n\nNo referral rewards yet."

    pending  = [r for r in rewards if r["status"] == "pending"]
    applied  = [r for r in rewards if r["status"] == "applied"]
    lines    = [f"🤝 *Referral Rewards* ({len(rewards)} total)\n"]

    if pending:
        lines.append(f"⏳ *Pending ({len(pending)}):*")
        for r in pending:
            lines.append(
                f"  #{r['id']} — referrer {r['referrer_id']} ← referred {r['referred_id']} "
                f"(+{r['reward_months']}mo)"
            )
        lines.append("  → Use `apply reward: <id>` to credit")
        lines.append("")

    if applied:
        lines.append(f"✅ *Applied ({len(applied)}):*")
        for r in applied[:10]:
            lines.append(
                f"  #{r['id']} — referrer {r['referrer_id']} ← referred {r['referred_id']}"
            )

    return "\n".join(lines)


async def _dashboard_link(client_id: int) -> str:
    """Return the per-clinic dashboard URL for this client."""
    client = db.get_client_by_id(client_id)   # sync — no await
    if client is None:
        return f"❌ Client {client_id} not found."
    key = client.get("dashboard_key")
    if not key:
        return f"❌ No dashboard key for client {client_id}. Run schema_v8 migration first."
    name = client.get("name", f"Client {client_id}")
    domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "YOUR_RAILWAY_DOMAIN")
    url = f"https://{domain}/clinic?key={key}"
    return (
        f"\U0001f517 *{name} — Clinic Dashboard*\n\n"
        f"{url}\n\n"
        f"_Share this link with the clinic to view their appointment stats._"
    )


def _renewal_template(client_id: int) -> str:
    """
    Generate a ready-to-forward personalised renewal offer message for a client.
    The admin receives this message and can forward it directly to the doctor's
    WhatsApp, or use it as a script for a phone call.
    """
    client = db.get_client_by_id(client_id)
    if not client:
        return f"❌ Client {client_id} not found."

    db_settings  = db.get_all_clinic_settings(client_id)
    doctor_name  = (db_settings.get("doctor_name") or client.get("doctor_name") or "Doctor")
    first_name   = doctor_name.split()[-1]
    clinic_name  = db_settings.get("clinic_name") or client.get("name", "your clinic")
    plan         = (client.get("plan") or "starter").lower()
    status       = client.get("status", "")

    # Fetch latest subscription for expiry date
    subs = db.get_db().table("subscriptions").select("end_date, start_date") \
        .eq("client_id", client_id).order("end_date", desc=True).limit(1).execute().data or []
    sub      = subs[0] if subs else {}
    end_date = str(sub.get("end_date", ""))[:10] or "—"

    # Plan details for upgrade nudge
    plan_details = {
        "starter": ("Starter", "₹1,999/year", "Pro", "₹4,999/year", "cancellation + reschedule for patients"),
        "pro":     ("Pro",     "₹4,999/year", "Suite", "₹7,999/year", "broadcast messages to all patients"),
        "suite":   ("Suite",   "₹7,999/year", None,    None,           None),
    }
    cur_plan, cur_price, up_plan, up_price, up_feature = plan_details.get(plan, plan_details["starter"])

    upgrade_line = ""
    if up_plan:
        upgrade_line = (
            f"\n💡 *Upgrade to {up_plan} ({up_price}/year)* and also unlock "
            f"{up_feature}!\n"
        )

    status_note = ""
    if status in ("grace", "expired"):
        status_note = (
            f"\n⚠️ _Note: Subscription is currently in {status} period. "
            f"Activate immediately to restore full service._\n"
        )

    # Divider for admin context
    divider = "─" * 30

    template = (
        f"📋 *Renewal Template — {client['name']} [ID: {client_id}]*\n"
        f"Plan: {cur_plan} | Expires: {end_date} | Status: {status}\n"
        f"{divider}\n"
        f"👇 *FORWARD THIS TO DR. {first_name.upper()}:*\n"
        f"{divider}\n\n"
        f"🌟 *Renew Your Clinic AI Agent — Special Offer!*\n\n"
        f"Hi Dr. {first_name}! 👋\n\n"
        f"Your *{cur_plan} plan* for *{clinic_name}* is due for renewal "
        f"(expires *{end_date}*).\n\n"
        f"✨ *Renew now and get 1 month FREE!*\n"
        f"Just renew before your expiry date to lock in this offer.\n"
        f"{upgrade_line}"
        f"{status_note}\n"
        f"✅ What you keep with renewal:\n"
        f"• 24/7 WhatsApp appointment booking\n"
        f"• 24-hour + 1-hour patient reminders\n"
        f"• Daily morning schedule to your WhatsApp\n"
        f"• 7-day follow-up messages\n"
        f"• Patient broadcast announcements\n\n"
        f"💳 Renew at {cur_price}/year via UPI / Bank Transfer.\n\n"
        f"Reply *YES* to renew, or call us at [YOUR NUMBER]. 🙏\n"
        f"_{divider}_"
    )
    return template


def _list_clients() -> str:
    clients = db.list_all_clients()
    if not clients:
        return "No clients found."
    lines = ["*All Clinics*\n"]
    for c in clients:
        emoji = _status_emoji(c.get("status", ""))
        end = _fmt_date(c.get("end_date") or c.get("subscription_end"))
        grace = _fmt_date(c.get("grace_until"))
        grace_str = f" (grace until {grace})" if grace and c.get("status") == "grace" else ""
        lines.append(
            f"{emoji} *[{c['id']}]* {c['name']}\n"
            f"   Plan: {c.get('plan','?')} | Expires: {end}{grace_str}\n"
            f"   Phone: {c.get('contact_phone','—')}"
        )
    return "\n\n".join(lines)


def _client_info(client_id: int) -> str:
    client = db.get_client_by_id(client_id)
    if not client:
        return f"❌ Client {client_id} not found."
    info = db.get_all_clinic_settings(client_id)
    subs = db.get_db().table("subscriptions").select("*").eq("client_id", client_id)\
        .order("end_date", desc=True).limit(1).execute().data or []
    sub = subs[0] if subs else {}
    pays = db.get_db().table("payments").select("*").eq("client_id", client_id)\
        .order("paid_at", desc=True).limit(3).execute().data or []

    emoji = _status_emoji(client.get("status", ""))
    grace = _fmt_date(client.get("grace_until"))
    grace_str = f"\n⚠️ Grace until: {grace}" if grace and client.get("status") == "grace" else ""

    pay_lines = "\n".join(
        f"   • ₹{p.get('amount','?')} via {p.get('method','?')} on {_fmt_date(p.get('paid_at'))}"
        for p in pays
    ) or "   None recorded"

    return (
        f"{emoji} *Client {client_id}: {client['name']}*\n"
        f"Status: {client.get('status','?')}{grace_str}\n"
        f"Plan: {client.get('plan','?')}\n"
        f"Doctor: {info.get('doctor_name') or client.get('doctor_name','—')}\n"
        f"Clinic: {info.get('clinic_name') or client.get('name','—')}\n"
        f"Address: {info.get('clinic_address','—')}\n"
        f"Contact: {client.get('contact_phone','—')}\n"
        f"WA Phone ID: {client.get('whatsapp_phone_id','—')}\n\n"
        f"📅 Subscription: {_fmt_date(sub.get('start_date'))} → {_fmt_date(sub.get('end_date'))}\n"
        f"Sub status: {sub.get('status','—')}\n\n"
        f"💳 Recent payments:\n{pay_lines}"
    )


def _usage_all() -> str:
    db_conn = db.get_db()
    month_start = date.today().replace(day=1).isoformat()
    rows = db_conn.table("usage_log").select("*, clients(name)").eq("month", month_start)\
        .execute().data or []
    if not rows:
        return f"No usage data for {date.today().strftime('%B %Y')}."
    lines = [f"📊 *Usage — {date.today().strftime('%B %Y')}*\n"]
    for r in rows:
        clinic = (r.get("clients") or {}).get("name", f"Client {r['client_id']}")
        lines.append(
            f"*{clinic}*\n"
            f"  Bookings: {r.get('bookings',0)} | Cancels: {r.get('cancels',0)}\n"
            f"  Followups: {r.get('followups',0)} | Reviews: {r.get('reviews',0)}"
        )
    return "\n\n".join(lines)


def _usage_one(client_id: int) -> str:
    client = db.get_client_by_id(client_id)
    name = client.get("name", f"Client {client_id}") if client else f"Client {client_id}"
    db_conn = db.get_db()
    rows = db_conn.table("usage_log").select("*").eq("client_id", client_id)\
        .order("month", desc=True).limit(3).execute().data or []
    if not rows:
        return f"No usage data for {name}."
    lines = [f"📊 *Usage: {name}*\n"]
    for r in rows:
        label = date.fromisoformat(str(r["month"])[:10]).strftime("%B %Y")
        lines.append(
            f"*{label}*\n"
            f"  Bookings: {r.get('bookings',0)} | Cancels: {r.get('cancels',0)}\n"
            f"  Reschedules: {r.get('reschedules',0)} | Followups: {r.get('followups',0)}\n"
            f"  Reviews: {r.get('reviews',0)}"
        )
    return "\n\n".join(lines)


async def _handle_payment(phone: str, cmd: str, pid: str) -> None:
    """Parse `payment: <client_id>|amount|method|notes` and record it."""
    body = cmd[len("payment:"):].strip()
    parts = [p.strip() for p in body.split("|")]
    if len(parts) < 3:
        await whatsapp.send_text(
            phone,
            "❌ Format: `payment: <client_id>|amount|method|notes`\n"
            "Example: `payment: 2|3000|upi|May renewal`",
            phone_id=pid,
        )
        return
    try:
        client_id = int(parts[0])
        amount    = float(parts[1])
        method    = parts[2]
        notes     = parts[3] if len(parts) > 3 else ""
    except (ValueError, IndexError):
        await whatsapp.send_text(phone, "❌ Invalid payment format.", phone_id=pid)
        return

    client = db.get_client_by_id(client_id)
    if not client:
        await whatsapp.send_text(phone, f"❌ Client {client_id} not found.", phone_id=pid)
        return

    db.record_payment(client_id, amount, method, notes)
    await whatsapp.send_text(
        phone,
        f"✅ Payment recorded!\n"
        f"Client: {client.get('name') or client.get('clinic_name') or client_id} [{client_id}]\n"
        f"Amount: ₹{amount:.0f}\n"
        f"Method: {method}\n"
        f"Notes: {notes or '—'}",
        phone_id=pid,
    )
    logger.info("[Admin] Payment recorded: client=%s amount=%s method=%s", client_id, amount, method)

    # ── Auto-trigger referral reward if this client was referred ─────────────
    referred_by_code = (client.get("referred_by") or "").strip().upper()
    if referred_by_code:
        referrer = db.get_referrer_by_code(referred_by_code)
        if referrer and referrer["id"] != client_id:
            reward = db.create_referral_reward(
                referrer_id=referrer["id"],
                referred_id=client_id,
                months=1,
            )
            if reward:
                # Notify referrer via WhatsApp
                referrer_phone  = referrer.get("contact_phone") or ""
                referrer_pid    = referrer.get("whatsapp_phone_id") or ""
                referrer_token  = referrer.get("whatsapp_token") or None
                referred_name   = client.get("name") or client.get("clinic_name") or f"Client {client_id}"
                referrer_name   = referrer.get("doctor_name") or "Doctor"
                upgrade_url     = f"{settings.SERVER_URL}/signup"
                if referrer_phone and referrer_pid:
                    reward_msg = (
                        f"🎉 *Referral reward unlocked, Dr. {referrer_name}!*\n\n"
                        f"*{referred_name}* just subscribed using your referral code.\n\n"
                        f"You've earned *1 free month* on your next renewal. "
                        f"It will be automatically applied when we process your next payment.\n\n"
                        f"Keep sharing your code to earn more!\n"
                        f"👉 {upgrade_url}?ref={referrer.get('referral_code', '')}"
                    )
                    try:
                        await whatsapp.send_text(
                            referrer_phone, reward_msg,
                            phone_id=referrer_pid, token=referrer_token,
                        )
                    except Exception as _re:
                        logger.warning("[Admin] Referral reward notify failed: %s", _re)
                # Notify admin too
                await whatsapp.send_text(
                    phone,
                    f"🔗 Referral reward created!\n"
                    f"Referrer: {referrer.get('name') or referrer.get('clinic_name')} [{referrer['id']}]\n"
                    f"Referred: {referred_name} [{client_id}]\n"
                    f"Reward: 1 free month (pending)",
                    phone_id=pid,
                )


async def _handle_new_client(phone: str, cmd: str, pid: str) -> None:
    """Parse `new client: Name|Doctor|Phone|PhoneId|plan[|token]` and onboard.

    The 6th field (token) is optional. If omitted, the global WHATSAPP_TOKEN is used.
    Use a per-client token when the clinic has their own Meta Business Account.
    """
    body = cmd[len("new client:"):].strip()
    parts = [p.strip() for p in body.split("|")]
    if len(parts) < 5:
        await whatsapp.send_text(
            phone,
            "❌ Format: `new client: Name|Doctor|Phone|PhoneId|plan`\n"
            "Optional 6th field for a dedicated token: `...|plan|EAAxxxTOKEN`\n"
            "Example: `new client: City Clinic|Dr. Patel|919876543210|1234567890|pro`",
            phone_id=pid,
        )
        return

    clinic_name   = parts[0]
    doctor_name   = parts[1]
    contact_phone = parts[2]
    wa_phone_id   = parts[3]
    plan          = parts[4].lower()
    wa_token      = parts[5] if len(parts) > 5 else ""   # optional per-client token

    if plan not in ("starter", "pro", "suite"):
        await whatsapp.send_text(
            phone, "❌ Plan must be: starter, pro, or suite.", phone_id=pid
        )
        return

    try:
        new_client = db.create_clinic_client(
            name=clinic_name,
            doctor_name=doctor_name,
            contact_phone=contact_phone,
            whatsapp_phone_id=wa_phone_id,
            plan=plan,
            whatsapp_token=wa_token,
        )
        new_id = new_client["id"]

        # Create a 30-day subscription
        sub_start = date.today().isoformat()
        sub_end   = (date.today() + timedelta(days=30)).isoformat()
        db.create_subscription(new_id, plan, 0.0, sub_start, sub_end)

        token_line = "\n🔑 Token: custom (per-client)" if wa_token else "\n🔑 Token: shared (global)"
        await whatsapp.send_text(
            phone,
            f"✅ *New client onboarded!*\n\n"
            f"🆔 Client ID: *{new_id}*\n"
            f"🏥 Clinic: {clinic_name}\n"
            f"👨‍⚕️ Doctor: {doctor_name}\n"
            f"📱 Contact: {contact_phone}\n"
            f"🔗 WA Phone ID: {wa_phone_id}\n"
            f"📋 Plan: {plan}"
            f"{token_line}\n"
            f"📅 Subscription: {sub_start} → {sub_end} (30 days)\n\n"
            f"Next: register their number in your Meta app, then test!",
            phone_id=pid,
        )
        logger.info("[Admin] New client created: id=%s name=%s plan=%s token=%s",
                    new_id, clinic_name, plan, "custom" if wa_token else "shared")
    except Exception as exc:
        logger.error("[Admin] Failed to create client: %s", exc, exc_info=True)
        await whatsapp.send_text(
            phone,
            f"❌ Failed to create client: {exc}",
            phone_id=pid,
        )


# ── Utility ───────────────────────────────────────────────────────────────────

def _parse_int(cmd: str, prefix: str) -> Optional[int]:
    try:
        return int(cmd[len(prefix):].strip())
    except (ValueError, AttributeError):
        return None


# ── Web dashboard HTML ────────────────────────────────────────────────────────

def render_dashboard() -> str:
    """
    Return a self-contained HTML admin dashboard with full feature set.
    Called by the FastAPI /admin endpoint.
    Pulls live data from DB at render time.
    """
    import json as _json

    db_conn     = db.get_db()
    today       = date.today()
    month_start = today.replace(day=1).isoformat()
    month_label = today.strftime("%B %Y")
    admin_key   = settings.ADMIN_SECRET or ""

    # ── Fetch all data (each query fault-tolerant) ────────────────────────────
    def _q(fn):
        try:
            return fn() or []
        except Exception as _e:
            logger.warning("[Admin] DB query failed (skipped): %s", _e)
            return []

    clients = _q(db.list_all_clients)

    # Usage this month
    usage_rows = _q(lambda: db_conn.table("usage_log").select("*")
        .eq("month", month_start).execute().data)
    usage_map = {r["client_id"]: r for r in usage_rows}

    # Usage history — last 4 months
    hist_start = (today.replace(day=1) - timedelta(days=93)).replace(day=1).isoformat()
    hist_rows = _q(lambda: db_conn.table("usage_log").select("*, clients(name)")
        .gte("month", hist_start).order("month", desc=True).execute().data)

    # All payments (recent first)
    all_pays = _q(lambda: db_conn.table("payments").select("*, clients(name)")
        .order("payment_date", desc=True).limit(200).execute().data)
    client_pays: dict = {}
    for p in all_pays:
        client_pays.setdefault(p["client_id"], []).append(p)

    # Latest subscription per client
    all_subs = _q(lambda: db_conn.table("subscriptions").select("*")
        .order("end_date", desc=True).execute().data)
    subs_map: dict = {}
    for s in all_subs:
        if s["client_id"] not in subs_map:
            subs_map[s["client_id"]] = s

    # Patient counts
    pat_rows = _q(lambda: db_conn.table("patients").select("client_id").execute().data)
    pat_counts: dict = {}
    for p in pat_rows:
        pat_counts[p["client_id"]] = pat_counts.get(p["client_id"], 0) + 1

    # Revenue — this month, last month, all time
    this_month_pfx  = month_start[:7]
    last_month_date = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
    last_month_pfx  = last_month_date.strftime("%Y-%m")
    rev_by_client: dict = {}
    rev_this_month = 0.0
    rev_last_month = 0.0
    rev_all_time   = 0.0
    for p in all_pays:
        cid = p["client_id"]
        amt = float(p.get("amount") or 0)
        rev_by_client[cid] = rev_by_client.get(cid, 0.0) + amt
        rev_all_time += amt
        pd_pfx = str(p.get("payment_date") or "")[:7]
        if pd_pfx == this_month_pfx:
            rev_this_month += amt
        elif pd_pfx == last_month_pfx:
            rev_last_month += amt

    # MRR — plan prices × active + trial clients
    _plan_price = {
        "starter": settings.PRICE_STARTER,
        "pro":     settings.PRICE_PRO,
        "suite":   settings.PRICE_SUITE,
    }
    mrr = sum(
        _plan_price.get((c.get("plan") or "starter").lower(), settings.PRICE_STARTER)
        for c in clients if c.get("status") in ("active", "trial")
    )

    # MRR growth % vs last month payments
    rev_growth_pct = (
        ((rev_this_month - rev_last_month) / rev_last_month * 100)
        if rev_last_month > 0 else (100.0 if rev_this_month > 0 else 0.0)
    )

    # Clinic settings (doctor name, address, etc.)
    all_cfg = _q(lambda: db_conn.table("clinic_settings").select("*").execute().data)
    cfg_by_client: dict = {}
    for row in all_cfg:
        cfg_by_client.setdefault(row["client_id"], {})[row["key"]] = row["value"]

    # ── Aggregate stats ───────────────────────────────────────────────────────
    total_clients   = len(clients)
    active_count    = sum(1 for c in clients if c.get("status") == "active")
    trial_count     = sum(1 for c in clients if c.get("status") == "trial")
    grace_count     = sum(1 for c in clients if c.get("status") == "grace")
    suspended_count = sum(1 for c in clients if c.get("status") in ("suspended", "expired"))
    total_bookings  = sum(r.get("bookings", 0) for r in usage_rows)
    total_patients  = sum(pat_counts.values())
    paid_clients    = active_count + trial_count + grace_count + suspended_count
    churn_rate      = (suspended_count / paid_clients * 100) if paid_clients > 0 else 0.0

    # Clinics approaching renewal (sub ends in next 14 days, status active)
    approaching_renewal = []
    for c in clients:
        if c.get("status") not in ("active", "grace"):
            continue
        sub = subs_map.get(c["id"], {})
        end_str = sub.get("end_date", "")
        if not end_str:
            continue
        try:
            end_date = date.fromisoformat(str(end_str)[:10])
            days_left = (end_date - today).days
            if 0 <= days_left <= 14:
                approaching_renewal.append({
                    "client": c, "sub": sub,
                    "days_left": days_left, "end_date": end_date,
                })
        except Exception:
            pass
    approaching_renewal.sort(key=lambda x: x["days_left"])

    # Top clinics by bookings this month
    top_clinics = sorted(
        [
            {
                "client": next((c for c in clients if c["id"] == cid), {}),
                "bookings": u.get("bookings", 0),
                "cancels":  u.get("cancels", 0),
                "followups": u.get("followups", 0),
            }
            for cid, u in usage_map.items()
            if u.get("bookings", 0) > 0
        ],
        key=lambda x: x["bookings"],
        reverse=True,
    )[:8]

    # ── Build client table rows ───────────────────────────────────────────────
    rows_html = ""
    for c in clients:
        cid      = c["id"]
        stat     = c.get("status", "unknown")
        plan     = c.get("plan", "?")
        cfg      = cfg_by_client.get(cid, {})
        doctor   = cfg.get("doctor_name") or c.get("doctor_name") or "—"
        sub      = subs_map.get(cid, {})
        grace    = _fmt_date(c.get("grace_until"))
        u        = usage_map.get(cid, {})
        rev      = rev_by_client.get(cid, 0.0)
        pats     = pat_counts.get(cid, 0)
        pays     = client_pays.get(cid, [])[:3]
        cname    = c.get("name", "?")
        cname_js = cname.replace("'", "\\'")

        pay_html = "".join(
            "<div class='pay-item'>₹{:.0f} &middot; {} &middot; {}</div>".format(
                float(p.get("amount") or 0), p.get("method", "?"), _fmt_date(p.get("payment_date"))
            )
            for p in pays
        ) or "<span class='muted'>None</span>"

        stat_cls = {
            "active": "badge-green", "trial": "badge-blue",
            "grace": "badge-yellow", "expired": "badge-red",
            "suspended": "badge-red",
        }.get(stat, "badge-grey")

        grace_html = (
            "<br><small class='muted'>Grace: {}</small>".format(grace)
            if grace and stat == "grace" else ""
        )

        sub_start_str = _fmt_date(sub.get("start_date"))
        sub_end_str   = _fmt_date(sub.get("end_date"))

        can_activate = stat in ("suspended", "expired")
        if can_activate:
            action_btn = (
                f"<button class='btn btn-ok' "
                f"onclick=\"doAction({cid},'activate','{cname_js}')\">&#10003; Activate</button>"
            )
        else:
            action_btn = (
                f"<button class='btn btn-warn' "
                f"onclick=\"doAction({cid},'suspend','{cname_js}')\">&#9940; Suspend</button>"
            )

        pay_btn = (
            f"<button class='btn btn-primary' "
            f"onclick=\"openPayModal({cid},'{cname_js}')\">&#128176; Payment</button>"
        )

        _bk  = u.get("bookings", 0)
        _cx  = u.get("cancels", 0)
        _fu  = u.get("followups", 0)
        _rv  = u.get("reviews", 0)
        _ph  = c.get("contact_phone", "—")
        rows_html += (
            f"<tr>"
            f"<td style='font-weight:700;color:#075E54'>{cid}</td>"
            f"<td><b>{cname}</b><br><small class='muted'>{_ph}</small></td>"
            f"<td><small>{doctor}</small></td>"
            f"<td><span class='badge {stat_cls}'>{stat}</span>{grace_html}</td>"
            f"<td><span class='plan-badge'>{plan}</span></td>"
            f"<td style='font-size:0.78rem'>{sub_start_str}<br><span class='muted'>&rarr;</span> {sub_end_str}</td>"
            f"<td class='num-cell'>{pats}</td>"
            f"<td style='font-size:0.8rem'><b>{_bk}</b> bk &nbsp;"
            f"<span class='muted'>{_cx} cx &nbsp;{_fu} fu &nbsp;{_rv} rv</span></td>"
            f"<td class='num-cell' style='color:#075E54;font-weight:600'>&#8377;{rev:.0f}</td>"
            f"<td class='pays'>{pay_html}</td>"
            f"<td class='actions-cell'>{action_btn}{pay_btn}</td>"
            f"</tr>"
        )

    if not rows_html:
        rows_html = "<tr><td colspan='11' class='muted' style='text-align:center;padding:24px'>No clients yet — use the New Client button above.</td></tr>"

    # ── Build payment history rows ────────────────────────────────────────────
    pays_hist_html = ""
    for p in all_pays[:50]:
        clinic = (p.get("clients") or {}).get("name", "Client {}".format(p["client_id"]))
        pays_hist_html += (
            "<tr>"
            "<td>{}</td><td>{}</td>"
            "<td class='num-cell' style='color:#075E54;font-weight:600'>&#8377;{:.0f}</td>"
            "<td>{}</td><td class='muted' style='font-size:0.78rem'>{}</td>"
            "</tr>"
        ).format(
            _fmt_date(p.get("payment_date")), clinic,
            float(p.get("amount") or 0),
            p.get("method", "—"), p.get("notes") or "—",
        )
    if not pays_hist_html:
        pays_hist_html = "<tr><td colspan='5' class='muted' style='text-align:center;padding:24px'>No payments recorded yet.</td></tr>"

    # ── Build usage history rows ──────────────────────────────────────────────
    usage_hist_html = ""
    for r in hist_rows:
        clinic = (r.get("clients") or {}).get("name", "Client {}".format(r["client_id"]))
        try:
            ml = date.fromisoformat(str(r["month"])[:10]).strftime("%b %Y")
        except Exception:
            ml = str(r.get("month", ""))[:7]
        usage_hist_html += (
            "<tr>"
            "<td>{}</td><td>{}</td>"
            "<td class='num-cell'>{}</td><td class='num-cell'>{}</td>"
            "<td class='num-cell'>{}</td><td class='num-cell'>{}</td>"
            "<td class='num-cell'>{}</td>"
            "</tr>"
        ).format(
            ml, clinic,
            r.get("bookings", 0), r.get("cancels", 0),
            r.get("reschedules", 0), r.get("followups", 0), r.get("reviews", 0),
        )
    if not usage_hist_html:
        usage_hist_html = "<tr><td colspan='7' class='muted' style='text-align:center;padding:24px'>No usage history yet.</td></tr>"

    # Client <option> list for payment modal
    client_opts = "\n".join(
        "<option value='{}'>{} (ID: {})</option>".format(c["id"], c.get("name", "?"), c["id"])
        for c in clients
    )

    # Safely embed admin key in JS
    ak = _json.dumps(admin_key)

    # ── Return HTML ───────────────────────────────────────────────────────────
    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Clinic AI Admin</title>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
     background:#f0f2f5;color:#1a1a1a;min-height:100vh}

/* Header */
header{background:linear-gradient(135deg,#075E54,#128C7E);color:#fff;
       padding:16px 32px;display:flex;align-items:center;justify-content:space-between;
       box-shadow:0 2px 8px rgba(0,0,0,.15);flex-wrap:wrap;gap:12px}
header h1{font-size:1.25rem;font-weight:700;letter-spacing:-.01em}
header small{font-size:0.78rem;opacity:.8;display:block;margin-top:2px}
.header-actions{display:flex;gap:10px;align-items:center;flex-wrap:wrap}

/* Container */
.container{max-width:1450px;margin:0 auto;padding:24px 16px}

/* Stats */
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));
       gap:14px;margin-bottom:28px}
.stat{background:#fff;border-radius:12px;padding:16px 18px;
      box-shadow:0 1px 4px rgba(0,0,0,.07);border-top:3px solid #e0e0e0}
.stat.s-teal{border-color:#075E54}
.stat.s-green{border-color:#28a745}
.stat.s-blue{border-color:#007bff}
.stat.s-yellow{border-color:#ffc107}
.stat.s-red{border-color:#dc3545}
.stat.s-purple{border-color:#6f42c1}
.stat .num{font-size:1.8rem;font-weight:800;line-height:1;color:#1a1a1a}
.stat .lbl{font-size:0.72rem;color:#888;margin-top:5px;font-weight:500;
           text-transform:uppercase;letter-spacing:.04em}

/* Section */
.section{background:#fff;border-radius:12px;box-shadow:0 1px 4px rgba(0,0,0,.07);
         overflow:hidden;margin-bottom:28px}
.section-header{padding:14px 20px;border-bottom:1px solid #f0f0f0;
                display:flex;align-items:center;justify-content:space-between;
                flex-wrap:wrap;gap:8px}
.section-header h2{font-size:0.95rem;font-weight:700;color:#1a1a1a}
.section-header small{color:#999;font-size:0.78rem}

/* Table */
.table-wrap{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:0.84rem}
th{background:#fafafa;padding:9px 13px;text-align:left;font-weight:600;color:#666;
   font-size:0.72rem;text-transform:uppercase;letter-spacing:.04em;
   border-bottom:2px solid #eee;white-space:nowrap}
td{padding:11px 13px;border-top:1px solid #f5f5f5;vertical-align:top}
tr:hover td{background:#fafcfc}
.num-cell{text-align:right;white-space:nowrap}

/* Badges */
.badge{display:inline-flex;align-items:center;padding:3px 9px;border-radius:20px;
       font-size:0.7rem;font-weight:700;text-transform:uppercase;letter-spacing:.03em}
.badge-green{background:#d4edda;color:#155724}
.badge-blue{background:#cce5ff;color:#004085}
.badge-yellow{background:#fff3cd;color:#856404}
.badge-red{background:#f8d7da;color:#721c24}
.badge-grey{background:#e9ecef;color:#495057}
.plan-badge{display:inline-block;padding:2px 8px;border-radius:6px;
            font-size:0.7rem;font-weight:600;background:#e8f5e9;color:#1b5e20;
            text-transform:capitalize}

/* Pay items */
.pay-item{font-size:0.76rem;color:#555;padding:1px 0;white-space:nowrap}
.pays{min-width:155px}
.muted{color:#bbb}

/* Buttons */
.btn{display:inline-flex;align-items:center;justify-content:center;gap:4px;
     padding:5px 11px;border-radius:7px;border:none;cursor:pointer;
     font-size:0.76rem;font-weight:600;transition:opacity .15s;white-space:nowrap;
     font-family:inherit}
.btn:hover{opacity:.82}
.btn-primary{background:#075E54;color:#fff}
.btn-ok{background:#28a745;color:#fff}
.btn-warn{background:#dc3545;color:#fff}
.btn-light{background:#f0f2f5;color:#333;border:1px solid #ddd}
.btn-new{background:#128C7E;color:#fff;padding:8px 18px;font-size:0.86rem}
.actions-cell{min-width:180px;display:flex;flex-direction:column;gap:5px;padding:8px 13px}

/* Modal */
.modal-overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.5);
               z-index:1000;align-items:center;justify-content:center}
.modal-overlay.active{display:flex}
.modal{background:#fff;border-radius:16px;padding:28px 30px;
       width:92%;max-width:460px;box-shadow:0 8px 32px rgba(0,0,0,.2);
       max-height:90vh;overflow-y:auto}
.modal h3{font-size:1.05rem;font-weight:700;margin-bottom:16px;color:#075E54}
.form-group{margin-bottom:13px}
.form-group label{display:block;font-size:0.75rem;font-weight:600;color:#555;
                  margin-bottom:4px;text-transform:uppercase;letter-spacing:.03em}
.form-group input,.form-group select{
  width:100%;padding:8px 11px;border:1px solid #ddd;border-radius:8px;
  font-size:0.87rem;outline:none;transition:border-color .15s;font-family:inherit}
.form-group input:focus,.form-group select:focus{border-color:#075E54}
.modal-actions{display:flex;gap:10px;justify-content:flex-end;margin-top:18px}
.err-msg{color:#dc3545;font-size:0.78rem;margin-top:6px;display:none}
.ok-msg{color:#28a745;font-size:0.78rem;margin-top:6px;display:none}

/* Toast */
#toast{position:fixed;bottom:24px;right:24px;color:#fff;padding:11px 18px;
       border-radius:8px;font-size:0.84rem;z-index:2000;
       transform:translateY(80px);transition:transform .3s;pointer-events:none}
#toast.show{transform:translateY(0)}

footer{text-align:center;padding:22px;font-size:0.76rem;color:#ccc;margin-top:4px}
</style>
</head>
<body>

<header>
  <div>
    <h1>&#127973; Clinic AI &mdash; Admin Dashboard</h1>
    <small>Last refreshed: """ + today.strftime("%d %b %Y, %H:%M") + """ &nbsp;&middot;&nbsp; """ + month_label + """</small>
  </div>
  <div class="header-actions">
    <button class="btn btn-light" onclick="location.reload()">&#8635; Refresh</button>
    <a class="btn" style="background:#e3b341;color:#000;padding:8px 18px;font-size:0.86rem;text-decoration:none;border-radius:6px;font-weight:600" href="/admin/activate?key=""" + admin_key + """">⚡ Activate Pending</a>
    <button class="btn btn-new" onclick="openNewClientModal()">+ New Client</button>
  </div>
</header>

<div class="container">

  <!-- Stats Row 1: Client health -->
  <div style="font-size:0.7rem;font-weight:700;color:#888;text-transform:uppercase;
              letter-spacing:.07em;margin-bottom:8px;">&#127970; Clients</div>
  <div class="stats" style="margin-bottom:14px">
    <div class="stat s-teal">
      <div class="num">""" + str(total_clients) + """</div>
      <div class="lbl">Total Clients</div>
    </div>
    <div class="stat s-green">
      <div class="num" style="color:#28a745">""" + str(active_count) + """</div>
      <div class="lbl">Active</div>
    </div>
    <div class="stat s-blue">
      <div class="num" style="color:#007bff">""" + str(trial_count) + """</div>
      <div class="lbl">Trial</div>
    </div>
    <div class="stat s-yellow">
      <div class="num" style="color:#e6a817">""" + str(grace_count) + """</div>
      <div class="lbl">Grace Period</div>
    </div>
    <div class="stat s-red">
      <div class="num" style="color:#dc3545">""" + str(suspended_count) + """</div>
      <div class="lbl">Suspended</div>
    </div>
    <div class="stat s-red">
      <div class="num" style="color:#dc3545">""" + "{:.1f}".format(churn_rate) + """%</div>
      <div class="lbl">Churn Rate</div>
    </div>
    <div class="stat s-teal">
      <div class="num">""" + str(total_patients) + """</div>
      <div class="lbl">Total Patients</div>
    </div>
    <div class="stat s-teal">
      <div class="num">""" + str(total_bookings) + """</div>
      <div class="lbl">Bookings This Month</div>
    </div>
  </div>

  <!-- Stats Row 2: Revenue / MRR -->
  <div style="font-size:0.7rem;font-weight:700;color:#888;text-transform:uppercase;
              letter-spacing:.07em;margin-bottom:8px;">&#128176; Revenue &amp; MRR</div>
  <div class="stats" style="margin-bottom:28px">
    <div class="stat s-green">
      <div class="num" style="color:#075E54">&#8377;""" + "{:,.0f}".format(mrr) + """</div>
      <div class="lbl">MRR (Expected)</div>
    </div>
    <div class="stat s-green">
      <div class="num" style="color:#075E54">&#8377;""" + "{:,.0f}".format(rev_this_month) + """</div>
      <div class="lbl">Collected This Month</div>
    </div>
    <div class="stat s-blue">
      <div class="num" style="color:#007bff">&#8377;""" + "{:,.0f}".format(rev_last_month) + """</div>
      <div class="lbl">Last Month</div>
    </div>
    <div class="stat """ + ("s-green" if rev_growth_pct >= 0 else "s-red") + """">
      <div class="num" style="color:""" + ("#28a745" if rev_growth_pct >= 0 else "#dc3545") + """">""" + ("+" if rev_growth_pct >= 0 else "") + "{:.1f}".format(rev_growth_pct) + """%</div>
      <div class="lbl">MoM Growth</div>
    </div>
    <div class="stat s-purple">
      <div class="num" style="color:#6f42c1">&#8377;""" + "{:,.0f}".format(rev_all_time) + """</div>
      <div class="lbl">Revenue All Time</div>
    </div>
  </div>

  <!-- Business Metrics: 2-column row -->
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-bottom:28px">

    <!-- Approaching Renewal -->
    <div class="section" style="margin-bottom:0">
      <div class="section-header">
        <h2>&#9203; Approaching Renewal</h2>
        <small>Next 14 days &mdash; """ + str(len(approaching_renewal)) + """ clinic(s)</small>
      </div>
      <div class="table-wrap">
        <table>
          <thead><tr>
            <th>Clinic</th><th>Plan</th>
            <th>Expires</th><th>Days Left</th>
          </tr></thead>
          <tbody>""" + (
    "".join(
        (
            "<tr style='background:{bg}'>"
            "<td><b>{name}</b><br><small class='muted'>{phone}</small></td>"
            "<td><span class='plan-badge'>{plan}</span></td>"
            "<td style='font-size:0.8rem'>{end}</td>"
            "<td style='text-align:center;font-weight:700;color:{col}'>{days}d</td>"
            "</tr>"
        ).format(
            bg="#fff8f8" if x["days_left"] <= 3 else "#fffdf0" if x["days_left"] <= 7 else "#fff",
            name=x["client"].get("name","?"),
            phone=x["client"].get("contact_phone",""),
            plan=(x["client"].get("plan") or "?").title(),
            end=x["end_date"].strftime("%d %b"),
            days=x["days_left"],
            col="#dc3545" if x["days_left"] <= 3 else "#e6a817" if x["days_left"] <= 7 else "#075E54",
        )
        for x in approaching_renewal
    ) or "<tr><td colspan='4' class='muted' style='text-align:center;padding:20px'>No renewals due soon ✓</td></tr>"
) + """
          </tbody>
        </table>
      </div>
    </div>

    <!-- Top Clinics by Activity -->
    <div class="section" style="margin-bottom:0">
      <div class="section-header">
        <h2>&#128293; Most Active Clinics</h2>
        <small>Bookings in """ + month_label + """</small>
      </div>
      <div class="table-wrap">
        <table>
          <thead><tr>
            <th>Clinic</th><th>Plan</th>
            <th class="num-cell">Bookings</th><th class="num-cell">Cancels</th>
          </tr></thead>
          <tbody>""" + (
    "".join(
        (
            "<tr>"
            "<td><b>{name}</b></td>"
            "<td><span class='plan-badge'>{plan}</span></td>"
            "<td class='num-cell' style='font-weight:700;color:#075E54'>{bk}</td>"
            "<td class='num-cell muted'>{cx}</td>"
            "</tr>"
        ).format(
            name=x["client"].get("name","?"),
            plan=(x["client"].get("plan") or "?").title(),
            bk=x["bookings"],
            cx=x["cancels"],
        )
        for x in top_clinics
    ) or "<tr><td colspan='4' class='muted' style='text-align:center;padding:20px'>No bookings this month yet.</td></tr>"
) + """
          </tbody>
        </table>
      </div>
    </div>

  </div>

  <!-- Clients Table -->
  <div class="section">
    <div class="section-header">
      <h2>All Clients</h2>
      <small>""" + str(total_clients) + """ client(s) &nbsp;&middot;&nbsp; Usage: """ + month_label + """</small>
    </div>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>ID</th>
            <th>Clinic</th>
            <th>Doctor</th>
            <th>Status</th>
            <th>Plan</th>
            <th>Subscription</th>
            <th>Patients</th>
            <th>Usage (""" + month_label + """)</th>
            <th>Revenue</th>
            <th>Recent Payments</th>
            <th>Actions</th>
          </tr>
        </thead>
        <tbody>
          """ + rows_html + """
        </tbody>
      </table>
    </div>
  </div>

  <!-- Payment History -->
  <div class="section">
    <div class="section-header">
      <h2>&#128179; Payment History</h2>
      <small>Last 50 payments across all clients</small>
    </div>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Date</th>
            <th>Clinic</th>
            <th>Amount</th>
            <th>Method</th>
            <th>Notes</th>
          </tr>
        </thead>
        <tbody>
          """ + pays_hist_html + """
        </tbody>
      </table>
    </div>
  </div>

  <!-- Usage History -->
  <div class="section">
    <div class="section-header">
      <h2>&#128202; Usage History</h2>
      <small>Last 4 months</small>
    </div>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Month</th>
            <th>Clinic</th>
            <th>Bookings</th>
            <th>Cancels</th>
            <th>Reschedules</th>
            <th>Followups</th>
            <th>Reviews</th>
          </tr>
        </thead>
        <tbody>
          """ + usage_hist_html + """
        </tbody>
      </table>
    </div>
  </div>

</div><!-- /container -->

<footer>Clinic AI Admin &nbsp;&middot;&nbsp; Arun Patel &nbsp;&middot;&nbsp; """ + str(today.year) + """</footer>

<!-- Toast -->
<div id="toast"></div>

<!-- Record Payment Modal -->
<div class="modal-overlay" id="payModal">
  <div class="modal">
    <h3>&#128176; Record Payment</h3>
    <div class="form-group">
      <label>Client</label>
      <select id="pay-client">""" + client_opts + """</select>
    </div>
    <div class="form-group">
      <label>Amount (&#8377;)</label>
      <input type="number" id="pay-amount" placeholder="e.g. 3000" min="1" step="1">
    </div>
    <div class="form-group">
      <label>Method</label>
      <select id="pay-method">
        <option value="UPI">UPI</option>
        <option value="Cash">Cash</option>
        <option value="Bank Transfer">Bank Transfer</option>
        <option value="Card">Card</option>
        <option value="Cheque">Cheque</option>
      </select>
    </div>
    <div class="form-group">
      <label>Notes</label>
      <input type="text" id="pay-notes" placeholder="e.g. May renewal">
    </div>
    <div class="err-msg" id="pay-err"></div>
    <div class="modal-actions">
      <button class="btn btn-light" onclick="closeModal('payModal')">Cancel</button>
      <button class="btn btn-primary" onclick="submitPayment()">Record Payment</button>
    </div>
  </div>
</div>

<!-- New Client Modal -->
<div class="modal-overlay" id="newClientModal">
  <div class="modal">
    <h3>&#127973; New Client</h3>
    <div class="form-group">
      <label>Clinic Name</label>
      <input type="text" id="nc-name" placeholder="e.g. City Clinic">
    </div>
    <div class="form-group">
      <label>Doctor Name</label>
      <input type="text" id="nc-doctor" placeholder="e.g. Dr. Patel">
    </div>
    <div class="form-group">
      <label>Contact Phone (with country code)</label>
      <input type="text" id="nc-phone" placeholder="e.g. 919876543210">
    </div>
    <div class="form-group">
      <label>WhatsApp Phone Number ID (Meta)</label>
      <input type="text" id="nc-waid" placeholder="from Meta Developer Console">
    </div>
    <div class="form-group">
      <label>Plan</label>
      <select id="nc-plan">
        <option value="starter">Starter</option>
        <option value="pro">Pro</option>
        <option value="suite">Suite</option>
      </select>
    </div>
    <div class="form-group">
      <label>Subscription Days</label>
      <input type="number" id="nc-days" value="30" min="1" max="365">
    </div>
    <div class="err-msg" id="nc-err"></div>
    <div class="ok-msg"  id="nc-ok"></div>
    <div class="modal-actions">
      <button class="btn btn-light" onclick="closeModal('newClientModal')">Cancel</button>
      <button class="btn btn-new"   onclick="submitNewClient()">Create Client</button>
    </div>
  </div>
</div>

<!-- Confirm Modal -->
<div class="modal-overlay" id="confirmModal">
  <div class="modal">
    <h3 id="conf-title">Confirm</h3>
    <p id="conf-msg" style="margin-bottom:18px;color:#555;font-size:0.9rem"></p>
    <div class="modal-actions">
      <button class="btn btn-light" onclick="closeModal('confirmModal')">Cancel</button>
      <button class="btn btn-warn"  id="conf-btn">Confirm</button>
    </div>
  </div>
</div>

<script>
const ADMIN_KEY = """ + ak + """;

function showToast(msg, ok) {
  var t = document.getElementById('toast');
  t.textContent = msg;
  t.style.background = ok !== false ? '#28a745' : '#dc3545';
  t.classList.add('show');
  setTimeout(function(){ t.classList.remove('show'); }, 3000);
}
function openModal(id)  { document.getElementById(id).classList.add('active'); }
function closeModal(id) { document.getElementById(id).classList.remove('active'); }

// Close on overlay click
document.querySelectorAll('.modal-overlay').forEach(function(el){
  el.addEventListener('click', function(e){ if(e.target===el) el.classList.remove('active'); });
});

async function api(payload) {
  var r = await fetch('/admin/action?key=' + encodeURIComponent(ADMIN_KEY), {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload)
  });
  return r.json();
}

// Suspend / Activate
function doAction(clientId, action, clientName) {
  var isSupend = action === 'suspend';
  document.getElementById('conf-title').textContent = isSupend ? 'Suspend Client' : 'Activate Client';
  document.getElementById('conf-msg').textContent   = isSupend
    ? 'Suspend "' + clientName + '"? Service will stop immediately.'
    : 'Activate "' + clientName + '"? Service will resume immediately.';
  var btn = document.getElementById('conf-btn');
  btn.textContent = isSupend ? 'Yes, Suspend' : 'Yes, Activate';
  btn.className   = 'btn ' + (isSupend ? 'btn-warn' : 'btn-ok');
  btn.onclick = async function() {
    closeModal('confirmModal');
    var res = await api({action: action, client_id: clientId});
    if (res.ok) { showToast('Client ' + clientId + ' ' + action + 'd', true); setTimeout(function(){ location.reload(); }, 1200); }
    else        { showToast('Error: ' + (res.error || 'Unknown'), false); }
  };
  openModal('confirmModal');
}

// Payment Modal
function openPayModal(clientId, clientName) {
  var sel = document.getElementById('pay-client');
  for (var i=0; i<sel.options.length; i++) {
    if (parseInt(sel.options[i].value) === clientId) { sel.selectedIndex = i; break; }
  }
  document.getElementById('pay-amount').value = '';
  document.getElementById('pay-notes').value  = '';
  document.getElementById('pay-err').style.display = 'none';
  openModal('payModal');
}
async function submitPayment() {
  var cid    = parseInt(document.getElementById('pay-client').value);
  var amount = parseFloat(document.getElementById('pay-amount').value);
  var method = document.getElementById('pay-method').value;
  var notes  = document.getElementById('pay-notes').value;
  var err    = document.getElementById('pay-err');
  if (!amount || amount <= 0) { err.textContent = 'Enter a valid amount.'; err.style.display='block'; return; }
  err.style.display = 'none';
  var res = await api({action:'payment', client_id:cid, amount:amount, method:method, notes:notes});
  if (res.ok) { closeModal('payModal'); showToast('Payment recorded', true); setTimeout(function(){ location.reload(); }, 1200); }
  else        { err.textContent = 'Error: '+(res.error||'Unknown'); err.style.display='block'; }
}

// New Client Modal
function openNewClientModal() {
  ['nc-name','nc-doctor','nc-phone','nc-waid'].forEach(function(id){ document.getElementById(id).value=''; });
  document.getElementById('nc-plan').value = 'starter';
  document.getElementById('nc-days').value = '30';
  document.getElementById('nc-err').style.display = 'none';
  document.getElementById('nc-ok').style.display  = 'none';
  openModal('newClientModal');
}
async function submitNewClient() {
  var name   = document.getElementById('nc-name').value.trim();
  var doctor = document.getElementById('nc-doctor').value.trim();
  var phone  = document.getElementById('nc-phone').value.trim();
  var waid   = document.getElementById('nc-waid').value.trim();
  var plan   = document.getElementById('nc-plan').value;
  var days   = parseInt(document.getElementById('nc-days').value) || 30;
  var err    = document.getElementById('nc-err');
  var ok     = document.getElementById('nc-ok');
  if (!name || !doctor || !phone || !waid) {
    err.textContent = 'All fields are required.'; err.style.display='block'; return;
  }
  err.style.display = 'none';
  var res = await api({action:'new_client', name:name, doctor_name:doctor,
                       contact_phone:phone, whatsapp_phone_id:waid,
                       plan:plan, subscription_days:days});
  if (res.ok) {
    ok.textContent = 'Client created! ID: ' + res.client_id + ' — refreshing…';
    ok.style.display = 'block';
    setTimeout(function(){ location.reload(); }, 2000);
  } else {
    err.textContent = 'Error: '+(res.error||'Unknown'); err.style.display='block';
  }
}
</script>
</body>
</html>"""
