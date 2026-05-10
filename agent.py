"""
agent.py — OpenAI GPT-4o-mini conversation engine with function calling.

The AI is given 3 tools it can decide to call:
  1. check_available_slots(date)       → returns available time slots
  2. create_appointment(...)           → books the appointment in Supabase
  3. get_clinic_info()                 → returns clinic address, hours, etc.

Flow:
  1. Load conversation history from Supabase (last 8 messages)
  2. Call OpenAI with system prompt + history + new user message + tools
  3. If AI calls a function → execute it → feed result back → get final reply
  4. Save reply to conversation history
  5. Return the reply text (caller sends it via WhatsApp)
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from openai import AsyncOpenAI

import database as db
from config import settings

logger = logging.getLogger(__name__)

_client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)

# ── Clinic slot utilities ─────────────────────────────────────────────────────

def _generate_all_slots() -> list[str]:
    """Generate all possible time slots (morning + evening)."""
    slots = []
    for start_str, end_str in [
        (settings.MORNING_START, settings.MORNING_END),
        (settings.EVENING_START, settings.EVENING_END),
    ]:
        h, m = map(int, start_str.split(":"))
        eh, em = map(int, end_str.split(":"))
        current = datetime(2000, 1, 1, h, m)
        end = datetime(2000, 1, 1, eh, em)
        while current < end:
            slots.append(current.strftime("%H:%M"))
            current += timedelta(minutes=settings.SLOT_DURATION_MIN)
    return slots


ALL_SLOTS = _generate_all_slots()


# ── OpenAI Tool Definitions ───────────────────────────────────────────────────

# Tools available on ALL plans
_TOOLS_BASE = [
    {
        "type": "function",
        "function": {
            "name": "check_available_slots",
            "description": (
                "Check which appointment slots are available on a specific date. "
                "Call this when the patient asks about available times or wants to book an appointment."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {
                        "type": "string",
                        "description": "Date in YYYY-MM-DD format (e.g. '2026-04-02')",
                    }
                },
                "required": ["date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_appointment",
            "description": (
                "Book an appointment for the patient. "
                "Call this only after the patient has confirmed a specific slot."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "patient_name": {
                        "type": "string",
                        "description": "Full name of the patient",
                    },
                    "date": {
                        "type": "string",
                        "description": "Appointment date in YYYY-MM-DD format",
                    },
                    "slot_time": {
                        "type": "string",
                        "description": "Selected slot in HH:MM format (e.g. '10:30')",
                    },
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

# Tools available on Pro + Suite plans only
_TOOLS_PRO = [
    {
        "type": "function",
        "function": {
            "name": "get_my_appointment",
            "description": (
                "Look up the patient's next upcoming confirmed appointment. "
                "Call this when a patient asks about their booking, wants to cancel, or wants to reschedule."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_appointment",
            "description": (
                "Cancel the patient's upcoming appointment. "
                "Always call get_my_appointment first to confirm which appointment to cancel. "
                "Ask the patient to confirm before calling this."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "appointment_id": {
                        "type": "integer",
                        "description": "ID of the appointment to cancel (from get_my_appointment result)",
                    }
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
                "Reschedule the patient's existing appointment to a new date and time. "
                "Always call get_my_appointment first, then check_available_slots for the new date, "
                "then ask patient to confirm the new slot before calling this."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "appointment_id": {
                        "type": "integer",
                        "description": "ID of the existing appointment to reschedule",
                    },
                    "new_date": {
                        "type": "string",
                        "description": "New appointment date in YYYY-MM-DD format",
                    },
                    "new_slot": {
                        "type": "string",
                        "description": "New time slot in HH:MM format (e.g. '11:00')",
                    },
                },
                "required": ["appointment_id", "new_date", "new_slot"],
            },
        },
    },
]


def _get_tools() -> list[dict]:
    """Return tool list based on the active plan tier."""
    tier = settings.PLAN_TIER.lower()
    if tier in ("pro", "suite"):
        return _TOOLS_BASE + _TOOLS_PRO
    return _TOOLS_BASE


# Keep a module-level alias for backwards compatibility
TOOLS = _TOOLS_BASE


# ── Doctor-only tools ─────────────────────────────────────────────────────────

_TOOLS_DOCTOR = [
    # Doctor can also check availability
    {
        "type": "function",
        "function": {
            "name": "check_available_slots",
            "description": "Check available appointment slots for a date.",
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
            "name": "view_appointments",
            "description": (
                "View all confirmed appointments for a specific date. "
                "Call this when the doctor asks about their schedule or today's patients."
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
            "name": "block_slots",
            "description": (
                "Block appointment slots on a date so patients cannot book them. "
                "Use slot_times=['all'] to block the entire day. "
                "Otherwise pass specific times like ['10:00','10:30']."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "Date in YYYY-MM-DD format"},
                    "slot_times": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of HH:MM slot times to block, or ['all'] to block entire day",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Optional reason for the block (e.g. 'Personal leave', 'Holiday')",
                    },
                },
                "required": ["date", "slot_times"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "unblock_slots",
            "description": (
                "Remove blocks from slots on a date, making them bookable again. "
                "Use slot_times=['all'] to unblock the entire day."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "Date in YYYY-MM-DD format"},
                    "slot_times": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of HH:MM slot times to unblock, or ['all'] to unblock entire day",
                    },
                },
                "required": ["date", "slot_times"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "view_blocked_slots",
            "description": "View which slots are currently blocked on a specific date.",
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "Date in YYYY-MM-DD format"}
                },
                "required": ["date"],
            },
        },
    },
]


# ── Function execution ────────────────────────────────────────────────────────

async def _execute_function(
    fn_name: str, fn_args: dict, phone: str
) -> tuple[str, dict | None]:
    """
    Execute the requested function.
    Returns (result_string, appointment_row_or_None).
    """
    if fn_name == "check_available_slots":
        date = fn_args.get("date", "")

        # Weekly off check
        try:
            day_name = datetime.strptime(date, "%Y-%m-%d").strftime("%A")
            if day_name in settings.WEEKLY_OFF_DAYS:
                return json.dumps({
                    "date": date,
                    "morning_slots": [],
                    "evening_slots": [],
                    "total_available": 0,
                    "note": f"Clinic is closed on {day_name}s.",
                }), None
        except ValueError:
            pass

        booked = db.get_booked_slots(date)
        blocked = db.get_blocked_slot_times(date)

        if "all" in blocked:
            available = []
        else:
            available = [s for s in ALL_SLOTS if s not in booked and s not in blocked]

        morning = [s for s in available if int(s.split(":")[0]) < 14]
        evening = [s for s in available if int(s.split(":")[0]) >= 14]
        result = {
            "date": date,
            "morning_slots": morning,
            "evening_slots": evening,
            "total_available": len(available),
        }
        return json.dumps(result), None

    elif fn_name == "create_appointment":
        patient_name = fn_args.get("patient_name", "Patient")
        date = fn_args.get("date", "")
        slot_time = fn_args.get("slot_time", "")
        try:
            appt = db.create_appointment(phone, patient_name, date, slot_time)
            result = {
                "success": True,
                "appointment_id": appt["id"],
                "patient_name": patient_name,
                "date": date,
                "slot_time": slot_time,
                "message": f"Appointment confirmed for {patient_name} on {date} at {slot_time}",
            }
            return json.dumps(result), appt
        except Exception as exc:
            logger.error("create_appointment error: %s", exc)
            return json.dumps({"success": False, "error": str(exc)}), None

    elif fn_name == "get_clinic_info":
        result = {
            "clinic_name": settings.CLINIC_NAME,
            "doctor_name": settings.DOCTOR_NAME,
            "address": settings.CLINIC_ADDRESS,
            "morning_hours": f"{settings.MORNING_START} – {settings.MORNING_END}",
            "evening_hours": f"{settings.EVENING_START} – {settings.EVENING_END}",
            "slot_duration_min": settings.SLOT_DURATION_MIN,
        }
        return json.dumps(result), None

    # ── Pro / Suite plan tools ────────────────────────────────────────────────

    elif fn_name == "get_my_appointment":
        appt = db.get_upcoming_appointment(phone)
        if appt:
            result = {
                "found": True,
                "appointment_id": appt["id"],
                "date": appt["appointment_date"],
                "slot_time": appt["slot_time"],
                "patient_name": appt["patient_name"],
                "status": appt["status"],
            }
        else:
            result = {"found": False, "message": "No upcoming appointment found."}
        return json.dumps(result), None

    elif fn_name == "cancel_appointment":
        appt_id = fn_args.get("appointment_id")
        if not appt_id:
            return json.dumps({"success": False, "error": "appointment_id is required"}), None
        try:
            db.cancel_appointment(int(appt_id))
            return json.dumps({"success": True, "message": f"Appointment {appt_id} cancelled successfully."}), None
        except Exception as exc:
            logger.error("cancel_appointment error: %s", exc)
            return json.dumps({"success": False, "error": str(exc)}), None

    elif fn_name == "reschedule_appointment":
        appt_id  = fn_args.get("appointment_id")
        new_date = fn_args.get("new_date", "")
        new_slot = fn_args.get("new_slot", "")
        if not all([appt_id, new_date, new_slot]):
            return json.dumps({"success": False, "error": "appointment_id, new_date and new_slot are all required"}), None
        try:
            # Fetch current appointment to get patient name
            cur = db.get_upcoming_appointment(phone)
            patient_name = cur["patient_name"] if cur else "Patient"
            new_appt = db.reschedule_appointment(int(appt_id), phone, patient_name, new_date, new_slot)
            result = {
                "success": True,
                "new_appointment_id": new_appt["id"],
                "patient_name": patient_name,
                "new_date": new_date,
                "new_slot": new_slot,
                "message": f"Appointment rescheduled to {new_date} at {new_slot}.",
            }
            return json.dumps(result), new_appt
        except Exception as exc:
            logger.error("reschedule_appointment error: %s", exc)
            return json.dumps({"success": False, "error": str(exc)}), None

    return json.dumps({"error": f"Unknown function: {fn_name}"}), None


# ── Doctor function execution ─────────────────────────────────────────────────

async def _execute_doctor_function(fn_name: str, fn_args: dict) -> str:
    """Execute doctor-only functions. Returns result JSON string."""

    if fn_name == "check_available_slots":
        # Reuse patient logic (no phone needed here)
        result_str, _ = await _execute_function(fn_name, fn_args, "")
        return result_str

    elif fn_name == "view_appointments":
        date = fn_args.get("date", "")
        appointments = db.get_appointments_for_date(date)
        if not appointments:
            return json.dumps({"date": date, "count": 0, "appointments": [],
                               "message": "No confirmed appointments for this date."})
        return json.dumps({
            "date": date,
            "count": len(appointments),
            "appointments": appointments,
        })

    elif fn_name == "block_slots":
        date = fn_args.get("date", "")
        slot_times = fn_args.get("slot_times", [])
        reason = fn_args.get("reason", "")
        if not date or not slot_times:
            return json.dumps({"success": False, "error": "date and slot_times are required"})
        try:
            count = db.block_slots(date, slot_times, reason)
            label = "Entire day" if "all" in slot_times else f"{count} slot(s) ({', '.join(slot_times)})"
            return json.dumps({
                "success": True,
                "date": date,
                "blocked": slot_times,
                "message": f"{label} blocked on {date}. Patients cannot book these slots.",
            })
        except Exception as exc:
            logger.error("block_slots error: %s", exc)
            return json.dumps({"success": False, "error": str(exc)})

    elif fn_name == "unblock_slots":
        date = fn_args.get("date", "")
        slot_times = fn_args.get("slot_times", [])
        if not date or not slot_times:
            return json.dumps({"success": False, "error": "date and slot_times are required"})
        try:
            count = db.unblock_slots(date, slot_times)
            label = "All blocks removed" if "all" in slot_times else f"{count} slot(s) unblocked"
            return json.dumps({
                "success": True,
                "date": date,
                "message": f"{label} on {date}. Slots are now available for booking.",
            })
        except Exception as exc:
            logger.error("unblock_slots error: %s", exc)
            return json.dumps({"success": False, "error": str(exc)})

    elif fn_name == "view_blocked_slots":
        date = fn_args.get("date", "")
        rows = db.get_blocked_slots_detail(date)
        if not rows:
            return json.dumps({"date": date, "blocked": [],
                               "message": "No slots are blocked on this date."})
        # Check for all-day block (slot_time is None)
        if any(r["slot_time"] is None for r in rows):
            return json.dumps({"date": date, "blocked": ["all"],
                               "message": f"Entire day is blocked on {date}."})
        return json.dumps({
            "date": date,
            "blocked": [r["slot_time"] for r in rows],
            "reasons": list({r.get("reason", "") for r in rows if r.get("reason")}),
        })

    return json.dumps({"error": f"Unknown doctor function: {fn_name}"})


# ── System prompt ─────────────────────────────────────────────────────────────

def _build_system_prompt() -> str:
    today = datetime.now(timezone.utc).strftime("%A, %d %B %Y")
    tier = settings.PLAN_TIER.lower()

    cancel_reschedule_rules = ""
    if tier in ("pro", "suite"):
        cancel_reschedule_rules = """
