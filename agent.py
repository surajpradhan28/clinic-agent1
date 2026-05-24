"""
agent.py — OpenAI GPT-4o-mini conversation engine with function calling (multi-tenant v4).

Every public entry point now accepts a `client` dict (the full clients table row).
The client dict drives:
  - Which DB rows to read/write (client["id"])
  - Which features are available (client["plan"])
  - Doctor mode detection (client["contact_phone"])
  - Clinic info (read from clinic_settings for this client)
  - Which WhatsApp phone_id to send replies from (client["whatsapp_phone_id"])
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from openai import AsyncOpenAI

import database as db
import whatsapp
from config import settings

logger = logging.getLogger(__name__)

_openai = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)


# ── Slot utilities ────────────────────────────────────────────────────────────

def _generate_slots_from_ranges(
    ranges: list[tuple[str | None, str | None]],
    duration_min: int,
) -> list[str]:
    slots = []
    for start_str, end_str in ranges:
        if not start_str or not end_str:
            continue
        h, m   = map(int, start_str.split(":"))
        eh, em = map(int, end_str.split(":"))
        current = datetime(2000, 1, 1, h, m)
        end_dt  = datetime(2000, 1, 1, eh, em)
        while current < end_dt:
            slots.append(current.strftime("%H:%M"))
            current += timedelta(minutes=duration_min)
    return slots


def _generate_default_slots() -> list[str]:
    return _generate_slots_from_ranges(
        [
            (settings.MORNING_START, settings.MORNING_END),
            (settings.EVENING_START, settings.EVENING_END),
        ],
        settings.SLOT_DURATION_MIN,
    )


_DEFAULT_SLOTS = _generate_default_slots()


def _get_slots_for_date(client_id: int, date: str) -> list[str]:
    custom = db.get_custom_schedule(client_id, date)
    if custom:
        duration = custom.get("slot_duration_min") or settings.SLOT_DURATION_MIN
        return _generate_slots_from_ranges(
            [
                (custom.get("morning_start"), custom.get("morning_end")),
                (custom.get("evening_start"), custom.get("evening_end")),
            ],
            duration,
        )
    return _DEFAULT_SLOTS


def _get_clinic_info(client_id: int) -> dict[str, str]:
    db_settings = db.get_all_clinic_settings(client_id)
    return {
        "clinic_name":        db_settings.get("clinic_name")        or settings.CLINIC_NAME,
        "doctor_name":        db_settings.get("doctor_name")         or settings.DOCTOR_NAME,
        "clinic_address":     db_settings.get("clinic_address")      or settings.CLINIC_ADDRESS,
        "clinic_phone":       db_settings.get("clinic_phone")        or "",
        "google_review_link": db_settings.get("google_review_link")  or settings.GOOGLE_REVIEW_LINK,
    }


# ── OpenAI Tool Definitions ───────────────────────────────────────────────────

_TOOLS_BASE = [
    {
        "type": "function",
        "function": {
            "name": "check_available_slots",
            "description": (
                "Check which appointment slots are available on a specific date. "
                "Call this when the patient asks about available times or wants to book."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "Date in YYYY-MM-DD format"}
                },
                "required": ["date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_appointment",
            "description": "Book an appointment after the patient confirms a specific slot.",
            "parameters": {
                "type": "object",
                "properties": {
                    "patient_name": {"type": "string"},
                    "date":         {"type": "string", "description": "YYYY-MM-DD"},
                    "slot_time":    {"type": "string", "description": "HH:MM"},
                },
                "required": ["patient_name", "date", "slot_time"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_clinic_info",
            "description": "Get clinic name, doctor name, address, and working hours.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
]

_TOOLS_PRO = [
    {
        "type": "function",
        "function": {
            "name": "get_my_appointment",
            "description": "Look up the patient's next upcoming confirmed appointment.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_appointment",
            "description": (
                "Cancel the patient's appointment. Always call get_my_appointment first. "
                "Ask the patient to confirm before calling this."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "appointment_id": {"type": "integer"}
                },
                "required": ["appointment_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reschedule_appointment",
            "description": (
                "Reschedule the patient's appointment. Call get_my_appointment first, "
                "then check_available_slots, confirm with patient, then call this."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "appointment_id": {"type": "integer"},
                    "new_date":       {"type": "string", "description": "YYYY-MM-DD"},
                    "new_slot":       {"type": "string", "description": "HH:MM"},
                },
                "required": ["appointment_id", "new_date", "new_slot"],
            },
        },
    },
]


def _get_patient_tools(plan: str) -> list[dict]:
    tier = plan.lower()
    if tier in ("pro", "suite"):
        return _TOOLS_BASE + _TOOLS_PRO
    return _TOOLS_BASE


_TOOLS_DOCTOR = [
    {
        "type": "function",
        "function": {
            "name": "check_available_slots",
            "description": "Check available appointment slots for a date.",
            "parameters": {
                "type": "object",
                "properties": {"date": {"type": "string", "description": "YYYY-MM-DD"}},
                "required": ["date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "view_appointments",
            "description": "View all confirmed appointments for a specific date.",
            "parameters": {
                "type": "object",
                "properties": {"date": {"type": "string", "description": "YYYY-MM-DD"}},
                "required": ["date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "block_slots",
            "description": (
                "Block appointment slots on a date. "
                "Use slot_times=['all'] to block entire day."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "date":       {"type": "string"},
                    "slot_times": {"type": "array", "items": {"type": "string"}},
                    "reason":     {"type": "string"},
                },
                "required": ["date", "slot_times"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "unblock_slots",
            "description": "Remove blocks from slots. Use slot_times=['all'] for entire day.",
            "parameters": {
                "type": "object",
                "properties": {
                    "date":       {"type": "string"},
                    "slot_times": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["date", "slot_times"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "view_blocked_slots",
            "description": "View which slots are blocked on a specific date.",
            "parameters": {
                "type": "object",
                "properties": {"date": {"type": "string"}},
                "required": ["date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_clinic_info",
            "description": "Update clinic name, address, phone number, or doctor name.",
            "parameters": {
                "type": "object",
                "properties": {
                    "field": {
                        "type": "string",
                        "enum": ["clinic_name", "doctor_name", "clinic_address", "clinic_phone", "google_review_link"],
                    },
                    "value": {"type": "string"},
                },
                "required": ["field", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "view_clinic_info",
            "description": "View current clinic details.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_clinic_note",
            "description": "Add a custom rule/instruction the patient AI assistant will follow.",
            "parameters": {
                "type": "object",
                "properties": {"note": {"type": "string"}},
                "required": ["note"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_clinic_notes",
            "description": "List all custom notes loaded into the AI assistant.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remove_clinic_note",
            "description": "Remove a custom note by its ID.",
            "parameters": {
                "type": "object",
                "properties": {"note_id": {"type": "integer"}},
                "required": ["note_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_day_schedule",
            "description": "Set custom clinic hours for a specific date.",
            "parameters": {
                "type": "object",
                "properties": {
                    "date":             {"type": "string"},
                    "morning_start":    {"type": "string"},
                    "morning_end":      {"type": "string"},
                    "evening_start":    {"type": "string"},
                    "evening_end":      {"type": "string"},
                    "slot_duration_min":{"type": "integer"},
                    "note":             {"type": "string"},
                },
                "required": ["date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "clear_day_schedule",
            "description": "Remove custom schedule for a date, reverting to default hours.",
            "parameters": {
                "type": "object",
                "properties": {"date": {"type": "string"}},
                "required": ["date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "broadcast_message",
            "description": (
                "Send a WhatsApp message to ALL registered patients of this clinic. "
                "Use for announcements, holiday notices, health tips, etc. "
                "Always confirm the message text before sending."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {
                        "type": "string",
                        "description": "The message to send to all patients.",
                    }
                },
                "required": ["message"],
            },
        },
    },
]


# ── Patient function execution ─────────────────────────────────────────────────

async def _execute_function(
    fn_name: str, fn_args: dict, phone: str, client: dict
) -> tuple[str, dict | None]:
    client_id = client["id"]

    if fn_name == "check_available_slots":
        date = fn_args.get("date", "")
        try:
            day_name = datetime.strptime(date, "%Y-%m-%d").strftime("%A")
            if day_name in settings.WEEKLY_OFF_DAYS:
                return json.dumps({
                    "date": date, "morning_slots": [], "evening_slots": [],
                    "total_available": 0, "note": f"Clinic is closed on {day_name}s.",
                }), None
        except ValueError:
            pass

        booked   = db.get_booked_slots(client_id, date)
        blocked  = db.get_blocked_slot_times(client_id, date)
        all_slots = _get_slots_for_date(client_id, date)

        if "all" in blocked:
            available = []
        else:
            available = [s for s in all_slots if s not in booked and s not in blocked]

        IST = timezone(timedelta(hours=5, minutes=30))
        today_ist = datetime.now(IST).strftime("%Y-%m-%d")
        if date == today_ist:
            now_hhmm  = datetime.now(IST).strftime("%H:%M")
            available = [s for s in available if s > now_hhmm]

        morning = [s for s in available if int(s.split(":")[0]) < 14]
        evening = [s for s in available if int(s.split(":")[0]) >= 14]
        return json.dumps({
            "date": date, "morning_slots": morning,
            "evening_slots": evening, "total_available": len(available),
        }), None

    elif fn_name == "create_appointment":
        patient_name = fn_args.get("patient_name", "Patient")
        date         = fn_args.get("date", "")
        slot_time    = fn_args.get("slot_time", "")
        try:
            appt = db.create_appointment(client_id, phone, patient_name, date, slot_time)
            return json.dumps({
                "success": True, "appointment_id": appt["id"],
                "patient_name": patient_name, "date": date, "slot_time": slot_time,
                "message": f"Appointment confirmed for {patient_name} on {date} at {slot_time}",
            }), appt
        except Exception as exc:
            logger.error("create_appointment error: %s", exc)
            return json.dumps({"success": False, "error": str(exc)}), None

    elif fn_name == "get_clinic_info":
        info = _get_clinic_info(client_id)
        return json.dumps({
            "clinic_name":       info["clinic_name"],
            "doctor_name":       info["doctor_name"],
            "address":           info["clinic_address"],
            "clinic_phone":      info["clinic_phone"],
            "morning_hours":     f"{settings.MORNING_START} – {settings.MORNING_END}",
            "evening_hours":     f"{settings.EVENING_START} – {settings.EVENING_END}",
            "slot_duration_min": settings.SLOT_DURATION_MIN,
        }), None

    elif fn_name == "get_my_appointment":
        appt = db.get_upcoming_appointment(client_id, phone)
        if appt:
            return json.dumps({
                "found": True, "appointment_id": appt["id"],
                "date": appt["appointment_date"], "slot_time": appt["slot_time"],
                "patient_name": appt["patient_name"], "status": appt["status"],
            }), None
        return json.dumps({"found": False, "message": "No upcoming appointment found."}), None

    elif fn_name == "cancel_appointment":
        appt_id = fn_args.get("appointment_id")
        if not appt_id:
            return json.dumps({"success": False, "error": "appointment_id required"}), None
        try:
            db.cancel_appointment(int(appt_id))
            return json.dumps({"success": True, "message": f"Appointment {appt_id} cancelled."}), None
        except Exception as exc:
            return json.dumps({"success": False, "error": str(exc)}), None

    elif fn_name == "reschedule_appointment":
        appt_id  = fn_args.get("appointment_id")
        new_date = fn_args.get("new_date", "")
        new_slot = fn_args.get("new_slot", "")
        if not all([appt_id, new_date, new_slot]):
            return json.dumps({"success": False, "error": "appointment_id, new_date and new_slot required"}), None
        try:
            cur = db.get_upcoming_appointment(client_id, phone)
            patient_name = cur["patient_name"] if cur else "Patient"
            new_appt = db.reschedule_appointment(client_id, int(appt_id), phone, patient_name, new_date, new_slot)
            return json.dumps({
                "success": True, "new_appointment_id": new_appt["id"],
                "patient_name": patient_name, "new_date": new_date, "new_slot": new_slot,
                "message": f"Rescheduled to {new_date} at {new_slot}.",
            }), new_appt
        except Exception as exc:
            return json.dumps({"success": False, "error": str(exc)}), None

    return json.dumps({"error": f"Unknown function: {fn_name}"}), None


# ── Doctor function execution ─────────────────────────────────────────────────

async def _execute_doctor_function(fn_name: str, fn_args: dict, client: dict) -> str:
    client_id    = client["id"]
    client_pid   = client.get("whatsapp_phone_id") or settings.WHATSAPP_PHONE_ID
    client_token = client.get("whatsapp_token") or None

    if fn_name == "check_available_slots":
        result_str, _ = await _execute_function(fn_name, fn_args, "", client)
        return result_str

    elif fn_name == "view_appointments":
        date = fn_args.get("date", "")
        appointments = db.get_appointments_for_date(client_id, date)
        if not appointments:
            return json.dumps({"date": date, "count": 0, "appointments": [],
                               "message": "No confirmed appointments for this date."})
        return json.dumps({"date": date, "count": len(appointments), "appointments": appointments})

    elif fn_name == "block_slots":
        date       = fn_args.get("date", "")
        slot_times = fn_args.get("slot_times", [])
        reason     = fn_args.get("reason", "")
        if not date or not slot_times:
            return json.dumps({"success": False, "error": "date and slot_times required"})
        try:
            affected = (
                db.get_all_appointments_for_date_full(client_id, date)
                if "all" in slot_times
                else db.get_appointments_in_slots(client_id, date, slot_times)
            )
            count = db.block_slots(client_id, date, slot_times, reason)
            info  = _get_clinic_info(client_id)
            notified = []
            for appt in affected:
                try:
                    db.cancel_appointment(appt["id"])
                    try:
                        date_display = datetime.strptime(date, "%Y-%m-%d").strftime("%A, %d %B %Y")
                    except Exception:
                        date_display = date
                    msg = (
                        f"⚠️ *Appointment Cancelled*\n\n"
                        f"Dear {appt['patient_name']}, your appointment at "
                        f"*{info['clinic_name']}* on *{date_display}* at *{appt['slot_time']}* "
                        f"has been cancelled by the clinic.\n\n"
                        f"We apologise for the inconvenience. Please reply *appointment* "
                        f"to book a new slot. 🙏"
                    )
                    await whatsapp.send_text(appt["patient_phone"], msg, phone_id=client_pid, token=client_token)
                    notified.append(appt["patient_name"])
                except Exception as notify_exc:
                    logger.error("Failed to notify %s: %s", appt.get("patient_phone"), notify_exc)

            label     = "Entire day" if "all" in slot_times else f"{count} slot(s) ({', '.join(slot_times)})"
            notif_msg = f" {len(notified)} patient(s) notified and appointments cancelled." if notified else ""
            return json.dumps({
                "success": True, "date": date, "blocked": slot_times,
                "patients_notified": notified,
                "message": f"{label} blocked on {date}.{notif_msg}",
            })
        except Exception as exc:
            logger.error("block_slots error: %s", exc)
            return json.dumps({"success": False, "error": str(exc)})

    elif fn_name == "unblock_slots":
        date       = fn_args.get("date", "")
        slot_times = fn_args.get("slot_times", [])
        if not date or not slot_times:
            return json.dumps({"success": False, "error": "date and slot_times required"})
        try:
            count = db.unblock_slots(client_id, date, slot_times)
            label = "All blocks removed" if "all" in slot_times else f"{count} slot(s) unblocked"
            return json.dumps({"success": True, "date": date,
                               "message": f"{label} on {date}. Slots are now available."})
        except Exception as exc:
            return json.dumps({"success": False, "error": str(exc)})

    elif fn_name == "view_blocked_slots":
        date = fn_args.get("date", "")
        rows = db.get_blocked_slots_detail(client_id, date)
        if not rows:
            return json.dumps({"date": date, "blocked": [], "message": "No slots blocked."})
        if any(r["slot_time"] is None for r in rows):
            return json.dumps({"date": date, "blocked": ["all"], "message": f"Entire day blocked on {date}."})
        return json.dumps({
            "date": date,
            "blocked": [r["slot_time"] for r in rows],
            "reasons": list({r.get("reason", "") for r in rows if r.get("reason")}),
        })

    elif fn_name == "update_clinic_info":
        field = fn_args.get("field", "")
        value = fn_args.get("value", "").strip()
        allowed = {"clinic_name", "doctor_name", "clinic_address", "clinic_phone", "google_review_link"}
        if field not in allowed:
            return json.dumps({"success": False, "error": f"Unknown field '{field}'"})
        if not value:
            return json.dumps({"success": False, "error": "Value cannot be empty"})
        try:
            db.update_clinic_setting(client_id, field, value)
            return json.dumps({
                "success": True, "field": field, "new_value": value,
                "message": f"{field.replace('_',' ').title()} updated: {value} ✅",
            })
        except Exception as exc:
            return json.dumps({"success": False, "error": str(exc)})

    elif fn_name == "view_clinic_info":
        info = _get_clinic_info(client_id)
        return json.dumps({
            "clinic_name":       info["clinic_name"],
            "doctor_name":       info["doctor_name"],
            "clinic_address":    info["clinic_address"],
            "clinic_phone":      info["clinic_phone"],
            "google_review_link":info["google_review_link"],
            "morning_hours":     f"{settings.MORNING_START} – {settings.MORNING_END}",
            "evening_hours":     f"{settings.EVENING_START} – {settings.EVENING_END}",
        })

    elif fn_name == "add_clinic_note":
        note = fn_args.get("note", "").strip()
        if not note:
            return json.dumps({"success": False, "error": "Note cannot be empty"})
        try:
            row = db.add_clinic_note(client_id, note)
            return json.dumps({
                "success": True, "id": row.get("id"),
                "message": f"Note added (ID {row.get('id')}). AI will follow this rule ✅",
            })
        except Exception as exc:
            return json.dumps({"success": False, "error": str(exc)})

    elif fn_name == "list_clinic_notes":
        notes = db.get_clinic_notes(client_id)
        if not notes:
            return json.dumps({"notes": [], "message": "No custom notes added yet."})
        return json.dumps({"count": len(notes), "notes": notes})

    elif fn_name == "remove_clinic_note":
        note_id = fn_args.get("note_id")
        if note_id is None:
            return json.dumps({"success": False, "error": "note_id required"})
        try:
            deleted = db.remove_clinic_note(client_id, int(note_id))
            if deleted:
                return json.dumps({"success": True, "message": f"Note {note_id} removed ✅"})
            return json.dumps({"success": False, "message": f"Note {note_id} not found"})
        except Exception as exc:
            return json.dumps({"success": False, "error": str(exc)})

    elif fn_name == "set_day_schedule":
        date = fn_args.get("date", "")
        if not date:
            return json.dumps({"success": False, "error": "date required"})
        try:
            db.set_custom_schedule(
                client_id=client_id,
                date=date,
                morning_start=fn_args.get("morning_start") or None,
                morning_end=fn_args.get("morning_end") or None,
                evening_start=fn_args.get("evening_start") or None,
                evening_end=fn_args.get("evening_end") or None,
                slot_duration_min=fn_args.get("slot_duration_min") or settings.SLOT_DURATION_MIN,
                note=fn_args.get("note") or "",
            )
            slots = _get_slots_for_date(client_id, date)
            return json.dumps({
                "success": True, "date": date,
                "slots_generated": slots,
                "message": f"Custom schedule set for {date}. {len(slots)} slots available ✅",
            })
        except Exception as exc:
            return json.dumps({"success": False, "error": str(exc)})

    elif fn_name == "clear_day_schedule":
        date = fn_args.get("date", "")
        if not date:
            return json.dumps({"success": False, "error": "date required"})
        try:
            cleared = db.clear_custom_schedule(client_id, date)
            msg = (
                f"Custom schedule removed for {date}. Reverted to default clinic hours ✅"
                if cleared else f"No custom schedule found for {date}."
            )
            return json.dumps({"success": True, "message": msg})
        except Exception as exc:
            return json.dumps({"success": False, "error": str(exc)})

    elif fn_name == "broadcast_message":
        message = fn_args.get("message", "").strip()
        if not message:
            return json.dumps({"success": False, "error": "Message cannot be empty"})
        try:
            phones = db.get_all_patient_phones(client_id)
            if not phones:
                return json.dumps({"success": False, "message": "No registered patients found to broadcast to."})
            sent_count = 0
            failed_count = 0
            for patient_phone in phones:
                try:
                    await whatsapp.send_text(patient_phone, message, phone_id=client_pid, token=client_token)
                    sent_count += 1
                except Exception as bc_exc:
                    logger.error("Broadcast failed for %s: %s", patient_phone, bc_exc)
                    failed_count += 1
            result_msg = f"✅ Broadcast sent to {sent_count} patient(s)."
            if failed_count:
                result_msg += f" ⚠️ Failed for {failed_count} patient(s)."
            return json.dumps({
                "success": True,
                "total_patients": len(phones),
                "sent": sent_count,
                "failed": failed_count,
                "message": result_msg,
            })
        except Exception as exc:
            logger.error("broadcast_message error: %s", exc)
            return json.dumps({"success": False, "error": str(exc)})

    return json.dumps({"error": f"Unknown doctor function: {fn_name}"})


# ── System prompts ────────────────────────────────────────────────────────────

def _build_system_prompt(client: dict) -> str:
    today     = datetime.now(timezone.utc).strftime("%A, %d %B %Y")
    client_id = client["id"]
    plan      = client.get("plan", "starter").lower()

    info           = _get_clinic_info(client_id)
    clinic_name    = info["clinic_name"]
    doctor_name    = info["doctor_name"]
    clinic_address = info["clinic_address"]
    clinic_phone   = info["clinic_phone"]
    phone_line     = f"\n- Phone: {clinic_phone}" if clinic_phone else ""

    cancel_reschedule_rules = ""
    if plan in ("pro", "suite"):
        cancel_reschedule_rules = """
