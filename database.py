"""
database.py - Supabase client and all database operations for the Clinic AI Agent.

Tables: patients | conversations | appointments | followups | review_requests
SQL schema is at the bottom of this file.
"""

from __future__ import annotations
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from supabase import create_client, Client
from config import settings

logger = logging.getLogger(__name__)
_client: Optional[Client] = None


def get_db() -> Client:
    global _client
    if _client is None:
        _client = create_client(settings.SUPABASE_URL, settings.SUPABASE_KEY)
    return _client


def upsert_patient(phone: str, name: str = None, language: str = "en") -> dict:
    db = get_db()
    payload: dict[str, Any] = {"phone": phone, "updated_at": _now()}
    if name:
        payload["name"] = name
    if language:
        payload["language"] = language
    result = db.table("patients").upsert(payload, on_conflict="phone").execute()
    return result.data[0] if result.data else {}


def get_patient(phone: str) -> dict:
    db = get_db()
    result = db.table("patients").select("*").eq("phone", phone).limit(1).execute()
    return result.data[0] if result.data else None


def save_message(phone: str, role: str, content: str) -> None:
    db = get_db()
    db.table("conversations").insert({"patient_phone": phone, "role": role, "content": content}).execute()


def get_conversation_history(phone: str, limit: int = 8) -> list:
    db = get_db()
    result = db.table("conversations").select("role, content").eq("patient_phone", phone).order("created_at", desc=True).limit(limit).execute()
    return list(reversed(result.data)) if result.data else []


def clear_conversation_history(phone: str) -> None:
    db = get_db()
    db.table("conversations").delete().eq("patient_phone", phone).execute()


def create_appointment(phone: str, patient_name: str, appointment_date: str, slot_time: str) -> dict:
    db = get_db()
    appt = db.table("appointments").insert({"patient_phone": phone, "patient_name": patient_name, "appointment_date": appointment_date, "slot_time": slot_time, "status": "confirmed"}).execute()
    if not appt.data:
        raise RuntimeError("Failed to create appointment")
    appt_row = appt.data[0]
    appt_id = appt_row["id"]
    appt_dt = datetime.strptime(f"{appointment_date} {slot_time}", "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
    followup_at = appt_dt + timedelta(days=settings.FOLLOWUP_DAYS)
    db.table("followups").insert({"appointment_id": appt_id, "scheduled_at": followup_at.isoformat(), "status": "pending"}).execute()
    logger.info("Appointment %s created for %s on %s %s", appt_id, patient_name, appointment_date, slot_time)
    return appt_row


def get_booked_slots(date: str) -> list:
    db = get_db()
    result = db.table("appointments").select("slot_time").eq("appointment_date", date).in_("status", ["confirmed"]).execute()
    return [row["slot_time"] for row in result.data] if result.data else []


def get_appointments_for_reminder() -> list:
    db = get_db()
    now = datetime.now(timezone.utc)
    window_start = now + timedelta(hours=23)
    window_end = now + timedelta(hours=25)
    result = db.table("appointments").select("*").eq("status", "confirmed").eq("reminder_sent", False).execute()
    due = []
    for appt in (result.data or []):
        appt_dt = _parse_appt_datetime(appt)
        if appt_dt and window_start <= appt_dt <= window_end:
            due.append(appt)
    return due


def mark_reminder_sent(appt_id: int) -> None:
    get_db().table("appointments").update({"reminder_sent": True}).eq("id", appt_id).execute()


def mark_appointment_completed(appt_id: int) -> None:
    get_db().table("appointments").update({"status": "completed", "completed_at": _now()}).eq("id", appt_id).execute()


def get_pending_followups() -> list:
    db = get_db()
    now = datetime.now(timezone.utc)
    window_start = (now - timedelta(minutes=30)).isoformat()
    window_end = (now + timedelta(minutes=30)).isoformat()
    result = db.table("followups").select("*, appointments(patient_phone, patient_name, appointment_date, slot_time)").eq("status", "pending").gte("scheduled_at", window_start).lte("scheduled_at", window_end).execute()
    return result.data or []


def get_active_followup_for_phone(phone: str) -> dict:
    db = get_db()
    result = db.table("followups").select("*, appointments!inner(patient_phone)").eq("appointments.patient_phone", phone).eq("status", "sent").order("sent_at", desc=True).limit(1).execute()
    return result.data[0] if result.data else None


def mark_followup_sent(followup_id: int) -> None:
    get_db().table("followups").update({"status": "sent", "sent_at": _now()}).eq("id", followup_id).execute()


def save_followup_response(followup_id: int, response_text: str, sentiment: str) -> None:
    get_db().table("followups").update({"status": "responded", "patient_response": response_text, "sentiment": sentiment, "responded_at": _now()}).eq("id", followup_id).execute()


def has_review_been_requested(phone: str, appt_id: int) -> bool:
    result = get_db().table("review_requests").select("id").eq("patient_phone", phone).eq("appointment_id", appt_id).limit(1).execute()
    return bool(result.data)


def log_review_request(phone: str, appt_id: int) -> None:
    get_db().table("review_requests").insert({"patient_phone": phone, "appointment_id": appt_id}).execute()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_appt_datetime(appt: dict):
    try:
        return datetime.strptime(f"{appt['appointment_date']} {appt['slot_time']}", "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
    except Exception:
        return None


SQL_SCHEMA = """
-- Clinic AI Agent Database Schema
-- Run this once in Supabase Dashboard SQL Editor

CREATE TABLE IF NOT EXISTS patients (
    id bigserial PRIMARY KEY, phone text UNIQUE NOT NULL, name text,
    language text DEFAULT 'en', created_at timestamptz DEFAULT now(), updated_at timestamptz DEFAULT now()
);

CREATE TABLE IF NOT EXISTS conversations (
    id bigserial PRIMARY KEY, patient_phone text NOT NULL,
    role text NOT NULL, content text NOT NULL, created_at timestamptz DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_conversations_phone_ts ON conversations (patient_phone, created_at);

CREATE TABLE IF NOT EXISTS appointments (
    id bigserial PRIMARY KEY, patient_phone text NOT NULL, patient_name text NOT NULL,
    appointment_date date NOT NULL, slot_time text NOT NULL,
    status text DEFAULT 'confirmed', reminder_sent boolean DEFAULT false,
    completed_at timestamptz, created_at timestamptz DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_appointments_phone ON appointments (patient_phone);
CREATE INDEX IF NOT EXISTS idx_appointments_date ON appointments (appointment_date);

CREATE TABLE IF NOT EXISTS followups (
    id bigserial PRIMARY KEY, appointment_id bigint REFERENCES appointments(id),
    scheduled_at timestamptz NOT NULL, sent_at timestamptz,
    status text DEFAULT 'pending', patient_response text,
    sentiment text, responded_at timestamptz
);

CREATE TABLE IF NOT EXISTS review_requests (
    id bigserial PRIMARY KEY, patient_phone text NOT NULL,
    appointment_id bigint REFERENCES appointments(id), sent_at timestamptz DEFAULT now()
);
"""