- If a patient wants to CANCEL: call get_my_appointment first to confirm details, show them the appointment, ask them to confirm cancellation, then call cancel_appointment.
- If a patient wants to RESCHEDULE: call get_my_appointment to find their current booking, ask for their preferred new date, call check_available_slots, let them pick a slot, confirm, then call reschedule_appointment.
- Always confirm with the patient before cancelling or rescheduling — these are irreversible actions.
"""

    return f"""You are Meera, a warm and professional appointment assistant for {settings.CLINIC_NAME} (run by {settings.DOCTOR_NAME}).

Today's date is {today}.

Your job is to help patients:
1. Book appointments at the clinic
2. Answer questions about the clinic (timings, address, doctor)
3. Handle general health-related queries politely (do NOT give medical advice)
4. Help patients cancel or reschedule their appointments (if available)

Guidelines:
- Be warm, concise, and helpful. Use a friendly Indian conversational tone.
- Keep replies short — max 3-4 sentences unless listing slots.
- Always ask for the patient's name if you don't have it yet.
- When a patient wants to book, use check_available_slots to find open times, then present them.
- After the patient selects a slot, use create_appointment to confirm the booking.
- After booking, tell the patient: clinic address, appointment date & time, and that a reminder will be sent 24 hours before.
- If no slots are available for a date, suggest nearby dates.
- Do NOT make up appointment times — always check with check_available_slots first.
- For anything medical (diagnosis, medicines, dosage), politely say "Please consult {settings.DOCTOR_NAME} during your appointment."
- Respond in the same language the patient uses (Hindi or English).
- If the patient greets you in Hindi (e.g., "Namaste", "Haan", "Theek hai"), reply warmly in Hindi.
{cancel_reschedule_rules}"""


def _build_doctor_prompt() -> str:
    today = datetime.now(timezone.utc).strftime("%A, %d %B %Y")
    off_days = ", ".join(settings.WEEKLY_OFF_DAYS) if settings.WEEKLY_OFF_DAYS else "None"
    return f"""You are a schedule assistant for {settings.DOCTOR_NAME} at {settings.CLINIC_NAME}.
