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
import hashlib
import hmac as _hmac
import io
import json
import os
import re
import socket
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import requests
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

# Unified cert audit log (tools.icp.us/api/cert-log)
CERT_LOG_API_BASE = os.environ.get("CERT_LOG_API_BASE", "https://tools.icp.us")
CERT_LOG_API_TOKEN = os.environ.get("CERT_LOG_API_TOKEN", "")

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

# Fonts bundled in the repo (SIL Open Font License — bundling is fine)
FONT_DIR = os.path.join(REPO_ROOT, "fonts")
NAME_FONT_PATH = os.path.join(FONT_DIR, "Merriweather_Regular.ttf")
TITLE_FONT_PATH = os.path.join(FONT_DIR, "Merriweather_Bold.ttf")
DATE_FONT_PATH = os.path.join(FONT_DIR, "Merriweather_Regular.ttf")


# ============================================================
# RESILIENT SEND
# ============================================================
# Resend rate-limit / 5xx errors are usually transient. We retry once after a
# short backoff. The Idempotency-Key header guards against any send the server
# already accepted but we missed the response for — Resend dedupes for 24h on
# this key, so a retry returns the original email id instead of sending twice.
def build_idempotency_key(course_slug, date_slug, email):
    safe_email = re.sub(r"[^a-z0-9._-]+", "-", (email or "").lower())
    return f"cert-{course_slug}-{date_slug}-{safe_email}"[:255]


# Consult the icp-tools suppression API before sending. Fails open: if the
# API isn't configured or isn't reachable, we proceed with the send rather
# than blocking legitimate emails on infra issues.
SUPPRESSION_API_TOKEN = os.environ.get("SUPPRESSION_API_TOKEN", "")
SUPPRESSION_API_BASE = os.environ.get("SUPPRESSION_API_BASE", "https://tools.icp.us")
SUPPRESSION_TOKEN_SECRET = os.environ.get("SUPPRESSION_TOKEN_SECRET", "")
UNSUBSCRIBE_BASE_URL = os.environ.get("UNSUBSCRIBE_BASE_URL", "https://learn.icp.us")


def is_suppressed(email):
    if not SUPPRESSION_API_TOKEN:
        return False, None
    url = f"{SUPPRESSION_API_BASE.rstrip('/')}/api/suppressions/check?email={requests.utils.quote(email)}"
    try:
        r = requests.get(url, headers={"x-suppression-token": SUPPRESSION_API_TOKEN}, timeout=5)
        if r.status_code != 200:
            return False, None
        data = r.json()
        return bool(data.get("suppressed")), data.get("reason")
    except Exception:
        return False, None


def build_unsubscribe_headers(email):
    if not SUPPRESSION_TOKEN_SECRET:
        return {}
    sig = _hmac.new(SUPPRESSION_TOKEN_SECRET.encode(), email.lower().encode(), hashlib.sha256).digest()
    token = base64.urlsafe_b64encode(sig).rstrip(b"=").decode()
    query = urlencode({"u": email.lower(), "t": token})
    url = f"{UNSUBSCRIBE_BASE_URL.rstrip('/')}/unsubscribe?{query}"
    return {
        "List-Unsubscribe": f"<mailto:unsubscribe@icp.us?subject=unsubscribe>, <{url}>",
        "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
    }


def _log_to_cert_api(row_data, timeout=8):
    """POST one cert record to the unified D1 audit log. Fail-open."""
    if not CERT_LOG_API_TOKEN:
        return
    try:
        resp = requests.post(
            f"{CERT_LOG_API_BASE.rstrip('/')}/api/cert-log",
            json=[row_data],
            headers={"x-cert-log-token": CERT_LOG_API_TOKEN},
            timeout=timeout,
        )
        data = resp.json()
        if not data.get("ok"):
            print(f"  [cert-log] warning: {data}")
    except Exception as exc:
        print(f"  [cert-log] warning: could not reach log API — {exc}")


_TRANSIENT_PATTERNS = ("503", "504", "502", "429", "timeout", "temporarily")


def _is_transient(err):
    msg = str(err).lower()
    return any(p in msg for p in _TRANSIENT_PATTERNS)


