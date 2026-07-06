"""
clinic_dashboard.py — Per-clinic read-only web dashboard.

Renders a self-contained HTML page for a single clinic identified by
their private dashboard_key.  No patient data from other clinics is ever
exposed — every query is scoped to client_id.

Used by:  GET /clinic?key=<dashboard_key>  (in main.py)
"""

from __future__ import annotations

import html
import json as _json
import logging
from datetime import datetime, timedelta, timezone, date

import database as db

logger = logging.getLogger(__name__)

_IST = timezone(timedelta(hours=5, minutes=30))


# ── Helpers ───────────────────────────────────────────────────────────────────

def _ist_now() -> datetime:
    return datetime.now(_IST)


def _fmt_date(date_str: str) -> str:
    """'2025-05-24' → 'Sat, 24 May 2025'"""
    try:
        d = date.fromisoformat(date_str)
        return d.strftime("%a, %d %b %Y")
    except Exception:
        return date_str


def _fmt_time(time_str: str) -> str:
    """'09:30' → '9:30 AM'"""
    try:
        t = datetime.strptime(time_str, "%H:%M")
        return t.strftime("%-I:%M %p")
    except Exception:
        return time_str


def _status_badge(status: str) -> str:
    colour = {
        "confirmed":  ("#e6f4ea", "#1e7e34"),
        "completed":  ("#e8f0fe", "#1967d2"),
        "cancelled":  ("#fce8e6", "#c5221f"),
    }.get(status, ("#f1f3f4", "#5f6368"))
    bg, fg = colour
    label = status.capitalize()
    return (
        f'<span style="background:{bg};color:{fg};padding:2px 8px;'
        f'border-radius:12px;font-size:12px;font-weight:600;">{html.escape(label)}</span>'
    )


def _esc(s) -> str:
    return html.escape(str(s or ""))


# ── HTML renderer ─────────────────────────────────────────────────────────────

