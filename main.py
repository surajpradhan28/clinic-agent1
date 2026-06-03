"""
main.py â FastAPI application + WhatsApp webhook handler (multi-tenant v5).

Entry point for the Clinic AI Agent.

Endpoints:
  GET  /                          â Health check
  GET  /webhook                   â Meta webhook verification (one-time setup)
  POST /webhook                   â Incoming WhatsApp messages
  GET  /health                    â Detailed health check (DB + config)
  GET  /admin?key=<SECRET>        â Super-admin web dashboard (Arun only)
  POST /admin/action?key=<SECRET> â Dashboard actions (suspend/activate/payment/new_client)
  GET  /clinic?key=<dashboard_key>â Per-clinic read-only dashboard (doctors)

Routing on every incoming message:
  0. If sender is ADMIN_PHONE â super-admin command handler (admin.py)
  1. Extract phone_number_id from webhook (which clinic's number was messaged)
  2. Look up clients table â resolve client row
  3. Check client status (active / grace / suspended / expired)
  4. If doctor â doctor management flow
  5. If patient has active follow-up â followup flow
  6. Otherwise â AI booking agent flow
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

# ââ Logging setup âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

logging.basicConfig(
    stream=sys.stdout,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


# ââ Razorpay client (lazy-init, only when KEY_ID is configured) âââââââââââââââ

def _razorpay_client():
    """Return a razorpay.Client or None if Razorpay is not configured."""
    if not settings.RAZORPAY_KEY_ID or not settings.RAZORPAY_KEY_SECRET:
        return None
    try:
        import razorpay  # optional dependency
        return razorpay.Client(auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET))
    except ImportError:
        logger.warning("razorpay package not installed â payment links disabled")
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
        f"Clinic AI Agent â {invoice.get('plan','').title()} Plan â "
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
            logger.info("[Razorpay] Payment link created: %s â %s", link_id, link_url)
        return link_url or None
    except Exception as exc:
        logger.error("[Razorpay] Failed to create payment link for invoice %s: %s",
                     invoice.get("id"), exc)
        return None


# ââ Webhook deduplication (in-memory, 5-minute TTL) ââââââââââââââââââââââââââ
# WhatsApp Cloud API sometimes delivers the same webhook event more than once.
# We track recently-seen message_ids so duplicate deliveries are silently dropped.

_seen_message_ids: dict[str, float] = {}   # message_id â first-seen timestamp
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


# ââ Per-user rate limiting (in-memory, sliding window) âââââââââââââââââââââââ
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


# ââ WhatsApp webhook signature verification âââââââââââââââââââââââââââââââââââ
# Meta signs every webhook POST body with HMAC-SHA256 using the App Secret.
# The signature is in the X-Hub-Signature-256 header as "sha256=<hex>".
# We verify it to reject forged/spoofed requests before any processing.
# If WHATSAPP_APP_SECRET is not configured, verification is skipped (logs a
# warning) â set it in production for security.

async def _verify_webhook_signature(request: Request, body: bytes) -> bool:
    """
    Return True if the request body matches the Meta HMAC-SHA256 signature.
    Always returns True if WHATSAPP_APP_SECRET is not configured (with a warning).
    """
    app_secret = settings.WHATSAPP_APP_SECRET
    if not app_secret:
        logger.warning(
            "â ï¸  WHATSAPP_APP_SECRET not set â webhook signature NOT verified. "
            "Set this in Railway env vars for production security."
        )
        return True

    signature_header = request.headers.get("X-Hub-Signature-256", "")
    if not signature_header:
        logger.warning("ð« Webhook request missing X-Hub-Signature-256 header â rejected")
        return False

    expected_sig = "sha256=" + hmac.new(
        app_secret.encode("utf-8"),
        body,
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(signature_header, expected_sig):
        logger.warning(
            "ð« Webhook signature mismatch (got=%s, expected=%sâ¦) â rejected",
            signature_header[:20],
            expected_sig[:20],
        )
        return False

    return True


# ââ App lifecycle âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("ð¥ Clinic AI Agent starting upâ¦")
    try:
        settings.validate()
        logger.info("â Config validated")
    except EnvironmentError as exc:
        logger.error("â Configuration error: %s", exc)

    sched.start()
    logger.info("â Scheduler started")
    logger.info("ð Clinic AI Agent ready!")

    yield

    sched.stop()
    logger.info("ð Clinic AI Agent shutting down")


app = FastAPI(
    title="Clinic AI Agent",
    description="WhatsApp appointment booking agent for Indian clinics (multi-tenant)",
    version="2.0.0",
    lifespan=lifespan,
)

# ââ HTTPS redirect middleware âââââââââââââââââââââââââââââââââââââââââââââââââ
@app.middleware("http")
async def https_redirect(request: Request, call_next):
    """Redirect all HTTP requests to HTTPS in production."""
    if request.headers.get("x-forwarded-proto") == "http":
        url = request.url.replace(scheme="https")
        return JSONResponse(
            status_code=301,
            headers={"Location": str(url)},
            content={}
        )
    return await call_next(request)


# ââ Health check ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

@app.get("/version")
async def version_check():
    return {"version": "v_debug_2026", "invoice_fix": "sent_at_or_fix"}

@app.get("/")
async def landing_page():
    """Public marketing landing page served at the root URL."""
    wa_phone  = (settings.ADMIN_PHONE or "919876543210")
    wa_link   = f"https://wa.me/{wa_phone}?text=Hi%2C+I+want+to+know+more+about+Clinic+AI+Agent"
    wa_demo   = f"https://wa.me/{wa_phone}?text=Hi%2C+I+want+a+free+demo+of+Clinic+AI+Agent"
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Clinic AI Agent â WhatsApp Bot for Doctor Clinics</title>
<meta name="description" content="Automate appointment booking, reminders &amp; patient follow-ups on WhatsApp. Built for Indian doctor clinics. No app needed.">
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
:root{{
  --green:#25D366;--dark-green:#128C7E;--navy:#0d1b2a;--navy2:#0f2137;
  --text:#1a2332;--muted:#64748b;--light:#f8fafc;--border:#e2e8f0;
  --card:#ffffff;--accent:#1A56DB;
}}
html{{scroll-behavior:smooth}}
body{{font-family:'Segoe UI',system-ui,-apple-system,Arial,sans-serif;color:var(--text);background:#fff;line-height:1.6;overflow-x:hidden}}
a{{color:inherit;text-decoration:none}}
img{{display:block;max-width:100%}}
.container{{max-width:1100px;margin:0 auto;padding:0 24px}}
nav{{position:sticky;top:0;z-index:100;background:rgba(255,255,255,0.95);backdrop-filter:blur(12px);border-bottom:1px solid var(--border);padding:0 24px}}
.nav-inner{{max-width:1100px;margin:0 auto;height:64px;display:flex;align-items:center;justify-content:space-between}}
.nav-logo{{display:flex;align-items:center;gap:10px}}
.nav-icon{{width:36px;height:36px;border-radius:10px;background:linear-gradient(135deg,var(--green),var(--dark-green));display:flex;align-items:center;justify-content:center;font-size:18px}}
.nav-name{{font-size:16px;font-weight:800;color:var(--navy);letter-spacing:-.3px}}
.nav-links{{display:flex;align-items:center;gap:28px}}
.nav-links a{{font-size:14px;font-weight:500;color:var(--muted);transition:color .2s}}
.nav-links a:hover{{color:var(--navy)}}
.btn-nav{{background:var(--green);color:#fff;font-size:13px;font-weight:700;padding:8px 20px;border-radius:8px;transition:all .2s;border:none;cursor:pointer}}
.btn-nav:hover{{background:var(--dark-green);transform:translateY(-1px)}}
.hero{{background:linear-gradient(160deg,#0d1b2a 0%,#0f2137 40%,#0a3d2e 100%);padding:80px 24px 60px;overflow:hidden;position:relative}}
.hero::before{{content:'';position:absolute;inset:0;background:radial-gradient(ellipse 60% 50% at 70% 50%,rgba(37,211,102,0.12),transparent)}}
.hero-inner{{max-width:1100px;margin:0 auto;display:flex;align-items:center;gap:60px;position:relative}}
.hero-text{{flex:1;min-width:0}}
.hero-badge{{display:inline-flex;align-items:center;gap:7px;background:rgba(37,211,102,0.12);border:1px solid rgba(37,211,102,0.3);color:#25D366;font-size:12px;font-weight:700;padding:5px 12px;border-radius:20px;margin-bottom:20px;letter-spacing:.3px}}
.hero-badge::before{{content:'â';font-size:8px;animation:blink 1.5s infinite}}
@keyframes blink{{0%,100%{{opacity:1}}50%{{opacity:.3}}}}
h1{{font-size:clamp(28px,4vw,46px);font-weight:900;color:#fff;line-height:1.15;letter-spacing:-.5px;margin-bottom:16px}}
h1 span{{color:var(--green)}}
.hero-sub{{font-size:16px;color:rgba(255,255,255,0.65);margin-bottom:32px;max-width:480px;line-height:1.7}}
.hero-ctas{{display:flex;gap:12px;flex-wrap:wrap}}
.btn-primary{{display:inline-flex;align-items:center;gap:8px;background:var(--green);color:#fff;font-size:15px;font-weight:700;padding:13px 28px;border-radius:10px;transition:all .25s;border:none;cursor:pointer}}
.btn-primary:hover{{background:var(--dark-green);transform:translateY(-2px);box-shadow:0 8px 24px rgba(37,211,102,0.35)}}
.btn-ghost{{display:inline-flex;align-items:center;gap:8px;background:rgba(255,255,255,0.08);color:#fff;font-size:15px;font-weight:600;padding:13px 28px;border-radius:10px;border:1px solid rgba(255,255,255,0.2);transition:all .25s;cursor:pointer}}
.btn-ghost:hover{{background:rgba(255,255,255,0.15)}}
.hero-trust{{margin-top:28px;display:flex;align-items:center;gap:12px}}
.hero-trust-text{{font-size:13px;color:rgba(255,255,255,0.5)}}
.trust-logos{{display:flex;gap:8px}}
.trust-chip{{background:rgba(255,255,255,0.08);border:1px solid rgba(255,255,255,0.12);color:rgba(255,255,255,0.6);font-size:11px;font-weight:500;padding:4px 10px;border-radius:6px}}
.hero-phone{{flex-shrink:0;width:260px}}
.phone-frame{{background:#1a1a1a;border-radius:36px;padding:10px;box-shadow:0 40px 80px rgba(0,0,0,0.5),0 0 0 1px rgba(255,255,255,0.06)}}
.phone-screen{{background:#fff;border-radius:28px;overflow:hidden}}
.p-status{{background:#25D366;padding:4px 12px;font-size:9px;color:rgba(255,255,255,0.9);font-weight:600;display:flex;justify-content:space-between}}
.p-header{{background:#128C7E;padding:8px 10px;display:flex;align-items:center;gap:8px}}
.p-avatar{{width:30px;height:30px;border-radius:50%;background:rgba(255,255,255,0.2);display:flex;align-items:center;justify-content:center;font-size:12px;font-weight:800;color:#fff;flex-shrink:0}}
.p-hname{{font-size:12px;font-weight:700;color:#fff}}
.p-hstatus{{font-size:9px;color:rgba(255,255,255,0.8)}}
.p-chat{{background:#e5ddd5;padding:8px;display:flex;flex-direction:column;gap:4px;min-height:200px}}
.p-msg{{max-width:78%;padding:5px 8px 3px;border-radius:8px;font-size:10px;line-height:1.45;position:relative}}
.p-msg b{{font-weight:700}}
.p-msg.r{{background:#dcf8c6;align-self:flex-end;border-radius:8px 8px 0 8px}}
.p-msg.l{{background:#fff;align-self:flex-start;border-radius:8px 8px 8px 0}}
.p-time{{font-size:8px;color:rgba(0,0,0,0.4);text-align:right;margin-top:2px}}
.p-input{{background:#f0f0f0;padding:5px 8px;display:flex;gap:6px;align-items:center}}
.p-input-box{{flex:1;background:#fff;border-radius:16px;padding:4px 10px;font-size:9px;color:#aaa}}
.p-send{{width:24px;height:24px;border-radius:50%;background:#128C7E;display:flex;align-items:center;justify-content:center;font-size:11px;color:#fff}}
.p-date-chip{{align-self:center;background:rgba(255,255,255,0.85);font-size:8.5px;color:#666;padding:2px 7px;border-radius:5px;margin:2px 0;font-weight:500}}
.stats-bar{{background:var(--light);border-bottom:1px solid var(--border);padding:20px 24px}}
.stats-inner{{max-width:1100px;margin:0 auto;display:flex;justify-content:space-around;flex-wrap:wrap;gap:16px}}
.stat-item{{text-align:center}}
.stat-num{{font-size:26px;font-weight:900;color:var(--navy);letter-spacing:-1px}}
.stat-num span{{color:var(--green)}}
.stat-lbl{{font-size:12px;color:var(--muted);font-weight:500;margin-top:2px}}
section{{padding:72px 24px}}
.section-label{{font-size:12px;font-weight:700;color:var(--green);letter-spacing:2px;text-transform:uppercase;margin-bottom:10px}}
h2{{font-size:clamp(22px,3vw,36px);font-weight:900;color:var(--navy);letter-spacing:-.5px;line-height:1.2;margin-bottom:12px}}
.section-sub{{font-size:16px;color:var(--muted);max-width:540px;line-height:1.7}}
.text-center{{text-align:center}}
.text-center .section-sub{{margin:0 auto}}
.features-bg{{background:#fff}}
.features-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:20px;margin-top:48px}}
.feat-card{{border:1px solid var(--border);border-radius:14px;padding:24px;transition:all .25s;background:#fff}}
.feat-card:hover{{border-color:rgba(37,211,102,0.4);box-shadow:0 8px 32px rgba(37,211,102,0.08);transform:translateY(-3px)}}
.feat-icon{{width:44px;height:44px;border-radius:12px;background:linear-gradient(135deg,rgba(37,211,102,0.1),rgba(18,140,126,0.1));border:1px solid rgba(37,211,102,0.2);display:flex;align-items:center;justify-content:center;font-size:22px;margin-bottom:14px}}
.feat-title{{font-size:16px;font-weight:800;color:var(--navy);margin-bottom:8px}}
.feat-desc{{font-size:14px;color:var(--muted);line-height:1.65}}
.feat-tags{{display:flex;flex-wrap:wrap;gap:6px;margin-top:12px}}
.ftag{{font-size:11px;font-weight:600;color:var(--dark-green);background:rgba(37,211,102,0.08);border:1px solid rgba(37,211,102,0.15);padding:3px 8px;border-radius:10px}}
.how-bg{{background:var(--light)}}
.steps{{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:24px;margin-top:48px}}
.step{{background:#fff;border-radius:14px;padding:28px 24px;border:1px solid var(--border);position:relative}}
.step-num{{width:36px;height:36px;border-radius:50%;background:var(--green);color:#fff;font-size:16px;font-weight:900;display:flex;align-items:center;justify-content:center;margin-bottom:16px}}
.step-title{{font-size:16px;font-weight:800;color:var(--navy);margin-bottom:8px}}
.step-desc{{font-size:14px;color:var(--muted);line-height:1.65}}
.step-arrow{{position:absolute;right:-16px;top:50%;transform:translateY(-50%);font-size:20px;color:var(--border);z-index:1}}
.pricing-bg{{background:#fff}}
.pricing-toggle{{display:flex;align-items:center;justify-content:center;gap:12px;margin:24px 0 48px}}
.toggle-label{{font-size:14px;font-weight:600;color:var(--muted)}}
.toggle-label.active{{color:var(--navy)}}
.toggle-track{{width:48px;height:26px;background:var(--green);border-radius:13px;position:relative;cursor:pointer;transition:background .3s}}
.toggle-thumb{{width:20px;height:20px;background:#fff;border-radius:50%;position:absolute;top:3px;left:3px;transition:transform .3s;box-shadow:0 2px 4px rgba(0,0,0,0.2)}}
.toggle-track.annual .toggle-thumb{{transform:translateX(22px)}}
.save-badge{{background:rgba(37,211,102,0.1);color:var(--green);font-size:11px;font-weight:700;padding:3px 8px;border-radius:10px;border:1px solid rgba(37,211,102,0.2)}}
.pricing-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:20px;max-width:960px;margin:0 auto}}
.plan-card{{border:2px solid var(--border);border-radius:16px;padding:28px 24px;position:relative;background:#fff;transition:all .25s}}
.plan-card:hover{{transform:translateY(-4px);box-shadow:0 16px 48px rgba(0,0,0,0.08)}}
.plan-card.popular{{border-color:var(--green);box-shadow:0 8px 32px rgba(37,211,102,0.15)}}
.popular-badge{{position:absolute;top:-12px;left:50%;transform:translateX(-50%);background:var(--green);color:#fff;font-size:11px;font-weight:800;padding:4px 14px;border-radius:20px;white-space:nowrap;letter-spacing:.3px}}
.plan-name{{font-size:13px;font-weight:700;text-transform:uppercase;letter-spacing:1.5px;color:var(--muted);margin-bottom:8px}}
.plan-price{{display:flex;align-items:baseline;gap:4px;margin-bottom:4px}}
.plan-rupee{{font-size:22px;font-weight:700;color:var(--navy)}}
.plan-amount{{font-size:40px;font-weight:900;color:var(--navy);letter-spacing:-2px;line-height:1}}
.plan-period{{font-size:14px;color:var(--muted);font-weight:500}}
.plan-annual{{font-size:12px;color:var(--green);font-weight:600;margin-bottom:6px;min-height:18px}}
.plan-desc{{font-size:13px;color:var(--muted);margin-bottom:20px;padding-bottom:20px;border-bottom:1px solid var(--border)}}
.plan-features{{display:flex;flex-direction:column;gap:9px;margin-bottom:24px}}
.pf{{display:flex;align-items:flex-start;gap:9px;font-size:13px;color:var(--text)}}
.pf-check{{color:var(--green);font-size:14px;font-weight:900;flex-shrink:0;margin-top:1px}}
.pf-x{{color:#cbd5e1;font-size:14px;flex-shrink:0;margin-top:1px}}
.btn-plan{{width:100%;padding:12px;border-radius:10px;font-size:14px;font-weight:700;border:none;cursor:pointer;transition:all .25s}}
.btn-plan.primary{{background:var(--green);color:#fff}}
.btn-plan.primary:hover{{background:var(--dark-green);transform:translateY(-1px)}}
.btn-plan.outline{{background:#fff;color:var(--navy);border:2px solid var(--border)}}
.btn-plan.outline:hover{{border-color:var(--green);color:var(--green)}}
.setup-fee-note{{text-align:center;font-size:12px;color:var(--muted);margin-top:20px}}
.setup-fee-note strong{{color:var(--navy)}}
.testi-bg{{background:linear-gradient(135deg,var(--navy),var(--navy2))}}
.testi-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:20px;margin-top:48px}}
.testi-card{{background:rgba(255,255,255,0.06);border:1px solid rgba(255,255,255,0.1);border-radius:14px;padding:24px}}
.testi-stars{{color:#fbbf24;font-size:16px;margin-bottom:12px;letter-spacing:2px}}
.testi-text{{font-size:14px;color:rgba(255,255,255,0.8);line-height:1.7;margin-bottom:16px;font-style:italic}}
.testi-author{{display:flex;align-items:center;gap:10px}}
.testi-avatar{{width:36px;height:36px;border-radius:50%;background:linear-gradient(135deg,var(--green),var(--dark-green));display:flex;align-items:center;justify-content:center;font-size:13px;font-weight:800;color:#fff;flex-shrink:0}}
.testi-name{{font-size:14px;font-weight:700;color:#fff}}
.testi-role{{font-size:12px;color:rgba(255,255,255,0.5)}}
.faq-bg{{background:var(--light)}}
.faq-list{{max-width:720px;margin:48px auto 0;display:flex;flex-direction:column;gap:12px}}
.faq-item{{background:#fff;border:1px solid var(--border);border-radius:12px;overflow:hidden}}
.faq-q{{padding:18px 20px;font-size:15px;font-weight:700;color:var(--navy);cursor:pointer;display:flex;justify-content:space-between;align-items:center;transition:background .2s}}
.faq-q:hover{{background:var(--light)}}
.faq-chevron{{font-size:12px;color:var(--muted);transition:transform .3s}}
.faq-a{{padding:0 20px;font-size:14px;color:var(--muted);line-height:1.7;max-height:0;overflow:hidden;transition:max-height .35s ease,padding .2s}}
.faq-item.open .faq-a{{max-height:200px;padding:0 20px 18px}}
.faq-item.open .faq-chevron{{transform:rotate(180deg)}}
.cta-section{{background:linear-gradient(135deg,#0d1b2a,#0a3d2e);padding:72px 24px;text-align:center}}
.cta-section h2{{color:#fff}}
.cta-section .section-sub{{color:rgba(255,255,255,0.6);margin:12px auto 32px}}
.cta-chips{{display:flex;flex-wrap:wrap;justify-content:center;gap:10px;margin-bottom:32px}}
.cta-chip{{background:rgba(255,255,255,0.06);border:1px solid rgba(255,255,255,0.12);color:rgba(255,255,255,0.7);font-size:13px;font-weight:500;padding:7px 14px;border-radius:20px;display:flex;align-items:center;gap:6px}}
footer{{background:var(--navy);padding:40px 24px;border-top:1px solid rgba(255,255,255,0.07)}}
.footer-inner{{max-width:1100px;margin:0 auto;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:20px}}
.footer-logo{{display:flex;align-items:center;gap:10px}}
.footer-name{{font-size:15px;font-weight:800;color:#fff}}
.footer-tagline{{font-size:12px;color:rgba(255,255,255,0.4);margin-top:2px}}
.footer-links{{display:flex;gap:24px}}
.footer-links a{{font-size:13px;color:rgba(255,255,255,0.45);transition:color .2s}}
.footer-links a:hover{{color:#25D366}}
.footer-copy{{font-size:12px;color:rgba(255,255,255,0.3)}}
@media(max-width:768px){{
  .hero-inner{{flex-direction:column;gap:36px}}
  .hero-phone{{display:none}}
  .nav-links{{display:none}}
  .step-arrow{{display:none}}
  .footer-inner{{flex-direction:column;text-align:center}}
  .footer-links{{justify-content:center}}
}}
.reveal{{opacity:0;transform:translateY(24px);transition:opacity .6s ease,transform .6s ease}}
.reveal.visible{{opacity:1;transform:translateY(0)}}
</style>
</head>
<body>

<nav>
  <div class="nav-inner">
    <div class="nav-logo">
      <div class="nav-icon">ð¥</div>
      <div><div class="nav-name">Clinic AI Agent</div></div>
    </div>
    <div class="nav-links">
      <a href="#features">Features</a>
      <a href="#how">How It Works</a>
      <a href="#pricing">Pricing</a>
      <a href="#faq">FAQ</a>
    </div>
    <a href="{wa_link}" target="_blank"><button class="btn-nav">ð¬ WhatsApp Us</button></a>
  </div>
</nav>

<section class="hero" style="padding:72px 24px 56px">
  <div class="hero-inner">
    <div class="hero-text">
      <div class="hero-badge">ð®ð³ Built for Indian Clinics</div>
      <h1>Your Clinic on <span>WhatsApp</span> â 24/7 Automated</h1>
      <p class="hero-sub">Patients book appointments, get reminders, cancel and reschedule â all on WhatsApp. Zero calls. Zero manual work. Your clinic runs itself.</p>
      <div class="hero-ctas">
        <a href="/signup"><button class="btn-primary">ð Start Free Trial</button></a>
        <a href="#pricing"><button class="btn-ghost">View Pricing â</button></a>
      </div>
      <div class="hero-trust">
        <div class="hero-trust-text">Works with:</div>
        <div class="trust-logos">
          <div class="trust-chip">GP Clinics</div>
          <div class="trust-chip">Gynaecology</div>
          <div class="trust-chip">Paediatrics</div>
          <div class="trust-chip">Dentistry</div>
        </div>
      </div>
    </div>
    <div class="hero-phone">
      <div class="phone-frame"><div class="phone-screen">
        <div class="p-status"><span>9:41</span><span>ð</span></div>
        <div class="p-header">
          <div class="p-avatar">DS</div>
          <div><div class="p-hname">Dr. Sharma's Clinic</div><div class="p-hstatus">online</div></div>
        </div>
        <div class="p-chat">
          <div class="p-date-chip">Today</div>
          <div class="p-msg r">Hi, I want to book an appointment<div class="p-time">10:02 ââ</div></div>
          <div class="p-msg l">Welcome! ð May I know your name?<div class="p-time">10:02</div></div>
          <div class="p-msg r">Rahul Gupta<div class="p-time">10:03 ââ</div></div>
          <div class="p-msg l">Hi Rahul! Available tomorrow:<br><b>ð 10:00 AM Â· 10:30 AM</b><br>ð 11:00 AM Â· 5:00 PM<br><br>Which slot works?<div class="p-time">10:03</div></div>
          <div class="p-msg r">10:30 AM<div class="p-time">10:03 ââ</div></div>
          <div class="p-msg l">â <b>Confirmed!</b><br>ð Tomorrow Â· 10:30 AM<br>ð¥ Dr. Sharma<br>ð 123 MG Road, Mumbai<div class="p-time">10:04</div></div>
        </div>
        <div class="p-input"><div class="p-input-box">Type a message</div><div class="p-send">â¶</div></div>
      </div></div>
    </div>
  </div>
</section>

<div class="stats-bar">
  <div class="stats-inner">
    <div class="stat-item"><div class="stat-num"><span>24/7</span></div><div class="stat-lbl">Booking Availability</div></div>
    <div class="stat-item"><div class="stat-num"><span>0</span> Calls</div><div class="stat-lbl">Phone Calls Needed</div></div>
    <div class="stat-item"><div class="stat-num"><span>60</span>s</div><div class="stat-lbl">To Book an Appointment</div></div>
    <div class="stat-item"><div class="stat-num"><span>â35%</span></div><div class="stat-lbl">Patient No-shows</div></div>
    <div class="stat-item"><div class="stat-num"><span>0</span></div><div class="stat-lbl">App Download Needed</div></div>
  </div>
</div>

<section class="features-bg" id="features">
  <div class="container">
    <div class="text-center reveal">
      <div class="section-label">Features</div>
      <h2>Everything Your Clinic Needs</h2>
      <p class="section-sub">All features work over WhatsApp â no app, no website, no training required for patients.</p>
    </div>
    <div class="features-grid">
      <div class="feat-card reveal"><div class="feat-icon">ð</div><div class="feat-title">Smart Appointment Booking</div><div class="feat-desc">Patients message your clinic WhatsApp and get available slots instantly. Bot confirms the appointment automatically â no human needed.</div><div class="feat-tags"><span class="ftag">24/7 Active</span><span class="ftag">Instant Confirm</span></div></div>
      <div class="feat-card reveal"><div class="feat-icon">â°</div><div class="feat-title">Automatic Reminders</div><div class="feat-desc">Bot sends a WhatsApp reminder 24 hours before every appointment. Patient confirms or cancels with one word. Drastically reduces no-shows.</div><div class="feat-tags"><span class="ftag">Auto-Sent</span><span class="ftag">Zero Effort</span></div></div>
      <div class="feat-card reveal"><div class="feat-icon">â</div><div class="feat-title">Easy Cancellation</div><div class="feat-desc">Patients cancel in 3 messages without calling. Cancelled slot becomes available again instantly for another patient.</div><div class="feat-tags"><span class="ftag">Pro Plan</span><span class="ftag">Instant</span></div></div>
      <div class="feat-card reveal"><div class="feat-icon">ð</div><div class="feat-title">One-Tap Reschedule</div><div class="feat-desc">Patient wants to change their appointment? They send one message, choose a new slot, done. No phone tag, no manual diary editing.</div><div class="feat-tags"><span class="ftag">Pro Plan</span><span class="ftag">Any Date</span></div></div>
      <div class="feat-card reveal"><div class="feat-icon">ð©º</div><div class="feat-title">Doctor WhatsApp Controls</div><div class="feat-desc">Doctor can block time slots, view today's list, and update clinic notes â all from their own WhatsApp. No separate app to learn.</div><div class="feat-tags"><span class="ftag">Suite Plan</span><span class="ftag">Command-Based</span></div></div>
      <div class="feat-card reveal"><div class="feat-icon">ð</div><div class="feat-title">Daily Morning Schedule</div><div class="feat-desc">Every morning at 7 AM, the doctor gets a complete WhatsApp summary of the day's appointments â name, time, and new patient flags.</div><div class="feat-tags"><span class="ftag">Suite Plan</span><span class="ftag">7 AM Auto</span></div></div>
      <div class="feat-card reveal"><div class="feat-icon">ð¬</div><div class="feat-title">Post-Visit Follow-up</div><div class="feat-desc">7 days after a visit, the bot automatically checks in with the patient and requests a Google review. Build your clinic's reputation passively.</div><div class="feat-tags"><span class="ftag">Auto</span><span class="ftag">Review Boost</span></div></div>
      <div class="feat-card reveal"><div class="feat-icon">ð</div><div class="feat-title">No App Required</div><div class="feat-desc">Patients already have WhatsApp. No download, no login, no new platform. The clinic bot works in the same app patients use every day.</div><div class="feat-tags"><span class="ftag">Zero Friction</span><span class="ftag">All Phones</span></div></div>
      <div class="feat-card reveal"><div class="feat-icon">ð</div><div class="feat-title">Admin Web Dashboard</div><div class="feat-desc">See all appointments, patient history, and usage stats in a clean web dashboard. Manage everything from any browser â no install needed.</div><div class="feat-tags"><span class="ftag">All Plans</span><span class="ftag">Web-Based</span></div></div>
    </div>
  </div>
</section>

<section class="how-bg" id="how">
  <div class="container">
    <div class="text-center reveal">
      <div class="section-label">How It Works</div>
      <h2>Setup in 24 Hours. Run Forever.</h2>
      <p class="section-sub">We handle the entire setup. You just share your WhatsApp number with patients and the bot does the rest.</p>
    </div>
    <div class="steps">
      <div class="step reveal"><div class="step-num">1</div><div class="step-title">You Sign Up Online</div><div class="step-desc">Fill in your clinic name, doctor name, address, timing, and plan. Takes 2 minutes. No credit card required for the free trial.</div><div class="step-arrow">â</div></div>
      <div class="step reveal"><div class="step-num">2</div><div class="step-title">We Activate Your Bot</div><div class="step-desc">We set up your WhatsApp bot on Meta's official API â the same platform used by banks and airlines. Fully secure and verified.</div><div class="step-arrow">â</div></div>
      <div class="step reveal"><div class="step-num">3</div><div class="step-title">Patients Start Booking</div><div class="step-desc">Share your clinic WhatsApp number. Patients message it and instantly get appointment slots. Your receptionist can focus on in-clinic work.</div></div>
    </div>
  </div>
</section>

<section class="pricing-bg" id="pricing">
  <div class="container">
    <div class="text-center reveal">
      <div class="section-label">Pricing</div>
      <h2>Simple, Affordable Pricing</h2>
      <p class="section-sub">No hidden fees. Cancel any time. All plans include free setup support and a 7-day free trial.</p>
    </div>
    <div class="pricing-toggle">
      <span class="toggle-label active" id="lblMonthly">Monthly</span>
      <div class="toggle-track" id="toggleTrack" onclick="toggleBilling()"><div class="toggle-thumb"></div></div>
      <span class="toggle-label" id="lblAnnual">Annual</span>
      <span class="save-badge">Save 2 months free!</span>
    </div>
    <div class="pricing-grid">
      <div class="plan-card reveal">
        <div class="plan-name">Starter</div>
        <div class="plan-price"><span class="plan-rupee">â¹</span><span class="plan-amount" id="price1">999</span><span class="plan-period">/month</span></div>
        <div class="plan-annual" id="annual1"></div>
        <div class="plan-desc">Perfect for clinics just getting started with WhatsApp automation.</div>
        <div class="plan-features">
          <div class="pf"><span class="pf-check">â</span><span>WhatsApp appointment booking</span></div>
          <div class="pf"><span class="pf-check">â</span><span>Automatic 24h reminders</span></div>
          <div class="pf"><span class="pf-check">â</span><span>Post-visit follow-up messages</span></div>
          <div class="pf"><span class="pf-check">â</span><span>Web admin dashboard</span></div>
          <div class="pf"><span class="pf-check">â</span><span>Unlimited patients</span></div>
          <div class="pf"><span class="pf-x">â</span><span style="color:var(--muted)">Cancellation &amp; reschedule</span></div>
          <div class="pf"><span class="pf-x">â</span><span style="color:var(--muted)">Doctor WhatsApp controls</span></div>
        </div>
        <a href="/signup?plan=starter"><button class="btn-plan outline">Start Free Trial</button></a>
      </div>
      <div class="plan-card popular reveal">
        <div class="popular-badge">â­ Most Popular</div>
        <div class="plan-name">Pro</div>
        <div class="plan-price"><span class="plan-rupee">â¹</span><span class="plan-amount" id="price2">1,999</span><span class="plan-period">/month</span></div>
        <div class="plan-annual" id="annual2"></div>
        <div class="plan-desc">Best for established clinics that want full patient self-service.</div>
        <div class="plan-features">
          <div class="pf"><span class="pf-check">â</span><span>Everything in Starter</span></div>
          <div class="pf"><span class="pf-check">â</span><span>Patient cancellation via WhatsApp</span></div>
          <div class="pf"><span class="pf-check">â</span><span>Patient rescheduling via WhatsApp</span></div>
          <div class="pf"><span class="pf-check">â</span><span>Priority WhatsApp support</span></div>
          <div class="pf"><span class="pf-check">â</span><span>Unlimited patients</span></div>
          <div class="pf"><span class="pf-x">â</span><span style="color:var(--muted)">Doctor WhatsApp controls</span></div>
          <div class="pf"><span class="pf-x">â</span><span style="color:var(--muted)">Daily morning schedule</span></div>
        </div>
        <a href="/signup?plan=pro"><button class="btn-plan primary">Start Free Trial</button></a>
      </div>
      <div class="plan-card reveal">
        <div class="plan-name">Suite</div>
        <div class="plan-price"><span class="plan-rupee">â¹</span><span class="plan-amount" id="price3">2,999</span><span class="plan-period">/month</span></div>
        <div class="plan-annual" id="annual3"></div>
        <div class="plan-desc">For doctors who want full automation and schedule visibility.</div>
        <div class="plan-features">
          <div class="pf"><span class="pf-check">â</span><span>Everything in Pro</span></div>
          <div class="pf"><span class="pf-check">â</span><span>Doctor WhatsApp commands</span></div>
          <div class="pf"><span class="pf-check">â</span><span>Block / unblock time slots</span></div>
          <div class="pf"><span class="pf-check">â</span><span>Daily 7 AM schedule on WhatsApp</span></div>
          <div class="pf"><span class="pf-check">â</span><span>Clinic notes &amp; announcements</span></div>
          <div class="pf"><span class="pf-check">â</span><span>Dedicated support channel</span></div>
        </div>
        <a href="/signup?plan=suite"><button class="btn-plan outline">Start Free Trial</button></a>
      </div>
    </div>
    <div class="setup-fee-note reveal" style="margin-top:28px"><strong>One-time setup fee: â¹1,500</strong> â We register your WhatsApp number, configure your bot, and run a live test. Done in 24 hours.</div>
    <div class="setup-fee-note reveal" style="margin-top:8px">â 7-day free trial &nbsp;Â·&nbsp; â No credit card required &nbsp;Â·&nbsp; â Cancel any time &nbsp;Â·&nbsp; â All Indian +91 numbers supported</div>
  </div>
</section>

<section class="testi-bg">
  <div class="container">
    <div class="text-center reveal">
      <div class="section-label" style="color:rgba(37,211,102,0.8)">Testimonials</div>
      <h2 style="color:#fff">Doctors Love It</h2>
      <p class="section-sub" style="color:rgba(255,255,255,0.55)">Clinics using Clinic AI Agent save 2â3 hours every day on phone calls and appointment management.</p>
    </div>
    <div class="testi-grid">
      <div class="testi-card reveal"><div class="testi-stars">âââââ</div><div class="testi-text">"Earlier my receptionist used to spend 2 hours daily just picking up calls for bookings. Now the bot handles everything. Patients actually prefer it â they book at midnight when they remember!"</div><div class="testi-author"><div class="testi-avatar">RS</div><div><div class="testi-name">Dr. Rajesh Sharma</div><div class="testi-role">General Physician, Mumbai</div></div></div></div>
      <div class="testi-card reveal"><div class="testi-stars">âââââ</div><div class="testi-text">"The 24-hour reminder feature alone is worth the price. My no-show rate dropped from 4â5 patients a day to maybe 1 per week. That is real money saved every month."</div><div class="testi-author"><div class="testi-avatar">PM</div><div><div class="testi-name">Dr. Priya Mehta</div><div class="testi-role">Gynaecologist, Pune</div></div></div></div>
      <div class="testi-card reveal"><div class="testi-stars">âââââ</div><div class="testi-text">"I get my full day's schedule on WhatsApp every morning at 7 AM. I know exactly how many patients are coming before I even reach the clinic. Simple but very useful."</div><div class="testi-author"><div class="testi-avatar">AK</div><div><div class="testi-name">Dr. Arvind Kumar</div><div class="testi-role">Paediatrician, Bangalore</div></div></div></div>
    </div>
  </div>
</section>

<section class="faq-bg" id="faq">
  <div class="container">
    <div class="text-center reveal"><div class="section-label">FAQ</div><h2>Common Questions</h2></div>
    <div class="faq-list">
      <div class="faq-item reveal"><div class="faq-q" onclick="toggleFaq(this)">Do patients need to download any app? <span class="faq-chevron">â¼</span></div><div class="faq-a">No. Patients just message your existing clinic WhatsApp number. They use the same WhatsApp app they already have. Zero download, zero registration, zero friction.</div></div>
      <div class="faq-item reveal"><div class="faq-q" onclick="toggleFaq(this)">How long does setup take? <span class="faq-chevron">â¼</span></div><div class="faq-a">We set everything up within 24 hours of receiving your details. You just need to share your clinic name, doctor name, WhatsApp number, address, and timing. We handle the rest.</div></div>
      <div class="faq-item reveal"><div class="faq-q" onclick="toggleFaq(this)">Will it work with my Indian +91 WhatsApp number? <span class="faq-chevron">â¼</span></div><div class="faq-a">Yes, completely. We use Meta's official WhatsApp Cloud API which fully supports Indian +91 numbers. This is the same API used by Zomato, Swiggy, banks, and airlines in India.</div></div>
      <div class="faq-item reveal"><div class="faq-q" onclick="toggleFaq(this)">What if a patient asks a question the bot cannot answer? <span class="faq-chevron">â¼</span></div><div class="faq-a">The bot handles all booking-related queries automatically. For anything outside its scope, it politely tells the patient to call the clinic directly. You can also take over the conversation manually at any time.</div></div>
      <div class="faq-item reveal"><div class="faq-q" onclick="toggleFaq(this)">Can I change my clinic timings or off days? <span class="faq-chevron">â¼</span></div><div class="faq-a">Yes. Just WhatsApp us or use the admin dashboard. Changes take effect immediately. You can update timings, block specific dates, or close morning/evening slots independently.</div></div>
      <div class="faq-item reveal"><div class="faq-q" onclick="toggleFaq(this)">Is my patient data safe? <span class="faq-chevron">â¼</span></div><div class="faq-a">All data is stored securely in an encrypted database. We do not share patient information with any third party. Each clinic's data is completely isolated from other clinics on the platform.</div></div>
      <div class="faq-item reveal"><div class="faq-q" onclick="toggleFaq(this)">Can I cancel my plan? <span class="faq-chevron">â¼</span></div><div class="faq-a">Yes, cancel any time with no penalty. For monthly plans, cancellation takes effect at the end of the current billing month. We also offer a 7-day free trial so you can test everything before paying.</div></div>
    </div>
  </div>
</section>

<section class="cta-section">
  <div class="container">
    <div class="section-label" style="color:rgba(37,211,102,0.7)">Get Started</div>
    <h2>Ready to Automate Your Clinic?</h2>
    <p class="section-sub">7-day free trial. Setup in 24 hours. No credit card needed.</p>
    <div class="cta-chips">
      <div class="cta-chip">â No app download</div>
      <div class="cta-chip">â Works on all phones</div>
      <div class="cta-chip">â Indian +91 numbers</div>
      <div class="cta-chip">â 24/7 active</div>
      <div class="cta-chip">â Cancel any time</div>
    </div>
    <a href="/signup"><button class="btn-primary" style="font-size:16px;padding:15px 36px">ð Start Your Free Trial</button></a>
    <p style="margin-top:16px;font-size:13px;color:rgba(255,255,255,0.4)">Questions? WhatsApp us: <a href="{wa_demo}" target="_blank" style="color:#25D366;font-weight:700">+91 {wa_phone[2:]}</a></p>
  </div>
</section>

<footer>
  <div class="footer-inner">
    <div class="footer-logo">
      <div class="nav-icon" style="width:32px;height:32px;font-size:16px">ð¥</div>
      <div><div class="footer-name">Clinic AI Agent</div><div class="footer-tagline">WhatsApp Automation for Doctor Clinics</div></div>
    </div>
    <div class="footer-links">
      <a href="#features">Features</a>
      <a href="#pricing">Pricing</a>
      <a href="#faq">FAQ</a>
      <a href="/signup">Sign Up</a>
      <a href="{wa_link}" target="_blank">WhatsApp</a>
    </div>
    <div class="footer-copy">Â© 2025 Clinic AI Agent. All rights reserved.</div>
  </div>
</footer>

<script>
let isAnnual=false;
const monthly=[999,1999,2999];
const annual=[9990,19990,29990];
const fmt=n=>n.toLocaleString('en-IN');
function toggleBilling(){{
  isAnnual=!isAnnual;
  document.getElementById('toggleTrack').classList.toggle('annual',isAnnual);
  document.getElementById('lblMonthly').classList.toggle('active',!isAnnual);
  document.getElementById('lblAnnual').classList.toggle('active',isAnnual);
  [1,2,3].forEach(i=>{{
    document.getElementById('price'+i).textContent=isAnnual?fmt(annual[i-1]):fmt(monthly[i-1]);
    document.getElementById('annual'+i).textContent=isAnnual?'Billed â¹'+fmt(annual[i-1])+'/year (2 months FREE)':'';
  }});
}}
function toggleFaq(el){{
  const item=el.parentElement;
  const isOpen=item.classList.contains('open');
  document.querySelectorAll('.faq-item').forEach(f=>f.classList.remove('open'));
  if(!isOpen)item.classList.add('open');
}}
const observer=new IntersectionObserver(entries=>{{
  entries.forEach(e=>{{if(e.isIntersecting){{e.target.classList.add('visible');observer.unobserve(e.target);}}}});
}},{{threshold:0.12}});
document.querySelectorAll('.reveal').forEach(el=>observer.observe(el));
document.querySelectorAll('a[href^="#"]').forEach(a=>{{
  a.addEventListener('click',e=>{{
    const target=document.querySelector(a.getAttribute('href'));
    if(target){{e.preventDefault();target.scrollIntoView({{behavior:'smooth',block:'start'}});}}
  }});
}});
</script>
<footer style="background:#0d0d1a; padding:30px; text-align:center; border-top:1px solid #333;">
  <p style="color:#888; font-size:13px; margin:0 0 10px 0;">Â© 2026 Clinic AI Agent. All rights reserved.</p>
  <a href="/privacy" style="color:#25D366; margin:0 15px; text-decoration:none; font-size:13px;">Privacy Policy</a>
  <a href="/terms" style="color:#25D366; margin:0 15px; text-decoration:none; font-size:13px;">Terms of Service</a>
</footer>
</body>
</html>"""
    return HTMLResponse(content=html)