def send_with_retry(params, idempotency_key, max_attempts=2, backoff_seconds=2.0):
    """Send via Resend with idempotency + one retry on transient errors.

    Idempotency-Key is added to params['headers']. If the SDK or upstream API
    returns a 5xx/429/timeout, we sleep and retry; permanent errors (4xx other
    than 429) propagate immediately so we don't loop on bad data.
    """
    headers = dict(params.get("headers") or {})
    headers["Idempotency-Key"] = idempotency_key
    params = {**params, "headers": headers}

    last_err = None
    for attempt in range(1, max_attempts + 1):
        try:
            return resend.Emails.send(params)
        except Exception as e:
            last_err = e
            if attempt < max_attempts and _is_transient(e):
                time.sleep(backoff_seconds)
                continue
            raise
    raise last_err  # unreachable, but explicit


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


def google_execute(request, label, retries=3, backoff_seconds=2.0):
    """Execute a Google API request with light retry for transient runner hiccups."""
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            return request.execute(num_retries=2)
        except Exception as exc:
            last_err = exc
            transient = isinstance(exc, (TimeoutError, socket.timeout, OSError))
            msg = str(exc).lower()
            transient = transient or any(token in msg for token in (
                "timeout",
                "timed out",
                "503",
                "502",
                "500",
                "rate limit",
                "connection reset",
                "temporarily unavailable",
            ))
            if attempt < retries and transient:
                sleep_for = backoff_seconds * attempt
                print(f"  [{label}] transient Google API error, retrying in {sleep_for:.0f}s: {exc}")
                time.sleep(sleep_for)
                continue
            raise last_err
    raise last_err


def read_sheet_rows(sheets):
    """Return all data rows from the sign-in sheet with their 1-indexed sheet row number."""
    result = google_execute(
        sheets.spreadsheets().values().get(
            spreadsheetId=SHEET_ID,
            range=f"'{SHEET_TAB}'!A:P",
        ),
        "read-sheet",
    )
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
    google_execute(
        sheets.spreadsheets().values().update(
            spreadsheetId=SHEET_ID,
            range=f"'{SHEET_TAB}'!{CERT_SENT_COL}{sheet_row}",
            valueInputOption="RAW",
            body={"values": [[value]]},
        ),
        f"write-cert-sent-row-{sheet_row}",
    )


def write_cert_error(sheets, sheet_row, value):
    google_execute(
        sheets.spreadsheets().values().update(
            spreadsheetId=SHEET_ID,
            range=f"'{SHEET_TAB}'!{CERT_ERROR_COL}{sheet_row}",
            valueInputOption="RAW",
            body={"values": [[value]]},
        ),
        f"write-cert-error-row-{sheet_row}",
    )


# Shared Drive support: every Drive API call needs supportsAllDrives=True;
# list queries also need includeItemsFromAllDrives=True. Works for My Drive too.
DRIVE_KWARGS = {"supportsAllDrives": True}
LIST_KWARGS = {"supportsAllDrives": True, "includeItemsFromAllDrives": True}


def upload_pdf_to_drive(drive, pdf_bytes, filename, parent_folder_id):
    media = MediaIoBaseUpload(io.BytesIO(pdf_bytes), mimetype="application/pdf", resumable=False)
    body = {"name": filename, "parents": [parent_folder_id]}
    f = google_execute(
        drive.files().create(body=body, media_body=media, fields="id", **DRIVE_KWARGS),
        f"upload-drive-file-{filename}",
    )
    return f["id"]