Today is {today}.

You help the doctor manage their clinic schedule. Available actions:
- Block slots so patients cannot book them (specific times or entire day)
- Unblock previously blocked slots
- View blocked slots for any date
- View confirmed appointments for any date
- Check available (free) slots for any date

Clinic hours:
  Morning : {settings.MORNING_START} – {settings.MORNING_END}
  Evening : {settings.EVENING_START} – {settings.EVENING_END}
  Slot gap: {settings.SLOT_DURATION_MIN} minutes
  Weekly off: {off_days}

Rules:
- Be brief and direct — the doctor is busy.
- Always confirm what was done with a clear summary (e.g., "Blocked 10:00–12:00 on 15 May ✓").
- When the doctor says "block morning of 15 May", block all morning slots for that date.
- "Block all day" or "not available today" → block_slots with slot_times=["all"].
- When unblocking, confirm the slots are free again.
- Use emojis sparingly for clarity (✅ ❌ 🚫)."""


# ── Doctor mode detection ─────────────────────────────────────────────────────

def _is_doctor(phone: str) -> bool:
    """Return True if the sender's phone matches the registered DOCTOR_PHONE."""
    if not settings.DOCTOR_PHONE:
        return False
    # Normalise by stripping leading zeros/plus
    clean = lambda p: p.lstrip("+").lstrip("0")
    return clean(phone) == clean(settings.DOCTOR_PHONE) or clean(phone).endswith(clean(settings.DOCTOR_PHONE))


