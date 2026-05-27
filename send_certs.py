#!/usr/bin/env python3
"""
ICP Cloud-Native Certificate Sender

Runs on a GitHub Actions cron every 15 min. Reads the ICP sign-in sheet,
finds rows where:
  - Sign-in timestamp is between 90 min and 24 hours old
  - Training name matches a course in courses.json
  - cert_sent column is empty (idempotent — skips already-processed rows)

For each qualifying row: generates a cert PDF (Pillow overlay on a blank
template), sends via Resend with the standard ICP post-course email body,
uploads the PDF to a designated Google Drive folder, and writes the send
timestamp back to the cert_sent column so the row is never reprocessed.

Auth: Google Workload Identity Federation (no service account keys).
GitHub Actions presents an OIDC token, exchanges it for a short-lived GCP
token, impersonates the icp-cert-sender service account. The service
account has Editor access on the sign-in sheet and the Drive backup folder.
Resend API key is a GitHub repo secret (RESEND_API_KEY).

Environment variables (all required, set in cron.yml):
  RESEND_API_KEY            Resend API key
  SHEET_ID                  Sign-in sheet ID
  DRIVE_BACKUP_FOLDER_ID    Drive folder for cert PDF backup
  CERT_DELAY_MINUTES        Minutes to wait after sign-in before sending (default: 90)
  DRY_RUN                   If "1", skip the actual send + write — print what would happen

Local testing:
  Set GOOGLE_APPLICATION_CREDENTIALS to a service account key JSON (only
  works if your org allows keys), or run via a gcloud user session with
  application-default credentials. In production this is unused —
  google-github-actions/auth@v2 sets up auth automatically.
"""

import base64
import csv
import io
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

import resend
from PIL import Image, ImageDraw, ImageFont
from google.auth import default as google_auth_default
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
COURSES_JSON = os.path.join(REPO_ROOT, "courses.json")
TEMPLATE_DIR = os.path.join(REPO_ROOT, "cert_templates")

# Required env vars
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
SHEET_ID = os.environ.get("SHEET_ID", "")
DRIVE_BACKUP_FOLDER_ID = os.environ.get("DRIVE_BACKUP_FOLDER_ID", "")
CERT_DELAY_MINUTES = int(os.environ.get("CERT_DELAY_MINUTES", "90"))
DRY_RUN = os.environ.get("DRY_RUN", "") == "1"

# Sheet config
SHEET_TAB = "Form Responses 1"  # default name; override if Andy renamed it
TIMESTAMP_COL = "A"
FIRST_NAME_COL = "B"
LAST_NAME_COL = "C"
EMAIL_COL = "D"
CONFIRM_EMAIL_COL = "E"
TRAINING_COL = "L"
CERT_SENT_COL = "O"
CERT_ERROR_COL = "P"

# Email branding
FROM_EMAIL = "Institute for Childhood Preparedness <info@learn.icp.us>"
REPLY_TO = "andy@icp.us"

# Cert rendering — matched to 1426x1103 ICP templates
NAME_CENTER_Y = 450
NAME_FONT_SIZE = 78
NAME_COLOR = (15, 42, 63)
NAME_MAX_WIDTH_RATIO = 0.75

COURSE_TITLE_CENTER_Y = 625
COURSE_TITLE_FONT_SIZE = 38
COURSE_TITLE_COLOR = (41, 182, 231)
COURSE_TITLE_LINE_SPACING = 12

DATE_LINE_CENTER_Y = 715
DATE_LINE_FONT_SIZE = 26
DATE_LINE_COLOR = (41, 182, 231)

# GitHub Actions Linux fonts (installed via apt in cron.yml)
NAME_FONT_PATH = "/usr/share/fonts/truetype/merriweather/Merriweather-Regular.ttf"
TITLE_FONT_PATH = "/usr/share/fonts/truetype/merriweather/Merriweather-Bold.ttf"
DATE_FONT_PATH = "/usr/share/fonts/truetype/merriweather/Merriweather-Regular.ttf"

