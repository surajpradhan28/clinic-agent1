"""
agent.py - OpenAI GPT-4o-mini conversation engine with function calling.

Tools: check_available_slots | create_appointment | get_clinic_info
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


def _generate_all_slots() -> list:
    slots = []
    for start_str, end_str in [(settings.MORNING_START, settings.MORNING_END), (settings.EVENING_START, settings.EVENING_END)]:
        h, m = map(int, start_str.split(":"))
        eh, em = map(int, end_str.split(":"))
        current = datetime(2000, 1, 1, h, m)
        end = datetime(2000, 1, 1, eh, em)
        while current < end:
            slots.append(current.strftime("%H:%M"))
            current += timedelta(minutes=settings.SLOT_DURATION_MIN)
    return slots


ALL_SLOTS = _generate_all_slots()

TOOLS = [
    {"type": "function", "function": {"name": "check_available_slots", "description": "Check which appointment slots are available on a specific date. Call this when the patient asks about available times or wants to book.", "parameters": {"type": "object", "properties": {"date": {"type": "string", "description": "Date in YYYY-MM-DD format"}}, "required": ["date"]}}},
    {"type": "function", "function": {"name": "create_appointment", "description": "Book an appointment for the patient. Call this only after the patient has confirmed a specific slot.", "parameters": {"type": "object", "properties": {"patient_name": {"type": "string", "description": "Full name of the patient"}, "date": {"type": "string", "description": "Appointment date in YYYY-MM-DD format"}, "slot_time": {"type": "string", "description": "Selected slot in HH:MM format"}}, "required": ["patient_name", "date", "slot_time"]}}},
    {"type": "function", "function": {"name": "get_clinic_info", "description": "Get clinic name, doctor name, address, and working hours.", "parameters": {"type": "object", "properties": {}, "required": []}}},
]


async def _execute_function(fn_name: str, fn_args: dict, phone: str) -> tuple:
    if fn_name == "check_available_slots":
        date = fn_args.get("date", "")
        booked = db.get_booked_slots(date)
        available = [s for s in ALL_SLOTS if s not in booked]
        morning = [s for s in available if int(s.split(":")[0]) < 14]
        evening = [s for s in available if int(s.split(":")[0]) >= 14]
        return json.dumps({"date": date, "morning_slots": morning, "evening_slots": evening, "total_available": len(available)}), None
    elif fn_name == "create_appointment":
        patient_name = fn_args.get("patient_name", "Patient")
        date = fn_args.get("date", "")
        slot_time = fn_args.get("slot_time", "")
        try:
            appt = db.create_appointment(phone, patient_name, date, slot_time)
            return json.dumps({"success": True, "appointment_id": appt["id"], "patient_name": patient_name, "date": date, "slot_time": slot_time}), appt
        except Exception as exc:
            logger.error("create_appointment error: %s", exc)
            return json.dumps({"success": False, "error": str(exc)}), None
    elif fn_name == "get_clinic_info":
        return json.dumps({"clinic_name": settings.CLINIC_NAME, "doctor_name": settings.DOCTOR_NAME, "address": settings.CLINIC_ADDRESS, "morning_hours": f"{settings.MORNING_START}-{settings.MORNING_END}", "evening_hours": f"{settings.EVENING_START}-{settings.EVENING_END}"}), None
    return json.dumps({"error": f"Unknown function: {fn_name}"}), None


def _build_system_prompt() -> str:
    today = datetime.now(timezone.utc).strftime("%A, %d %B %Y")
    return f"""You are Meera, a warm and professional appointment assistant for {settings.CLINIC_NAME} (run by {settings.DOCTOR_NAME}).

Today's date is {today}.

Your job:
1. Help patients book appointments
2. Answer questions about the clinic (timings, address, doctor)
3. Handle general queries politely (do NOT give medical advice)

Guidelines:
- Be warm, concise, friendly with an Indian conversational tone.
- Keep replies short - max 3-4 sentences unless listing slots.
- Always ask for the patient's name if you don't have it.
- Use check_available_slots to find open times, then present them.
- After patient selects a slot, use create_appointment to confirm booking.
- After booking, tell patient: clinic address, date and time, and that a reminder will be sent 24h before.
- Do NOT make up appointment times - always check with check_available_slots first.
- For medical questions, say "Please consult {settings.DOCTOR_NAME} during your appointment."
- Respond in the same language the patient uses (Hindi or English).
"""


async def get_agent_reply(phone: str, user_text: str) -> tuple:
    history = db.get_conversation_history(phone, limit=8)
    messages: list[dict[str, Any]] = [{"role": "system", "content": _build_system_prompt()}, *history, {"role": "user", "content": user_text}]

    response = await _client.chat.completions.create(model=settings.OPENAI_MODEL, messages=messages, tools=TOOLS, tool_choice="auto", max_tokens=500, temperature=0.7)
    choice = response.choices[0]
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
            logger.info("AI calling: %s(%s)", fn_name, fn_args)
            fn_result, maybe_appt = await _execute_function(fn_name, fn_args, phone)
            if maybe_appt:
                appt_row = maybe_appt
            tool_results.append({"role": "tool", "tool_call_id": tc.id, "content": fn_result})
        messages.extend(tool_results)
        response = await _client.chat.completions.create(model=settings.OPENAI_MODEL, messages=messages, tools=TOOLS, tool_choice="auto", max_tokens=500, temperature=0.7)
        choice = response.choices[0]

    reply_text = choice.message.content or "Sorry, I did not understand that. Please repeat?"
    db.save_message(phone, "user", user_text)
    db.save_message(phone, "assistant", reply_text)
    return reply_text, appt_row
