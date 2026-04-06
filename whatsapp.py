"""
whatsapp.py - Meta Cloud API helpers for the Clinic AI Agent.
"""

from __future__ import annotations
import logging
from typing import Any, Optional
import httpx
from config import settings

logger = logging.getLogger(__name__)
BASE_URL = "https://graph.facebook.com/v19.0"


def parse_incoming_message(body: dict) -> Optional[dict]:
    try:
        entry = body.get("entry", [{}])[0]
        changes = entry.get("changes", [{}])[0]
        value = changes.get("value", {})
        messages = value.get("messages")
        if not messages:
            return None
        msg = messages[0]
        contacts = value.get("contacts", [{}])
        contact = contacts[0] if contacts else {}
        phone = msg.get("from", "")
        name = contact.get("profile", {}).get("name", "")
        msg_id = msg.get("id", "")
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
        return {"phone": phone, "name": name, "text": text, "message_id": msg_id, "type": msg_type}
    except Exception as exc:
        logger.error("Failed to parse incoming message: %s", exc)
        return None


async def send_text(phone: str, text: str) -> bool:
    payload = {"messaging_product": "whatsapp", "recipient_type": "individual", "to": phone, "type": "text", "text": {"preview_url": False, "body": text}}
    return await _post(payload)


async def send_slot_list(phone, header_text, body_text, footer_text, morning_slots, evening_slots):
    def _make_rows(slots):
        return [{"id": slot, "title": slot, "description": ""} for slot in slots]
    sections = []
    if morning_slots:
        sections.append({"title": "Morning", "rows": _make_rows(morning_slots)})
    if evening_slots:
        sections.append({"title": "Evening", "rows": _make_rows(evening_slots)})
    if not sections:
        await send_text(phone, "Sorry, no slots available. Please try another day.")
        return False
    payload = {"messaging_product": "whatsapp", "recipient_type": "individual", "to": phone, "type": "interactive", "interactive": {"type": "list", "header": {"type": "text", "text": header_text}, "body": {"text": body_text}, "footer": {"text": footer_text}, "action": {"button": "Choose a Slot", "sections": sections}}}
    return await _post(payload)


async def send_buttons(phone, body_text, buttons, header_text="", footer_text=""):
    interactive: dict[str, Any] = {"type": "button", "body": {"text": body_text}, "action": {"buttons": [{"type": "reply", "reply": {"id": b["id"], "title": b["title"]}} for b in buttons[:3]]}}
    if header_text:
        interactive["header"] = {"type": "text", "text": header_text}
    if footer_text:
        interactive["footer"] = {"text": footer_text}
    payload = {"messaging_product": "whatsapp", "recipient_type": "individual", "to": phone, "type": "interactive", "interactive": interactive}
    return await _post(payload)


async def mark_as_read(message_id: str) -> None:
    payload = {"messaging_product": "whatsapp", "status": "read", "message_id": message_id}
    await _post(payload)


async def _post(payload: dict) -> bool:
    url = f"{BASE_URL}/{settings.WHATSAPP_PHONE_ID}/messages"
    headers = {"Authorization": f"Bearer {settings.WHATSAPP_TOKEN}", "Content-Type": "application/json"}
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