# Fallback for local dev on Mac
if not os.path.exists(NAME_FONT_PATH):
    NAME_FONT_PATH = "/Library/Fonts/Merriweather_Regular.ttf"
    TITLE_FONT_PATH = "/Library/Fonts/Merriweather_Bold.ttf"
    DATE_FONT_PATH = "/Library/Fonts/Merriweather_Regular.ttf"


# ============================================================
# EMAIL BODY (matches the canonical ICP post-course template)
# ============================================================
def build_email_html(first_name, course_title, pd_hours):
    return f"""
<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>
  body {{ font-family: Georgia, serif; color: #1F2937; line-height: 1.7; margin: 0; padding: 0; background: #f9fafb; }}
  .container {{ max-width: 640px; margin: 0 auto; padding: 32px 24px; background: #ffffff; }}
  h2 {{ font-size: 19px; margin-top: 32px; margin-bottom: 4px; color: #0F2A3F; }}
  h3 {{ font-size: 16px; margin-top: 22px; margin-bottom: 4px; color: #0F2A3F; }}
  a {{ color: #0F4C75; }}
  .signature {{ margin-top: 28px; line-height: 1.5; }}
  .signature .name {{ font-weight: bold; }}
  .footer {{ margin-top: 32px; padding-top: 20px; border-top: 1px solid #E5E7EB; color: #6B7280; font-size: 13px; }}
</style></head><body>
<div class="container">
<p>Hi {first_name},</p>
<p>Thanks again for joining us for <strong>{course_title}</strong>! We really appreciated your time and participation. Your energy and engagement made it a meaningful session.</p>
<p>Your certificate of attendance is attached. This session counts as <strong>{pd_hours} hours of professional development</strong>.</p>
<p>📝 We'd love your feedback &mdash; it only takes a minute at <a href="https://bit.ly/icpeval">bit.ly/icpeval</a>.</p>
<h3>🚨 Interested in more training for your team?</h3>
<p>We offer virtual and in-person sessions on preparedness, safety, and de-escalation. Feel free to forward this to your supervisor or reply here and I can help. Learn more at <a href="https://childhoodpreparedness.org">childhoodpreparedness.org</a>.</p>
<h2>Tools to Help Keep Your Program Safe</h2>
<h3>📡 Two-Way Radios for Child Care Programs</h3>
<p>Real-time communication is critical during emergencies, transportation, and everyday operations. Our push-to-talk walkie talkies are designed specifically for early childhood programs and trusted by over 6,000 programs nationwide, including U.S. military installations. Learn more at <a href="https://walkietalkies.us">walkietalkies.us</a>.</p>
<h3>📱 Mobile Texting for Parent Communication</h3>
<p>Need a better way to send safety reminders, weather alerts, and updates to your families? Our mobile texting platform makes it easy to reach every parent instantly. Learn more at <a href="https://childhoodpreparedness.org/texting">childhoodpreparedness.org/texting</a>.</p>
<h3>🎧 Early Childhood Chats Podcast</h3>
<p>Check out our <a href="https://earlychildhoodchats.com">Early Childhood Chats</a> podcast for expert discussions, practical tips, and the latest in early childhood education.</p>
<p>Thank you again for being part of today's session. If you have questions about anything we covered, don't hesitate to reach out.</p>
<p>Stay safe out there,</p>
<div class="signature">
<span class="name">Andy Roszak, JD, MPA</span><br>
Founder &amp; Executive Director<br>
Institute for Childhood Preparedness<br>
<a href="mailto:andy@icp.us">andy@icp.us</a> &nbsp;|&nbsp; 202-247-6903<br>
<a href="https://childhoodpreparedness.org">childhoodpreparedness.org</a>
</div>
<div class="footer">You're receiving this because you signed in for {course_title}.</div>
</div></body></html>
"""


# ============================================================
# CERT GENERATION (Pillow overlay)
# ============================================================
def sanitize(name):
    return re.sub(r"[^a-zA-Z0-9]", "_", name)


def fit_font(text, path, max_size, max_width):
    size = max_size
    f = ImageFont.truetype(path, size)
    while f.getlength(text) > max_width and size > 14:
        size -= 2
        f = ImageFont.truetype(path, size)
    return f


