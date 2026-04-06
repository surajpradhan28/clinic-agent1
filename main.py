"""
main.py - FastAPI application + WhatsApp webhook handler.
Entry point for the Clinic AI Agent.
"""

from __future__ import annotations
import logging
import sys
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse
import database as db
import whatsapp
import scheduler as sched
from config import settings
from flows.booking import handle_booking_flow
from flows.followup import handle_followup_response, is_followup_response

logging.basicConfig(stream=sys.stdout, level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Clinic AI Agent starting up")
    try:
        settings.validate()
    except EnvironmentError as exc:
        logger.error("Configuration error: %s", exc)
    sched.start()
    yield
    sched.stop()

app = FastAPI(title="Clinic AI Agent", version="1.0.0", lifespan=lifespan)

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
    return JSONResponse(status_code=200 if all_ok else 503, content={"status": "healthy" if all_ok else "degraded", "checks": checks})

@app.get("/webhook")
async def verify_webhook(request: Request):
    params = dict(request.query_params)
    if params.get("hub.mode") == "subscribe" and params.get("hub.verify_token") == settings.WHATSAPP_VERIFY_TOKEN:
        return PlainTextResponse(content=params.get("hub.challenge"), status_code=200)
    raise HTTPException(status_code=403, detail="Verification failed")

@app.post("/webhook")
async def receive_message(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"status": "error"}, status_code=400)
    try:
        msg = whatsapp.parse_incoming_message(body)
        if not msg:
            return JSONResponse({"status": "ignored"})
        phone, name, text = msg["phone"], msg["name"], msg["text"]
        message_id = msg.get("message_id", "")
        if not text:
            await whatsapp.send_text(phone, "Sorry, I can only process text messages right now.")
            return JSONResponse({"status": "unsupported_type"})
        logger.info("Message from %s (%s): %s", phone, name, text[:80])
        db.upsert_patient(phone, name)
        if message_id:
            await whatsapp.mark_as_read(message_id)
        if await is_followup_response(phone):
            await handle_followup_response(phone, name, text)
            return JSONResponse({"status": "ok", "flow": "followup"})
        await handle_booking_flow(phone, name, text)
        return JSONResponse({"status": "ok", "flow": "booking"})
    except Exception as exc:
        logger.error("Unhandled error: %s", exc, exc_info=True)
        return JSONResponse({"status": "error"}, status_code=200)
