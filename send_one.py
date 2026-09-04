#!/usr/bin/env python3
"""Render and send a single certificate via Resend.

Used for one-off recoveries (e.g., sign-in row had an email typo). Inputs come
from environment variables set by the `Send one-off certificate` workflow.
Reads the same cert templates and `courses.json` the cron uses, so the cert
looks identical to a cron-generated one. Does NOT touch the sign-in sheet —
clean up the row manually after running this.
"""
import base64
import io
import json
import os
import re
import sys
from datetime import datetime

import requests
import resend
from PIL import Image, ImageDraw, ImageFont

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_DIR = os.path.join(REPO_ROOT, "cert_templates")
FONT_DIR = os.path.join(REPO_ROOT, "fonts")
NAME_FONT_PATH = os.path.join(FONT_DIR, "Merriweather_Regular.ttf")
TITLE_FONT_PATH = os.path.join(FONT_DIR, "Merriweather_Bold.ttf")
DATE_FONT_PATH = os.path.join(FONT_DIR, "Merriweather_Regular.ttf")

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


SCHEDULER_SESSIONS_URL = os.environ.get(
    "SCHEDULER_SESSIONS_URL", "https://tools.icp.us/api/sessions/list")

# Kept in step with send_certs.py — see the DELIVERY FORMAT block there.
TEMPLATE_BY_FORMAT = {
    "in-person": "template_in_person.png",
    "webinar": "template_webinar.png",
}
_FORMAT_ALIASES = {
    "in-person": "in-person",
    "inperson": "in-person",
    "live-webinar": "webinar",
    "webinar": "webinar",
}


def normalize_format(value):
    if not value:
        return None
    return _FORMAT_ALIASES.get(str(value).strip().lower().replace("_", "-"))


def resolve_delivery_format(course_name, date_iso, default_format):
    """Match send_certs.py: the scheduler decides how the session was delivered."""
    try:
        r = requests.get(SCHEDULER_SESSIONS_URL, timeout=8)
        r.raise_for_status()
        sessions = r.json().get("sessions", [])
    except Exception as exc:
        print(f"  [scheduler] warning: could not fetch session list — {exc}")
        sessions = []
    for sess in sessions:
        if sess.get("date") != date_iso:
            continue
        for seg in sess.get("segments", []):
            if seg.get("course_name") != course_name:
                continue
            fmt = normalize_format(sess.get("delivery_mode"))
            if fmt:
                return fmt
    fallback = normalize_format(default_format) or "webinar"
    print(f"  [scheduler] no session matched {course_name!r} on {date_iso} — "
          f"using default format {fallback!r}")
    return fallback


def render_cert(full_name, course_config, course_date_str, course_format):
    template_path = os.path.join(TEMPLATE_DIR, TEMPLATE_BY_FORMAT[course_format])
    img = Image.open(template_path).convert("RGB")
    draw = ImageDraw.Draw(img)
    W, _ = img.size
    name_font = ImageFont.truetype(NAME_FONT_PATH, NAME_FONT_SIZE)
    if name_font.getlength(full_name) > W * NAME_MAX_WIDTH_RATIO:
        name_font = fit_font(full_name, NAME_FONT_PATH, NAME_FONT_SIZE, W * NAME_MAX_WIDTH_RATIO)
    draw_centered(draw, full_name, name_font, NAME_CENTER_Y, W, NAME_COLOR)
    title_font = ImageFont.truetype(TITLE_FONT_PATH, COURSE_TITLE_FONT_SIZE)
    title_lines = course_config["title_lines"]
    longest = max(title_lines, key=lambda s: title_font.getlength(s))
    if title_font.getlength(longest) > W * 0.85:
        title_font = fit_font(longest, TITLE_FONT_PATH, COURSE_TITLE_FONT_SIZE, W * 0.85)
    draw_multiline_centered(draw, title_lines, title_font, COURSE_TITLE_CENTER_Y, W,
                            COURSE_TITLE_COLOR, COURSE_TITLE_LINE_SPACING)
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


def main():
    full_name = os.environ["ATTENDEE_NAME"].strip()
    email = os.environ["ATTENDEE_EMAIL"].strip()
    course_key = os.environ["COURSE_KEY"].strip()
    course_date = os.environ["COURSE_DATE"].strip()
    resend_api_key = os.environ["RESEND_API_KEY"]

    with open(os.path.join(REPO_ROOT, "courses.json")) as f:
        courses = json.load(f)
    if course_key not in courses:
        sys.exit(f"course_key '{course_key}' not found in courses.json")

    course_config = courses[course_key]
    first_name = full_name.split()[0] if full_name else ""

    # COURSE_DATE is a display string ("September 3, 2026"); the scheduler keys
    # on ISO. Accept COURSE_DATE_ISO explicitly, else derive it.
    course_date_iso = os.environ.get("COURSE_DATE_ISO", "").strip()
    if not course_date_iso:
        try:
            course_date_iso = datetime.strptime(course_date, "%B %d, %Y").strftime("%Y-%m-%d")
        except ValueError:
            course_date_iso = ""
    course_format = resolve_delivery_format(course_key, course_date_iso,
                                            course_config.get("format"))
    print(f"  format: {course_format} (template {TEMPLATE_BY_FORMAT[course_format]})")

    pdf_bytes = render_cert(full_name, course_config, course_date, course_format)

    course_slug = re.sub(r"[^a-z0-9]+", "-", course_key.lower()).strip("-")
    date_slug = re.sub(r"[^a-z0-9]+", "-", course_date.lower()).strip("-")
    safe_email = re.sub(r"[^a-z0-9._-]+", "-", email.lower())
    idem_key = f"cert-{course_slug}-{date_slug}-{safe_email}"[:255]

    resend.api_key = resend_api_key
    resp = resend.Emails.send({
        "from": "Institute for Childhood Preparedness <info@learn.icp.us>",
        "to": [email],
        "reply_to": "andy@icp.us",
        "subject": f"Your Certificate from Today's {course_key} Training",
        "html": build_email_html(first_name, course_key, course_config["hours"]),
        "attachments": [{
            "filename": f"Certificate - {full_name}.pdf",
            "content": base64.b64encode(pdf_bytes).decode(),
        }],
        "headers": {"Idempotency-Key": idem_key},
    })
    print(f"Sent to {email} — resend id: {resp.get('id')}")


if __name__ == "__main__":
    main()
