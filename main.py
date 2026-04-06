"""
main.py — FastAPI application + WhatsApp webhook handler.

Entry point for the Clinic AI Agent.

Endpoints:
  GET  /          → Health check
  GET  /webhook   → Meta webhook verification (one-time setup)
  POST /webhook   → Incoming WhatsApp messages
  GET  /health    → Detailed health check (DB + config)

Routing priority:
  1. Active follow-up waiting for patient response → flows/followup.py
  2. Everything else → flows/booking.py (AI agent)
"""

from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse

import database as db
import whatsapp
import scheduler as sched
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
    """Startup: validate config and start scheduler. Shutdown: stop scheduler."""
    logger.info("🏥 Clinic AI Agent starting up…")
    try:
        settings.validate()
        logger.info("✅ Config validated")
    except EnvironmentError as exc:
        logger.error("❌ Configuration error: %s", exc)
        # Don't crash — let Railway show the error in logs

    sched.start()
    logger.info("✅ Scheduler started")
    logger.info("🚀 Clinic AI Agent ready!")

    yield  # App is running

    sched.stop()
    logger.info("👋 Clinic AI Agent shutting down")


app = FastAPI(
    title="Clinic AI Agent",
    description="WhatsApp appointment booking agent for Indian clinics",
    version="1.0.0",
    lifespan=lifespan,
)


# ── Health check ──────────────────────────────────────────────────────────────

@app.get("/")
async def root():
    return {"status": "ok", "service": "Clinic AI Agent", "version": "1.0.0"}


@app.get("/health")
async def health():
    checks = {
        "whatsapp_token": bool(settings.WHATSAPP_TOKEN),
        "openai_key": bool(settings.OPENAI_API_KEY),
        "supabase_url": bool(settings.SUPABASE_URL),
        "supabase_key": bool(settings.SUPABASE_KEY),
        "scheduler_running": sched.scheduler.running,
    }
    all_ok = all(checks.values())
    return JSONResponse(
        status_code=200 if all_ok else 503,
        content={"status": "healthy" if all_ok else "degraded", "checks": checks},
    )


# ── WhatsApp webhook verification (GET) ───────────────────────────────────────

@app.get("/webhook")
async def verify_webhook(request: Request):
    """
    Meta sends a GET request to verify the webhook URL.
    We must respond with the hub.challenge value if the verify token matches.
    """
    params = dict(request.query_params)
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
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

    Priority router:
      1. If patient has an active follow-up awaiting response → followup flow
      2. Everything else → AI booking agent flow
    """
    try:
        body = await request.json()
    except Exception:
        logger.error("Failed to parse webhook body")
        return JSONResponse({"status": "error"}, status_code=400)

    # Always return 200 quickly — Meta retries if we don't
    # (process asynchronously)
    try:
        msg = whatsapp.parse_incoming_message(body)
        if not msg:
            # Status update (delivered, read) — ignore
            return JSONResponse({"status": "ignored"})

        phone = msg["phone"]
        name = msg["name"]
        text = msg["text"]
        message_id = msg.get("message_id", "")

        if not text:
            # Unsupported message type (image, audio, etc.)
            await whatsapp.send_text(
                phone,
                "Sorry, I can only process text messages right now. "
                "Please type your message. 😊",
            )
            return JSONResponse({"status": "unsupported_type"})

        logger.info("📩 Message from %s (%s): %s", phone, name, text[:80])

        # Save / update patient record
        db.upsert_patient(phone, name)

        # Send read receipt
        if message_id:
            await whatsapp.mark_as_read(message_id)

        # ── Priority 1: Active follow-up waiting for response? ──────────────
        if await is_followup_response(phone):
            logger.info("[Router] → Follow-up flow for %s", phone)
            await handle_followup_response(phone, name, text)
            return JSONResponse({"status": "ok", "flow": "followup"})

        # ── Priority 2: AI booking agent ─────────────────────────────────────
        logger.info("[Router] → Booking flow for %s", phone)
        await handle_booking_flow(phone, name, text)
        return JSONResponse({"status": "ok", "flow": "booking"})

    except Exception as exc:
        logger.error("Unhandled error in webhook handler: %s", exc, exc_info=True)
        # Return 200 to Meta even on error (avoid retry storms)
        return JSONResponse({"status": "error"}, status_code=200)
