"""
clinic_dashboard.py — Per-clinic read-only web dashboard.

Renders a self-contained HTML page for a single clinic identified by
their private dashboard_key.  No patient data from other clinics is ever
exposed — every query is scoped to client_id.

Used by:  GET /clinic?key=<dashboard_key>  (in main.py)
"""

from __future__ import annotations

import html
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

def render_clinic_dashboard(client: dict) -> str:
    """
    Build and return the full HTML page for a clinic's dashboard.
    All data is freshly fetched from Supabase, scoped strictly to client["id"].
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

    stats           = db.get_dashboard_stats(client_id)
    today_appts     = db.get_appointments_for_date(client_id, today_str)
    week_appts      = db.get_appointments_range(client_id, today_str, week_end)
    recent_activity = db.get_recent_activity(client_id, limit=15)
    subscription    = db.get_active_subscription(client_id)
    visit_notes_history = db.get_patient_history_all(client_id, limit=50)

    # ── Sub-render helpers ────────────────────────────────────────────────────

    def _stat_card(value, label, icon, color):
        return f"""
        <div class="stat-card" style="border-top:4px solid {color}">
          <div class="stat-icon" style="color:{color}">{icon}</div>
          <div class="stat-value">{_esc(value)}</div>
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
                notes_cell = (
                    f'<span style="white-space:pre-wrap;font-size:13px">{_esc(notes_raw)}</span>'
                    if notes_raw
                    else '<span style="color:#9ca3af;font-style:italic;font-size:12px">No notes added</span>'
                )
                fu_days = v.get("followup_days", 2)
                fu_label = f"{fu_days}d" if fu_days else "—"
                rows += f"""
                <tr style="border-top:1px solid #f3f4f6">
                  <td style="padding:9px 24px;font-size:13px">
                    {_esc(_fmt_date(v['appointment_date']))}<br>
                    <small style="color:#888">{_esc(_fmt_time(v['slot_time']))}</small>
                  </td>
                  <td style="padding:9px 16px">{_status_badge(v['status'])}</td>
                  <td style="padding:9px 16px;max-width:280px;word-break:break-word">{notes_cell}</td>
                  <td style="padding:9px 16px;text-align:center">
                    <span style="background:#fef3c7;color:#92400e;padding:2px 8px;
                      border-radius:10px;font-size:11px;font-weight:600">{_esc(fu_label)}</span>
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
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}

    :root {{ --bg-sec: #f9fafb; }}

    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: #f0f2f5;
      color: #1f2937;
      min-height: 100vh;
    }}

    /* ── Top bar ── */
    .topbar {{
      background: #128c7e;
      color: #fff;
      padding: 14px 24px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      flex-wrap: wrap;
      gap: 8px;
    }}
    .topbar-left {{ display: flex; align-items: center; gap: 12px; }}
    .topbar-logo {{ font-size: 28px; }}
    .topbar-title h1 {{ font-size: 18px; font-weight: 700; line-height: 1.2; }}
    .topbar-title p  {{ font-size: 13px; opacity: 0.85; }}
    .topbar-meta     {{ font-size: 12px; opacity: 0.75; text-align: right; }}

    /* ── Plan badge ── */
    .plan-badge {{
      display: inline-block;
      background: {plan_badge_colour};
      color: #fff;
      padding: 2px 10px;
      border-radius: 12px;
      font-size: 12px;
      font-weight: 700;
      letter-spacing: 0.5px;
      text-transform: uppercase;
      margin-left: 8px;
      vertical-align: middle;
    }}

    /* ── Layout ── */
    .container {{ max-width: 1100px; margin: 0 auto; padding: 24px 16px; }}

    /* ── Stat cards ── */
    .stats-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 16px;
      margin-bottom: 24px;
    }}
    .stat-card {{
      background: #fff;
      border-radius: 10px;
      padding: 20px;
      text-align: center;
      box-shadow: 0 1px 4px rgba(0,0,0,.08);
    }}
    .stat-icon  {{ font-size: 28px; margin-bottom: 6px; }}
    .stat-value {{ font-size: 32px; font-weight: 700; line-height: 1; }}
    .stat-label {{ font-size: 13px; color: #6b7280; margin-top: 4px; }}

    /* ── Cards / panels ── */
    .card {{
      background: #fff;
      border-radius: 10px;
      box-shadow: 0 1px 4px rgba(0,0,0,.08);
      margin-bottom: 20px;
      overflow: hidden;
    }}
    .card-header {{
      padding: 14px 20px;
      border-bottom: 1px solid #f3f4f6;
      font-weight: 600;
      font-size: 15px;
      display: flex;
      align-items: center;
      gap: 8px;
    }}
    .card-body {{ padding: 0; }}

    /* ── Tables ── */
    table {{ width: 100%; border-collapse: collapse; }}
    th, td {{
      padding: 11px 16px;
      text-align: left;
      font-size: 14px;
      border-bottom: 1px solid #f3f4f6;
    }}
    th {{ background: #f9fafb; font-weight: 600; font-size: 12px; text-transform: uppercase;
          letter-spacing: 0.4px; color: #6b7280; }}
    tr:last-child td {{ border-bottom: none; }}
    tr:hover td {{ background: #fafafa; }}
    .empty-row {{ color: #9ca3af; font-style: italic; text-align: center; padding: 24px; }}

    /* ── Info section ── */
    .info-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
      gap: 0;
    }}
    .info-item {{
      padding: 14px 20px;
      border-bottom: 1px solid #f3f4f6;
    }}
    .info-item:last-child {{ border-bottom: none; }}
    .info-label {{ font-size: 11px; text-transform: uppercase; color: #9ca3af;
                   font-weight: 600; letter-spacing: 0.5px; margin-bottom: 3px; }}
    .info-value {{ font-size: 14px; color: #1f2937; }}

    /* ── Patient history notes ── */
    .notes-text {{
      white-space: pre-wrap;
      font-size: 13px;
      color: #374151;
      line-height: 1.5;
    }}

    /* ── Search box ── */
    .search-box {{
      padding: 10px 16px 14px;
      border-bottom: 1px solid #f3f4f6;
    }}
    .search-box input {{
      width: 100%;
      padding: 8px 12px;
      border: 1px solid #d1d5db;
      border-radius: 8px;
      font-size: 14px;
      outline: none;
      box-sizing: border-box;
    }}
    .search-box input:focus {{
      border-color: #128c7e;
      box-shadow: 0 0 0 2px rgba(18,140,126,.15);
    }}

    /* ── Footer ── */
    footer {{
      text-align: center;
      font-size: 12px;
      color: #9ca3af;
      padding: 20px 0 32px;
    }}

    /* ── Responsive ── */
    @media (max-width: 600px) {{
      .topbar {{ padding: 12px 16px; }}
      .stats-grid {{ grid-template-columns: repeat(2, 1fr); gap: 10px; }}
      .stat-value {{ font-size: 26px; }}
      th, td {{ padding: 9px 12px; font-size: 13px; }}
    }}
  </style>
</head>
<body>

<!-- Top bar -->
<div class="topbar">
  <div class="topbar-left">
    <div class="topbar-logo">🏥</div>
    <div class="topbar-title">
      <h1>{_esc(clinic_name)} <span class="plan-badge">{_esc(sub_plan)}</span></h1>
      <p>Dr. {_esc(doctor_name)}</p>
    </div>
  </div>
  <div class="topbar-meta">
    Last updated<br>{_esc(generated_at)}
  </div>
</div>

<div class="container">

  <!-- Stat cards -->
  <div class="stats-grid">
    {_stat_card(stats['today_appointments'],   "Today's Appointments", "📅", "#128c7e")}
    {_stat_card(stats['month_appointments'],   "This Month",           "📊", "#1967d2")}
    {_stat_card(stats['total_patients'],       "Total Patients",       "👥", "#7b2fbe")}
    {_stat_card(stats['pending_followups'],    "Pending Follow-ups",   "💬", "#e37400")}
  </div>

  <!-- Today's schedule -->
  <div class="card">
    <div class="card-header">📋 Today's Appointments — {_esc(_fmt_date(today_str))}</div>
    <div class="card-body">
      <table>
        <thead><tr><th>Time</th><th>Patient</th></tr></thead>
        <tbody>{_today_rows()}</tbody>
      </table>
    </div>
  </div>

  <!-- Upcoming week -->
  <div class="card">
    <div class="card-header">🗓️ Upcoming This Week</div>
    <div class="card-body">
      <table>
        <thead><tr><th>Date</th><th>Time</th><th>Patient</th></tr></thead>
        <tbody>{_week_rows()}</tbody>
      </table>
    </div>
  </div>

  <!-- Recent activity -->
  <div class="card">
    <div class="card-header">🕐 Recent Activity</div>
    <div class="card-body">
      <table>
        <thead><tr><th>Date &amp; Time</th><th>Patient</th><th>Status</th></tr></thead>
        <tbody>{_activity_rows()}</tbody>
      </table>
    </div>
  </div>

  <!-- Patient visit history (all visits, grouped by patient) -->
  <div class="card">
    <div class="card-header">📋 Patient History</div>
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

  <!-- Clinic info + subscription -->
  <div class="card">
    <div class="card-header">ℹ️ Clinic Info &amp; Subscription</div>
    <div class="card-body">
      <div class="info-grid">
        <div class="info-item">
          <div class="info-label">Clinic Name</div>
          <div class="info-value">{_esc(clinic_name)}</div>
        </div>
        <div class="info-item">
          <div class="info-label">Doctor</div>
          <div class="info-value">Dr. {_esc(doctor_name)}</div>
        </div>
        {"" if not clinic_addr else f'<div class="info-item"><div class="info-label">Address</div><div class="info-value">{_esc(clinic_addr)}</div></div>'}
        {"" if not clinic_ph else f'<div class="info-item"><div class="info-label">Phone</div><div class="info-value">{_esc(clinic_ph)}</div></div>'}
        <div class="info-item" style="grid-column: 1 / -1;">
          <div class="info-label">Subscription</div>
          <div class="info-value">{_sub_info()}</div>
        </div>
      </div>
    </div>
  </div>

</div><!-- /container -->

<script>
function togglePatient(id) {{
  var detail = document.getElementById('detail_' + id);
  var arrow  = document.getElementById('arrow_' + id);
  if (!detail) return;
  var open = detail.style.display !== 'none';
  detail.style.display = open ? 'none' : '';
  arrow.textContent    = open ? '▸' : '▾';
}}

function filterPatients(query) {{
  var q = query.toLowerCase().trim();
  var rows = document.querySelectorAll('#historyTbody tr.patient-hdr');
  rows.forEach(function(hdr) {{
    var text    = hdr.innerText.toLowerCase();
    var visible = !q || text.includes(q);
    hdr.style.display = visible ? '' : 'none';
    // Hide associated detail row too
    var pid    = hdr.getAttribute('onclick').match(/'([^']+)'/)[1];
    var detail = document.getElementById('detail_' + pid);
    if (detail) detail.style.display = 'none';
    var arrow  = document.getElementById('arrow_' + pid);
    if (arrow) arrow.textContent = '▸';
  }});
}}
</script>

<footer>
  Powered by Clinic AI Agent &nbsp;·&nbsp; Read-only view &nbsp;·&nbsp;
  Manage appointments via WhatsApp
</footer>

</body>
</html>"""
