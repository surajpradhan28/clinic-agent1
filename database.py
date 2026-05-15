"""
database.py — Supabase client + all database operations (multi-tenant v4).

Every data function takes a client_id: int as its first argument.
The top-level helpers (get_client_by_phone_id, get_all_active_clients, etc.)
are tenant-unaware — used by the webhook router and scheduler.

Tables:
  clients | subscriptions | payments
  patients | conversations | appointments | followups | review_requests
  blocked_slots | clinic_settings | clinic_notes | custom_schedule
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


# ══════════════════════════════════════════════════════════════════════════════
# CLIENT / TENANT MANAGEMENT
# ══════════════════════════════════════════════════════════════════════════════

def get_client_by_phone_id(whatsapp_phone_id: str) -> dict | None:
    """
    Look up a client by their Meta phone_number_id.
    Called on every incoming webhook to resolve which clinic was messaged.
    Returns the full client row or None if not registered.
    """
    db = get_db()
    result = (
        db.table("clients")
        .select("*")
        .eq("whatsapp_phone_id", whatsapp_phone_id)
        .limit(1)
        .execute()
    )
    return result.data[0] if result.data else None


def get_all_active_clients() -> list[dict]:
    """Return all clients with status 'active' or 'trial'. Used by scheduler."""
    db = get_db()
    result = (
        db.table("clients")
        .select("*")
        .in_("status", ["active", "trial"])
        .execute()
    )
    return result.data or []


def get_client_by_id(client_id: int) -> dict | None:
    db = get_db()
    result = db.table("clients").select("*").eq("id", client_id).limit(1).execute()
    return result.data[0] if result.data else None


def create_client(
    name: str,
    doctor_name: str,
    whatsapp_phone_id: str,
    contact_phone: str = "",
    email: str = "",
    plan: str = "starter",
    trial_days: int = 14,
) -> dict:
    """Onboard a new clinic. Returns the new client row."""
    from datetime import date, timedelta
    db = get_db()
    trial_end = (date.today() + timedelta(days=trial_days)).isoformat()
    result = (
        db.table("clients")
        .insert({
            "name": name,
            "doctor_name": doctor_name,
            "whatsapp_phone_id": whatsapp_phone_id,
            "contact_phone": contact_phone,
            "email": email,
            "plan": plan,
            "status": "trial",
            "trial_ends_at": trial_end,
        })
        .execute()
    )
    if not result.data:
        raise RuntimeError("Failed to create client")
    client = result.data[0]
    # Seed default clinic settings for this new client
    _seed_clinic_settings(client["id"], name, doctor_name)
    logger.info("New client created: %s (id=%s)", name, client["id"])
    return client


def _seed_clinic_settings(client_id: int, clinic_name: str, doctor_name: str) -> None:
    db = get_db()
    rows = [
        {"client_id": client_id, "key": "clinic_name",        "value": clinic_name},
        {"client_id": client_id, "key": "doctor_name",        "value": doctor_name},
        {"client_id": client_id, "key": "clinic_address",     "value": ""},
        {"client_id": client_id, "key": "clinic_phone",       "value": ""},
        {"client_id": client_id, "key": "google_review_link", "value": ""},
    ]
    db.table("clinic_settings").upsert(rows, on_conflict="client_id,key").execute()


def update_client_status(client_id: int, status: str) -> None:
    """Set client status: trial | active | suspended | expired."""
    db = get_db()
    db.table("clients").update(
        {"status": status, "updated_at": _now()}
    ).eq("id", client_id).execute()
    logger.info("Client %s status → %s", client_id, status)


def update_client_plan(client_id: int, plan: str) -> None:
    db = get_db()
    db.table("clients").update(
        {"plan": plan, "updated_at": _now()}
    ).eq("id", client_id).execute()


def list_all_clients() -> list[dict]:
    """Return all clients for admin dashboard / admin WhatsApp commands."""
    db = get_db()
    result = db.table("clients").select("*").order("id").execute()
    return result.data or []


# ── Subscriptions ──────────────────────────────────────────────────────────────

def create_subscription(
    client_id: int,
    plan_name: str,
    price: float,
    start_date: str,
    end_date: str,
    billing_cycle: str = "monthly",
) -> dict:
    db = get_db()
    # Expire any previous active subscription
    db.table("subscriptions").update({"status": "expired"}).eq(
        "client_id", client_id
    ).eq("status", "active").execute()
    # Create new
    result = (
        db.table("subscriptions")
        .insert({
            "client_id": client_id,
            "plan_name": plan_name,
            "price": price,
            "billing_cycle": billing_cycle,
            "start_date": start_date,
            "end_date": end_date,
            "status": "active",
        })
        .execute()
    )
    if not result.data:
        raise RuntimeError("Failed to create subscription")
    # Also update client plan + status to active
    update_client_plan(client_id, plan_name)
    update_client_status(client_id, "active")
    return result.data[0]


def get_active_subscription(client_id: int) -> dict | None:
    db = get_db()
    result = (
        db.table("subscriptions")
        .select("*")
        .eq("client_id", client_id)
        .eq("status", "active")
        .order("end_date", desc=True)
        .limit(1)
        .execute()
    )
    return result.data[0] if result.data else None


# ── Payments ───────────────────────────────────────────────────────────────────

def record_payment(
    client_id: int,
    amount: float,
    method: str = "UPI",
    notes: str = "",
    subscription_id: int | None = None,
    payment_date: str | None = None,
) -> dict:
    db = get_db()
    result = (
        db.table("payments")
        .insert({
            "client_id": client_id,
            "subscription_id": subscription_id,
            "amount": amount,
            "method": method,
            "status": "paid",
            "payment_date": payment_date or datetime.now(timezone.utc).date().isoformat(),
            "notes": notes,
        })
        .execute()
    )
    return result.data[0] if result.data else {}


def get_payments(client_id: int) -> list[dict]:
    db = get_db()
    result = (
        db.table("payments")
        .select("*")
        .eq("client_id", client_id)
        .order("payment_date", desc=True)
        .execute()
    )
    return result.data or []


# ══════════════════════════════════════════════════════════════════════════════
# PATIENTS
# ══════════════════════════════════════════════════════════════════════════════

def upsert_patient(client_id: int, phone: str, name: str | None = None, language: str = "en") -> dict:
    db = get_db()
    payload: dict[str, Any] = {
        "client_id": client_id,
        "phone": phone,
        "updated_at": _now(),
    }
    if name:
        payload["name"] = name
    if language:
        payload["language"] = language
    result = (
        db.table("patients")
        .upsert(payload, on_conflict="client_id,phone")
        .execute()
    )
    return result.data[0] if result.data else {}


def get_patient(client_id: int, phone: str) -> dict | None:
    db = get_db()
    result = (
        db.table("patients")
        .select("*")
        .eq("client_id", client_id)
        .eq("phone", phone)
        .limit(1)
        .execute()
    )
    return result.data[0] if result.data else None


# ══════════════════════════════════════════════════════════════════════════════
# CONVERSATIONS
# ══════════════════════════════════════════════════════════════════════════════

def save_message(client_id: int, phone: str, role: str, content: str) -> None:
    db = get_db()
    db.table("conversations").insert(
        {"client_id": client_id, "patient_phone": phone, "role": role, "content": content}
    ).execute()


def get_conversation_history(client_id: int, phone: str, limit: int = 8) -> list[dict]:
    db = get_db()
    result = (
        db.table("conversations")
        .select("role, content")
        .eq("client_id", client_id)
        .eq("patient_phone", phone)
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    return list(reversed(result.data)) if result.data else []


def clear_conversation_history(client_id: int, phone: str) -> None:
    db = get_db()
    db.table("conversations").delete().eq(
        "client_id", client_id
    ).eq("patient_phone", phone).execute()


# ══════════════════════════════════════════════════════════════════════════════
# APPOINTMENTS
# ══════════════════════════════════════════════════════════════════════════════

def create_appointment(
    client_id: int,
    phone: str,
    patient_name: str,
    appointment_date: str,
    slot_time: str,
) -> dict:
    db = get_db()
    appt = (
        db.table("appointments")
        .insert({
            "client_id": client_id,
            "patient_phone": phone,
            "patient_name": patient_name,
            "appointment_date": appointment_date,
            "slot_time": slot_time,
            "status": "confirmed",
        })
        .execute()
    )
    if not appt.data:
        raise RuntimeError("Failed to create appointment")

    appt_row = appt.data[0]
    appt_id  = appt_row["id"]

    # Schedule 7-day follow-up
    appt_dt = datetime.strptime(
        f"{appointment_date} {slot_time}", "%Y-%m-%d %H:%M"
    ).replace(tzinfo=timezone.utc)
    followup_at = appt_dt + timedelta(days=settings.FOLLOWUP_DAYS)

    db.table("followups").insert({
        "client_id": client_id,
        "appointment_id": appt_id,
        "scheduled_at": followup_at.isoformat(),
        "status": "pending",
    }).execute()

    logger.info(
        "Appointment %s created (client=%s) for %s on %s %s",
        appt_id, client_id, patient_name, appointment_date, slot_time,
    )
    return appt_row


def get_booked_slots(client_id: int, date: str) -> list[str]:
    db = get_db()
    result = (
        db.table("appointments")
        .select("slot_time")
        .eq("client_id", client_id)
        .eq("appointment_date", date)
        .in_("status", ["confirmed"])
        .execute()
    )
    return [row["slot_time"] for row in result.data] if result.data else []


def get_upcoming_appointment(client_id: int, phone: str) -> dict | None:
    db = get_db()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    result = (
        db.table("appointments")
        .select("*")
        .eq("client_id", client_id)
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
    db = get_db()
    db.table("appointments").update(
        {"status": "cancelled", "cancelled_at": _now()}
    ).eq("id", appt_id).execute()
    db.table("followups").update(
        {"status": "cancelled"}
    ).eq("appointment_id", appt_id).eq("status", "pending").execute()
    logger.info("Appointment %s cancelled", appt_id)


def reschedule_appointment(
    client_id: int,
    old_appt_id: int,
    phone: str,
    patient_name: str,
    new_date: str,
    new_slot: str,
) -> dict:
    cancel_appointment(old_appt_id)
    new_appt = create_appointment(client_id, phone, patient_name, new_date, new_slot)
    logger.info("Rescheduled appt %s → new appt %s on %s %s", old_appt_id, new_appt["id"], new_date, new_slot)
    return new_appt


def get_appointments_for_date(client_id: int, date: str) -> list[dict]:
    db = get_db()
    result = (
        db.table("appointments")
        .select("patient_name, patient_phone, slot_time")
        .eq("client_id", client_id)
        .eq("appointment_date", date)
        .eq("status", "confirmed")
        .order("slot_time", desc=False)
        .execute()
    )
    return result.data or []


def get_appointments_for_reminder(client_id: int) -> list[dict]:
    db = get_db()
    now = datetime.now(timezone.utc)
    window_start = now + timedelta(hours=23)
    window_end = now + timedelta(hours=25)

    result = (
        db.table("appointments")
        .select("*")
        .eq("client_id", client_id)
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


# ══════════════════════════════════════════════════════════════════════════════
# BLOCKED SLOTS
# ══════════════════════════════════════════════════════════════════════════════

def block_slots(client_id: int, date: str, slot_times: list[str], reason: str = "") -> int:
    db_client = get_db()
    if "all" in slot_times:
        db_client.table("blocked_slots").delete().eq(
            "client_id", client_id
        ).eq("block_date", date).execute()
        db_client.table("blocked_slots").insert(
            {"client_id": client_id, "block_date": date, "slot_time": None, "reason": reason}
        ).execute()
        return 1
    rows = [
        {"client_id": client_id, "block_date": date, "slot_time": s, "reason": reason}
        for s in slot_times
    ]
    db_client.table("blocked_slots").upsert(
        rows, on_conflict="client_id,block_date,slot_time"
    ).execute()
    return len(slot_times)


def unblock_slots(client_id: int, date: str, slot_times: list[str]) -> int:
    db_client = get_db()
    if "all" in slot_times:
        result = db_client.table("blocked_slots").delete().eq(
            "client_id", client_id
        ).eq("block_date", date).execute()
        return len(result.data) if result.data else 0
    count = 0
    for slot in slot_times:
        result = (
            db_client.table("blocked_slots")
            .delete()
            .eq("client_id", client_id)
            .eq("block_date", date)
            .eq("slot_time", slot)
            .execute()
        )
        count += len(result.data) if result.data else 0
    return count


def get_blocked_slot_times(client_id: int, date: str) -> list[str]:
    db_client = get_db()
    result = (
        db_client.table("blocked_slots")
        .select("slot_time")
        .eq("client_id", client_id)
        .eq("block_date", date)
        .execute()
    )
    if not result.data:
        return []
    times = [row["slot_time"] for row in result.data]
    if None in times:
        return ["all"]
    return times


def get_blocked_slots_detail(client_id: int, date: str) -> list[dict]:
    db_client = get_db()
    result = (
        db_client.table("blocked_slots")
        .select("*")
        .eq("client_id", client_id)
        .eq("block_date", date)
        .order("slot_time", desc=False)
        .execute()
    )
    return result.data or []


def get_appointments_in_slots(client_id: int, date: str, slot_times: list[str]) -> list[dict]:
    if not slot_times:
        return []
    db = get_db()
    result = (
        db.table("appointments")
        .select("id, patient_phone, patient_name, slot_time")
        .eq("client_id", client_id)
        .eq("appointment_date", date)
        .eq("status", "confirmed")
        .in_("slot_time", slot_times)
        .execute()
    )
    return result.data or []


def get_all_appointments_for_date_full(client_id: int, date: str) -> list[dict]:
    db = get_db()
    result = (
        db.table("appointments")
        .select("id, patient_phone, patient_name, slot_time")
        .eq("client_id", client_id)
        .eq("appointment_date", date)
        .eq("status", "confirmed")
        .order("slot_time", desc=False)
        .execute()
    )
    return result.data or []


# ══════════════════════════════════════════════════════════════════════════════
# FOLLOWUPS
# ══════════════════════════════════════════════════════════════════════════════

def get_pending_followups(client_id: int) -> list[dict]:
    db = get_db()
    now = datetime.now(timezone.utc)
    window_start = (now - timedelta(minutes=30)).isoformat()
    window_end = (now + timedelta(minutes=30)).isoformat()

    result = (
        db.table("followups")
        .select("*, appointments(patient_phone, patient_name, appointment_date, slot_time)")
        .eq("client_id", client_id)
        .eq("status", "pending")
        .gte("scheduled_at", window_start)
        .lte("scheduled_at", window_end)
        .execute()
    )
    return result.data or []


def get_active_followup_for_phone(client_id: int, phone: str) -> dict | None:
    db = get_db()
    result = (
        db.table("followups")
        .select("*, appointments!inner(patient_phone)")
        .eq("client_id", client_id)
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


def save_followup_response(followup_id: int, response_text: str, sentiment: str) -> None:
    db = get_db()
    db.table("followups").update({
        "status": "responded",
        "patient_response": response_text,
        "sentiment": sentiment,
        "responded_at": _now(),
    }).eq("id", followup_id).execute()


# ══════════════════════════════════════════════════════════════════════════════
# REVIEW REQUESTS
# ══════════════════════════════════════════════════════════════════════════════

def has_review_been_requested(client_id: int, phone: str, appt_id: int) -> bool:
    db = get_db()
    result = (
        db.table("review_requests")
        .select("id")
        .eq("client_id", client_id)
        .eq("patient_phone", phone)
        .eq("appointment_id", appt_id)
        .limit(1)
        .execute()
    )
    return bool(result.data)


def log_review_request(client_id: int, phone: str, appt_id: int) -> None:
    db = get_db()
    db.table("review_requests").insert(
        {"client_id": client_id, "patient_phone": phone, "appointment_id": appt_id}
    ).execute()


# ══════════════════════════════════════════════════════════════════════════════
# CLINIC SETTINGS
# ══════════════════════════════════════════════════════════════════════════════

def get_clinic_setting(client_id: int, key: str) -> str | None:
    try:
        db = get_db()
        result = (
            db.table("clinic_settings")
            .select("value")
            .eq("client_id", client_id)
            .eq("key", key)
            .limit(1)
            .execute()
        )
        return result.data[0]["value"] if result.data else None
    except Exception as exc:
        logger.warning("get_clinic_setting(%s, %s) error: %s", client_id, key, exc)
        return None


def get_all_clinic_settings(client_id: int) -> dict[str, str]:
    try:
        db = get_db()
        result = (
            db.table("clinic_settings")
            .select("key, value")
            .eq("client_id", client_id)
            .execute()
        )
        return {row["key"]: row["value"] for row in (result.data or [])}
    except Exception as exc:
        logger.warning("get_all_clinic_settings(%s) error: %s", client_id, exc)
        return {}


def update_clinic_setting(client_id: int, key: str, value: str) -> None:
    db = get_db()
    db.table("clinic_settings").upsert(
        {"client_id": client_id, "key": key, "value": value, "updated_at": _now()},
        on_conflict="client_id,key",
    ).execute()
    logger.info("Clinic setting updated (client=%s): %s = %s", client_id, key, value)


# ══════════════════════════════════════════════════════════════════════════════
# CLINIC NOTES
# ══════════════════════════════════════════════════════════════════════════════

def add_clinic_note(client_id: int, note: str) -> dict:
    db = get_db()
    result = db.table("clinic_notes").insert(
        {"client_id": client_id, "note": note}
    ).execute()
    return result.data[0] if result.data else {}


def get_clinic_notes(client_id: int) -> list[dict]:
    try:
        db = get_db()
        result = (
            db.table("clinic_notes")
            .select("id, note")
            .eq("client_id", client_id)
            .order("created_at", desc=False)
            .execute()
        )
        return result.data or []
    except Exception as exc:
        logger.warning("get_clinic_notes(%s) error: %s", client_id, exc)
        return []


def remove_clinic_note(client_id: int, note_id: int) -> bool:
    db = get_db()
    result = (
        db.table("clinic_notes")
        .delete()
        .eq("client_id", client_id)
        .eq("id", note_id)
        .execute()
    )
    return bool(result.data)


# ══════════════════════════════════════════════════════════════════════════════
# CUSTOM SCHEDULE
# ══════════════════════════════════════════════════════════════════════════════

def set_custom_schedule(
    client_id: int,
    date: str,
    morning_start: str | None = None,
    morning_end: str | None = None,
    evening_start: str | None = None,
    evening_end: str | None = None,
    slot_duration_min: int = 30,
    note: str = "",
) -> dict:
    db = get_db()
    payload = {
        "client_id": client_id,
        "schedule_date": date,
        "morning_start": morning_start,
        "morning_end": morning_end,
        "evening_start": evening_start,
        "evening_end": evening_end,
        "slot_duration_min": slot_duration_min,
        "note": note,
    }
    result = db.table("custom_schedule").upsert(
        payload, on_conflict="client_id,schedule_date"
    ).execute()
    logger.info("Custom schedule set (client=%s) for %s: %s", client_id, date, payload)
    return result.data[0] if result.data else {}


def get_custom_schedule(client_id: int, date: str) -> dict | None:
    try:
        db = get_db()
        result = (
            db.table("custom_schedule")
            .select("*")
            .eq("client_id", client_id)
            .eq("schedule_date", date)
            .limit(1)
            .execute()
        )
        return result.data[0] if result.data else None
    except Exception as exc:
        logger.warning("get_custom_schedule(%s, %s) error: %s", client_id, date, exc)
        return None


def clear_custom_schedule(client_id: int, date: str) -> bool:
    db = get_db()
    result = (
        db.table("custom_schedule")
        .delete()
        .eq("client_id", client_id)
        .eq("schedule_date", date)
        .execute()
    )
    return bool(result.data)


# ══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_appt_datetime(appt: dict) -> datetime | None:
    try:
        return datetime.strptime(
            f"{appt['appointment_date']} {appt['slot_time']}", "%Y-%m-%d %H:%M"
        ).replace(tzinfo=timezone.utc)
    except Exception:
        return None
