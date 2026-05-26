"""
database.py — Supabase client + all database operations (multi-tenant v4).

Every data function takes a client_id: int as its first argument.
The top-level helpers (get_client_by_phone_id, get_all_active_clients, etc.)
are tenant-unaware — used by the webhook router and scheduler.

Tables:
  clients | subscriptions | payments
  patients | conversations | appointments | followups | review_requests
  blocked_slots | clinic_settings | clinic_notes | custom_schedule
  waitlist | patient_intake
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

# Indian Standard Time — all appointment slot times are stored in IST
_IST = timezone(timedelta(hours=5, minutes=30))
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


def get_client_by_dashboard_key(key: str) -> dict | None:
    """
    Look up a client by their per-clinic dashboard key.
    Used by GET /clinic?key=<key> to authenticate and scope the dashboard.
    Returns None if the key is blank or doesn't match any client.
    """
    if not key:
        return None
    db = get_db()
    result = (
        db.table("clients")
        .select("*")
        .eq("dashboard_key", key)
        .limit(1)
        .execute()
    )
    return result.data[0] if result.data else None


def create_clinic_client(
    name: str,
    doctor_name: str,
    whatsapp_phone_id: str,
    contact_phone: str = "",
    email: str = "",
    plan: str = "starter",
    trial_days: int = 14,
    whatsapp_token: str = "",
) -> dict:
    """Onboard a new clinic. Returns the new client row."""
    from datetime import date, timedelta
    db = get_db()
    trial_end = (date.today() + timedelta(days=trial_days)).isoformat()
    row: dict = {
        "name": name,
        "doctor_name": doctor_name,
        "whatsapp_phone_id": whatsapp_phone_id,
        "contact_phone": contact_phone,
        "email": email,
        "plan": plan,
        "status": "trial",
        "trial_ends_at": trial_end,
    }
    if whatsapp_token:
        row["whatsapp_token"] = whatsapp_token
    result = (
        db.table("clients")
        .insert(row)
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


def get_all_patient_phones(client_id: int) -> list[str]:
    """Return list of all patient phone numbers for a clinic."""
    db = get_db()
    result = (
        db.table("patients")
        .select("phone")
        .eq("client_id", client_id)
        .execute()
    )
    return [row["phone"] for row in (result.data or [])]


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

    # ── Double-booking guard ──────────────────────────────────────────────────
    # Check if this slot is already taken before inserting.
    # This prevents race conditions where two patients book the same slot
    # simultaneously and the AI hasn't refreshed its available-slots cache.
    conflict = (
        db.table("appointments")
        .select("id")
        .eq("client_id", client_id)
        .eq("appointment_date", appointment_date)
        .eq("slot_time", slot_time)
        .eq("status", "confirmed")
        .limit(1)
        .execute()
    )
    if conflict.data:
        raise ValueError(
            f"Slot {slot_time} on {appointment_date} is already booked. "
            "Please choose a different time."
        )

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
    today = datetime.now(_IST).strftime("%Y-%m-%d")  # Use IST date
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


def cancel_appointment(appt_id: int, client_id: int | None = None) -> None:
    """Cancel an appointment.  Pass client_id to enforce tenant ownership at DB level."""
    db = get_db()
    query = db.table("appointments").update(
        {"status": "cancelled", "cancelled_at": _now()}
    ).eq("id", appt_id)
    if client_id is not None:
        query = query.eq("client_id", client_id)
    query.execute()
    db.table("followups").update(
        {"status": "cancelled"}
    ).eq("appointment_id", appt_id).eq("status", "pending").execute()
    logger.info("Appointment %s cancelled (client=%s)", appt_id, client_id)


def reschedule_appointment(
    client_id: int,
    old_appt_id: int,
    phone: str,
    patient_name: str,
    new_date: str,
    new_slot: str,
) -> dict:
    cancel_appointment(old_appt_id, client_id=client_id)
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
    now = datetime.now(_IST)
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


def get_appointments_for_1h_reminder(client_id: int) -> list[dict]:
    """Return confirmed appointments whose slot is 50-70 min from now and 1h reminder not sent."""
    db = get_db()
    now          = datetime.now(_IST)  # Compare in IST — slots are IST
    window_start = now + timedelta(minutes=50)
    window_end   = now + timedelta(minutes=70)

    result = (
        db.table("appointments")
        .select("*")
        .eq("client_id", client_id)
        .eq("status", "confirmed")
        .eq("reminder_1h_sent", False)
        .execute()
    )
    due = []
    for appt in (result.data or []):
        appt_dt = _parse_appt_datetime(appt)
        if appt_dt and window_start <= appt_dt <= window_end:
            due.append(appt)
    return due


def mark_1h_reminder_sent(appt_id: int) -> None:
    db = get_db()
    db.table("appointments").update({"reminder_1h_sent": True}).eq("id", appt_id).execute()


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


# ══════════════════════════════════════════════════════════════════════════════
# CLINIC DASHBOARD QUERIES  (read-only, per-client)
# ══════════════════════════════════════════════════════════════════════════════

def get_appointments_range(client_id: int, date_from: str, date_to: str) -> list[dict]:
    """Return confirmed appointments between two dates (inclusive) for the dashboard."""
    db = get_db()
    result = (
        db.table("appointments")
        .select("id, patient_name, patient_phone, appointment_date, slot_time, status, created_at")
        .eq("client_id", client_id)
        .gte("appointment_date", date_from)
        .lte("appointment_date", date_to)
        .in_("status", ["confirmed", "completed", "cancelled"])
        .order("appointment_date", desc=False)
        .order("slot_time", desc=False)
        .execute()
    )
    return result.data or []


def get_dashboard_stats(client_id: int) -> dict:
    """Aggregate stats for the clinic dashboard:
      - month_appointments: confirmed + completed appointments this calendar month
      - today_appointments: confirmed appointments today (IST)
      - pending_followups: follow-ups in 'pending' or 'sent' state
    """
    db = get_db()
    today_ist = datetime.now(_IST).strftime("%Y-%m-%d")
    month_start = datetime.now(_IST).strftime("%Y-%m-01")

    # Total patients
    pat = db.table("patients").select("id", count="exact").eq("client_id", client_id).execute()
    total_patients = pat.count if pat.count is not None else len(pat.data or [])

    # This month's bookings (confirmed + completed)
    mo = (
        db.table("appointments")
        .select("id", count="exact")
        .eq("client_id", client_id)
        .gte("appointment_date", month_start)
        .in_("status", ["confirmed", "completed"])
        .execute()
    )
    month_appointments = mo.count if mo.count is not None else len(mo.data or [])

    # Today's confirmed
    tod = (
        db.table("appointments")
        .select("id", count="exact")
        .eq("client_id", client_id)
        .eq("appointment_date", today_ist)
        .eq("status", "confirmed")
        .execute()
    )
    today_appointments = tod.count if tod.count is not None else len(tod.data or [])

    # Pending follow-ups
    fu = (
        db.table("followups")
        .select("id", count="exact")
        .eq("client_id", client_id)
        .in_("status", ["pending", "sent"])
        .execute()
    )
    pending_followups = fu.count if fu.count is not None else len(fu.data or [])

    return {
        "total_patients": total_patients,
        "month_appointments": month_appointments,
        "today_appointments": today_appointments,
        "pending_followups": pending_followups,
    }


def get_recent_activity(client_id: int, limit: int = 20) -> list[dict]:
    """Return the most recent appointments (any status) for the activity feed."""
    db = get_db()
    result = (
        db.table("appointments")
        .select("id, client_id, patient_name, patient_phone, appointment_date, slot_time, status, created_at, cancelled_at")
        .eq("client_id", client_id)
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    return result.data or []


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
# WAITLIST
# ══════════════════════════════════════════════════════════════════════════════

def add_to_waitlist(
    client_id: int,
    patient_phone: str,
    patient_name: str,
    requested_date: str,
    requested_slot: str,
) -> dict:
    """Add a patient to the waitlist for a fully-booked slot (upsert = idempotent)."""
    db_c = get_db()
    result = (
        db_c.table("waitlist")
        .upsert(
            {
                "client_id":     client_id,
                "patient_phone": patient_phone,
                "patient_name":  patient_name,
                "requested_date": requested_date,
                "requested_slot": requested_slot,
            },
            on_conflict="client_id,patient_phone,requested_date,requested_slot",
        )
        .execute()
    )
    logger.info(
        "Waitlist: %s added for %s %s (client=%s)",
        patient_phone, requested_date, requested_slot, client_id,
    )
    return result.data[0] if result.data else {}


def get_waitlist_for_slot(client_id: int, date: str, slot_time: str) -> list[dict]:
    """Return all waitlist entries for a slot, oldest first (FIFO)."""
    db_c = get_db()
    result = (
        db_c.table("waitlist")
        .select("*")
        .eq("client_id", client_id)
        .eq("requested_date", date)
        .eq("requested_slot", slot_time)
        .order("created_at", desc=False)
        .execute()
    )
    return result.data or []


def pop_next_from_waitlist(client_id: int, date: str, slot_time: str) -> dict | None:
    """Return the first waiter for a slot and remove them from the queue."""
    entries = get_waitlist_for_slot(client_id, date, slot_time)
    if not entries:
        return None
    first = entries[0]
    get_db().table("waitlist").delete().eq("id", first["id"]).execute()
    logger.info(
        "Waitlist: popped %s for %s %s (client=%s)",
        first["patient_phone"], date, slot_time, client_id,
    )
    return first


def get_patient_waitlist(client_id: int, phone: str) -> list[dict]:
    """Return all active waitlist entries for a specific patient."""
    db_c = get_db()
    result = (
        db_c.table("waitlist")
        .select("*")
        .eq("client_id", client_id)
        .eq("patient_phone", phone)
        .order("requested_date", desc=False)
        .execute()
    )
    return result.data or []


# ══════════════════════════════════════════════════════════════════════════════
# PATIENT INTAKE FORM
# ══════════════════════════════════════════════════════════════════════════════

def save_patient_intake(
    client_id: int,
    patient_phone: str,
    appointment_id: int,
    age: int | None,
    gender: str | None,
    chief_complaint: str | None,
) -> dict:
    """Persist the new-patient intake collected through conversation."""
    db_c = get_db()
    result = (
        db_c.table("patient_intake")
        .insert({
            "client_id":       client_id,
            "patient_phone":   patient_phone,
            "appointment_id":  appointment_id,
            "age":             age,
            "gender":          gender,
            "chief_complaint": chief_complaint,
        })
        .execute()
    )
    logger.info(
        "Intake saved (client=%s, phone=%s, appt=%s)",
        client_id, patient_phone, appointment_id,
    )
    return result.data[0] if result.data else {}


def get_patient_intake(client_id: int, patient_phone: str) -> dict | None:
    """Return the most recent intake form for this patient, or None."""
    db_c = get_db()
    result = (
        db_c.table("patient_intake")
        .select("*")
        .eq("client_id", client_id)
        .eq("patient_phone", patient_phone)
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    return result.data[0] if result.data else None


def is_new_patient(client_id: int, phone: str, current_appt_id: int | None = None) -> bool:
    """
    True if this patient has no prior confirmed/completed appointments at this clinic.
    Pass current_appt_id to exclude the just-created booking from the count.
    """
    db_c = get_db()
    query = (
        db_c.table("appointments")
        .select("id", count="exact")
        .eq("client_id", client_id)
        .eq("patient_phone", phone)
        .in_("status", ["confirmed", "completed"])
    )
    if current_appt_id:
        query = query.neq("id", current_appt_id)
    result = query.execute()
    count = result.count if result.count is not None else len(result.data or [])
    return count == 0


def get_appointments_for_intake_preview(client_id: int) -> list[dict]:
    """
    Return confirmed appointments whose slot is 25-35 minutes from now
    and whose intake_preview_sent flag is False.
    """
    db_c = get_db()
    now          = datetime.now(_IST)
    window_start = now + timedelta(minutes=25)
    window_end   = now + timedelta(minutes=35)

    result = (
        db_c.table("appointments")
        .select("*")
        .eq("client_id", client_id)
        .eq("status", "confirmed")
        .eq("intake_preview_sent", False)
        .execute()
    )
    due = []
    for appt in (result.data or []):
        appt_dt = _parse_appt_datetime(appt)
        if appt_dt and window_start <= appt_dt <= window_end:
            due.append(appt)
    return due


def mark_intake_preview_sent(appt_id: int) -> None:
    get_db().table("appointments").update(
        {"intake_preview_sent": True}
    ).eq("id", appt_id).execute()


# ══════════════════════════════════════════════════════════════════════════════
# INVOICES
# ══════════════════════════════════════════════════════════════════════════════

def _get_next_invoice_number(client_id: int, year: int, month: int) -> str:
    """
    Return the next sequential invoice number for this client.
    Format: INV-{YYYY}-{MM}-{SEQ:03d}   e.g. INV-2025-06-001
    """
    db_c = get_db()
    result = (
        db_c.table("invoices")
        .select("id", count="exact")
        .eq("client_id", client_id)
        .execute()
    )
    seq = (result.count or 0) + 1
    return f"INV-{year:04d}-{month:02d}-{seq:03d}"


def create_invoice(
    client_id: int,
    period_start: str,   # YYYY-MM-DD (1st of month)
    period_end: str,     # YYYY-MM-DD (last of month)
    due_date: str,       # YYYY-MM-DD
    amount: float,
    plan: str,
    currency: str = "INR",
    notes: str = "",
) -> dict:
    """
    Create an invoice record. Returns the full row including token and number.
    Raises ValueError if an invoice already exists for this client+period.
    """
    db_c = get_db()
    year  = int(period_start[:4])
    month = int(period_start[5:7])
    inv_number = _get_next_invoice_number(client_id, year, month)

    result = (
        db_c.table("invoices")
        .insert({
            "client_id":    client_id,
            "invoice_number": inv_number,
            "period_start": period_start,
            "period_end":   period_end,
            "due_date":     due_date,
            "amount":       amount,
            "currency":     currency,
            "plan":         plan,
            "notes":        notes,
            "status":       "sent",
        })
        .execute()
    )
    row = result.data[0] if result.data else {}
    logger.info(
        "Invoice created: %s (client=%s, amount=%.2f %s)",
        inv_number, client_id, amount, currency,
    )
    return row


def get_invoice_by_token(token: str) -> dict | None:
    """Fetch an invoice by its URL token. Joins client info for display."""
    db_c = get_db()
    result = (
        db_c.table("invoices")
        .select("*, clients(clinic_name, contact_name, contact_phone, contact_email)")
        .eq("invoice_token", token)
        .limit(1)
        .execute()
    )
    return result.data[0] if result.data else None


def get_invoices_for_client(client_id: int, limit: int = 12) -> list[dict]:
    """Return the most recent invoices for a client (newest first)."""
    db_c = get_db()
    result = (
        db_c.table("invoices")
        .select("*")
        .eq("client_id", client_id)
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    return result.data or []


def mark_invoice_paid(invoice_id: int, client_id: int) -> bool:
    """Mark an invoice as paid. Returns True if found and updated."""
    db_c = get_db()
    result = (
        db_c.table("invoices")
        .update({"status": "paid", "paid_at": datetime.now(_IST).isoformat()})
        .eq("id", invoice_id)
        .eq("client_id", client_id)
        .execute()
    )
    return bool(result.data)


def mark_overdue_invoices() -> int:
    """
    Mark all 'sent' invoices past their due_date as 'overdue'.
    Returns the count of invoices updated.
    """
    db_c = get_db()
    today = datetime.now(_IST).strftime("%Y-%m-%d")
    result = (
        db_c.table("invoices")
        .update({"status": "overdue"})
        .eq("status", "sent")
        .lt("due_date", today)
        .execute()
    )
    count = len(result.data or [])
    if count:
        logger.info("Marked %d invoice(s) as overdue", count)
    return count


def invoice_exists(client_id: int, period_start: str) -> bool:
    """Return True if an invoice already exists for this client+period."""
    db_c = get_db()
    result = (
        db_c.table("invoices")
        .select("id", count="exact")
        .eq("client_id", client_id)
        .eq("period_start", period_start)
        .execute()
    )
    return (result.count or 0) > 0


# ══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_appt_datetime(appt: dict) -> datetime | None:
    """Parse appointment date+slot into an IST-aware datetime.
    Slot times are stored as local India time (IST = UTC+5:30).
    Attaching _IST makes comparisons with datetime.now(_IST) correct.
    """
    try:
        return datetime.strptime(
            f"{appt['appointment_date']} {appt['slot_time']}", "%Y-%m-%d %H:%M"
        ).replace(tzinfo=_IST)
    except Exception:
        return None


def count_appointments_since(client_id: int, since_date: str) -> int:
    """Return number of confirmed/completed appointments for client_id on or after since_date (YYYY-MM-DD)."""
    try:
        db = get_db()
        result = (
            db.table("appointments")
            .select("id", count="exact")
            .eq("client_id", client_id)
            .in_("status", ["confirmed", "completed"])
            .gte("appointment_date", since_date)
            .execute()
        )
        return result.count or 0
    except Exception:
        return 0
