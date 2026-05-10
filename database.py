"""
database.py — Supabase client + all database operations.

Tables: patients | conversations | appointments | followups | review_requests

SQL schema is at the bottom of this file (copy & run in Supabase SQL Editor).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from supabase import create_client, Client

from config import settings

logger = logging.getLogger(__name__)

# ── Supabase client (singleton) ───────────────────────────────────────────────

_client: Optional[Client] = None


def get_db() -> Client:
    global _client
    if _client is None:
        _client = create_client(settings.SUPABASE_URL, settings.SUPABASE_KEY)
    return _client


# ── Patients ──────────────────────────────────────────────────────────────────

def upsert_patient(phone: str, name: str | None = None, language: str = "en") -> dict:
    """Insert or update a patient row; returns the row."""
    db = get_db()
    payload: dict[str, Any] = {"phone": phone, "updated_at": _now()}
    if name:
        payload["name"] = name
    if language:
        payload["language"] = language
    result = (
        db.table("patients")
        .upsert(payload, on_conflict="phone")
        .execute()
    )
    return result.data[0] if result.data else {}


def get_patient(phone: str) -> dict | None:
    db = get_db()
    result = db.table("patients").select("*").eq("phone", phone).limit(1).execute()
    return result.data[0] if result.data else None


# ── Conversations ─────────────────────────────────────────────────────────────

def save_message(phone: str, role: str, content: str) -> None:
    """Append one message to conversation history."""
    db = get_db()
    db.table("conversations").insert(
        {"patient_phone": phone, "role": role, "content": content}
    ).execute()


def get_conversation_history(phone: str, limit: int = 8) -> list[dict]:
    """Return last `limit` messages for the patient (oldest first)."""
    db = get_db()
    result = (
        db.table("conversations")
        .select("role, content")
        .eq("patient_phone", phone)
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    # Reverse so oldest is first (correct for OpenAI messages array)
    return list(reversed(result.data)) if result.data else []


def clear_conversation_history(phone: str) -> None:
    """Delete conversation history for a patient (e.g. after booking)."""
    db = get_db()
    db.table("conversations").delete().eq("patient_phone", phone).execute()


# ── Appointments ──────────────────────────────────────────────────────────────

def create_appointment(
    phone: str,
    patient_name: str,
    appointment_date: str,
    slot_time: str,
) -> dict:
    """
    Create a confirmed appointment.
    Also creates a followup row scheduled for FOLLOWUP_DAYS later.
    Returns the new appointment row.
    """
    db = get_db()
    appt = (
        db.table("appointments")
        .insert(
            {
                "patient_phone": phone,
                "patient_name": patient_name,
                "appointment_date": appointment_date,
                "slot_time": slot_time,
                "status": "confirmed",
            }
        )
        .execute()
    )
    if not appt.data:
        raise RuntimeError("Failed to create appointment")

    appt_row = appt.data[0]
    appt_id = appt_row["id"]

    # Schedule 7-day follow-up
    appt_dt = datetime.strptime(
        f"{appointment_date} {slot_time}", "%Y-%m-%d %H:%M"
    ).replace(tzinfo=timezone.utc)
    followup_at = appt_dt + timedelta(days=settings.FOLLOWUP_DAYS)

    db.table("followups").insert(
        {
            "appointment_id": appt_id,
            "scheduled_at": followup_at.isoformat(),
            "status": "pending",
        }
    ).execute()

    logger.info(
        "Appointment %s created for %s on %s %s; follow-up at %s",
        appt_id, patient_name, appointment_date, slot_time, followup_at,
    )
    return appt_row


def get_booked_slots(date: str) -> list[str]:
    """Return list of slot_time strings already booked for a given date."""
    db = get_db()
    result = (
        db.table("appointments")
        .select("slot_time")
        .eq("appointment_date", date)
        .in_("status", ["confirmed"])
        .execute()
    )
    return [row["slot_time"] for row in result.data] if result.data else []


# ── Blocked Slots ──────────────────────────────────────────────────────────────

def block_slots(date: str, slot_times: list[str], reason: str = "") -> int:
    """
    Block specific slots on a date so patients cannot book them.
    Pass slot_times=["all"] to block the entire day.
    Returns the number of slots blocked.
    """
    db_client = get_db()

    if "all" in slot_times:
        # Delete any existing per-slot blocks for this day and insert one "all day" row
        db_client.table("blocked_slots").delete().eq("block_date", date).execute()
        db_client.table("blocked_slots").insert(
            {"block_date": date, "slot_time": None, "reason": reason}
        ).execute()
        return 1  # 1 "all day" block row

    # For specific slots: upsert each one (ignore if already blocked)
    rows = [{"block_date": date, "slot_time": s, "reason": reason} for s in slot_times]
    db_client.table("blocked_slots").upsert(
        rows, on_conflict="block_date,slot_time"
    ).execute()
    return len(slot_times)


def unblock_slots(date: str, slot_times: list[str]) -> int:
    """
    Remove blocks for specific slots on a date.
    Pass slot_times=["all"] to clear all blocks for that date.
    Returns the number of block rows removed.
    """
    db_client = get_db()

    if "all" in slot_times:
        result = db_client.table("blocked_slots").delete().eq("block_date", date).execute()
        return len(result.data) if result.data else 0

    count = 0
    for slot in slot_times:
        result = (
            db_client.table("blocked_slots")
            .delete()
            .eq("block_date", date)
            .eq("slot_time", slot)
            .execute()
        )
        count += len(result.data) if result.data else 0
    return count


def get_blocked_slot_times(date: str) -> list[str]:
    """
    Return list of blocked slot_time strings for a date.
    Returns ["all"] if the entire day is blocked.
    Returns [] if nothing is blocked.
    """
    db_client = get_db()
    result = (
        db_client.table("blocked_slots")
        .select("slot_time")
        .eq("block_date", date)
        .execute()
    )
    if not result.data:
        return []
    times = [row["slot_time"] for row in result.data]
    # If any row has slot_time=None it means whole day is blocked
    if None in times:
        return ["all"]
    return times


def get_blocked_slots_detail(date: str) -> list[dict]:
    """Return full block rows for a date (for doctor view)."""
    db_client = get_db()
    result = (
        db_client.table("blocked_slots")
        .select("*")
        .eq("block_date", date)
        .order("slot_time", desc=False)
        .execute()
    )
    return result.data or []


def get_upcoming_appointment(phone: str) -> dict | None:
    """
    Return the next confirmed appointment for this patient (soonest future slot).
    Returns None if no upcoming appointment exists.
    """
    db = get_db()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    result = (
        db.table("appointments")
        .select("*")
        .eq("patient_phone", phone)
        .eq("status", "confirmed")
        .gte("appointment_date", today)
        .order("appointment_date", desc=False)
        .order("slot_time", desc=False)
        .limit(1)
        .execute()
    )
    return result.data[0] if result.data else None


def cancel_appointment(appt_id: int) -> None:
    """Mark an appointment as cancelled, freeing the slot."""
    db = get_db()
    db.table("appointments").update(
        {"status": "cancelled", "cancelled_at": _now()}
    ).eq("id", appt_id).execute()
    # Also cancel any pending followup for this appointment
    db.table("followups").update(
        {"status": "cancelled"}
    ).eq("appointment_id", appt_id).eq("status", "pending").execute()
    logger.info("Appointment %s cancelled", appt_id)


def reschedule_appointment(
    old_appt_id: int,
    phone: str,
    patient_name: str,
    new_date: str,
    new_slot: str,
) -> dict:
    """
    Cancel the old appointment and create a new one.
    Returns the new appointment row.
    """
    cancel_appointment(old_appt_id)
    new_appt = create_appointment(phone, patient_name, new_date, new_slot)
    logger.info(
        "Rescheduled appt %s → new appt %s on %s %s",
        old_appt_id, new_appt["id"], new_date, new_slot,
    )
    return new_appt


def get_appointments_for_date(date: str) -> list[dict]:
    """
    Return all confirmed appointments for a given date, ordered by slot time.
    Used by the daily doctor schedule job.
    """
    db = get_db()
    result = (
        db.table("appointments")
        .select("patient_name, patient_phone, slot_time")
        .eq("appointment_date", date)
        .eq("status", "confirmed")
        .order("slot_time", desc=False)
        .execute()
    )
    return result.data or []


def get_appointments_for_reminder() -> list[dict]:
    """
    Return appointments that:
    - are 'confirmed'
    - have reminder_sent = False
    - are ~24h away (within next 25h)
    """
    db = get_db()
    now = datetime.now(timezone.utc)
    window_start = now + timedelta(hours=23)
    window_end = now + timedelta(hours=25)

    result = (
        db.table("appointments")
        .select("*")
        .eq("status", "confirmed")
        .eq("reminder_sent", False)
        .execute()
    )
    due = []
    for appt in (result.data or []):
        appt_dt = _parse_appt_datetime(appt)
        if appt_dt and window_start <= appt_dt <= window_end:
            due.append(appt)
    return due


def mark_reminder_sent(appt_id: int) -> None:
    db = get_db()
    db.table("appointments").update({"reminder_sent": True}).eq("id", appt_id).execute()


def mark_appointment_completed(appt_id: int) -> None:
    db = get_db()
    db.table("appointments").update(
        {"status": "completed", "completed_at": _now()}
    ).eq("id", appt_id).execute()


# ── Followups ─────────────────────────────────────────────────────────────────

def get_pending_followups() -> list[dict]:
    """
    Return followups that:
    - status = 'pending'
    - scheduled_at is within ±30 min of now
    """
    db = get_db()
    now = datetime.now(timezone.utc)
    window_start = (now - timedelta(minutes=30)).isoformat()
    window_end = (now + timedelta(minutes=30)).isoformat()

    result = (
        db.table("followups")
        .select("*, appointments(patient_phone, patient_name, appointment_date, slot_time)")
        .eq("status", "pending")
        .gte("scheduled_at", window_start)
        .lte("scheduled_at", window_end)
        .execute()
    )
    return result.data or []


def get_active_followup_for_phone(phone: str) -> dict | None:
    """
    Return a followup row that was sent but not yet responded to,
    for the patient's most recent appointment.
    """
    db = get_db()
    result = (
        db.table("followups")
        .select("*, appointments!inner(patient_phone)")
        .eq("appointments.patient_phone", phone)
        .eq("status", "sent")
        .order("sent_at", desc=True)
        .limit(1)
        .execute()
    )
    return result.data[0] if result.data else None


def mark_followup_sent(followup_id: int) -> None:
    db = get_db()
    db.table("followups").update(
        {"status": "sent", "sent_at": _now()}
    ).eq("id", followup_id).execute()


def save_followup_response(
    followup_id: int, response_text: str, sentiment: str
) -> None:
    db = get_db()
    db.table("followups").update(
        {
            "status": "responded",
            "patient_response": response_text,
            "sentiment": sentiment,
            "responded_at": _now(),
        }
    ).eq("id", followup_id).execute()


# ── Review Requests ───────────────────────────────────────────────────────────

def has_review_been_requested(phone: str, appt_id: int) -> bool:
    """Idempotency guard — returns True if review was already requested."""
    db = get_db()
    result = (
        db.table("review_requests")
        .select("id")
        .eq("patient_phone", phone)
        .eq("appointment_id", appt_id)
        .limit(1)
        .execute()
    )
    return bool(result.data)


def log_review_request(phone: str, appt_id: int) -> None:
    db = get_db()
    db.table("review_requests").insert(
        {"patient_phone": phone, "appointment_id": appt_id}
    ).execute()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_appt_datetime(appt: dict) -> datetime | None:
    try:
        return datetime.strptime(
            f"{appt['appointment_date']} {appt['slot_time']}", "%Y-%m-%d %H:%M"
        ).replace(tzinfo=timezone.utc)
    except Exception:
        return None


# ══════════════════════════════════════════════════════════════════════════════
# SQL SCHEMA — Copy and run this in Supabase Dashboard → SQL Editor
# ══════════════════════════════════════════════════════════════════════════════
SQL_SCHEMA = """
-- ============================================================
-- Clinic AI Agent — Database Schema
-- Run this once in Supabase SQL Editor to initialise all tables
-- ============================================================

