#!/usr/bin/env python3
"""
Backfill the unified cert audit log (tools.icp.us/api/cert-log) from
data/certs.json — which captures every cert the cloud-native cron has
sent but pre-dates the /api/cert-log integration.

Idempotent: the endpoint uses INSERT OR IGNORE on resend_id, so re-running
this is safe.

Usage:
    CERT_LOG_API_TOKEN=<token> python3 backfill_cert_log.py

Optional env:
    CERT_LOG_API_BASE   (defaults to https://tools.icp.us)
"""

import json
import os
import sys
from datetime import datetime

import requests

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
CERTS_JSON = os.path.join(REPO_ROOT, "data", "certs.json")
COURSES_JSON = os.path.join(REPO_ROOT, "courses.json")

API_BASE = os.environ.get("CERT_LOG_API_BASE", "https://tools.icp.us")
API_TOKEN = os.environ.get("CERT_LOG_API_TOKEN", "")


def date_slug_to_human(date_str):
    """'2026-05-26' → 'May 26, 2026'"""
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").strftime("%B %-d, %Y")
    except ValueError:
        return date_str


def main():
    if not API_TOKEN:
        sys.exit("ERROR: CERT_LOG_API_TOKEN not set.")

    with open(CERTS_JSON) as f:
        certs = json.load(f)

    with open(COURSES_JSON) as f:
        courses = json.load(f)

    records = []
    skipped = 0

    for key, session in certs.get("sessions", {}).items():
        course_name = session["course"]
        date_str = session["date"]  # "2026-05-26"
        course_date = date_slug_to_human(date_str)
        course_info = courses.get(course_name, {})
        pd_hours = str(course_info.get("hours", "1"))
        course_format = course_info.get("format", "webinar")

        for attendee in session.get("attendees", []):
            resend_id = attendee.get("resend_id")
            sent_at = attendee.get("sent_at")
            if not resend_id:
                skipped += 1
                print(f"  skip (no resend_id): {attendee.get('name')} — {course_name} {date_str}")
                continue
            records.append({
                "full_name": attendee["name"],
                "email": "",          # certs.json omits emails for privacy
                "course_title": course_name,
                "course_date": course_date,
                "pd_hours": pd_hours,
                "course_format": course_format,
                "status": "sent",
                "timestamp_iso": sent_at or "",
                "resend_id": resend_id,
            })

    print(f"Records to import: {len(records)}  |  Skipped (no resend_id): {skipped}")
    if not records:
        print("Nothing to import.")
        return

    resp = requests.post(
        f"{API_BASE.rstrip('/')}/api/cert-log",
        json=records,
        headers={"x-cert-log-token": API_TOKEN},
        timeout=30,
    )
    resp.raise_for_status()
    result = resp.json()
    print(f"Result: {result}")
    print(f"\nDone. View at: {API_BASE}/certs/log")


if __name__ == "__main__":
    main()
