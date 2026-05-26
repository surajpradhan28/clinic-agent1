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


# ── Razorpay client (lazy-init, only when KEY_ID is configured) ───────────────

def _razorpay_client():
    """Return a razorpay.Client or None if Razorpay is not configured."""
    if not settings.RAZORPAY_KEY_ID or not settings.RAZORPAY_KEY_SECRET:
        return None
    try:
        import razorpay  # optional dependency
        return razorpay.Client(auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET))
    except ImportError:
        logger.warning("razorpay package not installed — payment links disabled")
        return None


async def _create_razorpay_link(invoice: dict, client_row: dict) -> str | None:
    """
    Create a Razorpay Payment Link for an invoice.
    Stores link_id and short_url on the invoice row.
    Returns the short_url or None if Razorpay is not configured / creation fails.
    """
    rz = _razorpay_client()
    if rz is None:
        return None

    amount_paise = int(float(invoice["amount"]) * 100)   # Razorpay uses paise
    doctor_name  = client_row.get("doctor_name") or client_row.get("contact_name") or ""
    email        = client_row.get("contact_email") or ""
    phone        = client_row.get("contact_phone") or ""
    token        = invoice["invoice_token"]
    description  = (
        f"Clinic AI Agent — {invoice.get('plan','').title()} Plan — "
        f"{invoice.get('period_start','')[:7]}"
    )
    callback_url = f"{settings.SERVER_URL}/invoice/{token}"

    try:
        # Payment link expires 3 days after due_date
        from datetime import datetime as _dt, timezone as _tz, timedelta as _td
        due = _dt.strptime(invoice["due_date"], "%Y-%m-%d").replace(tzinfo=_tz.utc)
        expire_ts = int((due + _td(days=3)).timestamp())

        link = rz.payment_link.create({
            "amount":          amount_paise,
            "currency":        "INR",
            "description":     description,
            "reference_id":    token,           # our unique identifier
            "callback_url":    callback_url,
            "callback_method": "get",
            "expire_by":       expire_ts,
            "customer": {
                "name":    doctor_name,
                "email":   email,
                "contact": phone,
            },
            "notify":           {"sms": False, "email": False},
            "reminder_enable":  False,
            "options": {
                "checkout": {"name": settings.INVOICE_BUSINESS_NAME or "Clinic AI Agent"}
            },
        })
        link_id  = link.get("id", "")
        link_url = link.get("short_url", "")
        if link_id:
            db.update_invoice_payment_link(invoice["id"], link_id, link_url)
            logger.info("[Razorpay] Payment link created: %s → %s", link_id, link_url)
        return link_url or None
    except Exception as exc:
        logger.error("[Razorpay] Failed to create payment link for invoice %s: %s",
                     invoice.get("id"), exc)
        return None


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


