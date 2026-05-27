# ICP Cloud Certificate Sender

GitHub Actions cron job that sends ICP training certificates automatically based on the ICP sign-in form. Runs without Andy needing to be at a computer.

## How it works

1. Attendee fills the sign-in Google Form at the start of a class.
2. The existing Apps Script trigger fires the welcome email + handout immediately.
3. **This repo's cron** runs every 15 minutes:
   - Reads the sign-in sheet via the Google Sheets API
   - Finds rows where: sign-in is 90+ min old, training matches a course in `courses.json`, and `cert_sent` is empty
   - Generates a cert PDF (Pillow overlays attendee name + course title + date/hours on a blank PNG template)
   - Sends via Resend with the standard ICP post-course email body and cert attached
   - Uploads the PDF to a designated Google Drive folder (ISET audit backup)
   - Writes `<ISO timestamp> resend:<id>` back to the `cert_sent` column so the row is never reprocessed
4. Failed sends write the error to the `cert_send_error` column for debugging.

## Auth: Workload Identity Federation

No service account keys. GitHub Actions presents an OIDC token, exchanges it for a short-lived GCP token, impersonates the `icp-cert-sender` service account. The service account has Editor access on the sign-in sheet and the Drive backup folder.

WIF config:
- Pool: `projects/998997004675/locations/global/workloadIdentityPools/github-actions`
- Provider: `.../providers/github`
- Service account: `icp-cert-sender@icp-scheduling.iam.gserviceaccount.com`
- Attribute condition restricts impersonation to `koolaidmanpavlov/icp-cert-sender`

## Required GitHub secrets

- `RESEND_API_KEY` — the ICP cert-sender Resend API key

(No GCP secrets needed — WIF handles auth without keys.)

## Config knobs (in `.github/workflows/cron.yml`)

| Env var | Value | What |
|---|---|---|
| `SHEET_ID` | `1Z0dpILZColNERZU1t2XFImXX6cYTPT_vrkbJKpupaFs` | The sign-in sheet |
| `DRIVE_BACKUP_FOLDER_ID` | `1__R6NI7co32HTebvgGVTw-3r8Gylh78I` | The Drive folder for cert PDF backups |
| `CERT_DELAY_MINUTES` | `90` | Don't send certs sooner than this many minutes after sign-in |

## Adding a new course

1. Add a new entry to `courses.json` with:
   - The exact training-name string as it appears in the sign-in form dropdown (must match Notion course catalog page title)
   - `template`: `template_webinar.png` or `template_in_person.png`
   - `title_lines`: course title broken into the line(s) you want on the cert
   - `hours`: PD hours as a string (e.g., `"1.5"`, `"2"`, `"3"`)
   - `format`: `"webinar"` or `"in-person"` (informational)
2. Commit and push. Next cron run picks it up.

## Sign-in sheet schema

Columns A–N are the original form columns. We added:
- O: `cert_sent` (cron writes ISO timestamp + Resend ID when sent)
- P: `cert_send_error` (cron writes error message if send fails)

## Manual trigger

Cron runs every 15 min, but cron timing on GitHub Actions can drift by 5–20 min during peak hours. To force a run:

- GitHub repo → **Actions** tab → **Send pending certificates** workflow → **Run workflow** button (top-right)
- Optional: toggle "Dry run" to log what *would* happen without actually sending

## ISET audit trail

For any given attendee, three independent sources:

1. **Sheet** — `cert_sent` column has the ISO timestamp + Resend send ID
2. **Drive** — PDF lives at `ICP Certificates / <course-slug> / <YYYY-MM-DD> / <Name>.pdf`
3. **Resend dashboard** — look up the send ID for delivery status, opens, clicks

## Related

- Mac-tied fallback scripts live in `~/Documents/Codex/situational_awareness/` and `~/Documents/Codex/legal_preparedness/`
- Welcome email automation (separate system, fires on form submission): `~/Documents/icp-email-automation/welcome-email.v2.gs`
