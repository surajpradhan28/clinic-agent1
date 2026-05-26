"""
gcal.py — Google Calendar integration for Clinic AI Agent.

Flow:
  1. Doctor visits GET /calendar/connect/<dashboard_key>
     → redirected to Google OAuth consent screen
  2. Doctor approves → Google redirects to GET /calendar/callback?code=...&state=<key>
     → tokens stored in oauth_tokens table
     → WhatsApp confirmation sent to clinic
  3. Scheduler job _run_gcal_sync() runs every 15 min:
     → for each connected clinic, fetches Google Calendar freebusy for next 7 days
     → converts busy time ranges → clinic slot times
     → blocks matching slots with source='gcal'
     → removes stale gcal blocks (event deleted in Google)

Required env vars (Railway):
  GOOGLE_CLIENT_ID      — from Google Cloud Console → Credentials → OAuth 2.0 Client ID
  GOOGLE_CLIENT_SECRET  — same
  SERVER_URL            — e.g. https://clinic-agent1-production-9d21.up.railway.app
                          (callback = SERVER_URL + /calendar/callback)
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlencode

import httpx

import database as db
from config import settings

logger = logging.getLogger(__name__)

_IST  = timezone(timedelta(hours=5, minutes=30))
_SCOPES = "https://www.googleapis.com/auth/calendar.readonly"
_AUTH_URI    = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_URI   = "https://oauth2.googleapis.com/token"
_FREEBUSY_URI = "https://www.googleapis.com/calendar/v3/freeBusy"


# ── OAuth helpers ─────────────────────────────────────────────────────────────

def get_oauth_url(state: str) -> str:
    """
    Build the Google OAuth consent-screen URL.
    `state` should be the clinic's dashboard_key so we can identify the clinic
    in the callback.
    """
    if not settings.GOOGLE_CLIENT_ID:
        raise RuntimeError("GOOGLE_CLIENT_ID not configured")
    params = {
        "client_id":     settings.GOOGLE_CLIENT_ID,
        "redirect_uri":  _redirect_uri(),
        "response_type": "code",
        "scope":         _SCOPES,
        "access_type":   "offline",   # get refresh token
        "prompt":        "consent",   # always show consent so we get refresh_token
        "state":         state,
    }
    return f"{_AUTH_URI}?{urlencode(params)}"


def _redirect_uri() -> str:
    return f"{settings.SERVER_URL}/calendar/callback"


async def exchange_code(code: str, client_id: int, calendar_id: str = "primary") -> dict:
    """
    Exchange an OAuth authorization code for access + refresh tokens.
    Stores the tokens in the DB and returns the token dict.
    """
    async with httpx.AsyncClient() as client:
        resp = await client.post(_TOKEN_URI, data={
            "code":          code,
            "client_id":     settings.GOOGLE_CLIENT_ID,
            "client_secret": settings.GOOGLE_CLIENT_SECRET,
            "redirect_uri":  _redirect_uri(),
            "grant_type":    "authorization_code",
        })
        resp.raise_for_status()
        tokens = resp.json()

    # Store in DB
    expiry = datetime.now(timezone.utc) + timedelta(seconds=tokens.get("expires_in", 3600))
    db.store_oauth_token(
        client_id     = client_id,
        provider      = "google",
        access_token  = tokens["access_token"],
        refresh_token = tokens.get("refresh_token"),
        token_expiry  = expiry.isoformat(),
        calendar_id   = calendar_id,
    )
    logger.info("[GCal] Tokens stored for client=%s", client_id)
    return tokens


async def _get_valid_token(client_id: int) -> Optional[str]:
    """
    Return a valid access token for the clinic, refreshing if expired.
    Returns None if no token is stored.
    """
    row = db.get_oauth_token(client_id, "google")
    if not row:
        return None

    expiry_str = row.get("token_expiry")
    if expiry_str:
        expiry = datetime.fromisoformat(expiry_str)
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        # Refresh if within 5 minutes of expiry
        if datetime.now(timezone.utc) >= expiry - timedelta(minutes=5):
            return await _refresh_token(client_id, row)

    return row["access_token"]


async def _refresh_token(client_id: int, row: dict) -> Optional[str]:
    """Refresh an expired access token using the stored refresh token."""
    refresh_token = row.get("refresh_token")
    if not refresh_token:
        logger.warning("[GCal] No refresh token for client=%s — re-auth needed", client_id)
        return None

    async with httpx.AsyncClient() as client:
        resp = await client.post(_TOKEN_URI, data={
            "refresh_token": refresh_token,
            "client_id":     settings.GOOGLE_CLIENT_ID,
            "client_secret": settings.GOOGLE_CLIENT_SECRET,
            "grant_type":    "refresh_token",
        })
        if resp.status_code != 200:
            logger.error("[GCal] Token refresh failed for client=%s: %s", client_id, resp.text)
            return None
        tokens = resp.json()

    expiry = datetime.now(timezone.utc) + timedelta(seconds=tokens.get("expires_in", 3600))
    db.store_oauth_token(
        client_id     = client_id,
        provider      = "google",
        access_token  = tokens["access_token"],
        refresh_token = tokens.get("refresh_token") or refresh_token,  # keep old if not returned
        token_expiry  = expiry.isoformat(),
        calendar_id   = row.get("calendar_id", "primary"),
    )
    logger.info("[GCal] Token refreshed for client=%s", client_id)
    return tokens["access_token"]


# ── FreeBusy query ────────────────────────────────────────────────────────────

async def get_busy_periods(
    client_id: int,
    date_from: datetime,
    date_to: datetime,
) -> list[tuple[datetime, datetime]]:
    """
    Query Google Calendar FreeBusy for the given time range.
    Returns a list of (start, end) datetime tuples (UTC) when the doctor is busy.
    """
    token = await _get_valid_token(client_id)
    if not token:
        return []

    row = db.get_oauth_token(client_id, "google")
    calendar_id = (row or {}).get("calendar_id", "primary")

    body = {
        "timeMin": date_from.isoformat(),
        "timeMax": date_to.isoformat(),
        "items":   [{"id": calendar_id}],
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            _FREEBUSY_URI,
            json=body,
            headers={"Authorization": f"Bearer {token}"},
        )
        if resp.status_code != 200:
            logger.error("[GCal] FreeBusy error for client=%s: %s", client_id, resp.text)
            return []
        data = resp.json()

    busy_list = data.get("calendars", {}).get(calendar_id, {}).get("busy", [])
    result = []
    for period in busy_list:
        start = datetime.fromisoformat(period["start"].replace("Z", "+00:00"))
        end   = datetime.fromisoformat(period["end"].replace("Z", "+00:00"))
        result.append((start, end))

    logger.debug("[GCal] client=%s busy periods: %d", client_id, len(result))
    return result


# ── Slot overlap logic ────────────────────────────────────────────────────────

def _get_all_slots_ist() -> list[str]:
    """
    Generate all clinic slot times (HH:MM) from config.
    Matches the same logic as agent.py check_available_slots.
    """
    from config import settings as s
    slots = []
    for range_start, range_end in [
        (s.MORNING_START, s.MORNING_END),
        (s.EVENING_START, s.EVENING_END),
    ]:
        h, m = map(int, range_start.split(":"))
        eh, em = map(int, range_end.split(":"))
        end_minutes = eh * 60 + em
        cur = h * 60 + m
        while cur < end_minutes:
            slots.append(f"{cur // 60:02d}:{cur % 60:02d}")
            cur += s.SLOT_DURATION_MIN
    return slots


def busy_slots_for_date(
    busy_periods: list[tuple[datetime, datetime]],
    date_ist: datetime.date,
) -> list[str]:
    """
    Given a list of UTC busy periods and an IST date, return which clinic
    slot times (HH:MM IST) are blocked by those periods.

    A slot is blocked if any busy period overlaps with [slot_start, slot_end).
    """
    all_slots = _get_all_slots_ist()
    blocked = []
    duration = timedelta(minutes=settings.SLOT_DURATION_MIN)

    for slot_str in all_slots:
        sh, sm = map(int, slot_str.split(":"))
        # Build slot window in IST, then convert to UTC for comparison
        slot_start_ist = datetime(
            date_ist.year, date_ist.month, date_ist.day,
            sh, sm, tzinfo=_IST,
        )
        slot_end_ist = slot_start_ist + duration
        # Convert to UTC
        slot_start_utc = slot_start_ist.astimezone(timezone.utc)
        slot_end_utc   = slot_end_ist.astimezone(timezone.utc)

        for busy_start, busy_end in busy_periods:
            # Overlap: busy starts before slot ends AND busy ends after slot starts
            if busy_start < slot_end_utc and busy_end > slot_start_utc:
                blocked.append(slot_str)
                break  # no need to check other busy periods for this slot

    return blocked


# ── Main sync function ────────────────────────────────────────────────────────

async def sync_calendar_blocks(client_id: int) -> dict:
    """
    Core sync: fetch Google Calendar freebusy for next 7 days, convert to
    slot times, block new ones, unblock removed ones.

    Returns a summary dict: {blocked_new: int, unblocked: int, days_synced: int}
    """
    now_utc    = datetime.now(timezone.utc)
    now_ist    = now_utc.astimezone(_IST)
    date_from  = now_utc
    date_to    = now_utc + timedelta(days=7)

    busy_periods = await get_busy_periods(client_id, date_from, date_to)

    blocked_new = 0
    unblocked   = 0

    # Process each of the next 7 days
    for day_offset in range(7):
        check_date = (now_ist + timedelta(days=day_offset)).date()
        date_str   = check_date.strftime("%Y-%m-%d")

        new_gcal_slots = busy_slots_for_date(busy_periods, check_date)

        # Get currently gcal-blocked slots for this date
        current_gcal_blocks = db.get_gcal_blocked_slots(client_id, date_str)

        # Slots to block: in Google but not yet in DB
        to_block = [s for s in new_gcal_slots if s not in current_gcal_blocks]

        # Slots to unblock: in DB as gcal but no longer in Google
        to_unblock = [s for s in current_gcal_blocks if s not in new_gcal_slots]

        if to_block:
            db.block_slots_gcal(client_id, date_str, to_block)
            blocked_new += len(to_block)

        if to_unblock:
            db.unblock_gcal_slots(client_id, date_str, to_unblock)
            unblocked += len(to_unblock)

    logger.info(
        "[GCal] Sync done for client=%s: +%d blocked, -%d unblocked",
        client_id, blocked_new, unblocked,
    )
    return {"blocked_new": blocked_new, "unblocked": unblocked, "days_synced": 7}
