"""
main.py — FastAPI application + WhatsApp webhook handler (multi-tenant v5).

Entry point for the Clinic AI Agent.

Endpoints:
  GET  /                          → Health check
  GET  /webhook                   → Meta webhook verification (one-time setup)
  POST /webhook                   → Incoming WhatsApp messages
  GET  /health                    → Detailed health check (DB + config)
  GET  /admin?key=<SECRET>        → Super-admin web dashboard (Arun only)
  POST /admin/action?key=<SECRET> → Dashboard actions (suspend/activate/payment/new_client)
  GET  /clinic?key=<dashboard_key>→ Per-clinic read-only dashboard (doctors)

Routing on every incoming message:
  0. If sender is ADMIN_PHONE → super-admin command handler (admin.py)
  1. Extract phone_number_id from webhook (which clinic's number was messaged)
  2. Look up clients table → resolve client row
  3. Check client status (active / grace / suspended / expired)
  4. If doctor → doctor management flow
  5. If patient has active follow-up → followup flow
  6. Otherwise → AI booking agent flow
"""

from __future__ import annotations

import collections
import hashlib
import hmac
import logging
import sys
import time
import traceback
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse, HTMLResponse

import database as db
import whatsapp
import scheduler as sched
import admin as admin_handler
import clinic_dashboard
from config import settings
from flows.booking import handle_booking_flow
from flows.followup import handle_followup_response, is_followup_response

# ── Logging setup ─────────────────────────────────────────────────────────────

