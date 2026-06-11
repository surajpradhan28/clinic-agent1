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

# Indian Standard Time — slot times and "today" shown to patients/doctor are IST
_IST = timezone(timedelta(hours=5, minutes=30))
from typing import Any

from openai import AsyncOpenAI

import database as db
import whatsapp
from config import settings

logger = logging.getLogger(__name__)

_openai = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)


# ── Prescription PDF generator ────────────────────────────────────────────────

def _build_prescription_pdf(
    clinic_name: str,
    doctor_name: str,
    clinic_address: str,
    patient_name: str,
    visit_date: str,
    notes: str,
) -> bytes:
    """
    Generate a clean prescription PDF and return the raw bytes.
    Uses reportlab; falls back to a minimal plain-text PDF if not installed.
    """
    try:
        from io import BytesIO
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.units import cm
        from reportlab.lib.colors import HexColor, black, white
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, HRFlowable, Table, TableStyle
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.enums import TA_CENTER, TA_LEFT

        buf = BytesIO()
        doc = SimpleDocTemplate(
            buf, pagesize=A4,
            leftMargin=2*cm, rightMargin=2*cm,
            topMargin=2*cm, bottomMargin=2*cm,
        )

        GREEN  = HexColor("#075E54")
        LIGHT  = HexColor("#f0faf8")
        GREY   = HexColor("#555555")

        styles = getSampleStyleSheet()
        h1 = ParagraphStyle("h1", fontSize=20, textColor=white, fontName="Helvetica-Bold",
                             alignment=TA_CENTER, spaceAfter=2)
        sub = ParagraphStyle("sub", fontSize=10, textColor=HexColor("#c8e6e0"),
                              fontName="Helvetica", alignment=TA_CENTER, spaceAfter=0)
        label = ParagraphStyle("label", fontSize=9, textColor=GREY,
                               fontName="Helvetica-Bold", spaceAfter=2)
        body  = ParagraphStyle("body", fontSize=11, textColor=black,
                               fontName="Helvetica", spaceAfter=6, leading=16)
        footer_style = ParagraphStyle("footer", fontSize=8, textColor=GREY,
                                      fontName="Helvetica", alignment=TA_CENTER)

        # Format visit date nicely
        try:
            vd = datetime.strptime(visit_date, "%Y-%m-%d").strftime("%d %B %Y")
        except Exception:
            vd = visit_date

        # Header table with green background
        header_data = [[
            Paragraph(clinic_name, h1),
        ], [
            Paragraph(f"{doctor_name}  ·  {clinic_address}", sub),
        ]]
        header_table = Table(header_data, colWidths=[17*cm])
        header_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), GREEN),
            ("TOPPADDING",    (0, 0), (-1, -1), 10),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ("LEFTPADDING",   (0, 0), (-1, -1), 12),
            ("RIGHTPADDING",  (0, 0), (-1, -1), 12),
            ("ROUNDEDCORNERS", [8]),
        ]))

        # Patient info row
        info_data = [[
            Paragraph("PATIENT", label),
            Paragraph("DATE", label),
        ], [
            Paragraph(patient_name, body),
            Paragraph(vd, body),
        ]]
        info_table = Table(info_data, colWidths=[9*cm, 8*cm])
        info_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), LIGHT),
            ("TOPPADDING",    (0, 0), (-1, -1), 8),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ("LEFTPADDING",   (0, 0), (-1, -1), 10),
            ("ROUNDEDCORNERS", [6]),
        ]))

        # Notes section — split into lines for readability
        notes_paragraphs = []
        for line in notes.strip().split("\n"):
            line = line.strip()
            if line:
                notes_paragraphs.append(Paragraph(line, body))
            else:
                notes_paragraphs.append(Spacer(1, 6))

        story = [
            header_table,
            Spacer(1, 0.5*cm),
            info_table,
            Spacer(1, 0.4*cm),
            HRFlowable(width="100%", thickness=1, color=HexColor("#d0e8e4")),
            Spacer(1, 0.3*cm),
            Paragraph("VISIT NOTES", label),
            Spacer(1, 0.2*cm),
            *notes_paragraphs,
            Spacer(1, 1*cm),
            HRFlowable(width="100%", thickness=0.5, color=HexColor("#cccccc")),
            Spacer(1, 0.2*cm),
            Paragraph(
                f"This prescription was prepared by {doctor_name} · {clinic_name}. "
                "Powered by MyWhatsApp Clinic.",
                footer_style,
            ),
        ]

        doc.build(story)
        return buf.getvalue()

    except ImportError:
        logger.warning("[Prescription] reportlab not installed — skipping PDF")
        return b""
    except Exception as exc:
        logger.error("[Prescription] PDF generation failed: %s", exc)
        return b""


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
            "description": (
                "Book an appointment after the patient confirms a specific slot. "
                "patient_name MUST be the patient's full name (first + last). "
                "If only a single name was given, ask for the full name before calling this."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "patient_name": {"type": "string", "description": "Full name of the patient (first and last name)"},
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
            "name": "join_waitlist",
            "description": (
                "Add the patient to the waitlist for a slot that is fully booked. "
                "Call this ONLY when create_appointment fails with a slot-taken/double-booking error "
                "and the patient agrees to be waitlisted. "
                "When a cancellation opens that slot, the patient will be auto-booked and notified."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "patient_name": {"type": "string", "description": "Full name of the patient"},
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
            "name": "save_patient_intake",
            "description": (
                "Save the new-patient intake details after collecting them through conversation. "
                "Call this ONCE after all three fields (age, gender, chief_complaint) have been gathered. "
                "Only use this for new patients (create_appointment returned is_new_patient=true)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "appointment_id":  {"type": "integer", "description": "ID from create_appointment result"},
                    "age":             {"type": "integer", "description": "Patient's age in years"},
                    "gender":          {"type": "string",  "description": "Male / Female / Other"},
                    "chief_complaint": {"type": "string",  "description": "Main reason for the visit"},
                },
                "required": ["appointment_id", "age", "gender", "chief_complaint"],
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
    {
        "type": "function",
        "function": {
            "name": "save_visit_notes",
            "description": (
                "Save doctor's notes/description for a patient's visit and set the follow-up timing. "
                "You do NOT need the appointment_id — you can identify the appointment by "
                "patient_name + date (defaults to today if omitted), or by slot_time. "
                "If appointment_id is known (from view_appointments), pass it directly. "
                "Always confirm notes with the doctor before saving."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "notes": {
                        "type": "string",
                        "description": "Doctor's notes for this visit (diagnosis, treatment, prescription, advice, etc.)",
                    },
                    "patient_name": {
                        "type": "string",
                        "description": "Patient's name — used to find the appointment when appointment_id is not known.",
                    },
                    "date": {
                        "type": "string",
                        "description": "Appointment date in YYYY-MM-DD format. Defaults to today if omitted.",
                    },
                    "slot_time": {
                        "type": "string",
                        "description": "Slot time (HH:MM) — helps narrow down if multiple patients share the same date.",
                    },
                    "appointment_id": {
                        "type": "integer",
                        "description": "Appointment ID — use this if already known; skips the name/date lookup.",
                    },
                    "followup_days": {
                        "type": "integer",
                        "description": "Days after the appointment to send the follow-up WhatsApp. Default: 2.",
                    },
                },
                "required": ["notes"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "view_patient_history",
            "description": (
                "View a patient's full visit history including doctor notes for each visit. "
                "Provide patient_phone, patient_name, or both. "
                "If multiple patients share the same name or phone, the tool will ask you "
                "to confirm which person before showing history — pass both fields to skip that step."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "patient_phone": {
                        "type": "string",
                        "description": "Patient's phone number (with country code, e.g. 919876543210)",
                    },
                    "patient_name": {
                        "type": "string",
                        "description": (
                            "Patient's full name. Required to narrow down when "
                            "multiple people share a phone, or when multiple patients "
                            "have the same name."
                        ),
                    },
                },
                "required": [],
            },
        },
    },
]

# ── Doctor plan-feature sets ────────────────────────────────────────────────
_DOCTOR_STARTER_FNS: frozenset = frozenset({
    "check_available_slots", "view_appointments",
    "block_slots", "unblock_slots", "view_blocked_slots", "view_clinic_info",
    "save_visit_notes", "view_patient_history",
})
_DOCTOR_PRO_FNS: frozenset = _DOCTOR_STARTER_FNS | frozenset({
    "update_clinic_info", "broadcast_message",
})
_DOCTOR_SUITE_FNS: frozenset = _DOCTOR_PRO_FNS | frozenset({
    "add_clinic_note", "list_clinic_notes", "remove_clinic_note",
    "set_day_schedule", "clear_day_schedule",
})
_DOCTOR_FEATURE_NAMES: dict = {
    "update_clinic_info":  "Update Clinic Info",
    "broadcast_message":   "Broadcast Message to All Patients",
    "add_clinic_note":     "Clinic Knowledge Notes",
    "list_clinic_notes":   "Clinic Knowledge Notes",
    "remove_clinic_note":  "Clinic Knowledge Notes",
    "set_day_schedule":    "Custom Day Schedule",
    "clear_day_schedule":  "Custom Day Schedule",
}

# ── Upsell messages (shown when doctor hits a plan-gated feature) ─────────────

def _upsell_reply(fn_name: str, current_plan: str) -> str:
    """Return a rich WhatsApp upsell nudge for a plan-gated doctor function."""
    from config import settings as _s
    upgrade_url = f"{_s.SERVER_URL}/signup"

    # Pro-gated features (Starter → Pro)
    _PRO_NUDGES = {
        "update_clinic_info": (
            "✏️ *Update Clinic Info*",
            "Keep your clinic name, address, and phone always accurate — patients see this on every booking.",
        ),
        "broadcast_message": (
            "📢 *Broadcast to All Patients*",
            "Send holiday notices, health tips, or important announcements to all registered patients in one tap.",
        ),
    }

    # Suite-gated features (Pro → Suite)
    _SUITE_NUDGES = {
        "add_clinic_note":   ("📝 *AI Knowledge Notes*", "Train the booking bot with clinic-specific rules — allergies policy, special instructions, anything."),
        "list_clinic_notes": ("📝 *AI Knowledge Notes*", "Train the booking bot with clinic-specific rules — allergies policy, special instructions, anything."),
        "remove_clinic_note":("📝 *AI Knowledge Notes*", "Train the booking bot with clinic-specific rules — allergies policy, special instructions, anything."),
        "set_day_schedule":  ("🗓️ *Custom Day Schedule*", "Override clinic hours for any specific date — perfect for conferences, holidays, or half-days."),
        "clear_day_schedule":("🗓️ *Custom Day Schedule*", "Override clinic hours for any specific date — perfect for conferences, holidays, or half-days."),
    }

    if fn_name in _PRO_NUDGES:
        title, benefit = _PRO_NUDGES[fn_name]
        return (
            f"🔒 {title} is a *Pro Plan* feature.\n\n"
            f"{benefit}\n\n"
            f"*Pro Plan — ₹{_s.PRICE_PRO:,}/mo* also includes:\n"
            f"  ✅ Patient self-cancel & reschedule via WhatsApp\n"
            f"  ✅ Auto waitlist when slots fill up\n"
            f"  ✅ 2-day follow-up message after every visit\n"
            f"  ✅ Update clinic info from WhatsApp\n"
            f"  ✅ Broadcast messages to all patients\n\n"
            f"👉 Upgrade now → {upgrade_url}\n"
            f"   or reply *UPGRADE* and I'll send the link."
        )
    elif fn_name in _SUITE_NUDGES:
        title, benefit = _SUITE_NUDGES[fn_name]
        return (
            f"🔒 {title} is a *Suite Plan* feature.\n\n"
            f"{benefit}\n\n"
            f"*Suite Plan — ₹{_s.PRICE_SUITE:,}/mo* also includes:\n"
            f"  ✅ Everything in Pro\n"
            f"  ✅ Custom clinic hours per day\n"
            f"  ✅ AI knowledge notes for the bot\n"
            f"  ✅ Daily morning schedule on WhatsApp\n"
            f"  ✅ Monthly automated invoice to your phone\n\n"
            f"👉 Upgrade now → {upgrade_url}\n"
            f"   or reply *UPGRADE* and I'll send the link."
        )
    else:
        required = "Suite" if fn_name in (_DOCTOR_SUITE_FNS - _DOCTOR_PRO_FNS) else "Pro"
        feature  = _DOCTOR_FEATURE_NAMES.get(fn_name, fn_name.replace("_", " ").title())
        return (
            f"🔒 *{feature}* requires the *{required} Plan*.\n\n"
            f"Reply *UPGRADE* to get the upgrade link, or visit:\n{upgrade_url}"
        )


def _upgrade_plan_card() -> str:
    """Full plan comparison card — sent when doctor types UPGRADE / pricing / plans."""
    from config import settings as _s
    upgrade_url = f"{_s.SERVER_URL}/signup"
    return (
        f"🚀 *Clinic AI Agent — Plan Comparison*\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🟢 *Starter — ₹{_s.PRICE_STARTER:,}/mo*\n"
        f"  • WhatsApp appointment booking\n"
        f"  • Slot management (block/unblock)\n"
        f"  • Appointment reminders (24h + 1h)\n"
        f"  • View daily schedule\n\n"
        f"⭐ *Pro — ₹{_s.PRICE_PRO:,}/mo*\n"
        f"  Everything in Starter, plus:\n"
        f"  • Patient self-cancel & reschedule\n"
        f"  • Auto waitlist for full slots\n"
        f"  • 2-day post-visit follow-up\n"
        f"  • Update clinic info via WhatsApp\n"
        f"  • Broadcast to all patients\n\n"
        f"💎 *Suite — ₹{_s.PRICE_SUITE:,}/mo*\n"
        f"  Everything in Pro, plus:\n"
        f"  • Custom hours per day\n"
        f"  • AI knowledge notes for the bot\n"
        f"  • Daily morning schedule summary\n"
        f"  • Monthly automated invoice\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"👉 *Upgrade here:*\n{upgrade_url}"
    )


_UPGRADE_KEYWORDS  = frozenset({"upgrade", "pricing", "plans", "plan", "cost", "price", "features"})
_REFERRAL_KEYWORDS = frozenset({"referral", "refer", "referrals", "my code", "my referral", "share code", "invite"})


def _referral_card(client_id: int) -> str:
    """Referral stats card — sent when doctor types 'referral' / 'my code'."""
    from config import settings as _s
    try:
        stats = db.get_referral_stats(client_id)
    except Exception:
        stats = {"referral_code": "", "total_signups": 0, "total_paid": 0,
                 "pending_months": 0, "applied_months": 0}

    code         = stats.get("referral_code") or "—"
    signups      = stats.get("total_signups", 0)
    paid         = stats.get("total_paid", 0)
    pending_mo   = stats.get("pending_months", 0)
    applied_mo   = stats.get("applied_months", 0)
    signup_url   = f"{_s.SERVER_URL}/signup?ref={code}"

    pending_line = (
        f"\n\n💰 *Reward pending:* {pending_mo} free month(s) on your next renewal!"
        if pending_mo else ""
    )
    applied_line = (
        f"\n✅ *Already credited:* {applied_mo} free month(s)"
        if applied_mo else ""
    )

    return (
        f"🤝 *Your Referral Code: `{code}`*\n\n"
        f"Share this link with doctor friends:\n"
        f"👉 {signup_url}\n\n"
        f"📊 *Your referral stats:*\n"
        f"  • {signups} clinic(s) signed up with your code\n"
        f"  • {paid} paid → {paid} free month(s) earned"
        f"{applied_line}"
        f"{pending_line}\n\n"
        f"*How it works:*\n"
        f"For every doctor friend who subscribes using your link, "
        f"you get *1 free month* added to your account automatically. "
        f"No limit — the more you refer, the more you save! 🎉"
    )


def _get_doctor_tools(plan: str) -> list[dict]:
    """Return doctor tool list filtered to the client's subscription plan."""
    tier = plan.lower()
    if tier == "suite":
        allowed = _DOCTOR_SUITE_FNS
    elif tier == "pro":
        allowed = _DOCTOR_PRO_FNS
    else:
        allowed = _DOCTOR_STARTER_FNS
    return [t for t in _TOOLS_DOCTOR if t["function"]["name"] in allowed]



# ── Patient function execution ─────────────────────────────────────────────────

async def _execute_function(
    fn_name: str, fn_args: dict, phone: str, client: dict
) -> tuple[str, dict | None]:
    client_id = client["id"]

    if fn_name == "check_available_slots":
        date = fn_args.get("date", "")
        try:
            day_name = datetime.strptime(date, "%Y-%m-%d").strftime("%A")
            # Merge global weekly-off with per-clinic setting stored in clinic_settings
            per_clinic_off_raw = db.get_clinic_setting(client_id, "weekly_off_days") or ""
            per_clinic_off = [d.strip() for d in per_clinic_off_raw.split(",") if d.strip()]
            all_off_days = list(set(settings.WEEKLY_OFF_DAYS + per_clinic_off))
            if day_name in all_off_days:
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

        # ── Hard weekly-off guard (cannot be bypassed by the model) ──────────
        try:
            _booking_day = datetime.strptime(date, "%Y-%m-%d").strftime("%A")
            _per_clinic_off_raw = db.get_clinic_setting(client_id, "weekly_off_days") or ""
            _per_clinic_off = [d.strip() for d in _per_clinic_off_raw.split(",") if d.strip()]
            _all_off = list(set(settings.WEEKLY_OFF_DAYS + _per_clinic_off))
            if _booking_day in _all_off:
                return json.dumps({
                    "success": False,
                    "weekly_off": True,
                    "error": f"The clinic is closed on {_booking_day}s. Appointments cannot be booked on weekly off days.",
                    "suggestion": (
                        f"Tell the patient: 'Sorry, the clinic is closed every {_booking_day}. "
                        f"Please choose a different day.' Then offer to check nearby available dates."
                    ),
                }), None
        except Exception:
            pass  # If date parse fails, let db.create_appointment handle the error

        try:
            appt = db.create_appointment(client_id, phone, patient_name, date, slot_time)
            new_patient = db.is_new_patient(client_id, phone, current_appt_id=appt["id"])
            return json.dumps({
                "success":        True,
                "appointment_id": appt["id"],
                "patient_name":   patient_name,
                "date":           date,
                "slot_time":      slot_time,
                "is_new_patient": new_patient,
                "can_cancel":     client.get("plan", "starter").lower() in ("pro", "suite"),
                "message": (
                    f"Appointment confirmed for {patient_name} on {date} at {slot_time}. "
                    + ("This is a NEW patient — collect intake (age, gender, chief complaint) before saving." if new_patient else "")
                ),
            }), appt
        except ValueError as exc:
            # Slot already booked — offer waitlist
            logger.warning("create_appointment slot conflict: %s", exc)
            return json.dumps({
                "success": False,
                "slot_taken": True,
                "error": str(exc),
                "suggestion": (
                    f"That slot is already taken. Offer to add {patient_name} to the waitlist for "
                    f"{date} at {slot_time} — they will be auto-booked if a cancellation opens up. "
                    "Ask: 'Would you like me to add you to the waitlist for this slot?' "
                    "If yes, call join_waitlist."
                ),
            }), None
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

    elif fn_name == "join_waitlist":
        patient_name = fn_args.get("patient_name", "Patient")
        date         = fn_args.get("date", "")
        slot_time    = fn_args.get("slot_time", "")
        if not date or not slot_time:
            return json.dumps({"success": False, "error": "date and slot_time required"}), None
        try:
            db.add_to_waitlist(client_id, phone, patient_name, date, slot_time)
            try:
                date_display = datetime.strptime(date, "%Y-%m-%d").strftime("%d %B %Y")
            except Exception:
                date_display = date
            return json.dumps({
                "success":  True,
                "date":     date,
                "slot_time": slot_time,
                "message":  (
                    f"Added to waitlist for {date_display} at {slot_time}. "
                    "You will be automatically booked and notified if someone cancels."
                ),
            }), None
        except Exception as exc:
            logger.error("join_waitlist error: %s", exc)
            return json.dumps({"success": False, "error": str(exc)}), None

    elif fn_name == "save_patient_intake":
        appt_id        = fn_args.get("appointment_id")
        age            = fn_args.get("age")
        gender         = fn_args.get("gender", "")
        chief_complaint = fn_args.get("chief_complaint", "")
        if not appt_id:
            return json.dumps({"success": False, "error": "appointment_id required"}), None
        try:
            db.save_patient_intake(
                client_id, phone, int(appt_id),
                int(age) if age is not None else None,
                gender, chief_complaint,
            )
            return json.dumps({
                "success": True,
                "message": "Intake details saved. The doctor will review them before your appointment.",
            }), None
        except Exception as exc:
            logger.error("save_patient_intake error: %s", exc)
            return json.dumps({"success": False, "error": str(exc)}), None

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
            # ── Ownership check: verify this appointment belongs to this patient at this clinic ──
            owned = db.get_upcoming_appointment(client_id, phone)
            if not owned or owned["id"] != int(appt_id):
                logger.warning(
                    "cancel_appointment ownership check FAILED "
                    "(client=%s, phone=%s, requested_id=%s, owned_id=%s)",
                    client_id, phone, appt_id, owned["id"] if owned else "none",
                )
                return json.dumps({
                    "success": False,
                    "error": "Appointment not found for your account. Please call get_my_appointment first.",
                }), None

            freed_date = owned["appointment_date"]
            freed_slot = owned["slot_time"]
            db.cancel_appointment(int(appt_id), client_id=client_id)

            # ── Waitlist: auto-book next waiting patient for the freed slot ──
            waiter = db.pop_next_from_waitlist(client_id, freed_date, freed_slot)
            if waiter:
                try:
                    new_appt = db.create_appointment(
                        client_id,
                        waiter["patient_phone"],
                        waiter["patient_name"],
                        waiter["requested_date"],
                        waiter["requested_slot"],
                    )
                    info = _get_clinic_info(client_id)
                    client_pid   = client.get("whatsapp_phone_id") or settings.WHATSAPP_PHONE_ID
                    client_token = client.get("whatsapp_token") or None
                    try:
                        date_display = datetime.strptime(freed_date, "%Y-%m-%d").strftime("%A, %d %B %Y")
                    except Exception:
                        date_display = freed_date
                    notify_msg = (
                        f"🎉 *Great news, {waiter['patient_name']}!*\n\n"
                        f"A slot just opened up — your waitlisted appointment has been *automatically confirmed*!\n\n"
                        f"🏥 *{info['clinic_name']}*\n"
                        f"👨‍⚕️ {info['doctor_name']}\n"
                        f"📅 *{date_display}*\n"
                        f"⏰ *{freed_slot}*\n"
                        f"📍 {info['clinic_address']}\n\n"
                        f"See you then! 🙏 Reply *cancel* if you can no longer make it."
                    )
                    await whatsapp.send_text(
                        waiter["patient_phone"], notify_msg,
                        phone_id=client_pid, token=client_token,
                    )
                    logger.info(
                        "Waitlist auto-booked %s → appt %s (client=%s)",
                        waiter["patient_phone"], new_appt["id"], client_id,
                    )
                except Exception as we:
                    logger.error("Waitlist auto-book error: %s", we)

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
            # ── Ownership check: verify this appointment belongs to this patient at this clinic ──
            if not cur or cur["id"] != int(appt_id):
                logger.warning(
                    "reschedule_appointment ownership check FAILED "
                    "(client=%s, phone=%s, requested_id=%s, owned_id=%s)",
                    client_id, phone, appt_id, cur["id"] if cur else "none",
                )
                return json.dumps({
                    "success": False,
                    "error": "Appointment not found for your account. Please call get_my_appointment first.",
                }), None
            patient_name = cur["patient_name"] if cur else "Patient"
            freed_date   = cur["appointment_date"]
            freed_slot   = cur["slot_time"]

            new_appt = db.reschedule_appointment(client_id, int(appt_id), phone, patient_name, new_date, new_slot)

            # ── Waitlist: auto-book next waiting patient for the freed slot ──
            waiter = db.pop_next_from_waitlist(client_id, freed_date, freed_slot)
            if waiter:
                try:
                    wb_appt = db.create_appointment(
                        client_id,
                        waiter["patient_phone"],
                        waiter["patient_name"],
                        waiter["requested_date"],
                        waiter["requested_slot"],
                    )
                    info = _get_clinic_info(client_id)
                    client_pid   = client.get("whatsapp_phone_id") or settings.WHATSAPP_PHONE_ID
                    client_token = client.get("whatsapp_token") or None
                    try:
                        date_display = datetime.strptime(freed_date, "%Y-%m-%d").strftime("%A, %d %B %Y")
                    except Exception:
                        date_display = freed_date
                    notify_msg = (
                        f"🎉 *Great news, {waiter['patient_name']}!*\n\n"
                        f"A slot just opened up — your waitlisted appointment has been *automatically confirmed*!\n\n"
                        f"🏥 *{info['clinic_name']}*\n"
                        f"👨‍⚕️ {info['doctor_name']}\n"
                        f"📅 *{date_display}*\n"
                        f"⏰ *{freed_slot}*\n"
                        f"📍 {info['clinic_address']}\n\n"
                        f"See you then! 🙏 Reply *cancel* if you can no longer make it."
                    )
                    await whatsapp.send_text(
                        waiter["patient_phone"], notify_msg,
                        phone_id=client_pid, token=client_token,
                    )
                    logger.info(
                        "Waitlist auto-booked %s → appt %s (client=%s)",
                        waiter["patient_phone"], wb_appt["id"], client_id,
                    )
                except Exception as we:
                    logger.error("Waitlist auto-book (reschedule) error: %s", we)

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

    # ── Plan-gate check ────────────────────────────────────────────────────
    _plan_tier = client.get("plan", "starter").lower()
    if _plan_tier == "suite":
        _allowed_fns = _DOCTOR_SUITE_FNS
    elif _plan_tier == "pro":
        _allowed_fns = _DOCTOR_PRO_FNS
    else:
        _allowed_fns = _DOCTOR_STARTER_FNS
    if fn_name not in _allowed_fns:
        return json.dumps({
            "upgrade_required": True,
            "reply": _upsell_reply(fn_name, _plan_tier),
        })

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
                    db.cancel_appointment(appt["id"], client_id=client_id)
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

    elif fn_name == "save_visit_notes":
        notes         = (fn_args.get("notes") or "").strip()
        appt_id       = fn_args.get("appointment_id")
        patient_name  = (fn_args.get("patient_name") or "").strip()
        date_str      = (fn_args.get("date") or "").strip()
        slot_time     = (fn_args.get("slot_time") or "").strip()
        followup_days = int(fn_args.get("followup_days") or 2)

        if not notes:
            return json.dumps({"success": False, "error": "notes cannot be empty"})
        if followup_days < 0 or followup_days > 365:
            return json.dumps({"success": False, "error": "followup_days must be between 0 and 365"})

        # ── Resolve appointment ID if not directly provided ───────────────────
        if not appt_id:
            # Default to today if date not specified
            today_str = datetime.now(_IST).strftime("%Y-%m-%d")
            lookup_date = date_str or today_str

            if not patient_name and not slot_time:
                return json.dumps({
                    "success": False,
                    "error": (
                        "Please provide the patient's name (or appointment_id / slot_time) "
                        "so I can find the right appointment."
                    ),
                })

            matches = db.find_appointment_for_notes(
                client_id,
                date=lookup_date,
                patient_name=patient_name or None,
                slot_time=slot_time or None,
            )

            if not matches:
                detail = patient_name or slot_time
                return json.dumps({
                    "success": False,
                    "error": (
                        f"No appointment found for '{detail}' on {lookup_date}. "
                        "Please check the date or patient name."
                    ),
                })

            if len(matches) > 1:
                # Multiple hits — ask doctor to be more specific
                options = [
                    f"• {m['patient_name']} at {m['slot_time']} (ID {m['id']})"
                    for m in matches
                ]
                return json.dumps({
                    "success": False,
                    "needs_clarification": True,
                    "matches": [
                        {"appointment_id": m["id"], "patient_name": m["patient_name"],
                         "slot_time": m["slot_time"]}
                        for m in matches
                    ],
                    "message": (
                        f"Found {len(matches)} appointments on {lookup_date}. "
                        "Which patient did you mean?\n" + "\n".join(options)
                    ),
                })

            appt_id = matches[0]["id"]
            patient_name = matches[0]["patient_name"]
            lookup_date_for_msg = lookup_date

        # ── Save notes ────────────────────────────────────────────────────────
        try:
            db.save_visit_notes(client_id, int(appt_id), notes, followup_days)
            followup_msg = (
                f"Follow-up message will be sent in *{followup_days} day(s)*."
                if followup_days > 0
                else "No follow-up scheduled."
            )
            saved_for = patient_name or f"appointment {appt_id}"

            # ── Auto-send prescription PDF to patient ─────────────────────────
            try:
                appt_row = db.get_appointment_by_id(client_id, int(appt_id))
                if appt_row:
                    patient_phone = appt_row.get("patient_phone", "")
                    visit_date    = (appt_row.get("appointment_date") or
                                     datetime.now(_IST).strftime("%Y-%m-%d"))
                    p_name        = appt_row.get("patient_name") or patient_name or "Patient"
                    info          = _get_clinic_info(client_id)
                    pdf_bytes     = _build_prescription_pdf(
                        clinic_name    = info["clinic_name"],
                        doctor_name    = info["doctor_name"],
                        clinic_address = info["clinic_address"],
                        patient_name   = p_name,
                        visit_date     = visit_date,
                        notes          = notes,
                    )
                    if pdf_bytes and patient_phone:
                        safe_name = p_name.replace(" ", "_")
                        filename  = f"Prescription_{safe_name}_{visit_date}.pdf"
                        pid       = client.get("whatsapp_phone_id") or settings.WHATSAPP_PHONE_ID
                        tok       = client.get("whatsapp_token") or None
                        sent = await whatsapp.send_document(
                            to        = patient_phone,
                            pdf_bytes = pdf_bytes,
                            filename  = filename,
                            caption   = (
                                f"📋 Your visit summary from {info['clinic_name']}.\n"
                                f"Date: {visit_date}  ·  Doctor: {info['doctor_name']}"
                            ),
                            phone_id  = pid,
                            token     = tok,
                        )
                        if sent:
                            logger.info("[Prescription] PDF sent to %s for appt %s", patient_phone, appt_id)
                        else:
                            logger.warning("[Prescription] PDF send failed for appt %s", appt_id)
            except Exception as pdf_exc:
                logger.warning("[Prescription] PDF flow error for appt %s: %s", appt_id, pdf_exc)
                # Never fail the save_visit_notes call because of PDF issues

            return json.dumps({
                "success":        True,
                "appointment_id": appt_id,
                "patient_name":   patient_name,
                "followup_days":  followup_days,
                "message":        f"✅ Notes saved for *{saved_for}*. {followup_msg} Prescription PDF sent to patient.",
            })
        except Exception as exc:
            logger.error("save_visit_notes error: %s", exc)
            return json.dumps({"success": False, "error": str(exc)})

    elif fn_name == "view_patient_history":
        patient_phone = (fn_args.get("patient_phone") or "").strip()
        patient_name  = (fn_args.get("patient_name") or "").strip()

        def _fmt_visit_date(d: str) -> str:
            try:
                return datetime.strptime(d, "%Y-%m-%d").strftime("%d %b %Y")
            except Exception:
                return d

        def _build_visits(appt_rows: list[dict]) -> list[dict]:
            return [
                {
                    "appointment_id": a["id"],
                    "date":           _fmt_visit_date(a["appointment_date"]),
                    "slot_time":      a.get("slot_time", ""),
                    "status":         a.get("status", ""),
                    "visit_notes":    a.get("visit_notes") or "—",
                    "followup_days":  a.get("followup_days", 2),
                }
                for a in appt_rows
            ]

        # ── Case A: both phone AND name provided ──────────────────────────────
        # Most specific — skip all disambiguation, go straight to history.
        if patient_phone and patient_name:
            rows = db.get_patient_history_by_name_and_phone(
                client_id, patient_phone, patient_name
            )
            if not rows:
                # Fall back to all rows for that phone (name might be slightly different)
                rows = db.get_patient_history(client_id, patient_phone)
            return json.dumps({
                "found": True,
                "patient_phone": patient_phone,
                "patient_name":  patient_name,
                "total_visits":  len(rows),
                "visits":        _build_visits(rows),
            })

        # ── Case B: only phone provided ───────────────────────────────────────
        # Check whether multiple people share this phone (family members).
        if patient_phone and not patient_name:
            distinct = db.get_distinct_names_for_phone(client_id, patient_phone)
            if not distinct:
                return json.dumps({
                    "found": False,
                    "patient_phone": patient_phone,
                    "message": "No appointment history found for this phone number.",
                })
            if len(distinct) == 1:
                # Only one person — return directly
                patient_name = distinct[0]["patient_name"]
                rows = db.get_patient_history(client_id, patient_phone)
                return json.dumps({
                    "found":         True,
                    "patient_phone": patient_phone,
                    "patient_name":  patient_name,
                    "total_visits":  len(rows),
                    "visits":        _build_visits(rows),
                })
            else:
                # Multiple people share this phone — ask which one
                people = [
                    {
                        "name":        p["patient_name"],
                        "visit_count": p["visit_count"],
                        "first_visit": _fmt_visit_date(p["first_visit"] or ""),
                        "last_visit":  _fmt_visit_date(p["last_visit"] or ""),
                    }
                    for p in distinct
                ]
                return json.dumps({
                    "found":              False,
                    "needs_name":         True,
                    "patient_phone":      patient_phone,
                    "people_on_this_phone": people,
                    "message": (
                        f"This phone ({patient_phone}) is shared by "
                        f"{len(distinct)} patients. "
                        "Please tell me which person's history you want: "
                        + ", ".join(p["patient_name"] for p in distinct)
                    ),
                })

        # ── Case C: only name provided ────────────────────────────────────────
        # Look up by name. May find 0, 1, or many matches.
        if patient_name and not patient_phone:
            patients = db.search_patient_by_name(client_id, patient_name)
            if not patients:
                return json.dumps({
                    "found": False,
                    "message": f"No patient found with name '{patient_name}'.",
                })
            if len(patients) == 1:
                patient_phone = patients[0]["phone"]
                # Even with one name match, check if that phone is shared
                distinct = db.get_distinct_names_for_phone(client_id, patient_phone)
                rows = db.get_patient_history_by_name_and_phone(
                    client_id, patient_phone, patient_name
                ) if len(distinct) > 1 else db.get_patient_history(client_id, patient_phone)
                return json.dumps({
                    "found":         True,
                    "patient_phone": patient_phone,
                    "patient_name":  patient_name,
                    "total_visits":  len(rows),
                    "visits":        _build_visits(rows),
                })
            else:
                # Multiple patients with the same name — show phone + first-visit to help doctor pick
                matches = []
                for p in patients[:6]:
                    ph = p["phone"]
                    distinct = db.get_distinct_names_for_phone(client_id, ph)
                    info = next(
                        (d for d in distinct if d["patient_name"].lower() == patient_name.lower()),
                        None,
                    )
                    matches.append({
                        "name":        p["name"],
                        "phone":       ph,
                        "visit_count": info["visit_count"] if info else 0,
                        "first_visit": _fmt_visit_date(info["first_visit"]) if info else "—",
                        "last_visit":  _fmt_visit_date(info["last_visit"]) if info else "—",
                    })
                return json.dumps({
                    "found":           False,
                    "needs_phone":     True,
                    "multiple_matches": matches,
                    "message": (
                        f"Found {len(patients)} patients named '{patient_name}'. "
                        "Please provide the phone number (or more details) to pick the right one:\n"
                        + "\n".join(
                            f"• {m['name']} — 📱 {m['phone']} "
                            f"({m['visit_count']} visit(s), first: {m['first_visit']})"
                            for m in matches
                        )
                    ),
                })

        # ── Case D: nothing provided ──────────────────────────────────────────
        return json.dumps({
            "found": False,
            "message": "Please provide patient_phone or patient_name to look up history.",
        })

    return json.dumps({"error": f"Unknown doctor function: {fn_name}"})


# ── System prompts ────────────────────────────────────────────────────────────

def _build_system_prompt(client: dict) -> str:
    today     = datetime.now(_IST).strftime("%A, %d %B %Y")   # IST date for India
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
    else:
        # Starter: patients cannot self-cancel/reschedule — guide them to call, which nudges doctor to upgrade
        cancel_reschedule_rules = f"""
- If a patient asks to CANCEL or RESCHEDULE, politely say: "Online cancellations aren't available at {clinic_name} right now. Please call the clinic directly to make changes." Do NOT try to cancel or modify the appointment.
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
- Always ask for the patient's **full name** (first and last name) before booking. Do not proceed with booking using a single name like "Raj" — politely ask "Could you please share your full name?" and wait for the complete name.
- When a patient wants to book, use check_available_slots first — ALWAYS, without exception.
- If check_available_slots returns total_available=0 with a weekly-off note, tell the patient the clinic is closed that day and suggest the next available date. NEVER call create_appointment on a weekly-off day even if the patient insists.
- After the patient selects a slot, use create_appointment to confirm.
- After booking, confirm the date, time, and clinic address. A separate confirmation card will also be sent.
- If create_appointment returns can_cancel=true, end the confirmation with: "💡 To cancel or reschedule, just reply *CANCEL* or *RESCHEDULE* here anytime." — keep it as a single short line, never make it the focus.
- If no slots are available, suggest nearby dates.
- Do NOT make up appointment times — always check_available_slots first.
- For anything medical (diagnosis, medicines, dosage), say "Please consult {doctor_name} during your appointment."
- Respond in the same language the patient uses (Hindi or English).

Waitlist:
- If create_appointment returns slot_taken=true, immediately say the slot is fully booked and ask: "Would you like me to add you to the waitlist? You'll be automatically booked and notified if someone cancels." If yes, call join_waitlist.

New Patient Intake (IMPORTANT — only for new patients):
- If create_appointment returns is_new_patient=true, the patient is visiting for the very first time.
- After confirming their booking, say: "Since this is your first visit with us, could I note a few quick details for {doctor_name}? It helps the doctor prepare. 😊"
- Ask ONE question at a time in this order — do NOT ask all at once:
  1. "How old are you?"
  2. "And your gender? (Male / Female / Other)"
  3. "Lastly, what is the main reason for your visit today?"
- After collecting all three answers, call save_patient_intake with the appointment_id from create_appointment.
- Then say: "Thank you! {doctor_name} will have everything ready. See you at your appointment! 🙏"
- If the patient skips or declines to answer intake questions, that's completely fine — end warmly without pushing.
- Only collect intake ONCE (is_new_patient=true). Do not ask returning patients again.
{cancel_reschedule_rules}{custom_notes_section}"""


def _build_doctor_prompt(client: dict) -> str:
    today     = datetime.now(_IST).strftime("%A, %d %B %Y")   # IST date for India
    client_id = client["id"]
    info      = _get_clinic_info(client_id)
    per_clinic_off_raw = db.get_clinic_setting(client_id, "weekly_off_days") or ""
    per_clinic_off = [d.strip() for d in per_clinic_off_raw.split(",") if d.strip()]
    all_off_days = list(set(settings.WEEKLY_OFF_DAYS + per_clinic_off))
    off_days  = ", ".join(all_off_days) if all_off_days else "None"
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
- Save visit notes for a patient after their appointment (diagnosis, treatment, advice)
- View a patient's full visit history including all past notes
- Set follow-up timing per patient (default: 2 days after visit)

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
- Use emojis sparingly for clarity (✅ ❌ 🚫).
- Adding visit notes (most common workflow):
  The doctor does NOT need to know the appointment ID.
  Natural phrases to recognise and act on immediately:
    "Notes for Rahul: fever, prescribed paracetamol, rest 3 days"
    "Add notes for 10am patient: BP high, referred cardiologist"
    "Priya Sharma — cold, syrup prescribed, follow up in 5 days"
  → Call save_visit_notes with patient_name (and slot_time if mentioned).
    Date defaults to today. If the doctor specifies a different day, set date.
  → If the doctor says "follow up in X days", set followup_days=X. Default is 2.
  → Always echo back a one-line confirmation: "✅ Notes saved for Rahul Sharma. Follow-up in 2 days."
  → If two patients share the same name on that date, list both with slot times and ask which one.
- For view_patient_history: show a clear chronological list with each visit date and notes.
- When the doctor says "history of [patient]" or "past visits of [patient]", call view_patient_history."""


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

    # ── Fast-path: upgrade / pricing keyword ────────────────────────────────
    _lower = user_text.strip().lower().rstrip("?!")
    if _lower in _UPGRADE_KEYWORDS or any(kw in _lower.split() for kw in _UPGRADE_KEYWORDS):
        reply = _upgrade_plan_card()
        db.save_message(client_id, phone, "user", user_text)
        db.save_message(client_id, phone, "assistant", reply)
        return reply, None

    # ── Fast-path: referral / my code keyword ───────────────────────────────
    if _lower in _REFERRAL_KEYWORDS or any(kw in _lower for kw in _REFERRAL_KEYWORDS):
        reply = _referral_card(client_id)
        db.save_message(client_id, phone, "user", user_text)
        db.save_message(client_id, phone, "assistant", reply)
        return reply, None

    history   = db.get_conversation_history(client_id, phone, limit=settings.DOCTOR_HISTORY_LIMIT)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": _build_doctor_prompt(client)},
        *history,
        {"role": "user", "content": user_text},
    ]

    response = await _openai.chat.completions.create(
        model=settings.OPENAI_MODEL, messages=messages,
        tools=_get_doctor_tools(client.get("plan", "starter")), tool_choice="auto",
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

    history = db.get_conversation_history(client_id, phone, limit=settings.PATIENT_HISTORY_LIMIT)

    # ── Start-of-turn intake check ────────────────────────────────────────────
    # If this patient has a recent appointment without intake collected yet,
    # inject a hard reminder so the AI never silently skips collection.
    pending_intake_appt = db.get_pending_intake_appointment(client_id, phone)
    intake_reminder = ""
    if pending_intake_appt:
        pname = pending_intake_appt.get("patient_name", "the patient")
        appt_id = pending_intake_appt.get("id")
        intake_reminder = (
            f"\n\n🔴 INTAKE PENDING — appointment ID {appt_id} for {pname} "
            f"has no intake on file. Before anything else, ask for: "
            f"(1) age, (2) gender, (3) chief complaint — ONE at a time. "
            f"Once all three are collected, call save_patient_intake with appointment_id={appt_id}. "
            f"Do not skip this even if the patient tries to change the subject."
        )

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": _build_system_prompt(client) + intake_reminder},
        *history,
        {"role": "user", "content": user_text},
    ]

    active_tools = _get_patient_tools(client.get("plan", "starter"))
    response = await _openai.chat.completions.create(
        model=settings.OPENAI_MODEL, messages=messages,
        tools=active_tools, tool_choice="auto",
        max_tokens=600, temperature=0.7,
    )

    choice   = response.choices[0]
    appt_row = None

    while choice.finish_reason == "tool_calls":
        tool_calls = choice.message.tool_calls or []
        messages.append(choice.message)
        tool_results = []
        intake_injection: dict | None = None   # collected here, added AFTER tool_results

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

            # ── Inline intake injection (collected, NOT appended yet) ─────────
            # OpenAI requires tool results to immediately follow the assistant
            # tool_calls message — injecting a system message before them causes
            # an API error.  We collect the injection here and add it AFTER
            # messages.extend(tool_results) below.
            if fn_name == "create_appointment" and intake_injection is None:
                try:
                    result_data = json.loads(fn_result)
                    if result_data.get("success") and result_data.get("is_new_patient"):
                        new_appt_id = result_data.get("appointment_id")
                        new_name    = result_data.get("patient_name", "the patient")
                        info        = _get_clinic_info(client_id)
                        intake_injection = {
                            "role": "system",
                            "content": (
                                f"⚡ MANDATORY INTAKE — {new_name} is visiting {info['clinic_name']} "
                                f"for the FIRST TIME (appointment ID {new_appt_id}). "
                                f"After your booking confirmation message, you MUST ask: "
                                f"'Since this is your first visit, may I note a few quick details for "
                                f"Dr. {info['doctor_name']}? 😊' "
                                f"Then ask ONE question at a time: age → gender → chief complaint. "
                                f"After collecting all three, call save_patient_intake with "
                                f"appointment_id={new_appt_id}. "
                                f"Do NOT skip this step."
                            ),
                        }
                except Exception:
                    pass

        # Tool results MUST come immediately after the assistant tool_calls message.
        # Only after extending tool_results do we add any extra system guidance.
        messages.extend(tool_results)
        if intake_injection:
            messages.append(intake_injection)

        response = await _openai.chat.completions.create(
            model=settings.OPENAI_MODEL, messages=messages,
            tools=active_tools, tool_choice="auto",
            max_tokens=600, temperature=0.7,
        )
        choice = response.choices[0]

    reply_text = choice.message.content or "Sorry, I didn't understand that. Could you please repeat?"
    db.save_message(client_id, phone, "user", user_text)
    db.save_message(client_id, phone, "assistant", reply_text)
    return reply_text, appt_row