def render_clinic_dashboard(client: dict, dashboard_key: str = "") -> str:
    """
    Build and return the full HTML page for a clinic's dashboard.
    All data is freshly fetched from Supabase, scoped strictly to client["id"].
    dashboard_key is embedded in the page so JS edit calls can authenticate.
    """
    client_id   = client["id"]
    now_ist     = _ist_now()
    today_str   = now_ist.strftime("%Y-%m-%d")
    week_end    = (now_ist + timedelta(days=6)).strftime("%Y-%m-%d")

    # ── Fetch data ────────────────────────────────────────────────────────────
    info        = db.get_all_clinic_settings(client_id)
    clinic_name = info.get("clinic_name") or client.get("name", "Clinic")
    doctor_name = info.get("doctor_name") or client.get("doctor_name", "Doctor")
    clinic_addr = info.get("clinic_address") or ""
    clinic_ph   = info.get("clinic_phone") or ""

    # Clinic initials for avatar (e.g. "Nishat Sheikh Clinic" → "NS")
    _words = (clinic_name or "Clinic").split()
    clinic_initials = "".join(w[0].upper() for w in _words if w)[:2]

    stats           = db.get_dashboard_stats(client_id)
    today_appts     = db.get_appointments_for_date(client_id, today_str)
    week_appts      = db.get_appointments_range(client_id, today_str, week_end)
    recent_activity = db.get_recent_activity(client_id, limit=15)
    subscription    = db.get_active_subscription(client_id)
    try:
        visit_notes_history = db.get_patient_history_all(client_id, limit=200)
    except Exception as _e:
        logger.warning("Dashboard: could not load patient history: %s", _e)
        visit_notes_history = []

    try:
        referral_stats = db.get_referral_stats(client_id)
    except Exception as _e:
        logger.warning("Dashboard: could not load referral stats: %s", _e)
        referral_stats = {}

    try:
        followup_responses = db.get_followup_responses(client_id, limit=30)
    except Exception as _e:
        logger.warning("Dashboard: followup_responses error: %s", _e)
        followup_responses = []

    try:
        monthly_counts = db.get_monthly_appointment_counts(client_id, months=6)
    except Exception as _e:
        logger.warning("Dashboard: monthly_counts error: %s", _e)
        monthly_counts = []

    try:
        cancel_stats = db.get_cancellation_stats(client_id)
    except Exception as _e:
        logger.warning("Dashboard: cancel_stats error: %s", _e)
        cancel_stats = {"total": 0, "completed": 0, "cancelled": 0, "confirmed": 0, "cancel_rate": 0}

    try:
        waitlist_rows = db.get_waitlist_summary(client_id, limit=30)
    except Exception as _e:
        logger.warning("Dashboard: waitlist error: %s", _e)
        waitlist_rows = []

    try:
        invoice_rows = db.get_invoices_for_client(client_id, limit=6)
    except Exception as _e:
        logger.warning("Dashboard: invoices error: %s", _e)
        invoice_rows = []

    try:
        upcoming_followups = db.get_upcoming_followups(client_id, days=4)
    except Exception as _e:
        logger.warning("Dashboard: upcoming_followups error: %s", _e)
        upcoming_followups = []

    try:
        intake_rows = db.get_recent_intakes(client_id, limit=20)
    except Exception as _e:
        logger.warning("Dashboard: intake_rows error: %s", _e)
        intake_rows = []

    from config import settings as _settings
    referral_code  = referral_stats.get("referral_code") or "—"
    referral_link  = (
        f"{_settings.SERVER_URL}/signup?ref={referral_code}"
        if referral_code != "—" else ""
    )
    ref_signups       = referral_stats.get("total_signups", 0)
    ref_paid          = referral_stats.get("total_paid", 0)
    ref_pending_mo    = referral_stats.get("pending_months", 0)
    ref_applied_mo    = referral_stats.get("applied_months", 0)
    ref_total_earned  = ref_pending_mo + ref_applied_mo

    # ── Sub-render helpers ────────────────────────────────────────────────────

    def _stat_card(value, label, icon, color):
        display_val = value if value is not None else 0
        return f"""
        <div class="stat-card" style="border-top:4px solid {color}">
          <div class="stat-icon-wrap" style="background:{color}1a">{icon}</div>
          <div class="stat-value" style="color:{color}">{_esc(display_val)}</div>
          <div class="stat-label">{_esc(label)}</div>
        </div>"""

    def _today_rows():
        if not today_appts:
            return '<tr><td colspan="2" class="empty-row">No appointments today</td></tr>'
        rows = ""
        for a in today_appts:
            rows += f"""
            <tr>
              <td><strong>{_esc(_fmt_time(a['slot_time']))}</strong></td>
              <td>{_esc(a['patient_name'])}</td>
            </tr>"""
        return rows

    def _week_rows():
        # Group by date
        from collections import defaultdict
        by_date: dict[str, list] = defaultdict(list)
        for a in week_appts:
            if a["appointment_date"] != today_str:  # today already shown above
                by_date[a["appointment_date"]].append(a)

        if not by_date:
            return '<tr><td colspan="3" class="empty-row">No upcoming appointments this week</td></tr>'

        rows = ""
        for d in sorted(by_date.keys()):
            for a in by_date[d]:
                rows += f"""
                <tr>
                  <td>{_esc(_fmt_date(d))}</td>
                  <td><strong>{_esc(_fmt_time(a['slot_time']))}</strong></td>
                  <td>{_esc(a['patient_name'])}</td>
                </tr>"""
        return rows

    def _activity_rows():
        if not recent_activity:
            return '<tr><td colspan="3" class="empty-row">No recent activity</td></tr>'
        rows = ""
        for a in recent_activity:
            rows += f"""
            <tr>
              <td>{_esc(_fmt_date(a['appointment_date']))}<br>
                  <small style="color:#888">{_esc(_fmt_time(a['slot_time']))}</small>
              </td>
              <td>{_esc(a['patient_name'])}</td>
              <td>{_status_badge(a['status'])}</td>
            </tr>"""
        return rows

    # ── Follow-up tracker ─────────────────────────────────────────────────────
    def _followup_tracker_rows():
        if not followup_responses:
            return '<tr><td colspan="4" class="empty-row">No follow-ups sent yet.</td></tr>'
        rows = ""
        for f in followup_responses:
            appt      = f.get("appointments") or {}
            pname     = _esc(appt.get("patient_name") or "—")
            appt_date = _esc(_fmt_date(appt.get("appointment_date") or ""))
            sentiment = (f.get("sentiment") or "").lower()
            response  = _esc(f.get("patient_response") or "—")
            status    = f.get("status", "sent")

            if sentiment == "positive":
                badge = '<span style="background:#e6f4ea;color:#1e7e34;padding:2px 8px;border-radius:12px;font-size:11px;font-weight:600">Recovered ✓</span>'
            elif sentiment == "negative":
                badge = '<span style="background:#fce8e6;color:#c5221f;padding:2px 8px;border-radius:12px;font-size:11px;font-weight:600">Getting worse ⚠</span>'
            elif sentiment == "neutral":
                badge = '<span style="background:#e8f0fe;color:#1967d2;padding:2px 8px;border-radius:12px;font-size:11px;font-weight:600">Same</span>'
            elif status == "sent":
                badge = '<span style="background:#fef3c7;color:#92400e;padding:2px 8px;border-radius:12px;font-size:11px;font-weight:600">No reply yet</span>'
            else:
                badge = '<span style="background:#f3f4f6;color:#6b7280;padding:2px 8px;border-radius:12px;font-size:11px;font-weight:600">—</span>'

            rows += f"""
            <tr>
              <td><strong>{pname}</strong></td>
              <td>{appt_date}</td>
              <td>{badge}</td>
              <td style="max-width:220px;font-size:12px;color:#6b7280;word-break:break-word">{response[:80] + ('…' if len(response) > 80 else '')}</td>
            </tr>"""
        return rows

    # ── Trend chart (Chart.js canvas) ────────────────────────────────────────
    def _trend_chart():
        if not monthly_counts:
            return '<p class="empty-row" style="padding:40px;text-align:center;color:#9ca3af">No appointment data yet.</p>'
        labels = [r["month"] for r in monthly_counts]
        counts = [r["count"] for r in monthly_counts]
        chart_json = html.escape(_json.dumps({"labels": labels, "data": counts}), quote=True)
        return f'<div style="padding:16px 20px 20px"><canvas id="trendChart" height="160" data-chart="{chart_json}"></canvas></div>'

    # ── Cancellation stats ────────────────────────────────────────────────────
    def _cancel_stats_html():
        total     = cancel_stats.get("total", 0)
        completed = cancel_stats.get("completed", 0)
        cancelled = cancel_stats.get("cancelled", 0)
        confirmed = cancel_stats.get("confirmed", 0)
        rate      = cancel_stats.get("cancel_rate", 0)
        comp_pct  = round(completed / total * 100) if total else 0
        return f"""
        <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:0">
          <div style="padding:16px 20px;border-bottom:1px solid #f3f4f6">
            <div style="font-size:11px;text-transform:uppercase;color:#9ca3af;font-weight:600;letter-spacing:.5px;margin-bottom:4px">Total (this month)</div>
            <div style="font-size:28px;font-weight:700;color:#1f2937">{total}</div>
          </div>
          <div style="padding:16px 20px;border-bottom:1px solid #f3f4f6">
            <div style="font-size:11px;text-transform:uppercase;color:#9ca3af;font-weight:600;letter-spacing:.5px;margin-bottom:4px">Completed</div>
            <div style="font-size:28px;font-weight:700;color:#1e7e34">{completed}</div>
            <div style="font-size:12px;color:#6b7280">{comp_pct}% completion rate</div>
          </div>
          <div style="padding:16px 20px;border-bottom:1px solid #f3f4f6">
            <div style="font-size:11px;text-transform:uppercase;color:#9ca3af;font-weight:600;letter-spacing:.5px;margin-bottom:4px">Cancelled</div>
            <div style="font-size:28px;font-weight:700;color:#c5221f">{cancelled}</div>
            <div style="font-size:12px;color:#6b7280">{rate}% cancellation rate</div>
          </div>
          <div style="padding:16px 20px;border-bottom:1px solid #f3f4f6">
            <div style="font-size:11px;text-transform:uppercase;color:#9ca3af;font-weight:600;letter-spacing:.5px;margin-bottom:4px">Upcoming (confirmed)</div>
            <div style="font-size:28px;font-weight:700;color:#1967d2">{confirmed}</div>
          </div>
        </div>"""

    # ── Waitlist ──────────────────────────────────────────────────────────────
    def _waitlist_rows():
        if not waitlist_rows:
            return '<tr><td colspan="4" class="empty-row">No patients on the waitlist.</td></tr>'
        rows = ""
        for w in waitlist_rows:
            try:
                from datetime import datetime as _dt
                waiting_since = _dt.fromisoformat(w["created_at"].replace("Z", "+00:00"))
                delta = datetime.now(timezone.utc) - waiting_since
                if delta.days == 0:
                    since_str = "Today"
                elif delta.days == 1:
                    since_str = "1 day ago"
                else:
                    since_str = f"{delta.days} days ago"
            except Exception:
                since_str = "—"
            rows += f"""
            <tr>
              <td><strong>{_esc(w['patient_name'])}</strong><br>
                  <small style="color:#6b7280">{_esc(w['patient_phone'])}</small></td>
              <td>{_esc(_fmt_date(w['requested_date']))}</td>
              <td><strong>{_esc(_fmt_time(w['requested_slot']))}</strong></td>
              <td style="color:#9ca3af;font-size:12px">{_esc(since_str)}</td>
            </tr>"""
        return rows

    # ── Invoice history ───────────────────────────────────────────────────────
    def _invoice_rows():
        if not invoice_rows:
            return '<tr><td colspan="4" class="empty-row">No invoices yet.</td></tr>'
        rows = ""
        for inv in invoice_rows:
            period = _esc(_fmt_date(inv.get("period_start", "")))
            try:
                period = datetime.strptime(inv["period_start"][:7], "%Y-%m").strftime("%B %Y")
            except Exception:
                pass
            amount  = f"₹{float(inv.get('amount', 0)):,.0f}"
            inv_status = (inv.get("status") or "").lower()
            if inv_status == "paid":
                sbadge = '<span style="background:#e6f4ea;color:#1e7e34;padding:2px 8px;border-radius:12px;font-size:11px;font-weight:600">Paid</span>'
            elif inv_status == "overdue":
                sbadge = '<span style="background:#fce8e6;color:#c5221f;padding:2px 8px;border-radius:12px;font-size:11px;font-weight:600">Overdue</span>'
            else:
                sbadge = '<span style="background:#fef3c7;color:#92400e;padding:2px 8px;border-radius:12px;font-size:11px;font-weight:600">Unpaid</span>'
            token = inv.get("invoice_token") or ""
            from config import settings as _s
            link_html = (
                f'<a href="{_esc(_s.SERVER_URL)}/invoice/{_esc(token)}" target="_blank" '
                f'style="color:#128c7e;font-size:12px">View →</a>'
                if token else "—"
            )
            rows += f"""
            <tr>
              <td><strong>{_esc(period)}</strong></td>
              <td>{_esc(amount)}</td>
              <td>{sbadge}</td>
              <td>{link_html}</td>
            </tr>"""
        return rows

    # ── Follow-ups due soon ───────────────────────────────────────────────────
    def _upcoming_followup_rows():
        if not upcoming_followups:
            return '<tr><td colspan="3" class="empty-row">No follow-ups due in the next 4 days.</td></tr>'
        rows = ""
        now_utc = datetime.now(timezone.utc)
        for f in upcoming_followups:
            appt     = f.get("appointments") or {}
            pname    = _esc(appt.get("patient_name") or "—")
            appt_d   = _esc(_fmt_date(appt.get("appointment_date") or ""))
            try:
                sched = datetime.fromisoformat(f["scheduled_at"].replace("Z", "+00:00"))
                diff  = sched - now_utc
                hrs   = int(diff.total_seconds() // 3600)
                if hrs < 24:
                    due_str = f"In {hrs}h"
                    due_col = "#c5221f"
                elif hrs < 48:
                    due_str = "Tomorrow"
                    due_col = "#92400e"
                else:
                    due_str = f"In {diff.days} days"
                    due_col = "#1967d2"
            except Exception:
                due_str = "—"
                due_col = "#6b7280"
            rows += f"""
            <tr>
              <td><strong>{pname}</strong></td>
              <td style="color:#6b7280;font-size:13px">{appt_d}</td>
              <td><span style="color:{due_col};font-weight:600;font-size:13px">{_esc(due_str)}</span></td>
            </tr>"""
        return rows

    # ── New patient intake forms ──────────────────────────────────────────────
    def _intake_cards():
        if not intake_rows:
            return '<p class="empty-row" style="padding:20px;color:#9ca3af;text-align:center;font-style:italic">No intake forms collected yet.</p>'
        cards = ""
        for intake in intake_rows:
            appt   = intake.get("appointments") or {}
            pname  = _esc(appt.get("patient_name") or intake.get("patient_phone") or "Patient")
            appt_d = _esc(_fmt_date(appt.get("appointment_date") or ""))
            age    = _esc(str(intake.get("age") or "—"))
            gender = _esc(intake.get("gender") or "—")
            cc     = _esc(intake.get("chief_complaint") or "Not provided")
            cards += f"""
            <div style="border-bottom:1px solid #f3f4f6;padding:14px 20px">
              <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
                <strong style="font-size:14px">{pname}</strong>
                <span style="font-size:12px;color:#9ca3af">{appt_d}</span>
              </div>
              <div style="display:flex;gap:12px;margin-bottom:6px">
                <span style="background:#e8f0fe;color:#1967d2;padding:2px 8px;border-radius:10px;font-size:11px;font-weight:600">Age: {age}</span>
                <span style="background:#e8f0fe;color:#1967d2;padding:2px 8px;border-radius:10px;font-size:11px;font-weight:600">{gender}</span>
              </div>
              <div style="font-size:13px;color:#374151">🩺 {cc}</div>
            </div>"""
        return cards

    def _patient_history_rows():
        if not visit_notes_history:
            return '<tr><td colspan="4" class="empty-row">No appointment history yet.</td></tr>'

        # Group by (patient_name, patient_phone)
        from collections import defaultdict
        groups: dict = defaultdict(list)
        for a in visit_notes_history:
            key = (a["patient_name"], a["patient_phone"])
            groups[key].append(a)

        rows = ""
        for (pname, pphone), visits in sorted(groups.items()):
            visit_count  = len(visits)
            last_visit   = visits[0]["appointment_date"]   # already sorted desc
            has_notes    = any(v.get("visit_notes") for v in visits)
            patient_id   = _esc(f"{pname}_{pphone}").replace(" ", "_")

            # Patient header row (clickable to expand)
            rows += f"""
            <tr class="patient-hdr" onclick="togglePatient('{patient_id}')"
                style="cursor:pointer;background:var(--bg-sec)">
              <td style="padding:10px 16px">
                <strong>{_esc(pname)}</strong><br>
                <small style="color:#6b7280">{_esc(pphone)}</small>
              </td>
              <td style="color:#6b7280;font-size:13px">
                {visit_count} visit(s)<br>
                <small>Last: {_esc(_fmt_date(last_visit))}</small>
              </td>
              <td style="color:#6b7280;font-size:12px">
                {'✅ Has notes' if has_notes else '<span style="color:#f59e0b">⚠️ No notes</span>'}
              </td>
              <td style="text-align:center;color:#6b7280;font-size:18px">
                <span id="arrow_{patient_id}">▸</span>
              </td>
            </tr>"""

            # Detail rows (hidden by default)
            rows += f'<tr id="detail_{patient_id}" style="display:none"><td colspan="4" style="padding:0">'
            rows += '<table style="width:100%;border-collapse:collapse">'
            rows += '<thead><tr style="background:#f9fafb"><th style="padding:8px 24px;font-size:11px;color:#6b7280;text-align:left">Date &amp; Time</th><th style="font-size:11px;color:#6b7280;text-align:left">Status</th><th style="font-size:11px;color:#6b7280;text-align:left">Doctor\'s Notes</th><th style="font-size:11px;color:#6b7280;text-align:center;width:80px">Follow-up</th></tr></thead><tbody>'

            for v in visits:
                notes_raw = v.get("visit_notes") or ""
                appt_id   = v.get("id", "")
                fu_days   = v.get("followup_days", 2)
                fu_label  = f"{fu_days}d" if fu_days else "—"
                notes_display = (
                    f'<span style="white-space:pre-wrap;font-size:13px">{_esc(notes_raw)}</span>'
                    if notes_raw
                    else '<span style="color:#9ca3af;font-style:italic;font-size:12px">No notes yet</span>'
                )
                rows += f"""
                <tr style="border-top:1px solid #f3f4f6" id="row-{appt_id}">
                  <td style="padding:9px 24px;font-size:13px">
                    {_esc(_fmt_date(v['appointment_date']))}<br>
                    <small style="color:#888">{_esc(_fmt_time(v['slot_time']))}</small>
                  </td>
                  <td style="padding:9px 16px">{_status_badge(v['status'])}</td>
                  <td style="padding:9px 16px;max-width:260px;word-break:break-word">
                    <div id="notes-view-{appt_id}">{notes_display}</div>
                    <div id="notes-edit-{appt_id}" style="display:none;margin-top:6px">
                      <textarea class="notes-edit-area" id="notes-inp-{appt_id}">{_esc(notes_raw)}</textarea>
                      <div style="display:flex;align-items:center;gap:8px;margin-top:6px;flex-wrap:wrap">
                        <label style="font-size:12px;color:#6b7280">Follow-up in
                          <input type="number" id="fu-inp-{appt_id}" value="{fu_days}" min="0" max="365"
                            style="width:52px;padding:2px 6px;border:1px solid #d1d5db;border-radius:6px;font-size:12px;margin:0 4px">
                          days</label>
                        <button class="save-btn" onclick="saveNotes({appt_id})">Save</button>
                        <button class="cancel-btn" onclick="cancelNotes({appt_id})">Cancel</button>
                      </div>
                    </div>
                  </td>
                  <td style="padding:9px 16px;text-align:center">
                    <div id="fu-view-{appt_id}">
                      <span style="background:#fef3c7;color:#92400e;padding:2px 8px;
                        border-radius:10px;font-size:11px;font-weight:600">{_esc(fu_label)}</span>
                    </div>
                    <button class="edit-btn" style="margin-top:4px;margin-left:0"
                      onclick="startNotes({appt_id})">✏️ Edit</button>
                  </td>
                </tr>"""
            rows += '</tbody></table></td></tr>'

        return rows

    def _sub_info():
        if not subscription:
            return '<span style="color:#c5221f">No active subscription</span>'
        plan  = subscription.get("plan_name", "—").capitalize()
        end   = _fmt_date(subscription.get("end_date", ""))
        status = subscription.get("status", "")
        colour = "#1e7e34" if status == "active" else "#c5221f"
        return (
            f'<strong>{_esc(plan)}</strong> plan &nbsp;·&nbsp; '
            f'Valid until <strong style="color:{colour}">{_esc(end)}</strong>'
        )

    sub_plan  = (subscription or {}).get("plan_name", "starter").lower()
    plan_badge_colour = {"starter": "#1967d2", "pro": "#7b2fbe", "suite": "#c5221f"}.get(sub_plan, "#5f6368")

    generated_at = now_ist.strftime("%d %b %Y, %-I:%M %p IST")

    # ── Full HTML ─────────────────────────────────────────────────────────────
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{_esc(clinic_name)} — Dashboard</title>
  <style>
    /* ── Reset & variables ── */
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    :root {{
      --green: #128c7e;
      --green-dark: #0d7066;
      --green-light: rgba(18,140,126,.1);
      --blue: #1967d2;
      --purple: #7b2fbe;
      --orange: #e37400;
      --red: #c5221f;
      --text: #1f2937;
      --text-muted: #6b7280;
      --text-faint: #9ca3af;
      --border: #f3f4f6;
      --bg: #f0f2f5;
      --bg-sec: #f9fafb;
      --white: #ffffff;
      --shadow: 0 1px 3px rgba(0,0,0,.08), 0 1px 2px rgba(0,0,0,.04);
      --shadow-md: 0 4px 6px rgba(0,0,0,.07), 0 2px 4px rgba(0,0,0,.06);
    }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Inter", sans-serif;
      background: var(--bg);
      color: var(--text);
      min-height: 100vh;
      -webkit-font-smoothing: antialiased;
    }}

    /* ── Top bar ── */
    .topbar {{
      background: linear-gradient(135deg, #0d7066 0%, #128c7e 60%, #16a085 100%);
      color: #fff;
      padding: 0 24px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      flex-wrap: wrap;
      gap: 8px;
      box-shadow: 0 2px 8px rgba(0,0,0,.18);
      min-height: 64px;
    }}
    .topbar-left {{ display: flex; align-items: center; gap: 14px; padding: 14px 0; }}
    .clinic-avatar {{
      width: 44px; height: 44px; border-radius: 12px;
      background: rgba(255,255,255,.22);
      display: flex; align-items: center; justify-content: center;
      font-size: 16px; font-weight: 800; color: #fff;
      letter-spacing: -0.5px; flex-shrink: 0;
    }}
    .topbar-title h1 {{ font-size: 17px; font-weight: 700; line-height: 1.2; }}
    .topbar-title p  {{ font-size: 12px; opacity: 0.82; margin-top: 2px; }}
    .topbar-right {{ display: flex; align-items: center; gap: 12px; padding: 14px 0; }}
    .topbar-meta  {{ font-size: 11px; opacity: 0.72; text-align: right; line-height: 1.5; }}
    .topbar-refresh {{
      background: rgba(255,255,255,.18); color: #fff; border: none;
      padding: 6px 14px; border-radius: 20px; font-size: 12px; font-weight: 600;
      cursor: pointer; transition: background .15s;
      text-decoration: none; display: inline-flex; align-items: center; gap: 5px;
    }}
    .topbar-refresh:hover {{ background: rgba(255,255,255,.32); }}

    /* ── Plan badge ── */
    .plan-badge {{
      display: inline-block;
      background: rgba(255,255,255,.25);
      color: #fff;
      padding: 2px 8px;
      border-radius: 10px;
      font-size: 10px;
      font-weight: 700;
      letter-spacing: 0.8px;
      text-transform: uppercase;
      margin-left: 8px;
      vertical-align: middle;
    }}

    /* ── Quick nav ── */
    .quick-nav {{
      background: var(--white);
      border-bottom: 1px solid var(--border);
      padding: 0 24px;
      display: flex;
      gap: 0;
      overflow-x: auto;
      scrollbar-width: none;
      position: sticky;
      top: 0;
      z-index: 100;
      box-shadow: 0 1px 4px rgba(0,0,0,.06);
    }}
    .quick-nav::-webkit-scrollbar {{ display: none; }}
    .quick-nav a {{
      padding: 11px 14px;
      font-size: 12px;
      font-weight: 500;
      color: var(--text-muted);
      text-decoration: none;
      white-space: nowrap;
      border-bottom: 2px solid transparent;
      transition: color .15s, border-color .15s;
    }}
    .quick-nav a:hover {{ color: var(--green); border-bottom-color: var(--green); }}

    /* ── Layout ── */
    .container {{ max-width: 1140px; margin: 0 auto; padding: 24px 16px 48px; }}

    /* ── Stat cards ── */
    .stats-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(190px, 1fr));
      gap: 16px;
      margin-bottom: 24px;
    }}
    .stat-card {{
      background: var(--white);
      border-radius: 12px;
      padding: 22px 20px;
      text-align: center;
      box-shadow: var(--shadow);
      transition: transform .18s, box-shadow .18s;
    }}
    .stat-card:hover {{
      transform: translateY(-2px);
      box-shadow: var(--shadow-md);
    }}
    .stat-icon-wrap {{
      width: 50px; height: 50px; border-radius: 14px;
      display: flex; align-items: center; justify-content: center;
      margin: 0 auto 12px; font-size: 22px;
    }}
    .stat-value {{ font-size: 36px; font-weight: 800; line-height: 1; letter-spacing: -1px; }}
    .stat-label {{ font-size: 12px; color: var(--text-muted); margin-top: 6px; font-weight: 500; }}

    /* ── Cards / panels ── */
    .card {{
      background: var(--white);
      border-radius: 12px;
      box-shadow: var(--shadow);
      margin-bottom: 16px;
      overflow: hidden;
    }}
    .card-header {{
      padding: 14px 20px;
      border-bottom: 1px solid var(--border);
      font-weight: 700;
      font-size: 14px;
      display: flex;
      align-items: center;
      gap: 8px;
      color: var(--text);
    }}
    .card-header-count {{
      margin-left: auto;
      background: var(--bg-sec);
      color: var(--text-muted);
      padding: 2px 8px;
      border-radius: 20px;
      font-size: 11px;
      font-weight: 600;
    }}
    .card-body {{ padding: 0; }}

    /* ── Tables ── */
    table {{ width: 100%; border-collapse: collapse; }}
    th, td {{
      padding: 11px 16px;
      text-align: left;
      font-size: 13px;
      border-bottom: 1px solid var(--border);
    }}
    th {{
      background: var(--bg-sec); font-weight: 700; font-size: 11px;
      text-transform: uppercase; letter-spacing: 0.5px; color: var(--text-faint);
    }}
    tr:last-child td {{ border-bottom: none; }}
    tbody tr:hover td {{ background: rgba(18,140,126,.03); }}
    .empty-row {{ color: var(--text-faint); font-style: italic; text-align: center; padding: 32px 16px; font-size: 13px; }}

    /* ── Info section ── */
    .info-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
      gap: 0;
    }}
    .info-item {{
      padding: 16px 20px;
      border-bottom: 1px solid var(--border);
    }}
    .info-item:last-child {{ border-bottom: none; }}
    .info-label {{
      font-size: 10px; text-transform: uppercase; color: var(--text-faint);
      font-weight: 700; letter-spacing: 0.5px; margin-bottom: 4px;
    }}
    .info-value {{ font-size: 14px; color: var(--text); }}

    /* ── Patient history notes ── */
    .notes-text {{
      white-space: pre-wrap;
      font-size: 13px;
      color: #374151;
      line-height: 1.55;
    }}

    /* ── Inline edit UI ── */
    .edit-btn {{
      background: none; border: 1px solid var(--border); color: var(--text-faint);
      font-size: 11px; padding: 2px 8px; border-radius: 6px; cursor: pointer;
      margin-left: 6px; vertical-align: middle; transition: all .15s;
    }}
    .edit-btn:hover {{ border-color: var(--green); color: var(--green); background: var(--green-light); }}
    .edit-row {{ display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }}
    .edit-input {{
      font-size: 14px; border: 1.5px solid var(--green); border-radius: 8px;
      padding: 5px 10px; outline: none; width: 100%; max-width: 360px;
      transition: box-shadow .15s;
    }}
    .edit-input:focus {{ box-shadow: 0 0 0 3px rgba(18,140,126,.15); }}
    .save-btn {{
      background: var(--green); color: #fff; border: none; padding: 5px 14px;
      border-radius: 7px; font-size: 12px; font-weight: 700; cursor: pointer;
      transition: background .15s;
    }}
    .save-btn:hover {{ background: var(--green-dark); }}
    .cancel-btn {{
      background: none; border: 1px solid var(--border); color: var(--text-muted);
      padding: 5px 10px; border-radius: 7px; font-size: 12px; cursor: pointer;
    }}
    .notes-edit-area {{
      width: 100%; min-height: 80px; font-size: 13px; border: 1.5px solid var(--green);
      border-radius: 8px; padding: 8px 10px; outline: none; resize: vertical;
      font-family: inherit; margin-top: 4px;
    }}
    .toast {{
      position: absolute; top: 12px; right: 12px;
      background: var(--green); color: #fff;
      font-size: 12px; font-weight: 700; padding: 7px 14px;
      border-radius: 8px; opacity: 0; transition: opacity .3s;
      pointer-events: none; z-index: 10; box-shadow: var(--shadow-md);
    }}
    .toast.show {{ opacity: 1; }}

    /* ── Two-column card grid ── */
    .two-col {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(340px, 1fr));
      gap: 16px;
      margin-bottom: 16px;
    }}
    .two-col .card {{ margin-bottom: 0; }}

    /* ── Search box ── */
    .search-box {{
      padding: 12px 16px 14px;
      border-bottom: 1px solid var(--border);
    }}
    .search-box input {{
      width: 100%;
      padding: 9px 12px;
      border: 1px solid #d1d5db;
      border-radius: 8px;
      font-size: 14px;
      outline: none;
      box-sizing: border-box;
      transition: border-color .15s, box-shadow .15s;
    }}
    .search-box input:focus {{
      border-color: var(--green);
      box-shadow: 0 0 0 3px rgba(18,140,126,.12);
    }}

    /* ── Export buttons ── */
    .export-btn {{
      display: inline-flex; align-items: center; gap: 6px;
      padding: 8px 16px; border-radius: 8px; border: none;
      font-size: 13px; font-weight: 600; cursor: pointer;
      transition: opacity .15s, transform .1s;
    }}
    .export-btn:hover {{ opacity: .85; transform: translateY(-1px); }}
    .export-btn:active {{ transform: translateY(0); }}

    /* ── Footer ── */
    footer {{
      text-align: center;
      font-size: 12px;
      color: var(--text-faint);
      padding: 24px 0 36px;
    }}

    /* ── Responsive ── */
    @media (max-width: 640px) {{
      .topbar {{ padding: 0 14px; }}
      .stats-grid {{ grid-template-columns: repeat(2, 1fr); gap: 10px; }}
      .stat-value {{ font-size: 28px; }}
      .two-col {{ grid-template-columns: 1fr; }}
      th, td {{ padding: 9px 12px; font-size: 12px; }}
      .quick-nav {{ padding: 0 14px; }}
      .container {{ padding: 16px 12px 48px; }}
    }}

    /* ── Print ── */
    @media print {{
      .topbar, .quick-nav, .export-btn, .edit-btn, .save-btn, .cancel-btn, footer {{ display: none !important; }}
      .card {{ box-shadow: none; border: 1px solid #e5e7eb; }}
      body {{ background: white; }}
    }}
  </style>
  <script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
</head>
<body>

<!-- Top bar -->
<div class="topbar">
  <div class="topbar-left">
    <div class="clinic-avatar">{_esc(clinic_initials)}</div>
    <div class="topbar-title">
      <h1>{_esc(clinic_name)} <span class="plan-badge">{_esc(sub_plan)}</span></h1>
      <p>Dr. {_esc(doctor_name)}</p>
    </div>
  </div>
  <div class="topbar-right">
    <div class="topbar-meta">Updated {_esc(generated_at)}</div>
    <a href="?key={_esc(dashboard_key)}" class="topbar-refresh">↻ Refresh</a>
  </div>
</div>

<!-- Quick navigation -->
<nav class="quick-nav">
  <a href="#today">📋 Today</a>
  <a href="#week">🗓️ Week</a>
  <a href="#activity">🕐 Activity</a>
  <a href="#followups">💬 Follow-ups</a>
  <a href="#due-soon">🔔 Due Soon</a>
  <a href="#history">📋 History</a>
  <a href="#billing">🧾 Billing</a>
  <a href="#info">ℹ️ Info</a>
</nav>

<div class="container">

  <!-- Stat cards -->
  <div class="stats-grid">
    {_stat_card(stats['today_appointments'],   "Today's Appointments", "📅", "#128c7e")}
    {_stat_card(stats['month_appointments'],   "This Month",           "📊", "#1967d2")}
    {_stat_card(stats['total_patients'],       "Total Patients",       "👥", "#7b2fbe")}
    {_stat_card(stats['pending_followups'],    "Pending Follow-ups",   "💬", "#e37400")}
  </div>

  <!-- Today's schedule -->
  <div id="today" class="card">
    <div class="card-header">📋 Today's Appointments — {_esc(_fmt_date(today_str))} <span class="card-header-count">{len(today_appts)}</span></div>
    <div class="card-body">
      <table>
        <thead><tr><th>Time</th><th>Patient</th></tr></thead>
        <tbody>{_today_rows()}</tbody>
      </table>
    </div>
  </div>

  <!-- Upcoming week -->
  <div id="week" class="card">
    <div class="card-header">🗓️ Upcoming This Week</div>
    <div class="card-body">
      <table>
        <thead><tr><th>Date</th><th>Time</th><th>Patient</th></tr></thead>
        <tbody>{_week_rows()}</tbody>
      </table>
    </div>
  </div>

  <!-- Recent activity -->
  <div id="activity" class="card">
    <div class="card-header">🕐 Recent Activity</div>
    <div class="card-body">
      <table>
        <thead><tr><th>Date &amp; Time</th><th>Patient</th><th>Status</th></tr></thead>
        <tbody>{_activity_rows()}</tbody>
      </table>
    </div>
  </div>

  <!-- ① Follow-up tracker + ② Trend chart (side by side) -->
  <div id="followups" class="two-col">

    <div class="card">
      <div class="card-header">💬 Follow-up Responses</div>
      <div class="card-body">
        <table>
          <thead><tr><th>Patient</th><th>Visit</th><th>Status</th><th>Response</th></tr></thead>
          <tbody>{_followup_tracker_rows()}</tbody>
        </table>
      </div>
    </div>

    <div class="card">
      <div class="card-header">📈 Appointment Trend — Last 6 Months</div>
      <div class="card-body">
        {_trend_chart()}
      </div>
    </div>

  </div>

  <!-- ③ Cancellation stats -->
  <div class="card">
    <div class="card-header">❌ Cancellation &amp; Completion Stats (This Month)</div>
    <div class="card-body">
      {_cancel_stats_html()}
    </div>
  </div>

  <!-- ④ Follow-ups due soon + ⑤ Waitlist (side by side) -->
  <div class="two-col">

    <div id="due-soon" class="card">
      <div class="card-header">🔔 Follow-ups Due Soon</div>
      <div class="card-body">
        <table>
          <thead><tr><th>Patient</th><th>Visit Date</th><th>Follow-up</th></tr></thead>
          <tbody>{_upcoming_followup_rows()}</tbody>
        </table>
      </div>
    </div>

    <div class="card">
      <div class="card-header">⏳ Waitlist Queue ({len(waitlist_rows)} waiting)</div>
      <div class="card-body">
        <table>
          <thead><tr><th>Patient</th><th>Date</th><th>Slot</th><th>Waiting</th></tr></thead>
          <tbody>{_waitlist_rows()}</tbody>
        </table>
      </div>
    </div>

  </div>

  <!-- ⑥ New patient intake forms + ⑦ Invoice history (side by side) -->
  <div class="two-col">

    <div class="card">
      <div class="card-header">🩺 New Patient Intake Forms <span class="card-header-count">{len(intake_rows)}</span></div>
      <div class="card-body">
        {_intake_cards()}
      </div>
    </div>

    <div id="billing" class="card">
      <div class="card-header">🧾 Invoice &amp; Billing History</div>
      <div class="card-body">
        <table>
          <thead><tr><th>Period</th><th>Amount</th><th>Status</th><th>Link</th></tr></thead>
          <tbody>{_invoice_rows()}</tbody>
        </table>
      </div>
    </div>

  </div>

  <!-- ⑧ Export to CSV -->
  <div class="card">
    <div class="card-header">⬇️ Export Data</div>
    <div class="card-body" style="padding:16px 20px">
      <div style="display:flex;flex-wrap:wrap;gap:10px">
        <button onclick="exportTableCSV('historyTable','patient_history.csv')"
          class="export-btn" style="background:#128c7e;color:#fff">
          ⬇ Patient History CSV
        </button>
        <button onclick="exportFollowupsCSV()"
          class="export-btn" style="background:#6b7280;color:#fff">
          ⬇ Follow-up Responses CSV
        </button>
      </div>
      <p style="font-size:12px;color:#9ca3af;margin-top:10px">Downloads a .csv file you can open in Excel or Google Sheets.</p>
    </div>
  </div>

  <!-- Patient visit history (all visits, grouped by patient) -->
  <div id="history" class="card">
    <div class="card-header">📋 Patient History <span class="card-header-count">{len(visit_notes_history)}</span></div>
    <div class="search-box">
      <input type="text" id="notesSearch" placeholder="🔍 Search by patient name or phone…"
             oninput="filterPatients(this.value)">
    </div>
    <div class="card-body">
      <table id="historyTable">
        <thead>
          <tr>
            <th>Patient</th>
            <th>Visits</th>
            <th>Notes</th>
            <th style="text-align:center"></th>
          </tr>
        </thead>
        <tbody id="historyTbody">{_patient_history_rows()}</tbody>
      </table>
    </div>
  </div>

  <!-- Referral stats -->
  <div class="card">
    <div class="card-header">🤝 Refer &amp; Earn — Free Months</div>
    <div class="card-body">
      <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:0">

        <div style="padding:16px 20px;border-bottom:1px solid #f3f4f6">
          <div style="font-size:11px;text-transform:uppercase;color:#9ca3af;font-weight:600;letter-spacing:.5px;margin-bottom:4px">Your Referral Code</div>
          <div style="font-size:22px;font-weight:700;color:#128c7e;letter-spacing:2px">{_esc(referral_code)}</div>
        </div>

        <div style="padding:16px 20px;border-bottom:1px solid #f3f4f6">
          <div style="font-size:11px;text-transform:uppercase;color:#9ca3af;font-weight:600;letter-spacing:.5px;margin-bottom:4px">Doctors Referred</div>
          <div style="font-size:28px;font-weight:700;color:#1f2937">{ref_signups}</div>
          <div style="font-size:12px;color:#6b7280">{ref_paid} paid → rewards triggered</div>
        </div>

        <div style="padding:16px 20px;border-bottom:1px solid #f3f4f6">
          <div style="font-size:11px;text-transform:uppercase;color:#9ca3af;font-weight:600;letter-spacing:.5px;margin-bottom:4px">Free Months Earned</div>
          <div style="font-size:28px;font-weight:700;color:#1f2937">{ref_total_earned}</div>
          <div style="font-size:12px;color:#6b7280">{ref_applied_mo} applied · {ref_pending_mo} pending</div>
        </div>

        <div style="padding:16px 20px;border-bottom:1px solid #f3f4f6;grid-column:1/-1">
          <div style="font-size:11px;text-transform:uppercase;color:#9ca3af;font-weight:600;letter-spacing:.5px;margin-bottom:6px">Your Referral Link</div>
          {"" if not referral_link else f'''
          <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
            <code style="background:#f3f4f6;padding:6px 12px;border-radius:8px;font-size:13px;color:#1f2937;word-break:break-all">{_esc(referral_link)}</code>
            <button onclick="navigator.clipboard.writeText('{_esc(referral_link)}');this.textContent='✅ Copied!';setTimeout(()=>this.textContent='📋 Copy',2000)"
              style="background:#128c7e;color:#fff;border:none;padding:6px 14px;border-radius:8px;font-size:12px;font-weight:600;cursor:pointer;white-space:nowrap">
              📋 Copy
            </button>
          </div>
          '''}
          {"" if referral_link else '<span style="color:#9ca3af;font-style:italic;font-size:13px">No referral code assigned yet — contact support to get yours.</span>'}
          <div style="margin-top:8px;font-size:12px;color:#6b7280">Share this with doctor friends. You earn <strong>1 free month</strong> for every friend who subscribes.</div>
        </div>

      </div>
    </div>
  </div>

  <!-- Clinic info + subscription (editable) -->
  <div id="info" class="card" style="position:relative">
    <div class="toast" id="infoToast">✅ Saved!</div>
    <div class="card-header">ℹ️ Clinic Info &amp; Subscription</div>
    <div class="card-body">
      <div class="info-grid">

        <div class="info-item">
          <div class="info-label">Clinic Name</div>
          <div class="info-value" id="val-clinic_name">
            <div class="edit-row">
              <span id="txt-clinic_name">{_esc(clinic_name)}</span>
              <button class="edit-btn" onclick="startEdit('clinic_name','{_esc(clinic_name)}')">✏️ Edit</button>
            </div>
            <div id="form-clinic_name" style="display:none;margin-top:6px">
              <input class="edit-input" id="inp-clinic_name" value="{_esc(clinic_name)}">
              <div style="display:flex;gap:6px;margin-top:6px">
                <button class="save-btn" onclick="saveInfo('clinic_name')">Save</button>
                <button class="cancel-btn" onclick="cancelEdit('clinic_name')">Cancel</button>
              </div>
            </div>
          </div>
        </div>

        <div class="info-item">
          <div class="info-label">Doctor Name</div>
          <div class="info-value" id="val-doctor_name">
            <div class="edit-row">
              <span id="txt-doctor_name">Dr. {_esc(doctor_name)}</span>
              <button class="edit-btn" onclick="startEdit('doctor_name','{_esc(doctor_name)}')">✏️ Edit</button>
            </div>
            <div id="form-doctor_name" style="display:none;margin-top:6px">
              <input class="edit-input" id="inp-doctor_name" value="{_esc(doctor_name)}">
              <div style="display:flex;gap:6px;margin-top:6px">
                <button class="save-btn" onclick="saveInfo('doctor_name')">Save</button>
                <button class="cancel-btn" onclick="cancelEdit('doctor_name')">Cancel</button>
              </div>
            </div>
          </div>
        </div>

        <div class="info-item">
          <div class="info-label">Address</div>
          <div class="info-value" id="val-clinic_address">
            <div class="edit-row">
              <span id="txt-clinic_address">{_esc(clinic_addr) or '<em style="color:#9ca3af">Not set</em>'}</span>
              <button class="edit-btn" onclick="startEdit('clinic_address','{_esc(clinic_addr)}')">✏️ Edit</button>
            </div>
            <div id="form-clinic_address" style="display:none;margin-top:6px">
              <input class="edit-input" id="inp-clinic_address" value="{_esc(clinic_addr)}">
              <div style="display:flex;gap:6px;margin-top:6px">
                <button class="save-btn" onclick="saveInfo('clinic_address')">Save</button>
                <button class="cancel-btn" onclick="cancelEdit('clinic_address')">Cancel</button>
              </div>
            </div>
          </div>
        </div>

        <div class="info-item">
          <div class="info-label">Phone</div>
          <div class="info-value" id="val-clinic_phone">
            <div class="edit-row">
              <span id="txt-clinic_phone">{_esc(clinic_ph) or '<em style="color:#9ca3af">Not set</em>'}</span>
              <button class="edit-btn" onclick="startEdit('clinic_phone','{_esc(clinic_ph)}')">✏️ Edit</button>
            </div>
            <div id="form-clinic_phone" style="display:none;margin-top:6px">
              <input class="edit-input" id="inp-clinic_phone" value="{_esc(clinic_ph)}">
              <div style="display:flex;gap:6px;margin-top:6px">
                <button class="save-btn" onclick="saveInfo('clinic_phone')">Save</button>
                <button class="cancel-btn" onclick="cancelEdit('clinic_phone')">Cancel</button>
              </div>
            </div>
          </div>
        </div>

        <div class="info-item">
          <div class="info-label">Google Review Link</div>
          <div class="info-value" id="val-google_review_link">
            <div class="edit-row">
              <span id="txt-google_review_link" style="font-size:12px;word-break:break-all">{_esc(info.get('google_review_link','')) or '<em style="color:#9ca3af">Not set</em>'}</span>
              <button class="edit-btn" onclick="startEdit('google_review_link','{_esc(info.get('google_review_link',''))}')">✏️ Edit</button>
            </div>
            <div id="form-google_review_link" style="display:none;margin-top:6px">
              <input class="edit-input" id="inp-google_review_link" value="{_esc(info.get('google_review_link',''))}">
              <div style="display:flex;gap:6px;margin-top:6px">
                <button class="save-btn" onclick="saveInfo('google_review_link')">Save</button>
                <button class="cancel-btn" onclick="cancelEdit('google_review_link')">Cancel</button>
              </div>
            </div>
          </div>
        </div>

        <div class="info-item" style="grid-column: 1 / -1;">
          <div class="info-label">Subscription</div>
          <div class="info-value">{_sub_info()}</div>
        </div>

      </div>
    </div>
  </div>

</div><!-- /container -->

<script>
var DASH_KEY = "{_esc(dashboard_key)}";

/* ── Toast helper ── */
function showToast(id, msg) {{
  var t = document.getElementById(id);
  if (!t) return;
  t.textContent = msg || "✅ Saved!";
  t.classList.add("show");
  setTimeout(function(){{ t.classList.remove("show"); }}, 2500);
}}

/* ── Clinic Info inline edit ── */
function startEdit(field, current) {{
  document.getElementById("form-" + field).style.display = "block";
  document.getElementById("txt-" + field).parentElement.querySelector(".edit-btn").style.display = "none";
  var inp = document.getElementById("inp-" + field);
  inp.value = current;
  inp.focus();
}}
function cancelEdit(field) {{
  document.getElementById("form-" + field).style.display = "none";
  document.getElementById("txt-" + field).parentElement.querySelector(".edit-btn").style.display = "";
}}
function saveInfo(field) {{
  var value = document.getElementById("inp-" + field).value.trim();
  fetch("/clinic/update-info", {{
    method: "POST",
    headers: {{ "Content-Type": "application/json" }},
    body: JSON.stringify({{ key: DASH_KEY, field: field, value: value }})
  }})
  .then(function(r){{ return r.json(); }})
  .then(function(data) {{
    if (data.success) {{
      var label = field === "doctor_name" ? "Dr. " + value : value;
      document.getElementById("txt-" + field).innerHTML = label || '<em style="color:#9ca3af">Not set</em>';
      cancelEdit(field);
      showToast("infoToast", "✅ Saved!");
    }} else {{
      alert("Error: " + (data.detail || "Could not save"));
    }}
  }})
  .catch(function(){{ alert("Network error — please try again."); }});
}}

/* ── Visit notes inline edit ── */
function startNotes(apptId) {{
  document.getElementById("notes-view-" + apptId).style.display = "none";
  document.getElementById("notes-edit-" + apptId).style.display = "block";
  document.getElementById("notes-inp-" + apptId).focus();
}}
function cancelNotes(apptId) {{
  document.getElementById("notes-view-" + apptId).style.display = "";
  document.getElementById("notes-edit-" + apptId).style.display = "none";
}}
function saveNotes(apptId) {{
  var notes  = document.getElementById("notes-inp-" + apptId).value.trim();
  var fuDays = parseInt(document.getElementById("fu-inp-" + apptId).value) || 2;
  fetch("/clinic/save-notes", {{
    method: "POST",
    headers: {{ "Content-Type": "application/json" }},
    body: JSON.stringify({{ key: DASH_KEY, appointment_id: apptId, notes: notes, followup_days: fuDays }})
  }})
  .then(function(r){{ return r.json(); }})
  .then(function(data) {{
    if (data.success) {{
      var notesView = document.getElementById("notes-view-" + apptId);
      notesView.innerHTML = notes
        ? '<span style="white-space:pre-wrap;font-size:13px">' + notes.replace(/</g,"&lt;") + '</span>'
        : '<span style="color:#9ca3af;font-style:italic;font-size:12px">No notes yet</span>';
      var fuView = document.getElementById("fu-view-" + apptId);
      fuView.innerHTML = '<span style="background:#fef3c7;color:#92400e;padding:2px 8px;border-radius:10px;font-size:11px;font-weight:600">' + fuDays + 'd</span>';
      cancelNotes(apptId);
    }} else {{
      alert("Error: " + (data.detail || "Could not save notes"));
    }}
  }})
  .catch(function(){{ alert("Network error — please try again."); }});
}}

/* ── CSV export helpers ── */
function _tableToCSV(table) {{
  var rows = Array.from(table.querySelectorAll('tr'));
  return rows.map(function(row) {{
    return Array.from(row.querySelectorAll('th,td')).map(function(cell) {{
      return '"' + cell.innerText.replace(/"/g, '""').replace(/\\n/g, ' ').trim() + '"';
    }}).join(',');
  }}).join('\\n');
}}
function _downloadCSV(csv, filename) {{
  var blob = new Blob([csv], {{ type: 'text/csv' }});
  var a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  a.click();
}}
function exportTableCSV(tableId, filename) {{
  var table = document.getElementById(tableId);
  if (!table) {{ alert('No data to export.'); return; }}
  _downloadCSV(_tableToCSV(table), filename);
}}
function exportFollowupsCSV() {{
  var fuTable = null;
  document.querySelectorAll('.card').forEach(function(card) {{
    var hdr = card.querySelector('.card-header');
    if (hdr && hdr.textContent.includes('Follow-up Response')) {{
      fuTable = card.querySelector('table');
    }}
  }});
  if (!fuTable) {{ alert('No follow-up data to export.'); return; }}
  _downloadCSV(_tableToCSV(fuTable), 'followup_responses.csv');
}}

/* ── Patient history expand/collapse ── */
function togglePatient(id) {{
  var detail = document.getElementById('detail_' + id);
  var arrow  = document.getElementById('arrow_' + id);
  if (!detail) return;
  var open = detail.style.display !== 'none';
  detail.style.display = open ? 'none' : '';
  arrow.textContent    = open ? '▸' : '▾';
}}

/* ── Patient search filter ── */
function filterPatients(query) {{
  var q = query.toLowerCase().trim();
  document.querySelectorAll('#historyTbody tr.patient-hdr').forEach(function(hdr) {{
    var visible = !q || hdr.innerText.toLowerCase().includes(q);
    hdr.style.display = visible ? '' : 'none';
    var match = hdr.getAttribute('onclick').match(/'([^']+)'/);
    if (match) {{
      var pid = match[1];
      var detail = document.getElementById('detail_' + pid);
      if (detail) detail.style.display = 'none';
      var arrow  = document.getElementById('arrow_' + pid);
      if (arrow) arrow.textContent = '▸';
    }}
  }});
}}

/* ── Chart.js trend chart ── */
(function() {{
  var canvas = document.getElementById('trendChart');
  if (!canvas || typeof Chart === 'undefined') return;
  var raw = canvas.getAttribute('data-chart');
  if (!raw) return;
  try {{
    var d = JSON.parse(raw);
    new Chart(canvas, {{
      type: 'bar',
      data: {{
        labels: d.labels,
        datasets: [{{
          label: 'Appointments',
          data: d.data,
          backgroundColor: 'rgba(18,140,126,0.15)',
          borderColor: '#128c7e',
          borderWidth: 2,
          borderRadius: 6,
          hoverBackgroundColor: 'rgba(18,140,126,0.35)'
        }}]
      }},
      options: {{
        responsive: true,
        plugins: {{ legend: {{ display: false }} }},
        scales: {{
          y: {{
            beginAtZero: true,
            ticks: {{ stepSize: 1, color: '#9ca3af', font: {{ size: 11 }} }},
            grid: {{ color: '#f3f4f6' }}
          }},
          x: {{
            ticks: {{ color: '#6b7280', font: {{ size: 11 }} }},
            grid: {{ display: false }}
          }}
        }}
      }}
    }});
  }} catch(e) {{ console.warn('Chart init failed', e); }}
}})();
</script>

<footer>
  Powered by <strong>Clinic AI Agent</strong> &nbsp;·&nbsp; Manage appointments via WhatsApp
</footer>

</body>
</html>"""