logging.basicConfig(
    stream=sys.stdout,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


# ── Webhook deduplication (in-memory, 5-minute TTL) ──────────────────────────
# WhatsApp Cloud API sometimes delivers the same webhook event more than once.
# We track recently-seen message_ids so duplicate deliveries are silently dropped.

_seen_message_ids: dict[str, float] = {}   # message_id → first-seen timestamp
_DEDUP_TTL_SECS = 300                       # 5 minutes


def _is_duplicate_message(message_id: str) -> bool:
    """Return True if this message_id was already processed within the TTL window."""
    if not message_id:
        return False
    now = time.monotonic()
    # Purge expired entries to keep memory bounded
    expired = [mid for mid, ts in _seen_message_ids.items() if now - ts > _DEDUP_TTL_SECS]
    for mid in expired:
        del _seen_message_ids[mid]
    if message_id in _seen_message_ids:
        return True
    _seen_message_ids[message_id] = now
    return False


# ── Per-user rate limiting (in-memory, sliding window) ───────────────────────
# Max 20 messages per phone number per 60-second window.
# Protects against accidental message loops and deliberate flooding.

_user_timestamps: dict[str, collections.deque] = collections.defaultdict(
    lambda: collections.deque()
)
_RATE_LIMIT_WINDOW_SECS = 60
_RATE_LIMIT_MAX_MSGS    = 20


def _is_rate_limited(phone: str) -> bool:
    """Return True if this phone number has exceeded the rate limit."""
    if not phone:
        return False
    now  = time.monotonic()
    dq   = _user_timestamps[phone]
    # Remove timestamps outside the sliding window
    while dq and now - dq[0] > _RATE_LIMIT_WINDOW_SECS:
        dq.popleft()
    if len(dq) >= _RATE_LIMIT_MAX_MSGS:
        return True
    dq.append(now)
    return False


# ── WhatsApp webhook signature verification ───────────────────────────────────
# Meta signs every webhook POST body with HMAC-SHA256 using the App Secret.
# The signature is in the X-Hub-Signature-256 header as "sha256=<hex>".
# We verify it to reject forged/spoofed requests before any processing.
# If WHATSAPP_APP_SECRET is not configured, verification is skipped (logs a
# warning) — set it in production for security.

async def _verify_webhook_signature(request: Request, body: bytes) -> bool:
    """
    Return True if the request body matches the Meta HMAC-SHA256 signature.
    Always returns True if WHATSAPP_APP_SECRET is not configured (with a warning).
    """
    app_secret = settings.WHATSAPP_APP_SECRET
    if not app_secret:
        logger.warning(
            "⚠️  WHATSAPP_APP_SECRET not set — webhook signature NOT verified. "
            "Set this in Railway env vars for production security."
        )
        return True

    signature_header = request.headers.get("X-Hub-Signature-256", "")
    if not signature_header:
        logger.warning("🚫 Webhook request missing X-Hub-Signature-256 header — rejected")
        return False

    expected_sig = "sha256=" + hmac.new(
        app_secret.encode("utf-8"),
        body,
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(signature_header, expected_sig):
        logger.warning(
            "🚫 Webhook signature mismatch (got=%s, expected=%s…) — rejected",
            signature_header[:20],
            expected_sig[:20],
        )
        return False

    return True


# ── App lifecycle ─────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("🏥 Clinic AI Agent starting up…")
    try:
        settings.validate()
        logger.info("✅ Config validated")
    except EnvironmentError as exc:
        logger.error("❌ Configuration error: %s", exc)

    sched.start()
    logger.info("✅ Scheduler started")
    logger.info("🚀 Clinic AI Agent ready!")

    yield

    sched.stop()
    logger.info("👋 Clinic AI Agent shutting down")


app = FastAPI(
    title="Clinic AI Agent",
    description="WhatsApp appointment booking agent for Indian clinics (multi-tenant)",
    version="2.0.0",
    lifespan=lifespan,
)


# ── Health check ──────────────────────────────────────────────────────────────

@app.get("/")
async def root():
    return {"status": "ok", "service": "Clinic AI Agent", "version": "2.0.0"}


@app.get("/admin")
async def admin_dashboard(request: Request):
    """
    Web admin dashboard — shows all clients, subscriptions, payments, usage.
    Protected by ADMIN_SECRET query parameter.
    """
    key = request.query_params.get("key", "")
    if not key or key != settings.ADMIN_SECRET:
        raise HTTPException(status_code=403, detail="Invalid admin key")
    try:
        html = admin_handler.render_dashboard()
        return HTMLResponse(content=html)
    except Exception as exc:
        logger.error("[Admin] Dashboard render error: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail="Dashboard error")


@app.post("/admin/action")
async def admin_action(request: Request):
    """
    Dashboard action endpoint — called by JS fetch() in the admin HTML.
    Actions: suspend | activate | payment | new_client
    """
    key = request.query_params.get("key", "")
    if not key or key != settings.ADMIN_SECRET:
        return JSONResponse({"ok": False, "error": "Unauthorized"}, status_code=403)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid JSON"}, status_code=400)

    action = body.get("action", "")
    logger.info("[Admin Action] %s — %s", action, {k: v for k, v in body.items() if k != "action"})

    try:
        if action == "suspend":
            cid = int(body["client_id"])
            db.update_client_status(cid, "suspended")
            return JSONResponse({"ok": True})

        elif action == "activate":
            cid = int(body["client_id"])
            db.update_client_status(cid, "active")
            return JSONResponse({"ok": True})

        elif action == "payment":
            cid    = int(body["client_id"])
            amount = float(body["amount"])
            method = body.get("method", "UPI")
            notes  = body.get("notes", "")
            db.record_payment(cid, amount, method, notes)
            return JSONResponse({"ok": True})

        elif action == "new_client":
            from datetime import date as _date, timedelta as _td
            new = db.create_clinic_client(
                name=body["name"],
                doctor_name=body["doctor_name"],
                contact_phone=body.get("contact_phone", ""),
                whatsapp_phone_id=body["whatsapp_phone_id"],
                plan=body.get("plan", "starter"),
                whatsapp_token=body.get("whatsapp_token", ""),
            )
            days      = int(body.get("subscription_days", 30))
            sub_start = _date.today().isoformat()
            sub_end   = (_date.today() + _td(days=days)).isoformat()
            db.create_subscription(
                new["id"],
                body.get("plan", "starter"),
                0.0,
                sub_start,
                sub_end,
            )
            logger.info("[Admin Action] New client created: id=%s name=%s", new["id"], body.get("name"))
            return JSONResponse({"ok": True, "client_id": new["id"]})

        else:
            return JSONResponse({"ok": False, "error": f"Unknown action: {action}"}, status_code=400)

    except KeyError as exc:
        return JSONResponse({"ok": False, "error": f"Missing field: {exc}"}, status_code=400)
    except Exception as exc:
        logger.error("[Admin Action] Error: %s", exc, exc_info=True)
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@app.get("/clinic")
async def clinic_dashboard_view(request: Request):
    """
    Per-clinic read-only web dashboard.
    Each clinic doctor gets a private link:  /clinic?key=<their_dashboard_key>
    Shows: today's schedule, upcoming week, stats, recent activity.
    No cross-clinic data is ever exposed.
    """
    key = request.query_params.get("key", "").strip()
    if not key:
        raise HTTPException(status_code=403, detail="Missing dashboard key")

    client = db.get_client_by_dashboard_key(key)
    if not client:
        raise HTTPException(status_code=403, detail="Invalid dashboard key")

    try:
        html_content = clinic_dashboard.render_clinic_dashboard(client)
        return HTMLResponse(content=html_content)
    except Exception as exc:
        logger.error(
            "[Clinic Dashboard] Render error for client=%s: %s",
            client.get("id"), exc, exc_info=True,
        )
        raise HTTPException(status_code=500, detail="Dashboard error")


@app.get("/invoice/{token}")
async def invoice_view(token: str):
    """
    Public invoice page served at a unique URL.
    URL format: /invoice/<invoice_token>
    Renders a print-ready HTML invoice with payment instructions.
    """
    invoice = db.get_invoice_by_token(token)
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")

    # Pull client info (joined in get_invoice_by_token)
    client_info = invoice.get("clients") or {}
    clinic_name  = client_info.get("clinic_name")  or "Clinic"
    contact_name = client_info.get("contact_name") or ""
    contact_email = client_info.get("contact_email") or ""

    plan_label = invoice["plan"].title()
    plan_desc  = {
        "starter": "Appointment booking + 24h reminders",
        "pro":     "Booking, reminders, cancellation & reschedule",
        "suite":   "All Pro features + daily schedule + priority support",
    }.get(invoice["plan"].lower(), invoice["plan"])

    # Format dates
    def _fmt(d: str) -> str:
        try:
            from datetime import datetime as _dt
            return _dt.strptime(d, "%Y-%m-%d").strftime("%d %B %Y")
        except Exception:
            return d

    period_label = f"{_fmt(invoice['period_start'])} – {_fmt(invoice['period_end'])}"
    due_str      = _fmt(invoice["due_date"])
    issued_str   = _fmt(invoice.get("sent_at", invoice["created_at"])[:10])
    amount_str   = f"₹{float(invoice['amount']):,.2f}"
    status       = invoice["status"].upper()
    status_color = {
        "SENT":    "#1565C0",
        "PAID":    "#2E7D32",
        "OVERDUE": "#C62828",
        "CANCELLED": "#757575",
    }.get(status, "#555555")

    gstin_line = (
        f"<p style='margin:2px 0;color:#555;font-size:13px;'>GSTIN: {settings.INVOICE_GSTIN}</p>"
        if settings.INVOICE_GSTIN else ""
    )
    paid_banner = ""
    if status == "PAID":
        paid_at = _fmt(invoice.get("paid_at", "")[:10]) if invoice.get("paid_at") else ""
        paid_banner = f"""
        <div style="background:#E8F5E9;border:2px solid #4CAF50;border-radius:8px;
                    padding:12px 20px;margin-bottom:24px;text-align:center;">
          <span style="color:#2E7D32;font-size:18px;font-weight:bold;">✅ PAID{(' — ' + paid_at) if paid_at else ''}</span>
        </div>"""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Invoice {invoice['invoice_number']}</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: 'Segoe UI', Arial, sans-serif;
      background: #f4f6f8;
      color: #1a1a2e;
      padding: 20px;
    }}
    .invoice-wrap {{
      max-width: 720px;
      margin: 0 auto;
      background: #fff;
      border-radius: 12px;
      overflow: hidden;
      box-shadow: 0 4px 24px rgba(0,0,0,0.10);
    }}
    .inv-header {{
      background: linear-gradient(135deg, #1A3A5C 0%, #2E75B6 100%);
      color: #fff;
      padding: 32px 36px 28px;
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      flex-wrap: wrap;
      gap: 16px;
    }}
    .inv-header h1 {{ font-size: 26px; font-weight: 700; letter-spacing: 0.5px; }}
    .inv-header p  {{ font-size: 13px; opacity: 0.85; margin-top: 4px; }}
    .inv-number-box {{
      text-align: right;
    }}
    .inv-number-box .label {{ font-size: 11px; text-transform: uppercase; opacity:0.7; }}
    .inv-number-box .value {{ font-size: 22px; font-weight: 700; letter-spacing: 1px; }}
    .status-badge {{
      display: inline-block;
      padding: 4px 14px;
      border-radius: 20px;
      font-size: 12px;
      font-weight: 700;
      letter-spacing: 1px;
      background: rgba(255,255,255,0.18);
      color: #fff;
      border: 1.5px solid rgba(255,255,255,0.4);
      margin-top: 6px;
    }}
    .inv-body {{ padding: 32px 36px; }}
    .meta-grid {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 24px;
      margin-bottom: 32px;
    }}
    @media(max-width:520px){{ .meta-grid{{ grid-template-columns:1fr; }} .inv-header{{ flex-direction:column; }} .inv-number-box{{ text-align:left; }} }}
    .meta-box h3 {{ font-size: 11px; text-transform: uppercase; color: #888; letter-spacing: 1px; margin-bottom: 6px; }}
    .meta-box p  {{ font-size: 14px; color: #222; line-height: 1.6; }}
    .line-divider {{ border: none; border-top: 1px solid #e8eaed; margin: 0 0 28px; }}
    table {{ width: 100%; border-collapse: collapse; margin-bottom: 24px; }}
    thead th {{
      background: #F3F6FB;
      padding: 10px 14px;
      font-size: 12px;
      text-transform: uppercase;
      color: #555;
      letter-spacing: 0.8px;
      text-align: left;
    }}
    thead th:last-child {{ text-align: right; }}
    tbody td {{ padding: 14px; font-size: 14px; border-bottom: 1px solid #f0f0f0; vertical-align: top; }}
    tbody td:last-child {{ text-align: right; font-weight: 600; }}
    .total-row td {{ font-size: 16px; font-weight: 700; border-bottom: none; padding-top: 16px; color: #1A3A5C; }}
    .payment-box {{
      background: #F0F8FF;
      border: 1.5px solid #B3D4F0;
      border-radius: 10px;
      padding: 20px 24px;
      margin-bottom: 24px;
    }}
    .payment-box h3 {{ color: #1A3A5C; font-size: 14px; margin-bottom: 10px; }}
    .payment-box p  {{ font-size: 13px; color: #333; line-height: 1.7; }}
    .payment-box .upi {{ font-size: 16px; font-weight: 700; color: #1565C0; letter-spacing: 0.5px; }}
    .footer-note {{
      font-size: 12px;
      color: #888;
      text-align: center;
      padding: 16px 0 8px;
      border-top: 1px solid #eee;
      line-height: 1.7;
    }}
    @media print {{
      body {{ background: #fff; padding: 0; }}
      .invoice-wrap {{ box-shadow: none; border-radius: 0; }}
      .no-print {{ display: none !important; }}
    }}
  </style>
</head>
<body>
  <div class="invoice-wrap">
    <div class="inv-header">
      <div>
        <h1>{settings.INVOICE_BUSINESS_NAME}</h1>
        <p>{settings.INVOICE_BUSINESS_ADDRESS}</p>
        {gstin_line.replace("style='", "style='")}
      </div>
      <div class="inv-number-box">
        <div class="label">Invoice</div>
        <div class="value">{invoice['invoice_number']}</div>
        <div class="status-badge">{status}</div>
      </div>
    </div>

    <div class="inv-body">
      {paid_banner}

      <div class="meta-grid">
        <div class="meta-box">
          <h3>Bill To</h3>
          <p><strong>{clinic_name}</strong><br>
          {'Contact: ' + contact_name + '<br>' if contact_name else ''}
          {'Email: ' + contact_email + '<br>' if contact_email else ''}
          Plan: {plan_label}</p>
        </div>
        <div class="meta-box">
          <h3>Invoice Details</h3>
          <p>Issue Date: {issued_str}<br>
          Billing Period: {period_label}<br>
          <strong>Due Date: {due_str}</strong></p>
        </div>
      </div>

      <hr class="line-divider">

      <table>
        <thead>
          <tr>
            <th>Description</th>
            <th>Period</th>
            <th>Amount</th>
          </tr>
        </thead>
        <tbody>
          <tr>
            <td>
              <strong>Clinic AI Agent — {plan_label} Plan</strong><br>
              <span style="color:#666;font-size:13px;">{plan_desc}</span>
            </td>
            <td style="color:#555;font-size:13px;">{period_label}</td>
            <td>{amount_str}</td>
          </tr>
          <tr class="total-row">
            <td colspan="2" style="text-align:right;padding-right:14px;">Total</td>
            <td>{amount_str}</td>
          </tr>
        </tbody>
      </table>

      <div class="payment-box">
        <h3>💳 Payment Instructions</h3>
        <p>
          Please pay before <strong>{due_str}</strong> to avoid service interruption.<br><br>
          UPI (Google Pay / PhonePe / Paytm):<br>
          <span class="upi">{settings.INVOICE_UPI_ID}</span><br><br>
          After paying, please WhatsApp the payment screenshot to confirm your renewal.
        </p>
      </div>

      <p class="no-print" style="text-align:center;margin-bottom:20px;">
        <button onclick="window.print()" style="
          background:#1A3A5C;color:#fff;border:none;padding:10px 28px;
          border-radius:8px;font-size:14px;cursor:pointer;font-weight:600;
        ">🖨️ Print / Save as PDF</button>
      </p>

      <div class="footer-note">
        This is a computer-generated invoice. For queries, contact {settings.INVOICE_BUSINESS_NAME}.<br>
        Thank you for your continued trust. 🙏
      </div>
    </div>
  </div>
</body>
</html>"""

    return HTMLResponse(content=html)


@app.get("/health")
async def health():
    checks = {
        "whatsapp_token": bool(settings.WHATSAPP_TOKEN),
        "openai_key":     bool(settings.OPENAI_API_KEY),
        "supabase_url":   bool(settings.SUPABASE_URL),
        "supabase_key":   bool(settings.SUPABASE_KEY),
        "scheduler_running": sched.scheduler.running,
    }
    all_ok = all(checks.values())
    # Always return 200 so Railway healthcheck passes; status field indicates health
    return JSONResponse(
        status_code=200,
        content={"status": "healthy" if all_ok else "degraded", "checks": checks},
    )


# ── WhatsApp webhook verification (GET) ───────────────────────────────────────

@app.get("/webhook")
async def verify_webhook(request: Request):
    params = dict(request.query_params)
    mode      = params.get("hub.mode")
    token     = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")

    if mode == "subscribe" and token == settings.WHATSAPP_VERIFY_TOKEN:
        logger.info("✅ Webhook verified by Meta")
        return PlainTextResponse(content=challenge, status_code=200)

    logger.warning("❌ Webhook verification failed (token mismatch)")
    raise HTTPException(status_code=403, detail="Verification failed")


# ── WhatsApp incoming messages (POST) ─────────────────────────────────────────

@app.post("/webhook")
async def receive_message(request: Request):
    """
    Receives incoming WhatsApp messages from Meta Cloud API.

    Multi-tenant routing:
      - Identifies clinic by phone_number_id (which of your registered numbers was messaged)
      - Passes resolved client dict to all downstream flows
    """
    # ── Read raw body first (needed for HMAC verification) ────────────────────
    try:
        raw_body = await request.body()
    except Exception:
        logger.error("Failed to read webhook body")
        return JSONResponse({"status": "error"}, status_code=400)

    # ── Verify Meta webhook signature ─────────────────────────────────────────
    if not await _verify_webhook_signature(request, raw_body):
        return JSONResponse({"status": "forbidden"}, status_code=403)

    # ── Parse JSON ────────────────────────────────────────────────────────────
    try:
        import json as _json
        body = _json.loads(raw_body)
    except Exception:
        logger.error("Failed to parse webhook body as JSON")
        return JSONResponse({"status": "error"}, status_code=400)

    phone: str | None = None
    try:
        msg = whatsapp.parse_incoming_message(body)
        if not msg:
            return JSONResponse({"status": "ignored"})

        phone            = msg["phone"]
        name             = msg["name"]
        text             = msg["text"]
        message_id       = msg.get("message_id", "")
        phone_number_id  = msg.get("phone_number_id", "")

        # ── Deduplication: drop re-delivered webhooks ─────────────────────────
        if _is_duplicate_message(message_id):
            logger.info("⚡ Duplicate message_id=%s from %s — ignored", message_id, phone)
            return JSONResponse({"status": "duplicate"})

        # ── Rate limiting: protect against flooding ───────────────────────────
        if _is_rate_limited(phone):
            logger.warning("🚫 Rate limit hit for %s — dropping message", phone)
            return JSONResponse({"status": "rate_limited"})

        # ── STEP 0: Super-admin routing ───────────────────────────────────────
        if _is_admin(phone):
            logger.info("[Router] → Admin flow (from=%s)", phone)
            await admin_handler.handle_admin_message(
                phone=phone,
                text=text,
                phone_id=phone_number_id or settings.WHATSAPP_PHONE_ID,
            )
            return JSONResponse({"status": "ok", "flow": "admin"})

        # ── STEP 1: Resolve which clinic this message is for ──────────────────
        client = _resolve_client(phone_number_id)
        if client is None:
            # Unknown phone number ID — not a registered clinic
            logger.warning(
                "Unrecognised phone_number_id '%s' — no client found, ignoring",
                phone_number_id,
            )
            return JSONResponse({"status": "ignored", "reason": "unknown_phone_id"})

        client_id    = client["id"]
        client_pid   = client.get("whatsapp_phone_id") or settings.WHATSAPP_PHONE_ID
        client_token = client.get("whatsapp_token") or None  # None → falls back to global token

        logger.info(
            "📩 Message from %s (%s) → client=%s (%s): %s",
            phone, name, client_id, client["name"], text[:80],
        )

        # ── STEP 2: Check subscription status ────────────────────────────────
        if client["status"] in ("suspended", "expired"):
            logger.info(
                "[Router] Client %s is %s — blocking message", client_id, client["status"]
            )
            # Only tell the doctor, not the patient
            if _is_doctor(phone, client):
                await whatsapp.send_text(
                    phone,
                    "⚠️ Your subscription has expired or been suspended.\n"
                    "Please contact support to renew and restore service.",
                    phone_id=client_pid,
                    token=client_token,
                )
            return JSONResponse({"status": "ignored", "reason": client["status"]})

        # Grace period: still serve but warn the doctor if they message
        if client["status"] == "grace" and _is_doctor(phone, client):
            grace_until = client.get("grace_until") or "soon"
            await whatsapp.send_text(
                phone,
                f"⚠️ *Subscription Expired — Grace Period*\n\n"
                f"Your subscription has expired but service continues until *{grace_until}*.\n"
                f"Please renew now to avoid interruption. Contact support. 🙏",
                phone_id=client_pid,
                token=client_token,
            )

        if not text:
            await whatsapp.send_text(
                phone,
                "Sorry, I can only process text messages right now. Please type your message. 😊",
                phone_id=client_pid,
                token=client_token,
            )
            return JSONResponse({"status": "unsupported_type"})

        # ── STEP 3: Doctor flow (skip patient tracking + follow-up check) ─────
        if _is_doctor(phone, client):
            logger.info("[Router] → Doctor flow (client=%s)", client_id)
            if message_id:
                await whatsapp.mark_as_read(message_id, phone_id=client_pid, token=client_token)
            await handle_booking_flow(phone, name, text, client=client)
            return JSONResponse({"status": "ok", "flow": "doctor"})

        # ── STEP 4: Save / update patient record ──────────────────────────────
        db.upsert_patient(client_id, phone, name)

        if message_id:
            await whatsapp.mark_as_read(message_id, phone_id=client_pid, token=client_token)

        # ── STEP 5: Active follow-up? ─────────────────────────────────────────
        if await is_followup_response(client_id, phone):
            logger.info("[Router] → Follow-up flow (client=%s)", client_id)
            await handle_followup_response(phone, name, text, client=client)
            return JSONResponse({"status": "ok", "flow": "followup"})

        # ── STEP 6: AI booking agent ──────────────────────────────────────────
        logger.info("[Router] → Booking flow (client=%s)", client_id)
        await handle_booking_flow(phone, name, text, client=client)
        return JSONResponse({"status": "ok", "flow": "booking"})

    except Exception as exc:
        logger.error("Unhandled error in webhook handler: %s", exc, exc_info=True)

        # ── Notify the patient / doctor that triggered the error ──────────────
        if phone:
            try:
                await whatsapp.send_text(
                    phone,
                    "Sorry, something went wrong on our end. Please try again in a moment. 🙏",
                )
            except Exception:
                pass

        # ── Alert admin on WhatsApp ───────────────────────────────────────────
        if settings.ADMIN_PHONE:
            try:
                tb_lines = traceback.format_exc().splitlines()
                # Keep last 6 lines of traceback (most relevant)
                tb_short = "\n".join(tb_lines[-6:]) if len(tb_lines) > 6 else "\n".join(tb_lines)
                alert_msg = (
                    f"🚨 *Bot Error Alert*\n\n"
                    f"*Error:* {type(exc).__name__}: {str(exc)[:200]}\n"
                    f"*Triggered by:* {phone or 'unknown'}\n\n"
                    f"*Traceback:*\n```\n{tb_short}\n```"
                )
                await whatsapp.send_text(settings.ADMIN_PHONE, alert_msg)
            except Exception as alert_exc:
                logger.error("Failed to send admin error alert: %s", alert_exc)

        return JSONResponse({"status": "error"}, status_code=200)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _resolve_client(phone_number_id: str) -> dict | None:
    """
    Look up the client by their Meta phone_number_id.

    Falls back to client_id=1 if WHATSAPP_PHONE_ID matches and there's only
    one client in the DB — makes migration from single-tenant painless.
    """
    if phone_number_id:
        client = db.get_client_by_phone_id(phone_number_id)
        if client:
            return client

    # Fallback: if no phone_number_id match, return client 1 (single-tenant compat)
    if settings.WHATSAPP_PHONE_ID and not phone_number_id:
        return db.get_client_by_id(1)

    # Also handle case where phone_number_id == settings.WHATSAPP_PHONE_ID
    # but the clients table hasn't been updated yet
    if phone_number_id == settings.WHATSAPP_PHONE_ID:
        return db.get_client_by_id(1)

    return None


def _is_admin(phone: str) -> bool:
    """Return True if this is the super-admin phone number."""
    admin_phone = (settings.ADMIN_PHONE or "").strip()
    if not admin_phone:
        return False
    clean = lambda p: p.lstrip("+").lstrip("0")
    return clean(phone) == clean(admin_phone) or clean(phone).endswith(clean(admin_phone))


def _is_doctor(phone: str, client: dict) -> bool:
    """
    Return True if the sender is the doctor for this specific clinic.
    Checks client.contact_phone (DB) first, then env DOCTOR_PHONE (legacy).
    """
    # DB value: contact_phone on the client row
    doctor_phone = (client.get("contact_phone") or "").strip()
    # Fallback to env var (legacy single-tenant)
    if not doctor_phone:
        doctor_phone = settings.DOCTOR_PHONE

    if not doctor_phone:
        return False

    clean = lambda p: p.lstrip("+").lstrip("0")
    return (
        clean(phone) == clean(doctor_phone)
        or clean(phone).endswith(clean(doctor_phone))
    )