-- 1. patients
CREATE TABLE IF NOT EXISTS patients (
    id          bigserial PRIMARY KEY,
    phone       text UNIQUE NOT NULL,
    name        text,
    language    text DEFAULT 'en',
    created_at  timestamptz DEFAULT now(),
    updated_at  timestamptz DEFAULT now()
);

-- 2. conversations (rolling message history per patient)
CREATE TABLE IF NOT EXISTS conversations (
    id              bigserial PRIMARY KEY,
    patient_phone   text NOT NULL,
    role            text NOT NULL,  -- 'user' | 'assistant'
    content         text NOT NULL,
    created_at      timestamptz DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_conversations_phone_ts
    ON conversations (patient_phone, created_at);

-- 3. appointments
CREATE TABLE IF NOT EXISTS appointments (
    id                  bigserial PRIMARY KEY,
    patient_phone       text NOT NULL,
    patient_name        text NOT NULL,
    appointment_date    date NOT NULL,
    slot_time           text NOT NULL,     -- e.g. '10:30'
    status              text DEFAULT 'confirmed',  -- confirmed|completed|cancelled
    reminder_sent       boolean DEFAULT false,
    completed_at        timestamptz,
    cancelled_at        timestamptz,
    created_at          timestamptz DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_appointments_phone
    ON appointments (patient_phone);
CREATE INDEX IF NOT EXISTS idx_appointments_date
    ON appointments (appointment_date);

-- 4. followups
CREATE TABLE IF NOT EXISTS followups (
    id              bigserial PRIMARY KEY,
    appointment_id  bigint REFERENCES appointments(id),
    scheduled_at    timestamptz NOT NULL,
    sent_at         timestamptz,
    status          text DEFAULT 'pending',  -- pending|sent|responded
    patient_response text,
    sentiment       text,                    -- positive|neutral|negative
    responded_at    timestamptz
);

-- 5. review_requests (idempotency guard)
CREATE TABLE IF NOT EXISTS review_requests (
    id              bigserial PRIMARY KEY,
    patient_phone   text NOT NULL,
    appointment_id  bigint REFERENCES appointments(id),
    sent_at         timestamptz DEFAULT now()
);
"""
