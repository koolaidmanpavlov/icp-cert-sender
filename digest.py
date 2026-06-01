#!/usr/bin/env python3
"""
ICP Daily Cert-Delivery Digest

Runs once daily via GitHub Actions cron. Reads the ICP sign-in sheet and
emails andy@icp.us a summary of:

1. Yesterday's sessions — sign-in count vs certs sent vs pending vs errors.
2. Persistent cert errors (any age) — rows where cert_send_error is populated
   and cert_sent is blank. These need investigation.
3. Sign-ins with unknown training names (any age) — the Salem-VA failure
   mode. Training string doesn't match any key in courses.json, so the
   cert sender silently skipped them. Andy needs to either update
   courses.json to include the new training name OR contact the attendees
   to confirm what training they attended.

When there's nothing to report (no training yesterday, no errors, no
unknowns), no email is sent — keeps the inbox clean.

Reuses the helpers in send_certs.py: read_sheet_rows, gcp_clients,
session_date_from_timestamp. Same WIF auth, same sheet, same courses.json.
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from html import escape

import resend

from send_certs import (
    COURSES_JSON,
    SHEET_ID,
    gcp_clients,
    read_sheet_rows,
    session_date_from_timestamp,
)

DIGEST_FROM_EMAIL = "Institute for Childhood Preparedness <info@learn.icp.us>"
DIGEST_TO_EMAIL = os.environ.get("DIGEST_TO_EMAIL", "andy@icp.us")
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")

# Rows before this date predate the automated cert system — those certs were
# sent manually. Exclude them from the persistent-issue sections so the digest
# only surfaces actionable problems.
DIGEST_IGNORE_BEFORE = "2026-05-25"


def yesterday_eastern_date():
    """Return yesterday's date in US/Eastern as 'YYYY-MM-DD'. The sheet
    timestamps are interpreted as Eastern, matching send_certs.py."""
    eastern_now = datetime.now(timezone.utc) - timedelta(hours=4)
    return (eastern_now.date() - timedelta(days=1)).strftime("%Y-%m-%d")


def collect(rows, courses):
    yday = yesterday_eastern_date()
    by_session_yday = {}
    persistent_errors = []
    persistent_unknowns = []

    for row in rows:
        date_str = session_date_from_timestamp(row["timestamp"])
        training = row["training"]
        first = row.get("first", "")
        last = row.get("last", "")
        full_name = f"{first} {last}".strip() or "(no name)"
        email = row.get("email", "")

        # Persistent issues — only rows from the automated-system era
        if date_str and date_str < DIGEST_IGNORE_BEFORE:
            pass
        elif training not in courses:
            persistent_unknowns.append({
                "date": date_str or "?",
                "training": training or "(blank)",
                "name": full_name,
                "email": email,
                "timestamp": row["timestamp"],
            })
        elif row.get("cert_error") and not row.get("cert_sent"):
            persistent_errors.append({
                "date": date_str or "?",
                "training": training,
                "name": full_name,
                "email": email,
                "error": row["cert_error"],
                "timestamp": row["timestamp"],
            })

        # Yesterday's per-session reconciliation
        if date_str == yday:
            key = (training, date_str)
            sess = by_session_yday.setdefault(key, {
                "training": training or "(blank)",
                "sign_ins": 0,
                "certs": 0,
                "pending": 0,
                "errors": 0,
                "unknown_course": training not in courses,
            })
            sess["sign_ins"] += 1
            if training not in courses:
                pass  # already counted; cert sender skips these
            elif row.get("cert_error") and not row.get("cert_sent"):
                sess["errors"] += 1
            elif row.get("cert_sent"):
                sess["certs"] += 1
            else:
                sess["pending"] += 1

    return yday, list(by_session_yday.values()), persistent_errors, persistent_unknowns


def render_html(yday, yday_sessions, persistent_errors, persistent_unknowns):
    yday_label = datetime.strptime(yday, "%Y-%m-%d").strftime("%A, %B %d, %Y")
    parts = []
    parts.append(f"<h2 style='color:#0F2A3F;margin:0 0 8px 0;font-size:18px;'>ICP Cert Delivery — Daily Digest</h2>")
    parts.append(f"<p style='color:#555;margin:0 0 18px 0;font-size:13px;'>Summary for <strong>{yday_label}</strong></p>")

    # Yesterday's sessions
    if yday_sessions:
        parts.append("<h3 style='color:#0F2A3F;margin:18px 0 6px 0;font-size:15px;'>Yesterday's sessions</h3>")
        parts.append("<table style='border-collapse:collapse;width:100%;font-size:13px;'>")
        parts.append("<tr style='background:#F4F4F4;text-align:left;'><th style='padding:6px 8px;border:1px solid #ddd;'>Training</th><th style='padding:6px 8px;border:1px solid #ddd;'>Sign-ins</th><th style='padding:6px 8px;border:1px solid #ddd;'>Certs sent</th><th style='padding:6px 8px;border:1px solid #ddd;'>Pending</th><th style='padding:6px 8px;border:1px solid #ddd;'>Errors</th><th style='padding:6px 8px;border:1px solid #ddd;'>Status</th></tr>")
        for s in sorted(yday_sessions, key=lambda x: x["training"]):
            ok = (not s["unknown_course"]) and s["errors"] == 0 and s["pending"] == 0 and s["certs"] == s["sign_ins"]
            if s["unknown_course"]:
                status_html = "<span style='color:#9a3412;font-weight:600;'>UNKNOWN COURSE</span>"
            elif s["errors"]:
                status_html = "<span style='color:#9a3412;font-weight:600;'>NEEDS ATTENTION</span>"
            elif s["pending"]:
                status_html = "<span style='color:#854d0e;'>pending</span>"
            elif ok:
                status_html = "<span style='color:#166534;font-weight:600;'>OK</span>"
            else:
                status_html = "<span style='color:#555;'>—</span>"
            parts.append(
                f"<tr><td style='padding:6px 8px;border:1px solid #ddd;'>{escape(s['training'])}</td>"
                f"<td style='padding:6px 8px;border:1px solid #ddd;'>{s['sign_ins']}</td>"
                f"<td style='padding:6px 8px;border:1px solid #ddd;'>{s['certs']}</td>"
                f"<td style='padding:6px 8px;border:1px solid #ddd;'>{s['pending']}</td>"
                f"<td style='padding:6px 8px;border:1px solid #ddd;'>{s['errors']}</td>"
                f"<td style='padding:6px 8px;border:1px solid #ddd;'>{status_html}</td></tr>"
            )
        parts.append("</table>")
    else:
        parts.append("<h3 style='color:#0F2A3F;margin:18px 0 6px 0;font-size:15px;'>Yesterday's sessions</h3>")
        parts.append("<p style='color:#555;font-size:13px;margin:0 0 12px 0;'>No sign-ins recorded for yesterday.</p>")

    # Persistent cert errors
    if persistent_errors:
        parts.append("<h3 style='color:#9a3412;margin:22px 0 6px 0;font-size:15px;'>⚠️ Cert send errors needing attention</h3>")
        parts.append("<table style='border-collapse:collapse;width:100%;font-size:13px;'>")
        parts.append("<tr style='background:#FEE2E2;text-align:left;'><th style='padding:6px 8px;border:1px solid #ddd;'>Date</th><th style='padding:6px 8px;border:1px solid #ddd;'>Training</th><th style='padding:6px 8px;border:1px solid #ddd;'>Attendee</th><th style='padding:6px 8px;border:1px solid #ddd;'>Email</th><th style='padding:6px 8px;border:1px solid #ddd;'>Error</th></tr>")
        for e in persistent_errors:
            parts.append(
                f"<tr><td style='padding:6px 8px;border:1px solid #ddd;'>{escape(e['date'])}</td>"
                f"<td style='padding:6px 8px;border:1px solid #ddd;'>{escape(e['training'])}</td>"
                f"<td style='padding:6px 8px;border:1px solid #ddd;'>{escape(e['name'])}</td>"
                f"<td style='padding:6px 8px;border:1px solid #ddd;'>{escape(e['email'])}</td>"
                f"<td style='padding:6px 8px;border:1px solid #ddd;color:#9a3412;'>{escape(e['error'])}</td></tr>"
            )
        parts.append("</table>")

    # Persistent unknown-course rows
    if persistent_unknowns:
        parts.append("<h3 style='color:#9a3412;margin:22px 0 6px 0;font-size:15px;'>⚠️ Sign-ins with unknown training name (no cert sent)</h3>")
        parts.append("<p style='color:#555;font-size:12px;margin:0 0 6px 0;'>The training name in column L doesn't match any key in courses.json, so the cert sender skipped these rows. Fix the form dropdown or add the training to courses.json — see the Salem VA incident (2026-05-30) for context.</p>")
        parts.append("<table style='border-collapse:collapse;width:100%;font-size:13px;'>")
        parts.append("<tr style='background:#FEE2E2;text-align:left;'><th style='padding:6px 8px;border:1px solid #ddd;'>Date</th><th style='padding:6px 8px;border:1px solid #ddd;'>Training (as typed)</th><th style='padding:6px 8px;border:1px solid #ddd;'>Attendee</th><th style='padding:6px 8px;border:1px solid #ddd;'>Email</th></tr>")
        for u in persistent_unknowns:
            parts.append(
                f"<tr><td style='padding:6px 8px;border:1px solid #ddd;'>{escape(u['date'])}</td>"
                f"<td style='padding:6px 8px;border:1px solid #ddd;'>{escape(u['training'])}</td>"
                f"<td style='padding:6px 8px;border:1px solid #ddd;'>{escape(u['name'])}</td>"
                f"<td style='padding:6px 8px;border:1px solid #ddd;'>{escape(u['email'])}</td></tr>"
            )
        parts.append("</table>")

    parts.append("<p style='color:#888;font-size:11px;margin:24px 0 0 0;'>Generated by icp-cert-sender · daily-digest workflow · andy@icp.us</p>")
    return "".join(parts)


def main():
    if not RESEND_API_KEY:
        sys.exit("ERROR: RESEND_API_KEY required")
    if not SHEET_ID:
        sys.exit("ERROR: SHEET_ID required")

    with open(COURSES_JSON) as f:
        courses = json.load(f)

    sheets, _drive = gcp_clients()
    rows = read_sheet_rows(sheets)

    yday, yday_sessions, persistent_errors, persistent_unknowns = collect(rows, courses)

    has_issues = persistent_errors or persistent_unknowns
    has_news = bool(yday_sessions) or has_issues
    if not has_news:
        print(f"Nothing to report for {yday} (no sessions, no errors, no unknowns). Skipping email.")
        return

    html = render_html(yday, yday_sessions, persistent_errors, persistent_unknowns)
    subject_bits = []
    if has_issues:
        subject_bits.append(f"⚠️ {len(persistent_errors) + len(persistent_unknowns)} issue(s)")
    if yday_sessions:
        n_sessions = len(yday_sessions)
        subject_bits.append(f"{n_sessions} session{'' if n_sessions == 1 else 's'} on {yday}")
    subject = "ICP Cert Digest — " + " · ".join(subject_bits)

    resend.api_key = RESEND_API_KEY
    result = resend.Emails.send({
        "from": DIGEST_FROM_EMAIL,
        "to": DIGEST_TO_EMAIL,
        "subject": subject,
        "html": html,
    })
    print(f"Sent digest: subject='{subject}', resend_id={result.get('id') if isinstance(result, dict) else result}")


if __name__ == "__main__":
    main()
