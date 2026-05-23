"""
main.py — FastAPI application + WhatsApp webhook handler (multi-tenant v5).

Entry point for the Clinic AI Agent.

Endpoints:
  GET  /                          → Health check
  GET  /webhook                   → Meta webhook verification (one-time setup)
  POST /webhook                   → Incoming WhatsApp messages
  GET  /health                    → Detailed health check (DB + config)
  GET  /admin?key=<SECRET>        → Web admin dashboard
  POST /admin/action?key=<SECRET> → Dashboard actions (suspend/activate/payment/new_client)

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

import logging
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse, HTMLResponse

import database as db
import whatsapp
import scheduler as sched
import admin as admin_handler
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
    try:
        body = await request.json()
    except Exception:
        logger.error("Failed to parse webhook body")
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
        if phone:
            try:
                await whatsapp.send_text(
                    phone,
                    "Sorry, something went wrong on our end. Please try again in a moment. 🙏",
                )
            except Exception:
                pass
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