# ── Doctor reply flow ─────────────────────────────────────────────────────────

async def _get_doctor_reply(phone: str, user_text: str) -> tuple[str, dict | None]:
    """Handle messages from the doctor — schedule management mode."""
    history = db.get_conversation_history(phone, limit=6)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": _build_doctor_prompt()},
        *history,
        {"role": "user", "content": user_text},
    ]

    response = await _client.chat.completions.create(
        model=settings.OPENAI_MODEL,
        messages=messages,
        tools=_TOOLS_DOCTOR,
        tool_choice="auto",
        max_tokens=400,
        temperature=0.3,  # lower temp for precise schedule ops
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
            logger.info("[DOCTOR] calling function: %s(%s)", fn_name, fn_args)
            fn_result = await _execute_doctor_function(fn_name, fn_args)
            tool_results.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": fn_result,
            })

        messages.extend(tool_results)
        response = await _client.chat.completions.create(
            model=settings.OPENAI_MODEL,
            messages=messages,
            tools=_TOOLS_DOCTOR,
            tool_choice="auto",
            max_tokens=400,
            temperature=0.3,
        )
        choice = response.choices[0]

    reply_text = choice.message.content or "Done."
    db.save_message(phone, "user", user_text)
    db.save_message(phone, "assistant", reply_text)
    return reply_text, None