def find_or_create_drive_folder(drive, name, parent_id):
    q = (f"name='{name}' and mimeType='application/vnd.google-apps.folder' "
         f"and '{parent_id}' in parents and trashed=false")
    res = google_execute(
        drive.files().list(q=q, fields="files(id)", **LIST_KWARGS),
        f"find-drive-folder-{name}",
    )
    items = res.get("files", [])
    if items:
        return items[0]["id"]
    meta = {"name": name, "mimeType": "application/vnd.google-apps.folder", "parents": [parent_id]}
    f = google_execute(
        drive.files().create(body=meta, fields="id", **DRIVE_KWARGS),
        f"create-drive-folder-{name}",
    )
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

    # Skip suppressed addresses (bounced / complained / unsubscribed).
    suppressed, reason = is_suppressed(row["email"])
    if suppressed:
        print(f"  suppressed ({reason}) — skipping")
        return f"skipped-suppressed-{reason}"

    sign_in_utc = parse_sheet_timestamp(row["timestamp"])
    course_date = format_course_date(sign_in_utc)

    pdf_bytes = render_cert(full_name, course_config, course_date)
    safe = sanitize(full_name)
    pdf_filename = f"{safe}.pdf"

    if DRY_RUN:
        print(f"  [DRY] would send {full_name} <{row['email']}> for {row['training']} on {course_date}")
        return "dry-run"

    # Send email — with idempotency + retry + List-Unsubscribe + suppression-aware.
    resend.api_key = RESEND_API_KEY
    subject = f"Your Certificate from Today's {row['training']} Training"
    headers = dict(build_unsubscribe_headers(row["email"]))
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
        "headers": headers,
    }
    course_slug = re.sub(r"[^a-z0-9]+", "-", row["training"].lower()).strip("-")
    date_slug = course_date_to_slug(course_date)
    idem = build_idempotency_key(course_slug, date_slug, row["email"])
    r = send_with_retry(params, idem)
    resend_id = r.get("id", "unknown")

    # Log to unified cert audit trail
    _log_to_cert_api({
        "full_name": full_name,
        "email": row["email"],
        "course_title": row["training"],
        "course_date": course_date,
        "pd_hours": str(course_config["hours"]),
        "course_format": course_config.get("format", "webinar"),
        "status": "sent",
        "timestamp_iso": datetime.now(timezone.utc).isoformat(),
        "resend_id": resend_id,
    })

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


# ============================================================
# CERTS.JSON BUILDER
#
# After the send loop, walk every row in the sign-in sheet that has a non-empty
# cert_sent value, group by (course_date, training), and emit a public JSON
# summary the eval dashboard can fetch. Includes per-attendee Drive file URLs
# (looked up by listing each dated folder once and matching sanitized name).
# ============================================================
CERTS_JSON_PATH = os.path.join(REPO_ROOT, "data", "certs.json")


def parse_cert_sent(cell):
    """`<iso timestamp> resend:<id>` -> (sent_at, resend_id). Tolerates older formats."""
    if not cell:
        return None, None
    sent_at = None
    resend_id = None
    parts = cell.split(" resend:", 1)
    sent_at = parts[0].strip() or None
    if len(parts) == 2:
        resend_id = parts[1].strip() or None
    return sent_at, resend_id


def list_drive_folder_files(drive, folder_id):
    """Return [(name, id, web_view_link), ...] for files in a folder. Pages through results."""
    out = []
    page_token = None
    while True:
        resp = drive.files().list(
            q=f"'{folder_id}' in parents and trashed=false",
            fields="nextPageToken, files(id, name, webViewLink)",
            pageSize=1000,
            pageToken=page_token,
            **LIST_KWARGS,
        )
        resp = google_execute(resp, f"list-drive-folder-{folder_id}")
        for f in resp.get("files", []):
            out.append((f["name"], f["id"], f.get("webViewLink")))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return out


def find_drive_subfolder(drive, parent_id, name):
    """Return (folder_id, web_view_link) for the named subfolder, or (None, None)."""
    q = (f"name='{name}' and mimeType='application/vnd.google-apps.folder' "
         f"and '{parent_id}' in parents and trashed=false")
    res = google_execute(
        drive.files().list(
            q=q, fields="files(id, webViewLink)", **LIST_KWARGS,
        ),
        f"find-drive-subfolder-{name}",
    )
    items = res.get("files", [])
    if not items:
        return None, None
    return items[0]["id"], items[0].get("webViewLink")


def session_date_from_timestamp(ts_str):
    """Convert a sign-in timestamp into a YYYY-MM-DD date string in Eastern.
    The cron treats the sheet as Eastern (UTC-4 EDT during training season)."""
    utc_dt = parse_sheet_timestamp(ts_str)
    if not utc_dt:
        return None
    eastern = utc_dt - timedelta(hours=4)
    return eastern.strftime("%Y-%m-%d")


