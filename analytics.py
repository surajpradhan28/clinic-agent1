"""
analytics.py — PostHog event tracking for MyWhatsApp Clinic.

All calls are fire-and-forget: if PostHog is not configured or fails,
nothing breaks — the error is logged silently and the bot continues.

Usage:
    import analytics
    analytics.track(phone, "appointment_booked", client_id=4, date="2026-06-15", slot="10:00")

Setup:
    Set POSTHOG_API_KEY in Railway env vars (get from posthog.com → Project Settings → API Keys).
    Optionally set POSTHOG_HOST (default: https://app.posthog.com).
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

logger = logging.getLogger(__name__)

_ph = None   # posthog client, lazily initialised

def _client():
    """Lazy-init the PostHog client once."""
    global _ph
    if _ph is not None:
        return _ph
    try:
        from config import settings
        api_key = getattr(settings, "POSTHOG_API_KEY", "")
        if not api_key:
            return None
        import posthog as _posthog_lib
        _posthog_lib.project_api_key = api_key
        _posthog_lib.host           = getattr(settings, "POSTHOG_HOST", "https://app.posthog.com")
        # Disable PostHog's own noisy logging
        logging.getLogger("posthog").setLevel(logging.WARNING)
        _ph = _posthog_lib
        logger.info("PostHog analytics initialised (host=%s)", _posthog_lib.host)
        return _ph
    except Exception as exc:
        logger.debug("PostHog not available: %s", exc)
        return None


def _distinct_id(phone: str) -> str:
    """
    Hash the phone number so raw phone numbers never leave the server.
    PostHog will show a consistent anonymous ID per patient.
    """
    return "ph_" + hashlib.sha256(phone.encode()).hexdigest()[:16]


def track(phone: str, event: str, **properties: Any) -> None:
    """
    Fire a PostHog event. Silently swallows all errors.

    Args:
        phone:       Patient / doctor phone number (hashed before sending).
        event:       Event name, e.g. "appointment_booked".
        **properties: Any extra key=value pairs attached to the event.
    """
    ph = _client()
    if ph is None:
        return
    try:
        ph.capture(
            distinct_id=_distinct_id(phone),
            event=event,
            properties=properties,
        )
    except Exception as exc:
        logger.debug("PostHog capture failed (%s): %s", event, exc)


# ── Convenience helpers (named so PostHog funnels are easy to set up) ─────────

def appointment_booked(phone: str, client_id: int, date: str, slot: str,
                       plan: str, is_new_patient: bool) -> None:
    track(phone, "appointment_booked",
          client_id=client_id, date=date, slot=slot,
          plan=plan, is_new_patient=is_new_patient)


def appointment_cancelled(phone: str, client_id: int, appointment_id: int,
                          plan: str) -> None:
    track(phone, "appointment_cancelled",
          client_id=client_id, appointment_id=appointment_id, plan=plan)


def slot_checked(phone: str, client_id: int, date: str,
                 slots_available: int) -> None:
    track(phone, "slot_checked",
          client_id=client_id, date=date, slots_available=slots_available)


def waitlist_joined(phone: str, client_id: int, date: str, slot: str) -> None:
    track(phone, "waitlist_joined",
          client_id=client_id, date=date, slot=slot)


def message_received(phone: str, client_id: int, is_doctor: bool) -> None:
    track(phone, "message_received",
          client_id=client_id, is_doctor=is_doctor)


def bot_error(phone: str, client_id: int, error: str, fn_name: str = "") -> None:
    track(phone, "bot_error",
          client_id=client_id, error=error[:200], fn_name=fn_name)