# ── Main agent entry point ────────────────────────────────────────────────────

async def get_agent_reply(phone: str, user_text: str) -> tuple[str, dict | None]:
    """
    Process a user message and return (reply_text, appointment_row_or_None).
    Saves the conversation to DB and returns the assistant's reply.
    """
    # Route to doctor mode if sender is the registered doctor
    if _is_doctor(phone):
        return await _get_doctor_reply(phone, user_text)

    # 1. Load history
    history = db.get_conversation_history(phone, limit=8)

    # 2. Build messages array
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": _build_system_prompt()},
        *history,
        {"role": "user", "content": user_text},
    ]

    # 3. First OpenAI call
    active_tools = _get_tools()
    response = await _client.chat.completions.create(
        model=settings.OPENAI_MODEL,
        messages=messages,
        tools=active_tools,
        tool_choice="auto",
        max_tokens=500,
        temperature=0.7,
    )

    choice = response.choices[0]
    appt_row = None

    # 4. Handle function calls (may loop for multi-step)
    while choice.finish_reason == "tool_calls":
        tool_calls = choice.message.tool_calls or []
        # Append assistant's message with tool_calls to history
        messages.append(choice.message)

        tool_results = []
        for tc in tool_calls:
            fn_name = tc.function.name
            try:
                fn_args = json.loads(tc.function.arguments)
            except json.JSONDecodeError:
                fn_args = {}

            logger.info("AI calling function: %s(%s)", fn_name, fn_args)
            fn_result, maybe_appt = await _execute_function(fn_name, fn_args, phone)
            if maybe_appt:
                appt_row = maybe_appt

            tool_results.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": fn_result,
            })

        messages.extend(tool_results)

        # Feed results back to AI for natural language reply
        response = await _client.chat.completions.create(
            model=settings.OPENAI_MODEL,
            messages=messages,
            tools=active_tools,
            tool_choice="auto",
            max_tokens=500,
            temperature=0.7,
        )
        choice = response.choices[0]

    reply_text = choice.message.content or "Sorry, I didn't understand that. Could you please repeat?"

    # 5. Save conversation to DB
    db.save_message(phone, "user", user_text)
    db.save_message(phone, "assistant", reply_text)

    return reply_text, appt_row