- If a patient wants to CANCEL: call get_my_appointment first, show details, ask to confirm, then call cancel_appointment.
- If a patient wants to RESCHEDULE: call get_my_appointment, ask preferred new date, check_available_slots, let them pick, confirm, then call reschedule_appointment.
- Always confirm before cancelling or rescheduling — these are irreversible.
"""

    notes = db.get_clinic_notes(client_id)
    custom_notes_section = ""
    if notes:
        note_lines = "\n".join(f"- {n['note']}" for n in notes)
        custom_notes_section = f"\n\nClinic-specific rules (follow strictly):\n{note_lines}"

    return f"""You are Meera, a warm and professional appointment assistant for {clinic_name} (run by {doctor_name}).

Today's date is {today}.

Clinic details:
- Name: {clinic_name}
- Doctor: {doctor_name}
- Address: {clinic_address}{phone_line}
- Morning hours: {settings.MORNING_START} – {settings.MORNING_END}
- Evening hours: {settings.EVENING_START} – {settings.EVENING_END}

Your job is to help patients:
1. Book appointments
2. Answer questions about the clinic (timings, address, doctor)
3. Handle general health-related queries politely (do NOT give medical advice)
4. Help patients cancel or reschedule their appointments (if available)

Guidelines:
- Be warm, concise, and helpful. Use a friendly Indian conversational tone.
- Keep replies short — max 3-4 sentences unless listing slots.
- Always ask for the patient's name if you don't have it.
- When a patient wants to book, use check_available_slots, then present the slots.
- After the patient selects a slot, use create_appointment to confirm.
- After booking, confirm the date, time, and clinic address. A separate confirmation card will also be sent.
- If no slots are available, suggest nearby dates.
- Do NOT make up appointment times — always check_available_slots first.
- For anything medical (diagnosis, medicines, dosage), say "Please consult {doctor_name} during your appointment."
- Respond in the same language the patient uses (Hindi or English).
{cancel_reschedule_rules}{custom_notes_section}"""


def _build_doctor_prompt(client: dict) -> str:
    today     = datetime.now(timezone.utc).strftime("%A, %d %B %Y")
    client_id = client["id"]
    info      = _get_clinic_info(client_id)
    off_days  = ", ".join(settings.WEEKLY_OFF_DAYS) if settings.WEEKLY_OFF_DAYS else "None"
    return f"""You are a schedule assistant for {info['doctor_name']} at {info['clinic_name']}.
