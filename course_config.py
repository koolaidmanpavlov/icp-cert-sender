"""Course certificate config: scheduler first, courses.json as the safety net.

The master course list lives in the scheduler (tools.icp.us, /crm/settings/courses). Each
course carries its own certificate settings there (title lines, PD hours, template), and a
course cannot be made active without them. This module merges that list over courses.json:

  - scheduler entry present and valid  -> used (it wins over the file)
  - course only in courses.json        -> kept (old sign-ins for retired courses still certify)
  - scheduler unreachable / bad reply  -> courses.json alone, with a warning (never a hard stop)

Result: adding a course in the scheduler is enough for certificates to work. courses.json is
now only the fallback and can stay as it is.
"""
import json
import os

import requests

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
COURSES_JSON = os.path.join(REPO_ROOT, "courses.json")
COURSES_API_URL = os.environ.get("SCHEDULER_COURSES_URL", "https://tools.icp.us/api/courses/certs")
FORMATS = {"webinar", "in-person"}


def _valid(cfg):
    try:
        lines = cfg["title_lines"]
        return (isinstance(lines, list) and lines and all(isinstance(x, str) and x.strip() for x in lines)
                and cfg.get("format") in FORMATS and float(cfg["hours"]) > 0)
    except (KeyError, TypeError, ValueError):
        return False


def fetch_scheduler_courses(url=None, get=requests.get, attempts=3):
    """Returns (courses_dict, uncertified_list), or (None, []) when the scheduler cannot be used."""
    url = url or COURSES_API_URL
    last = None
    for _ in range(attempts):
        try:
            r = get(url, timeout=10)
            r.raise_for_status()
            body = r.json()
            courses = body.get("courses")
            if not isinstance(courses, dict):
                raise ValueError("reply has no 'courses' object")
            return courses, [str(x) for x in body.get("uncertified", [])]
        except Exception as exc:  # noqa: BLE001 - any failure falls back to the file
            last = exc
    print(f"::warning::Could not read course config from the scheduler ({last}). Using courses.json only.")
    return None, []


def load_courses(url=None, get=requests.get, path=None):
    with open(path or COURSES_JSON) as f:
        courses = json.load(f)
    remote, uncertified = fetch_scheduler_courses(url, get)
    info = {"api_ok": remote is not None, "uncertified": uncertified, "from_scheduler": [], "only_in_file": []}
    if remote is not None:
        for name, cfg in remote.items():
            if _valid(cfg):
                if name not in courses:
                    info["from_scheduler"].append(name)
                courses[name] = cfg
            else:
                print(f"::warning::Scheduler course {name!r} has an invalid certificate config; ignoring it.")
        info["only_in_file"] = sorted(n for n in courses if n not in remote)
    for name in uncertified:
        print(f"::error::Course {name!r} is ACTIVE in the scheduler but has no certificate settings. "
              f"Attendees would not get certificates. Fix it at https://tools.icp.us/crm/settings/courses")
    if info["from_scheduler"]:
        print(f"Courses added in the scheduler (not in courses.json): {', '.join(info['from_scheduler'])}")
    return courses, info