def draw_centered(draw, text, font, center_y, w, color):
    bbox = font.getbbox(text)
    th = bbox[3] - bbox[1]
    tw = font.getlength(text)
    draw.text(((w - tw) / 2, center_y - th / 2 - bbox[1]), text, font=font, fill=color)


def draw_multiline_centered(draw, lines, font, center_y, w, color, spacing):
    bbox = font.getbbox("Ag")
    lh = bbox[3] - bbox[1]
    total = len(lines) * lh + (len(lines) - 1) * spacing
    start_y = center_y - total / 2 - bbox[1]
    for i, line in enumerate(lines):
        tw = font.getlength(line)
        draw.text(((w - tw) / 2, start_y + i * (lh + spacing)), line, font=font, fill=color)


def render_cert(full_name, course_config, course_date_str):
    template_path = os.path.join(TEMPLATE_DIR, course_config["template"])
    img = Image.open(template_path).convert("RGB")
    draw = ImageDraw.Draw(img)
    W, _ = img.size

    # Name
    name_font = ImageFont.truetype(NAME_FONT_PATH, NAME_FONT_SIZE)
    if name_font.getlength(full_name) > W * NAME_MAX_WIDTH_RATIO:
        name_font = fit_font(full_name, NAME_FONT_PATH, NAME_FONT_SIZE, W * NAME_MAX_WIDTH_RATIO)
    draw_centered(draw, full_name, name_font, NAME_CENTER_Y, W, NAME_COLOR)

    # Title
    title_font = ImageFont.truetype(TITLE_FONT_PATH, COURSE_TITLE_FONT_SIZE)
    title_lines = course_config["title_lines"]
    longest = max(title_lines, key=lambda s: title_font.getlength(s))
    if title_font.getlength(longest) > W * 0.85:
        title_font = fit_font(longest, TITLE_FONT_PATH, COURSE_TITLE_FONT_SIZE, W * 0.85)
    draw_multiline_centered(draw, title_lines, title_font, COURSE_TITLE_CENTER_Y, W,
                            COURSE_TITLE_COLOR, COURSE_TITLE_LINE_SPACING)

    # Date | Hours
    hours = course_config["hours"]
    hours_word = "Hour" if str(hours).strip() == "1" else "Hours"
    date_text = f"{course_date_str}   |   {hours} {hours_word} of Professional Development"
    date_font = ImageFont.truetype(DATE_FONT_PATH, DATE_LINE_FONT_SIZE)
    if date_font.getlength(date_text) > W * 0.85:
        date_font = fit_font(date_text, DATE_FONT_PATH, DATE_LINE_FONT_SIZE, W * 0.85)
    draw_centered(draw, date_text, date_font, DATE_LINE_CENTER_Y, W, DATE_LINE_COLOR)

    buf = io.BytesIO()
    img.save(buf, "PDF", resolution=200.0)
    buf.seek(0)
    return buf.read()


# ============================================================
# GOOGLE SHEETS / DRIVE
# ============================================================
SCOPES = ["https://www.googleapis.com/auth/spreadsheets",
          "https://www.googleapis.com/auth/drive.file"]


def gcp_clients():
    creds, _ = google_auth_default(scopes=SCOPES)
    sheets = build("sheets", "v4", credentials=creds)
    drive = build("drive", "v3", credentials=creds)
    return sheets, drive


def read_sheet_rows(sheets):
    """Return all data rows from the sign-in sheet with their 1-indexed sheet row number."""
    result = sheets.spreadsheets().values().get(
        spreadsheetId=SHEET_ID,
        range=f"'{SHEET_TAB}'!A:P",
    ).execute()
    values = result.get("values", [])
    if not values:
        return []
    # values[0] is the header row; data rows start at sheet row 2
    rows = []
    for i, row in enumerate(values[1:], start=2):
        row += [""] * (16 - len(row))
        rows.append({
            "sheet_row": i,
            "timestamp": row[0],
            "first": row[1].strip(),
            "last": row[2].strip(),
            "email": row[3].strip(),
            "confirm_email": row[4].strip(),
            "training": row[11].strip(),
            "cert_sent": row[14].strip(),
            "cert_error": row[15].strip(),
        })
    return rows


