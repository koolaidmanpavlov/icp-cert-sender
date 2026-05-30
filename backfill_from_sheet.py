#!/usr/bin/env python3
"""
Backfill the unified cert audit log from the Google Sign-in Sheet directly.
Finds all rows where cert_sent is non-empty and imports them to D1.

Use this when data/certs.json is stale (e.g., after a class that ran after
the last certs.json rebuild). Idempotent — safe to re-run.

Requires GCP auth (same as send_certs.py). In GitHub Actions this is handled
automatically via WIF. Locally, set GOOGLE_APPLICATION_CREDENTIALS to a
service account key JSON if your org permits keys.

Usage:
    CERT_LOG_API_TOKEN=<token> python3 backfill_from_sheet.py
"""

import json
import os
import sys

import requests

# Reuse auth + sheet helpers from send_certs.py
from send_certs import (
    gcp_clients,
    read_sheet_rows,
    parse_cert_sent,
    format_course_date,
    parse_sheet_timestamp,
    SHEET_ID,
)

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
COURSES_JSON = os.path.join(REPO_ROOT, "courses.json")

API_BASE = os.environ.get("CERT_LOG_API_BASE", "https://tools.icp.us")
API_TOKEN = os.environ.get("CERT_LOG_API_TOKEN", "")


def main():
    if not API_TOKEN:
        sys.exit("ERROR: CERT_LOG_API_TOKEN not set.")
    if not SHEET_ID:
        sys.exit("ERROR: SHEET_ID not set.")

    with open(COURSES_JSON) as f:
        courses = json.load(f)

    print("Authenticating to Google...")
    sheets, _ = gcp_clients()
    rows = read_sheet_rows(sheets)
    print(f"Loaded {len(rows)} rows from sheet")

    records = []
    skipped = 0
    for row in rows:
        if not row["cert_sent"]:
            continue
        if row["training"] not in courses:
            skipped += 1
            continue
        sent_at, resend_id = parse_cert_sent(row["cert_sent"])
        if not resend_id:
            skipped += 1
            print(f"  skip (no resend_id): {row['first']} {row['last']} — {row['training']}")
            continue

        sign_in_utc = parse_sheet_timestamp(row["timestamp"])
        course_date = format_course_date(sign_in_utc) if sign_in_utc else ""
        course_info = courses[row["training"]]
        full_name = f"{row['first']} {row['last']}".strip()
        if full_name.isupper() or full_name.islower():
            full_name = full_name.title()

        records.append({
            "full_name": full_name,
            "email": row["email"],
            "course_title": row["training"],
            "course_date": course_date,
            "pd_hours": str(course_info.get("hours", "1")),
            "course_format": course_info.get("format", "webinar"),
            "status": "sent",
            "timestamp_iso": sent_at or "",
            "resend_id": resend_id,
        })

    print(f"Records to import: {len(records)}  |  Skipped: {skipped}")
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