Today is {today}.

You help the doctor manage their clinic schedule. Available actions:
- Block / unblock slots or entire days
- View appointments and blocked slots for any date
- Check available slots
- Update clinic info (name, address, phone, doctor name)
- Add / list / remove custom AI knowledge notes
- Set custom clinic hours for a specific date
- Broadcast a message to ALL registered patients (holiday notice, health tips, announcements)

Default clinic hours:
  Morning : {settings.MORNING_START} – {settings.MORNING_END}
  Evening : {settings.EVENING_START} – {settings.EVENING_END}
  Slot gap: {settings.SLOT_DURATION_MIN} minutes
  Weekly off: {off_days}

Current clinic info:
  Name   : {info['clinic_name']}
  Address: {info['clinic_address']}
  Phone  : {info['clinic_phone'] or 'Not set'}

Rules:
- Be brief and direct — the doctor is busy.
- Always confirm what was done with a clear summary.
- "Block all day" → block_slots with slot_times=["all"].
- When blocking slots with existing appointments, patients are auto-notified and appointments cancelled.
- Use emojis sparingly for clarity (✅ ❌ 🚫)."""


# ── Doctor mode detection ─────────────────────────────────────────────────────

def _is_doctor(phone: str, client: dict) -> bool:
    doctor_phone = (client.get("contact_phone") or "").strip() or settings.DOCTOR_PHONE
    if not doctor_phone:
        return False
    clean = lambda p: p.lstrip("+").lstrip("0")
    return clean(phone) == clean(doctor_phone) or clean(phone).endswith(clean(doctor_phone))


# ── Doctor reply flow ─────────────────────────────────────────────────────────

async def _get_doctor_reply(phone: str, user_text: str, client: dict) -> tuple[str, dict | None]:
    client_id = client["id"]
    history   = db.get_conversation_history(client_id, phone, limit=6)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": _build_doctor_prompt(client)},
        *history,
        {"role": "user", "content": user_text},
    ]

    response = await _openai.chat.completions.create(
        model=settings.OPENAI_MODEL, messages=messages,
        tools=_TOOLS_DOCTOR, tool_choice="auto",
        max_tokens=400, temperature=0.3,
    )
    choice = response.choices[0]

    while choice.finish_reason == "tool_calls":
        tool_calls = choice.message.tool_calls or []
        messages.append(choice.message)
        tool_results = []
        for tc in tool_calls:
            fn_name = tc.function.name
            try:
                fn_args = json.loads(tc.function.arguments)
            except json.JSONDecodeError:
                fn_args = {}
            logger.info("[DOCTOR] calling %s(%s) for client=%s", fn_name, fn_args, client_id)
            fn_result = await _execute_doctor_function(fn_name, fn_args, client)
            tool_results.append({"role": "tool", "tool_call_id": tc.id, "content": fn_result})

        messages.extend(tool_results)
        response = await _openai.chat.completions.create(
            model=settings.OPENAI_MODEL, messages=messages,
            tools=_TOOLS_DOCTOR, tool_choice="auto",
            max_tokens=400, temperature=0.3,
        )
        choice = response.choices[0]

    reply_text = choice.message.content or "Done."
    db.save_message(client_id, phone, "user", user_text)
    db.save_message(client_id, phone, "assistant", reply_text)
    return reply_text, None


# ── Main agent entry point ─────────────────────────────────────────────────────

async def get_agent_reply(phone: str, user_text: str, client: dict) -> tuple[str, dict | None]:
    """
    Process a message and return (reply_text, appointment_row_or_None).
    client: full row from the clients table (resolved in main.py).
    Never raises — always returns a reply string.
    """
    try:
        return await _get_agent_reply_inner(phone, user_text, client)
    except Exception as exc:
        logger.error("get_agent_reply unexpected error (client=%s): %s", client.get("id"), exc, exc_info=True)
        if _is_doctor(phone, client):
            return "⚠️ Something went wrong on our end. Please try again.", None
        return "Sorry, I'm having a little trouble right now. Please try again shortly. 😊", None


async def _get_agent_reply_inner(phone: str, user_text: str, client: dict) -> tuple[str, dict | None]:
    client_id = client["id"]

    if _is_doctor(phone, client):
        return await _get_doctor_reply(phone, user_text, client)

    history = db.get_conversation_history(client_id, phone, limit=8)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": _build_system_prompt(client)},
        *history,
        {"role": "user", "content": user_text},
    ]

    active_tools = _get_patient_tools(client.get("plan", "starter"))
    response = await _openai.chat.completions.create(
        model=settings.OPENAI_MODEL, messages=messages,
        tools=active_tools, tool_choice="auto",
        max_tokens=500, temperature=0.7,
    )

    choice   = response.choices[0]
    appt_row = None

    while choice.finish_reason == "tool_calls":
        tool_calls = choice.message.tool_calls or []
        messages.append(choice.message)
        tool_results = []
        for tc in tool_calls:
            fn_name = tc.function.name
            try:
                fn_args = json.loads(tc.function.arguments)
            except json.JSONDecodeError:
                fn_args = {}
            logger.info("AI calling %s(%s) for client=%s", fn_name, fn_args, client_id)
            fn_result, maybe_appt = await _execute_function(fn_name, fn_args, phone, client)
            if maybe_appt:
                appt_row = maybe_appt
            tool_results.append({"role": "tool", "tool_call_id": tc.id, "content": fn_result})

        messages.extend(tool_results)
        response = await _openai.chat.completions.create(
            model=settings.OPENAI_MODEL, messages=messages,
            tools=active_tools, tool_choice="auto",
            max_tokens=500, temperature=0.7,
        )
        choice = response.choices[0]

    reply_text = choice.message.content or "Sorry, I didn't understand that. Could you please repeat?"
    db.save_message(client_id, phone, "user", user_text)
    db.save_message(client_id, phone, "assistant", reply_text)
    return reply_text, appt_row
