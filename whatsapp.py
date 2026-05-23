"""
whatsapp.py — Meta Cloud API helpers (multi-tenant v4).

Key change from v3: send functions now accept an optional `phone_id` argument.
When provided, messages are sent from that clinic's WhatsApp number.
Falls back to settings.WHATSAPP_PHONE_ID if omitted (backwards compatible).

Covers:
  - Parsing incoming messages (text + interactive replies) + phone_number_id extraction
  - Sending text messages
  - Sending WhatsApp interactive LIST messages (for slot selection)
  - Sending interactive BUTTON messages
  - Marking messages as read
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

    Returns a dict with keys: phone, name, text, message_id, type, phone_number_id
      - phone_number_id: the clinic's Meta phone_number_id that received the message.
                         Used to resolve which client (clinic) this message belongs to.

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

        # ← The clinic's Meta phone number ID (identifies WHICH clinic was messaged)
        phone_number_id = value.get("metadata", {}).get("phone_number_id", "")

        phone    = msg.get("from", "")
        name     = contact.get("profile", {}).get("name", "")
        msg_id   = msg.get("id", "")
        msg_type = msg.get("type", "text")

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
            text = ""

        return {
            "phone":           phone,
            "name":            name,
            "text":            text,
            "message_id":      msg_id,
            "type":            msg_type,
            "phone_number_id": phone_number_id,   # ← NEW: clinic identifier
        }
    except Exception as exc:
        logger.error("Failed to parse incoming message: %s", exc)
        return None


# ── Send text message ─────────────────────────────────────────────────────────

async def send_text(
    phone: str,
    text: str,
    phone_id: str | None = None,
    token: str | None = None,
) -> bool:
    """
    Send a plain text WhatsApp message. Returns True on success.
    phone_id: the sending clinic's Meta phone_number_id.
              Defaults to settings.WHATSAPP_PHONE_ID if not provided.
    token: the clinic's WhatsApp access token.
           Defaults to settings.WHATSAPP_TOKEN if not provided.
    """
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": phone,
        "type": "text",
        "text": {"preview_url": False, "body": text},
    }
    return await _post(payload, phone_id=phone_id, token=token)


# ── Send interactive LIST message ─────────────────────────────────────────────

async def send_slot_list(
    phone: str,
    header_text: str,
    body_text: str,
    footer_text: str,
    morning_slots: list[str],
    evening_slots: list[str],
    phone_id: str | None = None,
    token: str | None = None,
) -> bool:
    def _make_rows(slots: list[str]) -> list[dict]:
        return [{"id": slot, "title": slot, "description": ""} for slot in slots]

    sections = []
    if morning_slots:
        sections.append({"title": "Morning", "rows": _make_rows(morning_slots)})
    if evening_slots:
        sections.append({"title": "Evening", "rows": _make_rows(evening_slots)})

    if not sections:
        await send_text(phone, "Sorry, no slots available for that date. Please try another day.", phone_id=phone_id, token=token)
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
    return await _post(payload, phone_id=phone_id, token=token)


# ── Send interactive BUTTON message ──────────────────────────────────────────

async def send_buttons(
    phone: str,
    body_text: str,
    buttons: list[dict],
    header_text: str = "",
    footer_text: str = "",
    phone_id: str | None = None,
    token: str | None = None,
) -> bool:
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
    return await _post(payload, phone_id=phone_id, token=token)


# ── Mark message as read ──────────────────────────────────────────────────────

async def mark_as_read(
    message_id: str,
    phone_id: str | None = None,
    token: str | None = None,
) -> None:
    payload = {
        "messaging_product": "whatsapp",
        "status": "read",
        "message_id": message_id,
    }
    await _post(payload, phone_id=phone_id, token=token)


# ── Internal HTTP helper ──────────────────────────────────────────────────────

async def _post(
    payload: dict,
    phone_id: str | None = None,
    token: str | None = None,
) -> bool:
    """
    POST to the Meta Graph API.
    phone_id: which clinic's number is sending (defaults to global WHATSAPP_PHONE_ID).
    token: per-client access token (defaults to global WHATSAPP_TOKEN).
    """
    pid = phone_id or settings.WHATSAPP_PHONE_ID
    tok = token or settings.WHATSAPP_TOKEN
    url = f"{BASE_URL}/{pid}/messages"
    headers = {
        "Authorization": f"Bearer {tok}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            response = await client.post(url, json=payload, headers=headers)
            if response.status_code not in (200, 201):
                logger.error("WhatsApp API error %s: %s", response.status_code, response.text)
                return False
            return True
        except httpx.HTTPError as exc:
            logger.error("WhatsApp HTTP error: %s", exc)
            return False
