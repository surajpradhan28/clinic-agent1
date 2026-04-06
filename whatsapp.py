"""
whatsapp.py — Meta Cloud API helpers.

Covers:
  - Webhook signature verification
  - Parsing incoming messages (text + interactive replies)
  - Sending text messages
  - Sending WhatsApp interactive LIST messages (for slot selection)
  - Sending template messages (future-proofing)
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

from config import settings

logger = logging.getLogger(__name__)

BASE_URL = "https://graph.facebook.com/v19.0"


# ── Parse incoming webhook payload ───────────────────────────────────────────

def parse_incoming_message(body: dict) -> Optional[dict]:
    """
    Extract the first message from a Meta webhook payload.

    Returns a dict with keys: phone, name, text, message_id, type
    Returns None if there is no user message (e.g. status update).
    """
    try:
        entry = body.get("entry", [{}])[0]
        changes = entry.get("changes", [{}])[0]
        value = changes.get("value", {})

        messages = value.get("messages")
        if not messages:
            return None  # Status update, not a user message

        msg = messages[0]
        contacts = value.get("contacts", [{}])
        contact = contacts[0] if contacts else {}

        phone = msg.get("from", "")
        name = contact.get("profile", {}).get("name", "")
        msg_id = msg.get("id", "")
        msg_type = msg.get("type", "text")

        # Resolve text from different message types
        if msg_type == "text":
            text = msg.get("text", {}).get("body", "").strip()
        elif msg_type == "interactive":
            interactive = msg.get("interactive", {})
            i_type = interactive.get("type", "")
            if i_type == "list_reply":
                text = interactive.get("list_reply", {}).get("title", "").strip()
            elif i_type == "button_reply":
                text = interactive.get("button_reply", {}).get("title", "").strip()
            else:
                text = ""
        else:
            # Unsupported type (image, audio, etc.) — treat as empty
            text = ""

        return {
            "phone": phone,
            "name": name,
            "text": text,
            "message_id": msg_id,
            "type": msg_type,
        }
    except Exception as exc:
        logger.error("Failed to parse incoming message: %s", exc)
        return None


# ── Send text message ─────────────────────────────────────────────────────────

async def send_text(phone: str, text: str) -> bool:
    """Send a plain text WhatsApp message. Returns True on success."""
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": phone,
        "type": "text",
        "text": {"preview_url": False, "body": text},
    }
    return await _post(payload)


# ── Send interactive LIST message ─────────────────────────────────────────────

async def send_slot_list(
    phone: str,
    header_text: str,
    body_text: str,
    footer_text: str,
    morning_slots: list[str],
    evening_slots: list[str],
) -> bool:
    """
    Send a WhatsApp List message with morning + evening slot sections.
    Each slot becomes a selectable list item.
    """
    def _make_rows(slots: list[str]) -> list[dict]:
        return [
            {"id": slot, "title": slot, "description": ""}
            for slot in slots
        ]

    sections = []
    if morning_slots:
        sections.append({"title": "🌅 Morning", "rows": _make_rows(morning_slots)})
    if evening_slots:
        sections.append({"title": "🌆 Evening", "rows": _make_rows(evening_slots)})

    if not sections:
        await send_text(phone, "Sorry, no slots available for that date. Please try another day.")
        return False

    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": phone,
        "type": "interactive",
        "interactive": {
            "type": "list",
            "header": {"type": "text", "text": header_text},
            "body": {"text": body_text},
            "footer": {"text": footer_text},
            "action": {
                "button": "Choose a Slot",
                "sections": sections,
            },
        },
    }
    return await _post(payload)


# ── Send interactive BUTTON message ──────────────────────────────────────────

async def send_buttons(
    phone: str,
    body_text: str,
    buttons: list[dict],  # [{"id": "...", "title": "..."}]
    header_text: str = "",
    footer_text: str = "",
) -> bool:
    """Send up to 3 quick-reply buttons."""
    interactive: dict[str, Any] = {
        "type": "button",
        "body": {"text": body_text},
        "action": {
            "buttons": [
                {"type": "reply", "reply": {"id": b["id"], "title": b["title"]}}
                for b in buttons[:3]
            ]
        },
    }
    if header_text:
        interactive["header"] = {"type": "text", "text": header_text}
    if footer_text:
        interactive["footer"] = {"text": footer_text}

    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": phone,
        "type": "interactive",
        "interactive": interactive,
    }
    return await _post(payload)


# ── Mark message as read ──────────────────────────────────────────────────────

async def mark_as_read(message_id: str) -> None:
    """Send read receipt for a message."""
    payload = {
        "messaging_product": "whatsapp",
        "status": "read",
        "message_id": message_id,
    }
    await _post(payload)


# ── Internal HTTP helper ──────────────────────────────────────────────────────

async def _post(payload: dict) -> bool:
    url = f"{BASE_URL}/{settings.WHATSAPP_PHONE_ID}/messages"
    headers = {
        "Authorization": f"Bearer {settings.WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            response = await client.post(url, json=payload, headers=headers)
            if response.status_code not in (200, 201):
                logger.error(
                    "WhatsApp API error %s: %s", response.status_code, response.text
                )
                return False
            return True
        except httpx.HTTPError as exc:
            logger.error("WhatsApp HTTP error: %s", exc)
            return False