@app.get("/signup")
async def signup_page(request: Request, plan: str = "", ref: str = ""):
    """
    Self-serve clinic signup page.
    ?plan=starter|pro|suite   — pre-selects a plan card
    ?ref=CODE                 — pre-fills referral code
    """
    plan = plan.lower() if plan in ("starter", "pro", "suite") else ""
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Start Free Trial — Clinic AI Agent</title>
  <style>
    *{{box-sizing:border-box;margin:0;padding:0;}}
    body{{font-family:'Segoe UI',Arial,sans-serif;background:#f0f4f8;color:#1a1a2e;}}
    .hero{{background:linear-gradient(135deg,#1A3A5C 0%,#2E75B6 100%);color:#fff;
           text-align:center;padding:48px 20px 40px;}}
    .hero h1{{font-size:clamp(24px,5vw,38px);font-weight:800;margin-bottom:10px;}}
    .hero p{{font-size:16px;opacity:.88;max-width:520px;margin:0 auto;}}
    .trial-badge{{display:inline-block;background:rgba(255,255,255,.18);
                  border:1.5px solid rgba(255,255,255,.4);border-radius:20px;
                  padding:5px 16px;font-size:13px;font-weight:700;margin-bottom:18px;
                  letter-spacing:.5px;}}
    .container{{max-width:900px;margin:0 auto;padding:32px 16px 60px;}}

    /* Plan cards */
    .plans{{display:grid;grid-template-columns:repeat(3,1fr);gap:16px;margin-bottom:36px;}}
    @media(max-width:640px){{.plans{{grid-template-columns:1fr;}}}}
    .plan-card{{background:#fff;border:2px solid #e0e7ef;border-radius:14px;
                padding:24px 20px;cursor:pointer;transition:all .2s;text-align:center;
                position:relative;}}
    .plan-card:hover,.plan-card.selected{{border-color:#2E75B6;box-shadow:0 4px 20px rgba(46,117,182,.18);}}
    .plan-card.selected{{background:#F0F8FF;}}
    .plan-card .badge{{position:absolute;top:-12px;left:50%;transform:translateX(-50%);
                       background:#2E75B6;color:#fff;font-size:11px;font-weight:700;
                       padding:3px 14px;border-radius:20px;letter-spacing:.5px;white-space:nowrap;}}
    .plan-card h3{{font-size:18px;font-weight:700;color:#1A3A5C;margin-bottom:4px;}}
    .plan-card .price{{font-size:28px;font-weight:800;color:#2E75B6;margin:8px 0 4px;}}
    .plan-card .price span{{font-size:14px;font-weight:400;color:#888;}}
    .plan-card ul{{list-style:none;margin-top:12px;text-align:left;}}
    .plan-card ul li{{font-size:13px;color:#444;padding:3px 0;}}
    .plan-card ul li::before{{content:"✓ ";color:#2E75B6;font-weight:700;}}
    .plan-card ul li.no::before{{content:"✗ ";color:#ccc;}}
    .plan-card ul li.no{{color:#bbb;}}

    /* Form */
    .form-card{{background:#fff;border-radius:16px;padding:36px 32px;
                box-shadow:0 4px 24px rgba(0,0,0,.08);}}
    @media(max-width:480px){{.form-card{{padding:24px 16px;}}}}
    .form-card h2{{font-size:22px;font-weight:700;color:#1A3A5C;margin-bottom:6px;}}
    .form-card p{{font-size:14px;color:#666;margin-bottom:24px;}}
    .form-grid{{display:grid;grid-template-columns:1fr 1fr;gap:16px;}}
    @media(max-width:560px){{.form-grid{{grid-template-columns:1fr;}}}}
    .field{{display:flex;flex-direction:column;gap:6px;}}
    .field.full{{grid-column:1/-1;}}
    label{{font-size:13px;font-weight:600;color:#444;}}
    input,select{{border:1.5px solid #d0d7e0;border-radius:8px;padding:10px 14px;
                  font-size:14px;color:#1a1a2e;outline:none;transition:border .2s;
                  font-family:inherit;}}
    input:focus,select:focus{{border-color:#2E75B6;box-shadow:0 0 0 3px rgba(46,117,182,.1);}}
    .phone-hint{{font-size:11px;color:#888;margin-top:2px;}}
    .submit-btn{{width:100%;background:linear-gradient(135deg,#1A3A5C,#2E75B6);
                 color:#fff;border:none;border-radius:10px;padding:14px;
                 font-size:16px;font-weight:700;cursor:pointer;margin-top:20px;
                 transition:opacity .2s;letter-spacing:.3px;}}
    .submit-btn:hover{{opacity:.9;}}
    .terms{{font-size:12px;color:#888;text-align:center;margin-top:12px;}}
    .terms a{{color:#2E75B6;}}

    /* Trust row */
    .trust{{display:flex;justify-content:center;gap:32px;flex-wrap:wrap;
             margin-top:28px;padding-top:24px;border-top:1px solid #eee;}}
    .trust-item{{text-align:center;}}
    .trust-item .num{{font-size:22px;font-weight:800;color:#1A3A5C;}}
    .trust-item .lbl{{font-size:12px;color:#888;}}
  </style>
</head>
<body>
  <div class="hero">
    <div class="trial-badge">🎁 7-Day Free Trial — No Credit Card Needed</div>
    <h1>Your Clinic's 24/7 WhatsApp Receptionist</h1>
    <p>Automate appointment booking, reminders, and patient communication — set up in under 10 minutes.</p>
  </div>

  <div class="container">
    <!-- Plan selector -->
    <div class="plans">
      <div class="plan-card {'selected' if plan=='starter' else ''}" id="card-starter" onclick="selectPlan('starter')">
        <h3>Starter</h3>
        <div class="price">₹999<span>/mo</span></div>
        <ul>
          <li>AI appointment booking</li>
          <li>24-hour reminders</li>
          <li>Clinic info Q&amp;A</li>
          <li class="no">Cancel &amp; reschedule</li>
          <li class="no">Waitlist</li>
          <li class="no">Patient intake form</li>
        </ul>
      </div>
      <div class="plan-card {'selected' if plan=='pro' else ''}" id="card-pro" onclick="selectPlan('pro')" style="{'border-color:#2E75B6;' if plan!='suite' else ''}">
        <div class="badge">⭐ Most Popular</div>
        <h3>Pro</h3>
        <div class="price">₹1,999<span>/mo</span></div>
        <ul>
          <li>Everything in Starter</li>
          <li>Cancel &amp; reschedule</li>
          <li>Waitlist auto-booking</li>
          <li>Patient intake form</li>
          <li>1-hour reminders</li>
          <li class="no">Daily schedule to doctor</li>
        </ul>
      </div>
      <div class="plan-card {'selected' if plan=='suite' else ''}" id="card-suite" onclick="selectPlan('suite')">
        <h3>Suite</h3>
        <div class="price">₹2,999<span>/mo</span></div>
        <ul>
          <li>Everything in Pro</li>
          <li>Daily schedule to doctor</li>
          <li>Broadcast to all patients</li>
          <li>Custom clinic hours</li>
          <li>Priority support</li>
          <li>Multi-doctor (coming soon)</li>
        </ul>
      </div>
    </div>

    <!-- Signup form -->
    <div class="form-card">
      <h2>Start Your Free 7-Day Trial</h2>
      <p>Fill in your details below. We'll set up your WhatsApp AI and contact you within a few hours.</p>

      <form method="POST" action="/signup" id="signupForm">
        <div class="form-grid">
          <div class="field">
            <label for="clinic_name">Clinic Name *</label>
            <input type="text" id="clinic_name" name="clinic_name" placeholder="e.g. City Health Clinic" required>
          </div>
          <div class="field">
            <label for="doctor_name">Doctor's Name *</label>
            <input type="text" id="doctor_name" name="doctor_name" placeholder="e.g. Dr. Priya Sharma" required>
          </div>
          <div class="field">
            <label for="contact_phone">Doctor's WhatsApp Number *</label>
            <input type="tel" id="contact_phone" name="contact_phone"
                   placeholder="e.g. 919876543210" required
                   pattern="[0-9]{{10,15}}">
            <span class="phone-hint">Country code + number, no spaces or + (e.g. 919876543210)</span>
          </div>
          <div class="field">
            <label for="city">City *</label>
            <input type="text" id="city" name="city" placeholder="e.g. Mumbai" required>
          </div>
          <div class="field">
            <label for="contact_email">Email <span style="color:#aaa;font-weight:400">(for invoices)</span></label>
            <input type="email" id="contact_email" name="contact_email" placeholder="doctor@example.com">
          </div>
          <div class="field">
            <label for="referred_by">Referral Code <span style="color:#aaa;font-weight:400">(optional)</span></label>
            <input type="text" id="referred_by" name="referred_by"
                   value="{ref}" placeholder="e.g. ABC123" style="text-transform:uppercase;">
          </div>
          <div class="field full">
            <label for="plan">Selected Plan *</label>
            <select id="plan" name="plan" required>
              <option value="starter" {'selected' if plan=='starter' else ''}>Starter — ₹999/month</option>
              <option value="pro" {'selected' if plan in ('pro','') else ''}>Pro — ₹1,999/month ⭐ Most Popular</option>
              <option value="suite" {'selected' if plan=='suite' else ''}>Suite — ₹2,999/month</option>
            </select>
          </div>
        </div>
        <button type="submit" class="submit-btn">🚀 Start My Free 7-Day Trial</button>
        <p class="terms">By signing up you agree to our
          <a href="/privacy" target="_blank">Privacy Policy</a> and
          <a href="/terms" target="_blank">Terms of Service</a>.
          No credit card required for the trial.
        </p>
      </form>

      <div class="trust">
        <div class="trust-item"><div class="num">10 min</div><div class="lbl">Average setup time</div></div>
        <div class="trust-item"><div class="num">40%</div><div class="lbl">Fewer no-shows</div></div>
        <div class="trust-item"><div class="num">24/7</div><div class="lbl">Patient bookings</div></div>
        <div class="trust-item"><div class="num">0</div><div class="lbl">App downloads needed</div></div>
      </div>
    </div>
  </div>

  <script>
    function selectPlan(plan) {{
      ['starter','pro','suite'].forEach(p => {{
        document.getElementById('card-'+p).classList.toggle('selected', p===plan);
      }});
      document.getElementById('plan').value = plan;
    }}
    // Pre-select Pro if nothing selected
    const sel = document.getElementById('plan').value;
    if(sel) selectPlan(sel); else selectPlan('pro');
  </script>
</body>
</html>"""
    return HTMLResponse(content=html)


@app.post("/signup")
async def signup_submit(request: Request):
    """
    Process clinic self-serve signup.
    Creates a client record with status='pending', logs the signup,
    notifies admin via WhatsApp, and shows the success page.
    """
    import secrets as _secrets
    from datetime import datetime as _dt, timedelta as _td, timezone

    form   = await request.form()
    clinic_name   = (form.get("clinic_name")   or "").strip()
    doctor_name   = (form.get("doctor_name")   or "").strip()
    contact_phone = (form.get("contact_phone") or "").strip().replace("+", "").replace(" ", "")
    city          = (form.get("city")          or "").strip()
    contact_email = (form.get("contact_email") or "").strip()
    plan          = (form.get("plan")          or "pro").lower()
    referred_by   = (form.get("referred_by")   or "").strip().upper()

    if not all([clinic_name, doctor_name, contact_phone, city]):
        raise HTTPException(status_code=400, detail="Please fill in all required fields.")
    if plan not in ("starter", "pro", "suite"):
        plan = "pro"

    # Generate a unique referral code for this new clinic
    ref_code = _secrets.token_hex(3).upper()

    # Trial window: 7 days from now
    now = _dt.now(timezone.utc)
    trial_ends = now + _td(days=7)

    # Create client in DB
    try:
        supabase = db.get_db()

        # Check for duplicate phone
        existing = (
            supabase.table("clients")
            .select("id, status")
            .eq("contact_phone", contact_phone)
            .limit(1)
            .execute()
        )
        if existing.data:
            # Already exists — show friendly message
            return HTMLResponse(content=_signup_already_exists_html(clinic_name, contact_phone), status_code=200)

        new_client = (
            supabase.table("clients")
            .insert({
                "clinic_name":      clinic_name,
                "doctor_name":      doctor_name,
                "contact_name":     doctor_name,
                "contact_phone":    contact_phone,
                "contact_email":    contact_email or None,
                "city":             city,
                "plan":             plan,
                "status":           "pending",
                "signup_source":    "web",
                "referred_by":      referred_by or None,
                "referral_code":    ref_code,
                "trial_started_at": now.isoformat(),
                "trial_ends_at":    trial_ends.isoformat(),
                "dashboard_key":    _secrets.token_urlsafe(24),
                "whatsapp_phone_id": "",   # Admin will fill in during setup
                "whatsapp_token":    "",
            })
            .execute()
        )
        client_id = new_client.data[0]["id"] if new_client.data else None

        # Log the signup
        supabase.table("signups").insert({
            "clinic_name":   clinic_name,
            "doctor_name":   doctor_name,
            "contact_phone": contact_phone,
            "contact_email": contact_email or None,
            "city":          city,
            "plan":          plan,
            "referred_by":   referred_by or None,
            "client_id":     client_id,
        }).execute()

    except Exception as exc:
        logger.error("[Signup] DB error: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail="Something went wrong. Please try again.")

    # Notify admin via WhatsApp
    if settings.ADMIN_PHONE:
        admin_msg = (
            f"🆕 *New Clinic Signup!*\n\n"
            f"🏥 {clinic_name}\n"
            f"👨‍⚕️ {doctor_name}\n"
            f"📱 {contact_phone}\n"
            f"📍 {city}\n"
            f"📦 Plan: {plan.title()}\n"
            f"{'🔗 Referred by: ' + referred_by if referred_by else ''}\n\n"
            f"⚡ Action needed: Set up their WhatsApp Business number and activate the account.\n"
            f"Client ID: {client_id}"
        )
        try:
            await whatsapp.send_text(settings.ADMIN_PHONE, admin_msg)
        except Exception:
            pass  # Don't fail signup if admin notify fails

    return HTMLResponse(content=_signup_success_html(clinic_name, doctor_name, plan, ref_code), status_code=200)


def _signup_success_html(clinic_name: str, doctor_name: str, plan: str, referral_code: str = "") -> str:
    plan_label   = plan.title()
    signup_url   = f"{settings.SERVER_URL}/signup"
    ref_link     = f"{signup_url}?ref={referral_code}" if referral_code else signup_url
    referral_box = f"""
    <div class="referral-box">
      <div class="ref-title">🤝 Refer a Doctor, Get 1 Free Month</div>
      <p style="font-size:13px;color:#444;margin-bottom:10px;">
        Share your unique referral link. Every doctor friend who subscribes earns you <strong>1 free month</strong> — no limit!
      </p>
      <div class="ref-code">{referral_code}</div>
      <div class="ref-url" id="refUrl">{ref_link}</div>
      <button class="copy-btn" onclick="copyLink()">📋 Copy Referral Link</button>
      <div id="copied" style="display:none;color:#2E7D32;font-size:13px;margin-top:6px;">✅ Copied!</div>
    </div>""" if referral_code else ""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>You're Signed Up! — Clinic AI Agent</title>
  <style>
    body{{font-family:'Segoe UI',Arial,sans-serif;background:#f0f4f8;
         display:flex;align-items:center;justify-content:center;min-height:100vh;padding:20px;}}
    .card{{background:#fff;border-radius:16px;padding:48px 40px;max-width:540px;width:100%;
           text-align:center;box-shadow:0 8px 32px rgba(0,0,0,.1);}}
    .icon{{font-size:56px;margin-bottom:16px;}}
    h1{{font-size:26px;font-weight:800;color:#1A3A5C;margin-bottom:8px;}}
    p{{color:#555;font-size:15px;line-height:1.7;margin-bottom:12px;}}
    .steps{{background:#F0F8FF;border-radius:12px;padding:20px 24px;text-align:left;margin:24px 0;}}
    .steps h3{{font-size:14px;font-weight:700;color:#1A3A5C;margin-bottom:12px;text-transform:uppercase;letter-spacing:.5px;}}
    .step{{display:flex;gap:12px;align-items:flex-start;margin-bottom:10px;font-size:14px;color:#333;}}
    .step .num{{background:#2E75B6;color:#fff;border-radius:50%;width:22px;height:22px;
                display:flex;align-items:center;justify-content:center;font-size:12px;
                font-weight:700;flex-shrink:0;margin-top:1px;}}
    .badge{{display:inline-block;background:#E8F5E9;color:#2E7D32;border-radius:20px;
            padding:4px 14px;font-size:13px;font-weight:700;margin:8px 0 0;}}
    .referral-box{{background:linear-gradient(135deg,#1A3A5C08,#2E75B612);
                   border:1.5px solid #2E75B630;border-radius:14px;
                   padding:20px 24px;margin:24px 0;text-align:center;}}
    .ref-title{{font-size:16px;font-weight:800;color:#1A3A5C;margin-bottom:8px;}}
    .ref-code{{font-size:28px;font-weight:900;color:#2E75B6;letter-spacing:4px;
               background:#fff;border-radius:8px;padding:8px 20px;
               display:inline-block;margin-bottom:8px;border:2px dashed #2E75B680;}}
    .ref-url{{font-size:12px;color:#888;word-break:break-all;margin-bottom:10px;}}
    .copy-btn{{background:#2E75B6;color:#fff;border:none;border-radius:8px;
               padding:10px 24px;font-size:14px;font-weight:700;cursor:pointer;}}
    .copy-btn:hover{{opacity:.88;}}
  </style>
</head>
<body>
  <div class="card">
    <div class="icon">🎉</div>
    <h1>You're all set, {doctor_name.split()[-1]}!</h1>
    <div class="badge">7-Day Free Trial Started</div>
    <br><br>
    <p>We've received your signup for <strong>{clinic_name}</strong> on the <strong>{plan_label} Plan</strong>.
       Our team will have your WhatsApp AI up and running shortly.</p>

    <div class="steps">
      <h3>What happens next</h3>
      <div class="step"><div class="num">1</div>
        <div>Our team sets up your dedicated WhatsApp Business number — usually within a few hours.</div>
      </div>
      <div class="step"><div class="num">2</div>
        <div>You'll receive a WhatsApp welcome message with setup instructions.</div>
      </div>
      <div class="step"><div class="num">3</div>
        <div>Send your first test booking and see the AI in action. Setup takes under 10 minutes.</div>
      </div>
      <div class="step"><div class="num">4</div>
        <div>Your 7-day trial begins from activation day — full access, no credit card needed.</div>
      </div>
    </div>

    {referral_box}

    <p style="font-size:13px;color:#888;">Questions? WhatsApp us at <strong>{settings.ADMIN_PHONE or 'our support number'}</strong>.</p>
  </div>
  <script>
    function copyLink() {{
      const url = document.getElementById('refUrl').innerText;
      navigator.clipboard.writeText(url).then(() => {{
        document.getElementById('copied').style.display = 'block';
        setTimeout(() => document.getElementById('copied').style.display = 'none', 2500);
      }});
    }}
  </script>
</body>
</html>"""


def _signup_already_exists_html(clinic_name: str, phone: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Already Registered</title>
  <style>
    body{{font-family:'Segoe UI',Arial,sans-serif;background:#f0f4f8;
         display:flex;align-items:center;justify-content:center;min-height:100vh;padding:20px;}}
    .card{{background:#fff;border-radius:16px;padding:48px 40px;max-width:460px;width:100%;
           text-align:center;box-shadow:0 8px 32px rgba(0,0,0,.1);}}
  </style>
</head>
<body>
  <div class="card">
    <div style="font-size:48px;margin-bottom:16px;">👋</div>
    <h2 style="color:#1A3A5C;margin-bottom:12px;">You're already registered!</h2>
    <p style="color:#555;font-size:15px;">
      A clinic account already exists for <strong>{phone}</strong>.
      If you need help, please WhatsApp us at <strong>{settings.ADMIN_PHONE or 'our support number'}</strong>.
    </p>
    <a href="/signup" style="display:inline-block;margin-top:24px;background:#2E75B6;color:#fff;
       padding:10px 24px;border-radius:8px;text-decoration:none;font-weight:700;font-size:14px;">
      ← Back to Signup
    </a>
  </div>
</body>
</html>"""


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

    # Build payment section (pre-computed to avoid nested f-string issues)
    rzp_url = invoice.get("razorpay_payment_link_url", "")
    if rzp_url and status != "PAID":
        payment_section = (
            f'<p style="margin-bottom:14px;">Please pay before <strong>{due_str}</strong> to avoid service interruption.</p>'
            f'<a href="{rzp_url}" target="_blank" class="no-print" style="display:inline-block;background:linear-gradient(135deg,#1A3A5C,#2E75B6);color:#fff;text-decoration:none;padding:13px 36px;border-radius:10px;font-size:16px;font-weight:700;letter-spacing:.3px;margin-bottom:12px;">&#x1F4B3; Pay Now &mdash; {amount_str}</a>'
            f'<p style="font-size:12px;color:#888;margin-top:6px;">Secure payment powered by Razorpay &middot; UPI / Cards / Netbanking</p>'
        )
    else:
        payment_section = (
            f'<p>Please pay before <strong>{due_str}</strong> to avoid service interruption.<br><br>'
            f'UPI (Google Pay / PhonePe / Paytm):<br>'
            f'<span class="upi">{settings.INVOICE_UPI_ID}</span><br><br>'
            f'After paying, WhatsApp the screenshot to confirm your renewal.</p>'
        )

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
        <h3>💳 Payment</h3>
        {payment_section}
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


@app.post("/razorpay/webhook")
async def razorpay_webhook(request: Request):
    """
    Razorpay payment webhook — auto-marks invoices paid.

    Setup in Razorpay Dashboard → Webhooks:
      URL:    https://<your-railway-domain>/razorpay/webhook
      Events: payment_link.paid
      Secret: set RAZORPAY_WEBHOOK_SECRET env var to the same value

    On payment_link.paid:
      1. Verify HMAC-SHA256 signature
      2. Find invoice by razorpay_payment_link_id (reference_id = invoice_token)
      3. Mark invoice paid + store razorpay_payment_id
      4. Record payment in payments table
      5. Activate client subscription (trial → active)
      6. WhatsApp doctor "✅ Payment received"
      7. Notify admin
    """
    raw_body = await request.body()

    # ── Signature verification ────────────────────────────────────────────────
    if settings.RAZORPAY_WEBHOOK_SECRET:
        sig = request.headers.get("X-Razorpay-Signature", "")
        expected = hmac.new(
            settings.RAZORPAY_WEBHOOK_SECRET.encode(),
            raw_body,
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(expected, sig):
            logger.warning("[Razorpay] Webhook signature mismatch — rejecting")
            raise HTTPException(status_code=400, detail="Invalid signature")
    else:
        logger.warning("[Razorpay] RAZORPAY_WEBHOOK_SECRET not set — skipping signature check")

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    event = payload.get("event", "")
    logger.info("[Razorpay] Webhook event: %s", event)

    if event != "payment_link.paid":
        return JSONResponse({"status": "ignored", "event": event})

    # ── Extract identifiers ───────────────────────────────────────────────────
    try:
        pl_entity  = payload["payload"]["payment_link"]["entity"]
        pay_entity = payload["payload"]["payment"]["entity"]
        link_id    = pl_entity["id"]            # plink_xxx
        ref_id     = pl_entity["reference_id"]  # our invoice_token
        rzp_pay_id = pay_entity["id"]           # pay_xxx
        amount_paid_paise = int(pay_entity.get("amount", 0))
    except (KeyError, TypeError) as exc:
        logger.error("[Razorpay] Malformed webhook payload: %s", exc)
        raise HTTPException(status_code=400, detail="Malformed payload")

    # ── Find invoice ──────────────────────────────────────────────────────────
    # Try by payment link ID first, then fall back to reference_id (invoice_token)
    invoice = db.get_invoice_by_razorpay_link(link_id)
    if not invoice:
        invoice = db.get_invoice_by_token(ref_id)
    if not invoice:
        logger.error("[Razorpay] Invoice not found for link_id=%s ref_id=%s", link_id, ref_id)
        return JSONResponse({"status": "invoice_not_found"})

    if invoice["status"] == "paid":
        logger.info("[Razorpay] Invoice %s already paid — skipping", invoice["id"])
        return JSONResponse({"status": "already_paid"})

    client_id  = invoice["client_id"]
    amount_inr = amount_paid_paise / 100

    # ── Mark invoice paid ─────────────────────────────────────────────────────
    db.mark_invoice_paid(invoice["id"], client_id, razorpay_payment_id=rzp_pay_id)

    # ── Record payment in payments table ──────────────────────────────────────
    db.record_payment(
        client_id=client_id,
        amount=amount_inr,
        method="razorpay",
        notes=f"Auto-detected via Razorpay webhook. pay_id={rzp_pay_id}",
    )

    # ── Activate client if still on trial ────────────────────────────────────
    client_row = db.get_client_by_id(client_id)
    if client_row and client_row.get("status") in ("trial", "pending", "suspended"):
        db.update_client_status(client_id, "active")
        logger.info("[Razorpay] Client %s activated after payment", client_id)

    # ── WhatsApp doctor: payment confirmed ────────────────────────────────────
    if client_row:
        doctor_phone  = client_row.get("contact_phone") or ""
        client_pid    = client_row.get("whatsapp_phone_id") or settings.WHATSAPP_PHONE_ID
        client_token  = client_row.get("whatsapp_token") or None
        cli_settings  = db.get_all_clinic_settings(client_id)
        doctor_name   = cli_settings.get("doctor_name") or client_row.get("doctor_name") or "Doctor"
        plan_label    = invoice.get("plan", "").title()
        inv_num       = invoice.get("invoice_number", "")

        if doctor_phone:
            confirm_msg = (
                f"✅ *Payment Received — Thank you, Dr. {doctor_name}!*\n\n"
                f"Invoice: *{inv_num}*\n"
                f"Plan: *{plan_label}*\n"
                f"Amount: *₹{amount_inr:,.0f}*\n"
                f"Payment ID: `{rzp_pay_id}`\n\n"
                f"Your Clinic AI Agent subscription is active. "
                f"All features are running as usual. 🙏"
            )
            try:
                await whatsapp.send_text(doctor_phone, confirm_msg, phone_id=client_pid, token=client_token)
            except Exception as exc:
                logger.warning("[Razorpay] WhatsApp confirm failed for client %s: %s", client_id, exc)

        # Notify admin
        if settings.ADMIN_PHONE:
            clinic_name = cli_settings.get("clinic_name") or client_row.get("clinic_name") or f"Client {client_id}"
            try:
                await whatsapp.send_text(
                    settings.ADMIN_PHONE,
                    f"💰 *Auto-payment received!*\n\n"
                    f"🏥 {clinic_name} [{client_id}]\n"
                    f"📋 Invoice: {inv_num}\n"
                    f"💵 ₹{amount_inr:,.0f} via Razorpay\n"
                    f"🔑 {rzp_pay_id}",
                )
            except Exception:
                pass

    logger.info("[Razorpay] Invoice %s marked paid (₹%.0f, pay_id=%s)", invoice["id"], amount_inr, rzp_pay_id)
    return JSONResponse({"status": "ok"})


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