def build_certs_json(sheets, drive, courses, rows):
    """
    Walk every sign-in row, group into sessions keyed by 'YYYY-MM-DD|<training>',
    record reconciliation stats (sign-in count, certs sent, errors, pending),
    resolve Drive file URLs by listing the dated folder once per session, and
    capture sign-ins whose training name doesn't match any known course (the
    Salem-VA failure mode — surfaces them at the top level so the daily digest
    and scheduler reconciliation panel can flag them).
    Writes data/certs.json.
    """
    sessions = {}
    unknown_course_rows = []
    for row in rows:
        date_str = session_date_from_timestamp(row["timestamp"])
        if not date_str:
            continue
        training = row["training"]
        full_name = f"{row['first']} {row['last']}".strip()
        if full_name.isupper() or full_name.islower():
            full_name = full_name.title()

        # Pre-system rows were handled manually; exclude from reconciliation.
        if date_str < "2026-05-25":
            continue

        if training not in courses:
            unknown_course_rows.append({
                "date": date_str,
                "training": training,
                "name": full_name,
                "email": row.get("email", ""),
                "timestamp": row["timestamp"],
                "sheet_row": row.get("sheet_row"),
            })
            continue

        key = f"{date_str}|{training}"
        sess = sessions.setdefault(key, {
            "date": date_str,
            "course": training,
            "course_slug": re.sub(r"[^a-z0-9]+", "-", training.lower()).strip("-"),
            "attendees": [],
            "errors": [],
            "sign_in_count": 0,
            "cert_pending_count": 0,
        })
        sess["sign_in_count"] += 1

        if row.get("cert_error"):
            sess["errors"].append({
                "name": full_name,
                "email": row.get("email", ""),
                "error": row["cert_error"],
                "timestamp": row["timestamp"],
                "sheet_row": row.get("sheet_row"),
            })
            continue

        if not row["cert_sent"]:
            sess["cert_pending_count"] += 1
            continue

        sent_at, resend_id = parse_cert_sent(row["cert_sent"])
        sess["attendees"].append({
            "name": full_name,
            "sanitized": sanitize(full_name),
            "sent_at": sent_at,
            "resend_id": resend_id,
        })

    # Resolve Drive folder + file URLs per session
    # Cache course-slug folder lookups so we don't repeat them per session.
    course_folders = {}  # course_slug -> (folder_id, link)
    for key, sess in sessions.items():
        slug = sess["course_slug"]
        if slug not in course_folders:
            course_folders[slug] = find_drive_subfolder(drive, DRIVE_BACKUP_FOLDER_ID, slug)
        course_folder_id, _ = course_folders[slug]
        if not course_folder_id:
            sess["drive_folder_url"] = None
        else:
            date_folder_id, date_folder_link = find_drive_subfolder(drive, course_folder_id, sess["date"])
            sess["drive_folder_url"] = date_folder_link
            file_index = {}
            if date_folder_id:
                for name, file_id, link in list_drive_folder_files(drive, date_folder_id):
                    # Strip .pdf to match against sanitize(full_name)
                    base = name[:-4] if name.lower().endswith(".pdf") else name
                    file_index[base] = link
            for a in sess["attendees"]:
                a["drive_pdf_url"] = file_index.get(a["sanitized"])
        # Drop sanitized field from final output (only needed for lookup)
        for a in sess["attendees"]:
            a.pop("sanitized", None)
        sess["cert_count"] = len(sess["attendees"])
        sess["cert_error_count"] = len(sess["errors"])
        # Stable sort by attendee name
        sess["attendees"].sort(key=lambda a: a["name"].lower())
        sess["errors"].sort(key=lambda e: e["name"].lower())

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sessions": sessions,
        "unknown_course_rows": unknown_course_rows,
    }
    os.makedirs(os.path.dirname(CERTS_JSON_PATH), exist_ok=True)
    with open(CERTS_JSON_PATH, "w") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
    print(f"Wrote {CERTS_JSON_PATH} with {len(sessions)} sessions, "
          f"{len(unknown_course_rows)} unknown-course rows")


def main():
    if not RESEND_API_KEY or not SHEET_ID or not DRIVE_BACKUP_FOLDER_ID:
        sys.exit("ERROR: RESEND_API_KEY, SHEET_ID, DRIVE_BACKUP_FOLDER_ID required")

    with open(COURSES_JSON) as f:
        courses = json.load(f)

    try:
        sheets, drive = gcp_clients()
        rows = read_sheet_rows(sheets)
    except Exception as exc:
        sys.exit(f"ERROR: could not initialize Google clients or read the sign-in sheet: {exc}")
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

    # Refresh the public certs.json that the eval dashboard consumes.
    # Re-read the sheet to pick up any cert_sent writes from this run.
    if not DRY_RUN:
        try:
            fresh_rows = read_sheet_rows(sheets)
            build_certs_json(sheets, drive, courses, fresh_rows)
        except Exception as e:
            print(f"certs.json refresh failed (non-fatal): {e}")


if __name__ == "__main__":
    main()