def write_cert_sent(sheets, sheet_row, value):
    sheets.spreadsheets().values().update(
        spreadsheetId=SHEET_ID,
        range=f"'{SHEET_TAB}'!{CERT_SENT_COL}{sheet_row}",
        valueInputOption="RAW",
        body={"values": [[value]]},
    ).execute()


def write_cert_error(sheets, sheet_row, value):
    sheets.spreadsheets().values().update(
        spreadsheetId=SHEET_ID,
        range=f"'{SHEET_TAB}'!{CERT_ERROR_COL}{sheet_row}",
        valueInputOption="RAW",
        body={"values": [[value]]},
    ).execute()


def upload_pdf_to_drive(drive, pdf_bytes, filename, parent_folder_id):
    """Upload a PDF to Drive. Creates date-stamped subfolder under parent."""
    media = MediaIoBaseUpload(io.BytesIO(pdf_bytes), mimetype="application/pdf", resumable=False)
    body = {"name": filename, "parents": [parent_folder_id]}
    drive.files().create(body=body, media_body=media, fields="id").execute()


def find_or_create_drive_folder(drive, name, parent_id):
    q = (f"name='{name}' and mimeType='application/vnd.google-apps.folder' "
         f"and '{parent_id}' in parents and trashed=false")
    res = drive.files().list(q=q, fields="files(id)").execute()
    items = res.get("files", [])
    if items:
        return items[0]["id"]
    meta = {"name": name, "mimeType": "application/vnd.google-apps.folder", "parents": [parent_id]}
    f = drive.files().create(body=meta, fields="id").execute()
    return f["id"]


# ============================================================
# TIMESTAMP PARSING
# ============================================================
SHEET_TIME_FORMATS = [
    "%m/%d/%Y %H:%M:%S",      # 5/27/2026 14:32:05 (US, 24h)
    "%m/%d/%Y %I:%M:%S %p",   # 5/27/2026 2:32:05 PM (US, 12h)
    "%m/%d/%Y %H:%M",
    "%Y-%m-%dT%H:%M:%S",      # ISO-ish, just in case
]


def parse_sheet_timestamp(s):
    """
    Parse a Google Sheets form-submission timestamp. Treat as Eastern (Andy's
    sheet locale). Returns a tz-aware UTC datetime, or None if unparseable.

    Note: Eastern offset is hardcoded as UTC-4 (EDT). Switch to UTC-5 (EST) if
    this is ever run in standard time. For ICP's May–Oct training season we're
    safely in EDT.
    """
    s = s.strip()
    dt_naive = None
    for fmt in SHEET_TIME_FORMATS:
        try:
            dt_naive = datetime.strptime(s, fmt)
            break
        except ValueError:
            continue
    if not dt_naive:
        return None
    eastern_offset = timedelta(hours=-4)
    dt_eastern = dt_naive.replace(tzinfo=timezone(eastern_offset))
    return dt_eastern.astimezone(timezone.utc)


def format_course_date(utc_dt):
    """'May 28, 2026' style, in Eastern time."""
    from datetime import timezone, timedelta
    eastern_offset = timedelta(hours=-4)
    local = utc_dt.astimezone(timezone(eastern_offset))
    return local.strftime("%B %-d, %Y")