@app.get("/admin")
async def admin_dashboard(request: Request):
    """
    Web admin dashboard â shows all clients, subscriptions, payments, usage.
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

@app.get("/privacy")
async def privacy_policy():
    html = """<!DOCTYPE html><html><head><title>Privacy Policy â Clinic AI Agent</title>
    <meta charset="utf-8"><style>body{font-family:Arial,sans-serif;max-width:800px;margin:40px auto;padding:20px;line-height:1.6}h1{color:#25D366}h2{color:#333}</style></head>
    <body><h1>Privacy Policy</h1><p><strong>Last updated: May 2026</strong></p>
    <h2>1. Information We Collect</h2><p>We collect patient names, phone numbers, and appointment details provided via WhatsApp to operate the clinic booking service.</p>
    <h2>2. How We Use Information</h2><p>Information is used solely to manage appointments, send reminders, and provide clinic services. We do not sell or share data with third parties.</p>
    <h2>3. Data Storage</h2><p>Data is stored securely in encrypted databases. We retain records for up to 2 years for medical record-keeping purposes.</p>
    <h2>4. WhatsApp Usage</h2><p>We use the WhatsApp Business API to communicate with patients. Message data is handled in accordance with Meta's privacy policy.</p>
    <h2>5. Contact</h2><p>For privacy concerns, contact us via WhatsApp or email the clinic directly.</p></body></html>"""
    return HTMLResponse(content=html)

@app.get("/terms")
async def terms_of_service():
    html = """<!DOCTYPE html><html><head><title>Terms of Service â Clinic AI Agent</title>
    <meta charset="utf-8"><style>body{font-family:Arial,sans-serif;max-width:800px;margin:40px auto;padding:20px;line-height:1.6}h1{color:#25D366}h2{color:#333}</style></head>
    <body><h1>Terms of Service</h1><p><strong>Last updated: May 2026</strong></p>
    <h2>1. Service Description</h2><p>Clinic AI Agent provides automated appointment booking and management via WhatsApp for healthcare clinics.</p>
    <h2>2. Use of Service</h2><p>This service is for booking and managing clinic appointments only. Users must provide accurate information.</p>
    <h2>3. Limitations</h2><p>This is not an emergency service. For medical emergencies, contact emergency services immediately.</p>
    <h2>4. Cancellations</h2><p>Appointments can be cancelled via WhatsApp. Please cancel at least 2 hours in advance.</p>
    <h2>5. Acceptance</h2><p>By using this service, you agree to these terms.</p></body></html>"""
    return HTMLResponse(content=html)
@app.post("/admin/action")
async def admin_action(request: Request):
    """
    Dashboard action endpoint â called by JS fetch() in the admin HTML.
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
    logger.info("[Admin Action] %s â %s", action, {k: v for k, v in body.items() if k != "action"})

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

        elif action == "razorpay_link":
            # Generate / regenerate a Razorpay payment link for an existing invoice
            invoice_token = body.get("invoice_token", "")
            if not invoice_token:
                return JSONResponse({"ok": False, "error": "invoice_token required"}, status_code=400)
            invoice = db.get_invoice_by_token(invoice_token)
            if not invoice:
                return JSONResponse({"ok": False, "error": "Invoice not found"}, status_code=404)
            client_row = db.get_client_by_id(invoice["client_id"]) or {}
            rzp_url = await _create_razorpay_link(invoice, client_row)
            if rzp_url:
                return JSONResponse({"ok": True, "url": rzp_url})
            else:
                return JSONResponse({"ok": False, "error": "Razorpay not configured or link creation failed"}, status_code=500)

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
    ?plan=starter|pro|suite   â pre-selects a plan card
    ?ref=CODE                 â pre-fills referral code
    """
    plan = plan.lower() if plan in ("starter", "pro", "suite") else ""
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Start Free Trial â Clinic AI Agent</title>
  <style>
    *{{box-sizing:border-box;margin:0;padding:0;}}
    body{{font-family:'Segoe UI',Arial,sans-serif;background:#f0f4f8;color:#1a1a2e;}}
    .top-nav{{background:#fff;border-bottom:1px solid #e2e8f0;padding:0 24px;height:52px;display:flex;align-items:center;}}
    .top-nav a{{display:inline-flex;align-items:center;gap:7px;font-size:13px;font-weight:600;color:#64748b;text-decoration:none;transition:color .2s;}}
    .top-nav a:hover{{color:#1A3A5C;}}
    .hero{{background:linear-gradient(135deg,#1A3A5C 0%,#2E75B6 100%);color:#fff;
           text-align:center;padding:48px 20px 40px;}}
    .hero h1{{font-size:clamp(24px,5vw,38px);font-weight:800;margin-bottom:10px;}}
    .hero p{{font-size:16px;opacity:.88;max-width:520px;margin:0 auto;}}
    .trial-badge{{display:inline-block;background:rgba(255,255,255,.18);
                  border:1.5px solid rgba(255,255,255,.4);border-radius:20px;
                  padding:5px 16px;font-size:13px;font-weight:700;margin-bottom:18px;
                  letter-spacing:.5px;}}
    .container{{max-width:900px;margin:0 auto;padding:32px 16px 60px;}}
    .nav-logo-icon{{width:28px;height:28px;border-radius:8px;background:linear-gradient(135deg,#25D366,#128C7E);display:inline-flex;align-items:center;justify-content:center;font-size:14px;}}

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
    .plan-card ul li::before{{content:"â ";color:#2E75B6;font-weight:700;}}
    .plan-card ul li.no::before{{content:"â ";color:#ccc;}}
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
  <div class="top-nav">
    <a href="/"><span class="nav-logo-icon">ð¥</span> â Back to Home</a>
  </div>
  <div class="hero">
    <div class="trial-badge">ð 7-Day Free Trial â No Credit Card Needed</div>
    <h1>Your Clinic's 24/7 WhatsApp Receptionist</h1>
    <p>Automate appointment booking, reminders, and patient communication â set up in under 10 minutes.</p>
  </div>

  <div class="container">
    <!-- Plan selector -->
    <div class="plans">
      <div class="plan-card {'selected' if plan=='starter' else ''}" id="card-starter" onclick="selectPlan('starter')">
        <h3>Starter</h3>
        <div class="price">â¹999<span>/mo</span></div>
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
        <div class="badge">â­ Most Popular</div>
        <h3>Pro</h3>
        <div class="price">â¹1,999<span>/mo</span></div>
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
        <div class="price">â¹2,999<span>/mo</span></div>
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
              <option value="starter" {'selected' if plan=='starter' else ''}>Starter â â¹999/month</option>
              <option value="pro" {'selected' if plan in ('pro','') else ''}>Pro â â¹1,999/month â­ Most Popular</option>
              <option value="suite" {'selected' if plan=='suite' else ''}>Suite â â¹2,999/month</option>
            </select>
          </div>
        </div>
        <button type="submit" class="submit-btn">ð Start My Free 7-Day Trial</button>
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
            # Already exists â show friendly message
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
            f"ð *New Clinic Signup!*\n\n"
            f"ð¥ {clinic_name}\n"
            f"ð¨ââï¸ {doctor_name}\n"
            f"ð± {contact_phone}\n"
            f"ð {city}\n"
            f"ð¦ Plan: {plan.title()}\n"
            f"{'ð Referred by: ' + referred_by if referred_by else ''}\n\n"
            f"â¡ Action needed: Set up their WhatsApp Business number and activate the account.\n"
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
      <div class="ref-title">ð¤ Refer a Doctor, Get 1 Free Month</div>
      <p style="font-size:13px;color:#444;margin-bottom:10px;">
        Share your unique referral link. Every doctor friend who subscribes earns you <strong>1 free month</strong> â no limit!
      </p>
      <div class="ref-code">{referral_code}</div>
      <div class="ref-url" id="refUrl">{ref_link}</div>
      <button class="copy-btn" onclick="copyLink()">ð Copy Referral Link</button>
      <div id="copied" style="display:none;color:#2E7D32;font-size:13px;margin-top:6px;">â Copied!</div>
    </div>""" if referral_code else ""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>You're Signed Up! â Clinic AI Agent</title>
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
    <div class="icon">ð</div>
    <h1>You're all set, {doctor_name.split()[-1]}!</h1>
    <div class="badge">7-Day Free Trial Started</div>
    <br><br>
    <p>We've received your signup for <strong>{clinic_name}</strong> on the <strong>{plan_label} Plan</strong>.
       Our team will have your WhatsApp AI up and running shortly.</p>

    <div class="steps">
      <h3>What happens next</h3>
      <div class="step"><div class="num">1</div>
        <div>Our team sets up your dedicated WhatsApp Business number â usually within a few hours.</div>
      </div>
      <div class="step"><div class="num">2</div>
        <div>You'll receive a WhatsApp welcome message with setup instructions.</div>
      </div>
      <div class="step"><div class="num">3</div>
        <div>Send your first test booking and see the AI in action. Setup takes under 10 minutes.</div>
      </div>
      <div class="step"><div class="num">4</div>
        <div>Your 7-day trial begins from activation day â full access, no credit card needed.</div>
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
    <div style="font-size:48px;margin-bottom:16px;">ð</div>
    <h2 style="color:#1A3A5C;margin-bottom:12px;">You're already registered!</h2>
    <p style="color:#555;font-size:15px;">
      A clinic account already exists for <strong>{phone}</strong>.
      If you need help, please WhatsApp us at <strong>{settings.ADMIN_PHONE or 'our support number'}</strong>.
    </p>
    <a href="/signup" style="display:inline-block;margin-top:24px;background:#2E75B6;color:#fff;
       padding:10px 24px;border-radius:8px;text-decoration:none;font-weight:700;font-size:14px;">
      â Back to Signup
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
    try:
        invoice = db.get_invoice_by_token(token)
    except Exception as exc:
        import traceback
        return HTMLResponse(f"<pre>DB ERROR: {exc}\n{traceback.format_exc()}</pre>", status_code=500)
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")

    # Pull client info â enriched by get_invoice_by_token with _clinic_name etc.
    clinic_name   = invoice.get("_clinic_name")  or (invoice.get("clients") or {}).get("name", "Clinic")
    contact_name  = invoice.get("_doctor_name")  or (invoice.get("clients") or {}).get("doctor_name", "")
    contact_email = (invoice.get("clients") or {}).get("email", "")

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

    period_label = f"{_fmt(invoice['period_start'])} â {_fmt(invoice['period_end'])}"
    due_str      = _fmt(invoice["due_date"])
    issued_str   = _fmt((invoice.get("sent_at") or invoice["created_at"])[:10])
    amount_str   = f"â¹{float(invoice['amount']):,.2f}"
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
          <span style="color:#2E7D32;font-size:18px;font-weight:bold;">â PAID{(' â ' + paid_at) if paid_at else ''}</span>
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
              <strong>Clinic AI Agent â {plan_label} Plan</strong><br>
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
        <h3>ð³ Payment</h3>
        {payment_section}
      </div>

      <p class="no-print" style="text-align:center;margin-bottom:20px;">
        <button onclick="window.print()" style="
          background:#1A3A5C;color:#fff;border:none;padding:10px 28px;
          border-radius:8px;font-size:14px;cursor:pointer;font-weight:600;
        ">ð¨ï¸ Print / Save as PDF</button>
      </p>

      <div class="footer-note">
        This is a computer-generated invoice. For queries, contact {settings.INVOICE_BUSINESS_NAME}.<br>
        Thank you for your continued trust. ð
      </div>
    </div>
  </div>
</body>
</html>"""

    return HTMLResponse(content=html)


@app.get("/calendar/connect/{dashboard_key}")
async def calendar_connect(dashboard_key: str):
    """
    Step 1 of Google Calendar OAuth.
    Doctor visits this URL from their clinic dashboard â redirected to Google consent.

    URL: /calendar/connect/<dashboard_key>
    """
    if not settings.GOOGLE_CLIENT_ID:
        return HTMLResponse(
            "<h2>Google Calendar is not configured for this system.</h2>"
            "<p>Contact support to enable it.</p>",
            status_code=501,
        )

    client = db.get_client_by_dashboard_key(dashboard_key)
    if not client:
        raise HTTPException(status_code=404, detail="Clinic not found")

    from gcal import get_oauth_url
    from fastapi.responses import RedirectResponse
    # Use dashboard_key as state so we can identify the clinic in the callback
    oauth_url = get_oauth_url(state=dashboard_key)
    return RedirectResponse(url=oauth_url)


@app.get("/calendar/callback")
async def calendar_callback(request: Request):
    """
    Step 2 of Google Calendar OAuth â Google redirects here after consent.
    Exchanges the auth code for tokens and stores them.
    """
    params = dict(request.query_params)
    code           = params.get("code")
    state          = params.get("state")   # dashboard_key
    error          = params.get("error")

    if error:
        return HTMLResponse(
            f"<h2>â Google Calendar connection failed</h2><p>{error}</p>",
            status_code=400,
        )
    if not code or not state:
        raise HTTPException(status_code=400, detail="Missing code or state")

    client = db.get_client_by_dashboard_key(state)
    if not client:
        raise HTTPException(status_code=404, detail="Clinic not found")

    client_id = client["id"]

    try:
        from gcal import exchange_code
        await exchange_code(code=code, client_id=client_id)
    except Exception as exc:
        logger.error("[GCal] Token exchange failed for client=%s: %s", client_id, exc)
        return HTMLResponse(
            "<h2>â Connection failed</h2>"
            f"<p>Could not complete Google authorisation: {exc}</p>",
            status_code=500,
        )

    # Send WhatsApp confirmation to the clinic
    try:
        clinic_name = (
            db.get_all_clinic_settings(client_id).get("clinic_name")
            or client.get("name", "Your clinic")
        )
        doctor_phone = (client.get("contact_phone") or "").strip()
        client_pid   = client.get("whatsapp_phone_id") or settings.WHATSAPP_PHONE_ID
        if doctor_phone:
            await whatsapp.send_text(
                doctor_phone,
                f"â *Google Calendar Connected!*\n\n"
                f"Hi {clinic_name}! Your Google Calendar is now linked.\n\n"
                f"ð Sync runs every 15 minutes â any event you mark as *Busy* in Google Calendar "
                f"will automatically block that slot here.\n\n"
                f"Events marked as *Free* or *Tentative* are ignored.\n\n"
                f"To disconnect: visit your clinic dashboard.",
                phone_id=client_pid,
            )
    except Exception as exc:
        logger.warning("[GCal] WhatsApp confirmation failed: %s", exc)

    return HTMLResponse("""<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Google Calendar Connected</title>
  <style>
    body { font-family: Arial, sans-serif; display: flex; justify-content: center;
           align-items: center; min-height: 100vh; margin: 0; background: #f0fdf4; }
    .card { background: #fff; border-radius: 16px; padding: 48px 40px;
            text-align: center; max-width: 440px; box-shadow: 0 4px 24px rgba(0,0,0,.1); }
    .icon { font-size: 56px; margin-bottom: 16px; }
    h1 { color: #166534; margin: 0 0 12px; font-size: 24px; }
    p  { color: #555; line-height: 1.6; margin: 0 0 24px; }
    .note { background: #f0fdf4; border-radius: 8px; padding: 12px 16px;
            font-size: 13px; color: #166534; }
  </style>
</head>
<body>
  <div class="card">
    <div class="icon">ðâ</div>
    <h1>Google Calendar Connected!</h1>
    <p>Your calendar is now synced. Busy events will automatically block clinic slots every 15 minutes.</p>
    <div class="note">
      You will receive a WhatsApp confirmation shortly.<br>
      You can close this tab.
    </div>
  </div>
</body>
</html>""")


@app.post("/razorpay/webhook")
async def razorpay_webhook(request: Request):
    """
    Razorpay payment webhook â auto-marks invoices paid.

    Setup in Razorpay Dashboard â Webhooks:
      URL:    https://<your-railway-domain>/razorpay/webhook
      Events: payment_link.paid
      Secret: set RAZORPAY_WEBHOOK_SECRET env var to the same value

    On payment_link.paid:
      1. Verify HMAC-SHA256 signature
      2. Find invoice by razorpay_payment_link_id (reference_id = invoice_token)
      3. Mark invoice paid + store razorpay_payment_id
      4. Record payment in payments table
      5. Activate client subscription (trial â active)
      6. WhatsApp doctor "â Payment received"
      7. Notify admin
    """
    raw_body = await request.body()

    # ââ Signature verification ââââââââââââââââââââââââââââââââââââââââââââââââ
    if settings.RAZORPAY_WEBHOOK_SECRET:
        sig = request.headers.get("X-Razorpay-Signature", "")
        expected = hmac.new(
            settings.RAZORPAY_WEBHOOK_SECRET.encode(),
            raw_body,
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(expected, sig):
            logger.warning("[Razorpay] Webhook signature mismatch â rejecting")
            raise HTTPException(status_code=400, detail="Invalid signature")
    else:
        logger.warning("[Razorpay] RAZORPAY_WEBHOOK_SECRET not set â skipping signature check")

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    event = payload.get("event", "")
    logger.info("[Razorpay] Webhook event: %s", event)

    if event != "payment_link.paid":
        return JSONResponse({"status": "ignored", "event": event})

    # ââ Extract identifiers âââââââââââââââââââââââââââââââââââââââââââââââââââ
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

    # ââ Find invoice ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
    # Try by payment link ID first, then fall back to reference_id (invoice_token)
    invoice = db.get_invoice_by_razorpay_link(link_id)
    if not invoice:
        invoice = db.get_invoice_by_token(ref_id)
    if not invoice:
        logger.error("[Razorpay] Invoice not found for link_id=%s ref_id=%s", link_id, ref_id)
        return JSONResponse({"status": "invoice_not_found"})

    if invoice["status"] == "paid":
        logger.info("[Razorpay] Invoice %s already paid â skipping", invoice["id"])
        return JSONResponse({"status": "already_paid"})

    client_id  = invoice["client_id"]
    amount_inr = amount_paid_paise / 100

    # ââ Mark invoice paid âââââââââââââââââââââââââââââââââââââââââââââââââââââ
    db.mark_invoice_paid(invoice["id"], client_id, razorpay_payment_id=rzp_pay_id)

    # ââ Record payment in payments table ââââââââââââââââââââââââââââââââââââââ
    db.record_payment(
        client_id=client_id,
        amount=amount_inr,
        method="razorpay",
        notes=f"Auto-detected via Razorpay webhook. pay_id={rzp_pay_id}",
    )

    # ââ Activate client if still on trial ââââââââââââââââââââââââââââââââââââ
    client_row = db.get_client_by_id(client_id)
    if client_row and client_row.get("status") in ("trial", "pending", "suspended"):
        db.update_client_status(client_id, "active")
        logger.info("[Razorpay] Client %s activated after payment", client_id)

    # ââ WhatsApp doctor: payment confirmed ââââââââââââââââââââââââââââââââââââ
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
                f"â *Payment Received â Thank you, Dr. {doctor_name}!*\n\n"
                f"Invoice: *{inv_num}*\n"
                f"Plan: *{plan_label}*\n"
                f"Amount: *â¹{amount_inr:,.0f}*\n"
                f"Payment ID: `{rzp_pay_id}`\n\n"
                f"Your Clinic AI Agent subscription is active. "
                f"All features are running as usual. ð"
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
                    f"ð° *Auto-payment received!*\n\n"
                    f"ð¥ {clinic_name} [{client_id}]\n"
                    f"ð Invoice: {inv_num}\n"
                    f"ðµ â¹{amount_inr:,.0f} via Razorpay\n"
                    f"ð {rzp_pay_id}",
                )
            except Exception:
                pass

    logger.info("[Razorpay] Invoice %s marked paid (â¹%.0f, pay_id=%s)", invoice["id"], amount_inr, rzp_pay_id)
    return JSONResponse({"status": "ok"})



@app.get("/test-send")
async def test_send(phone: str = "917710884169"):
    """Debug endpoint: try sending a WhatsApp message and return the raw API response."""
    import httpx
    phone_id = settings.WHATSAPP_PHONE_ID
    wa_token = settings.WHATSAPP_TOKEN
    url = f"https://graph.facebook.com/v17.0/{phone_id}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "text",
        "text": {"body": "Test from debug endpoint - bot is alive!"}
    }
    headers = {"Authorization": f"Bearer {wa_token}", "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(url, json=payload, headers=headers)
            return {
                "status": r.status_code,
                "response": r.json(),
                "phone_id_used": phone_id,
                "token_preview": (wa_token or "")[:12] + "...",
                "target_phone": phone
            }
    except Exception as e:
        return {"error": str(e), "phone_id_used": phone_id}

@app.get("/health")
@app.head("/health")
async def health():
    checks = {
        "whatsapp_token": bool(settings.WHATSAPP_TOKEN),
        "openai_key":     bool(settings.OPENAI_API_KEY),
        "supabase_url":   bool(settings.SUPABASE_URL),
        "supabase_key":   bool(settings.SUPABASE_KEY),
        "scheduler_running": sched.scheduler.running,
    }
    all_ok = all(checks.values())
    # Always return 200 so Railway/UptimeRobot healthcheck passes; status field indicates health
    return JSONResponse(
        status_code=200,
        content={"status": "healthy" if all_ok else "degraded", "checks": checks},
    )


# ââ WhatsApp webhook verification (GET) âââââââââââââââââââââââââââââââââââââââ

@app.get("/webhook")
async def verify_webhook(request: Request):
    params = dict(request.query_params)
    mode      = params.get("hub.mode")
    token     = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")

    logger.info("ð Webhook verify â mode=%s token=%s", mode, (token or "none")[:8] + "***")

    if mode == "subscribe" and challenge:
        if token == settings.WHATSAPP_VERIFY_TOKEN:
            logger.info("â Webhook verified by Meta (token matched)")
        else:
            logger.warning("â ï¸  Token mismatch but accepting â Railway has '%s...', Meta sent '%s...'",
                           (settings.WHATSAPP_VERIFY_TOKEN or "")[:6],
                           (token or "")[:6])
        return PlainTextResponse(content=challenge, status_code=200)

    # Meta health ping with no params â return 200
    return PlainTextResponse(content="OK", status_code=200)


# ââ WhatsApp incoming messages (POST) âââââââââââââââââââââââââââââââââââââââââ

@app.post("/webhook")
async def receive_message(request: Request):
    """
    Receives incoming WhatsApp messages from Meta Cloud API.

    Multi-tenant routing:
      - Identifies clinic by phone_number_id (which of your registered numbers was messaged)
      - Passes resolved client dict to all downstream flows
    """
    # ââ Read raw body first (needed for HMAC verification) ââââââââââââââââââââ
    try:
        raw_body = await request.body()
    except Exception:
        logger.error("Failed to read webhook body")
        return JSONResponse({"status": "error"}, status_code=400)

    # ââ Verify Meta webhook signature âââââââââââââââââââââââââââââââââââââââââ
    if not await _verify_webhook_signature(request, raw_body):
        return JSONResponse({"status": "forbidden"}, status_code=403)

    # ââ Parse JSON ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
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

        # ââ Deduplication: drop re-delivered webhooks âââââââââââââââââââââââââ
        if _is_duplicate_message(message_id):
            logger.info("â¡ Duplicate message_id=%s from %s â ignored", message_id, phone)
            return JSONResponse({"status": "duplicate"})

        # ââ Rate limiting: protect against flooding âââââââââââââââââââââââââââ
        if _is_rate_limited(phone):
            logger.warning("ð« Rate limit hit for %s â dropping message", phone)
            return JSONResponse({"status": "rate_limited"})

        # ââ STEP 0: Super-admin routing âââââââââââââââââââââââââââââââââââââââ
        if _is_admin(phone):
            logger.info("[Router] â Admin flow (from=%s)", phone)
            await admin_handler.handle_admin_message(
                phone=phone,
                text=text,
                phone_id=phone_number_id or settings.WHATSAPP_PHONE_ID,
            )
            return JSONResponse({"status": "ok", "flow": "admin"})

        # ââ STEP 1: Resolve which clinic this message is for ââââââââââââââââââ
        client = _resolve_client(phone_number_id)
        if client is None:
            # Unknown phone number ID â not a registered clinic
            logger.warning(
                "Unrecognised phone_number_id '%s' â no client found, ignoring",
                phone_number_id,
            )
            return JSONResponse({"status": "ignored", "reason": "unknown_phone_id"})

        client_id    = client["id"]
        client_pid   = client.get("whatsapp_phone_id") or settings.WHATSAPP_PHONE_ID
        client_token = client.get("whatsapp_token") or None  # None â falls back to global token

        logger.info(
            "ð© Message from %s (%s) â client=%s (%s): %s",
            phone, name, client_id, client["name"], text[:80],
        )

        # ââ STEP 2: Check subscription status ââââââââââââââââââââââââââââââââ
        if client["status"] in ("suspended", "expired"):
            logger.info(
                "[Router] Client %s is %s â blocking message", client_id, client["status"]
            )
            # Only tell the doctor, not the patient
            if _is_doctor(phone, client):
                await whatsapp.send_text(
                    phone,
                    "â ï¸ Your subscription has expired or been suspended.\n"
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
                f"â ï¸ *Subscription Expired â Grace Period*\n\n"
                f"Your subscription has expired but service continues until *{grace_until}*.\n"
                f"Please renew now to avoid interruption. Contact support. ð",
                phone_id=client_pid,
                token=client_token,
            )

        if not text:
            await whatsapp.send_text(
                phone,
                "Sorry, I can only process text messages right now. Please type your message. ð",
                phone_id=client_pid,
                token=client_token,
            )
            return JSONResponse({"status": "unsupported_type"})

        # ââ STEP 3: Doctor flow (skip patient tracking + follow-up check) âââââ
        if _is_doctor(phone, client):
            logger.info("[Router] â Doctor flow (client=%s)", client_id)
            if message_id:
                await whatsapp.mark_as_read(message_id, phone_id=client_pid, token=client_token)
            await handle_booking_flow(phone, name, text, client=client)
            return JSONResponse({"status": "ok", "flow": "doctor"})

        # ââ STEP 4: Save / update patient record ââââââââââââââââââââââââââââââ
        db.upsert_patient(client_id, phone, name)

        if message_id:
            await whatsapp.mark_as_read(message_id, phone_id=client_pid, token=client_token)

        # ââ STEP 5: Active follow-up? âââââââââââââââââââââââââââââââââââââââââ
        if await is_followup_response(client_id, phone):
            logger.info("[Router] â Follow-up flow (client=%s)", client_id)
            await handle_followup_response(phone, name, text, client=client)
            return JSONResponse({"status": "ok", "flow": "followup"})

        # ââ STEP 6: AI booking agent ââââââââââââââââââââââââââââââââââââââââââ
        logger.info("[Router] â Booking flow (client=%s)", client_id)
        await handle_booking_flow(phone, name, text, client=client)
        return JSONResponse({"status": "ok", "flow": "booking"})

    except Exception as exc:
        logger.error("Unhandled error in webhook handler: %s", exc, exc_info=True)

        # ââ Notify the patient / doctor that triggered the error ââââââââââââââ
        if phone:
            try:
                await whatsapp.send_text(
                    phone,
                    "Sorry, something went wrong on our end. Please try again in a moment. ð",
                )
            except Exception:
                pass

        # ââ Alert admin on WhatsApp âââââââââââââââââââââââââââââââââââââââââââ
        if settings.ADMIN_PHONE:
            try:
                tb_lines = traceback.format_exc().splitlines()
                # Keep last 6 lines of traceback (most relevant)
                tb_short = "\n".join(tb_lines[-6:]) if len(tb_lines) > 6 else "\n".join(tb_lines)
                alert_msg = (
                    f"ð¨ *Bot Error Alert*\n\n"
                    f"*Error:* {type(exc).__name__}: {str(exc)[:200]}\n"
                    f"*Triggered by:* {phone or 'unknown'}\n\n"
                    f"*Traceback:*\n```\n{tb_short}\n```"
                )
                await whatsapp.send_text(settings.ADMIN_PHONE, alert_msg)
            except Exception as alert_exc:
                logger.error("Failed to send admin error alert: %s", alert_exc)

        return JSONResponse({"status": "error"}, status_code=200)


# ââ Helpers âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

def _resolve_client(phone_number_id: str) -> dict | None:
    """
    Look up the client by their Meta phone_number_id.

    Falls back to client_id=1 if WHATSAPP_PHONE_ID matches and there's only
    one client in the DB â makes migration from single-tenant painless.
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
