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
            db.update_client_status(cid, "active")
            await whatsapp.send_text(phone, f"🟢 Client {cid} activated.", phone_id=pid)

    # ── payment: <client_id>|amount|method|notes ──────────────────────────────
    elif lower.startswith("payment:"):
        await _handle_payment(phone, cmd, pid)

    # ── new client: Name|Doctor|Phone|PhoneId|plan ────────────────────────────
    elif lower.startswith("new client:"):
        await _handle_new_client(phone, cmd, pid)

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
        "📊 Web dashboard:\n"
        f"/admin?key=YOUR_SECRET"
    )


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
        f"Client: {client['name']} [{client_id}]\n"
        f"Amount: ₹{amount:.0f}\n"
        f"Method: {method}\n"
        f"Notes: {notes or '—'}",
        phone_id=pid,
    )
    logger.info("[Admin] Payment recorded: client=%s amount=%s method=%s", client_id, amount, method)


async def _handle_new_client(phone: str, cmd: str, pid: str) -> None:
    """Parse `new client: Name|Doctor|Phone|PhoneId|plan` and onboard."""
    body = cmd[len("new client:"):].strip()
    parts = [p.strip() for p in body.split("|")]
    if len(parts) < 5:
        await whatsapp.send_text(
            phone,
            "❌ Format: `new client: Name|Doctor|Phone|PhoneId|plan`\n"
            "Example: `new client: City Clinic|Dr. Patel|919876543210|1234567890|pro`",
            phone_id=pid,
        )
        return

    clinic_name, doctor_name, contact_phone, wa_phone_id, plan = (
        parts[0], parts[1], parts[2], parts[3], parts[4].lower()
    )
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
        )
        new_id = new_client["id"]

        # Create a 30-day starter subscription
        from datetime import datetime as _dt
        sub_start = date.today().isoformat()
        sub_end   = (date.today() + timedelta(days=30)).isoformat()
        db.create_subscription(new_id, sub_start, sub_end)

        await whatsapp.send_text(
            phone,
            f"✅ *New client onboarded!*\n\n"
            f"🆔 Client ID: *{new_id}*\n"
            f"🏥 Clinic: {clinic_name}\n"
            f"👨‍⚕️ Doctor: {doctor_name}\n"
            f"📱 Contact: {contact_phone}\n"
            f"🔗 WA Phone ID: {wa_phone_id}\n"
            f"📋 Plan: {plan}\n"
            f"📅 Subscription: {sub_start} → {sub_end} (30 days)\n\n"
            f"Next: add their WA number to Meta's allowed list, then test!",
            phone_id=pid,
        )
        logger.info("[Admin] New client created: id=%s name=%s plan=%s", new_id, clinic_name, plan)
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
    Return a self-contained HTML admin dashboard.
    Called by the FastAPI /admin endpoint.
    Pulls live data from DB at render time.
    """
    clients = db.list_all_clients()
    month_start = date.today().replace(day=1).isoformat()
    db_conn = db.get_db()

    # Usage this month
    usage_rows = db_conn.table("usage_log").select("*").eq("month", month_start)\
        .execute().data or []
    usage_map = {r["client_id"]: r for r in usage_rows}

    # Last 3 payments per client
    all_pays = db_conn.table("payments").select("*")\
        .order("paid_at", desc=True).limit(50).execute().data or []

    # Build client rows
    rows_html = ""
    for c in clients:
        cid   = c["id"]
        stat  = c.get("status", "unknown")
        plan  = c.get("plan", "?")
        grace = _fmt_date(c.get("grace_until"))
        end   = _fmt_date(c.get("end_date") or c.get("subscription_end"))
        u     = usage_map.get(cid, {})
        pays  = [p for p in all_pays if p["client_id"] == cid][:3]
        pay_html = "".join(
            f"<div class='pay'>₹{p.get('amount','?')} · {p.get('method','?')} · "
            f"{_fmt_date(p.get('paid_at'))}</div>"
            for p in pays
        ) or "<div class='pay muted'>None</div>"

        stat_class = {
            "active": "badge-green", "grace": "badge-yellow",
            "expired": "badge-red", "suspended": "badge-red",
        }.get(stat, "badge-grey")

        grace_cell = f"<br><small class='muted'>Grace: {grace}</small>" if grace and stat == "grace" else ""

        rows_html += f"""
        <tr>
          <td><b>{cid}</b></td>
          <td>
            {c.get('name','—')}<br>
            <small class='muted'>{c.get('contact_phone','—')}</small>
          </td>
          <td><span class='badge {stat_class}'>{stat}</span>{grace_cell}</td>
          <td>{plan}</td>
          <td>{end}</td>
          <td>
            <b>{u.get('bookings',0)}</b> bk &nbsp;
            {u.get('cancels',0)} cx &nbsp;
            {u.get('followups',0)} fu &nbsp;
            {u.get('reviews',0)} rv
          </td>
          <td class='pays'>{pay_html}</td>
        </tr>"""

    total_clients  = len(clients)
    active_clients = sum(1 for c in clients if c.get("status") == "active")
    grace_clients  = sum(1 for c in clients if c.get("status") == "grace")
    total_bookings = sum(r.get("bookings", 0) for r in usage_rows)
    month_label    = date.today().strftime("%B %Y")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Clinic AI Admin</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         background: #f0f2f5; color: #1a1a1a; }}
  header {{ background: #075E54; color: #fff; padding: 18px 32px;
            display: flex; align-items: center; gap: 12px; }}
  header h1 {{ font-size: 1.3rem; font-weight: 600; }}
  header small {{ font-size: 0.8rem; opacity: 0.75; }}
  .container {{ max-width: 1200px; margin: 0 auto; padding: 24px 16px; }}
  .stats {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
            gap: 16px; margin-bottom: 28px; }}
  .stat {{ background: #fff; border-radius: 10px; padding: 18px 22px;
           box-shadow: 0 1px 4px rgba(0,0,0,.08); }}
  .stat .num {{ font-size: 2rem; font-weight: 700; color: #075E54; }}
  .stat .lbl {{ font-size: 0.82rem; color: #666; margin-top: 4px; }}
  .card {{ background: #fff; border-radius: 10px;
           box-shadow: 0 1px 4px rgba(0,0,0,.08); overflow: hidden; }}
  .card-header {{ padding: 16px 22px; border-bottom: 1px solid #eee;
                  font-weight: 600; font-size: 1rem; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.875rem; }}
  th {{ background: #f7f7f7; padding: 10px 14px; text-align: left;
        font-weight: 600; color: #555; font-size: 0.8rem;
        text-transform: uppercase; letter-spacing: .03em; }}
  td {{ padding: 12px 14px; border-top: 1px solid #f0f0f0; vertical-align: top; }}
  tr:hover td {{ background: #fafafa; }}
  .badge {{ display: inline-block; padding: 2px 9px; border-radius: 20px;
            font-size: 0.75rem; font-weight: 600; }}
  .badge-green  {{ background: #d4edda; color: #155724; }}
  .badge-yellow {{ background: #fff3cd; color: #856404; }}
  .badge-red    {{ background: #f8d7da; color: #721c24; }}
  .badge-grey   {{ background: #e2e3e5; color: #383d41; }}
  .pay {{ font-size: 0.78rem; color: #444; padding: 1px 0; }}
  .muted {{ color: #999; }}
  .pays {{ min-width: 180px; }}
  footer {{ text-align: center; padding: 24px; font-size: 0.78rem; color: #aaa; }}
  @media (max-width: 768px) {{
    table, thead, tbody, th, td, tr {{ display: block; }}
    thead {{ display: none; }}
    td {{ padding: 6px 12px; }}
    td:first-child {{ font-weight: bold; padding-top: 12px; }}
  }}
</style>
</head>
<body>
<header>
  <div>
    <h1>🏥 Clinic AI — Admin Dashboard</h1>
    <small>Last refreshed: {date.today().strftime("%d %b %Y")} &nbsp;·&nbsp; {month_label}</small>
  </div>
</header>
<div class="container">
  <div class="stats">
    <div class="stat">
      <div class="num">{total_clients}</div>
      <div class="lbl">Total Clients</div>
    </div>
    <div class="stat">
      <div class="num" style="color:#28a745">{active_clients}</div>
      <div class="lbl">Active</div>
    </div>
    <div class="stat">
      <div class="num" style="color:#ffc107">{grace_clients}</div>
      <div class="lbl">In Grace Period</div>
    </div>
    <div class="stat">
      <div class="num">{total_bookings}</div>
      <div class="lbl">Bookings This Month</div>
    </div>
  </div>

  <div class="card">
    <div class="card-header">All Clients</div>
    <table>
      <thead>
        <tr>
          <th>ID</th><th>Clinic</th><th>Status</th><th>Plan</th>
          <th>Expires</th><th>Usage ({month_label})</th><th>Payments</th>
        </tr>
      </thead>
      <tbody>
        {rows_html}
      </tbody>
    </table>
  </div>
</div>
<footer>Clinic AI Admin · Arun Patel · {date.today().year}</footer>
</body>
</html>"""