# ============================================================
# MAIN
# ============================================================
def process_row(row, course_config, sheets, drive):
    full_name = f"{row['first']} {row['last']}".strip()
    if full_name.isupper() or full_name.islower():
        full_name = full_name.title()
    first = row["first"].title() if row["first"].isupper() or row["first"].islower() else row["first"]

    sign_in_utc = parse_sheet_timestamp(row["timestamp"])
    course_date = format_course_date(sign_in_utc)

    pdf_bytes = render_cert(full_name, course_config, course_date)
    safe = sanitize(full_name)
    pdf_filename = f"{safe}.pdf"

    if DRY_RUN:
        print(f"  [DRY] would send {full_name} <{row['email']}> for {row['training']} on {course_date}")
        return "dry-run"

    # Send email
    resend.api_key = RESEND_API_KEY
    subject = f"Your Certificate from Today's {row['training']} Training"
    params = {
        "from": FROM_EMAIL,
        "to": [row["email"]],
        "reply_to": REPLY_TO,
        "subject": subject,
        "html": build_email_html(first, row["training"], course_config["hours"]),
        "attachments": [{
            "filename": f"Certificate - {full_name}.pdf",
            "content": base64.b64encode(pdf_bytes).decode(),
        }],
    }
    r = resend.Emails.send(params)
    resend_id = r.get("id", "unknown")

    # Drive backup
    try:
        course_slug = re.sub(r"[^a-z0-9]+", "-", row["training"].lower()).strip("-")
        course_folder_id = find_or_create_drive_folder(drive, course_slug, DRIVE_BACKUP_FOLDER_ID)
        date_slug = course_date_to_slug(course_date)
        date_folder_id = find_or_create_drive_folder(drive, date_slug, course_folder_id)
        upload_pdf_to_drive(drive, pdf_bytes, pdf_filename, date_folder_id)
    except Exception as e:
        print(f"  drive backup failed (send succeeded): {e}")

    return resend_id


def course_date_to_slug(s):
    """'May 28, 2026' -> '2026-05-28'"""
    try:
        return datetime.strptime(s, "%B %d, %Y").strftime("%Y-%m-%d")
    except ValueError:
        return re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-").lower()


def main():
    if not RESEND_API_KEY or not SHEET_ID or not DRIVE_BACKUP_FOLDER_ID:
        sys.exit("ERROR: RESEND_API_KEY, SHEET_ID, DRIVE_BACKUP_FOLDER_ID required")

    with open(COURSES_JSON) as f:
        courses = json.load(f)

    sheets, drive = gcp_clients()
    rows = read_sheet_rows(sheets)
    print(f"Loaded {len(rows)} rows from sheet")

    now_utc = datetime.now(timezone.utc)
    min_age = timedelta(minutes=CERT_DELAY_MINUTES)
    max_age = timedelta(hours=24)

    processed = skipped_already_sent = skipped_too_new = skipped_other = errors = 0

    for row in rows:
        if row["cert_sent"]:
            skipped_already_sent += 1
            continue
        if row["training"] not in courses:
            skipped_other += 1
            continue

        sign_in_utc = parse_sheet_timestamp(row["timestamp"])
        if not sign_in_utc:
            print(f"  row {row['sheet_row']}: unparseable timestamp '{row['timestamp']}'")
            errors += 1
            continue

        age = now_utc - sign_in_utc
        if age < min_age:
            skipped_too_new += 1
            continue
        if age > max_age:
            skipped_other += 1
            continue

        if row["confirm_email"] and row["confirm_email"].lower() != row["email"].lower():
            print(f"  row {row['sheet_row']}: email mismatch, skipping")
            if not DRY_RUN:
                write_cert_error(sheets, row["sheet_row"], "email mismatch")
            errors += 1
            continue

        print(f"  row {row['sheet_row']}: processing {row['first']} {row['last']} ({row['training']})")
        try:
            resend_id = process_row(row, courses[row["training"]], sheets, drive)
            if not DRY_RUN:
                stamp = datetime.now(timezone.utc).isoformat()
                write_cert_sent(sheets, row["sheet_row"], f"{stamp} resend:{resend_id}")
            processed += 1
        except Exception as e:
            print(f"  row {row['sheet_row']}: send failed: {e}")
            if not DRY_RUN:
                try:
                    write_cert_error(sheets, row["sheet_row"], str(e)[:500])
                except Exception:
                    pass
            errors += 1

    print(f"\nSummary: processed={processed} already_sent={skipped_already_sent} "
          f"too_new={skipped_too_new} other={skipped_other} errors={errors}")


if __name__ == "__main__":
    main()
