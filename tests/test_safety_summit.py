"""python3 -m unittest tests.test_safety_summit   (no network)

Safety Summit is date-restricted: only certified on dates listed in `event_dates`.
Detroit (2026-10-10) is credited by the Michigan registry and must NEVER get an ICP cert.
"""
import json, os, unittest
import course_config as cc
import send_certs as sc

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COURSES = json.load(open(os.path.join(ROOT, "courses.json")))
SS = COURSES["Safety Summit"]


class T(unittest.TestCase):
    def test_dc_event_date_enabled_with_topics(self):
        self.assertEqual(sc.known_course_names("Safety Summit", COURSES, "2026-10-13"), ["Safety Summit"])
        self.assertEqual(sc.event_topics(SS, "2026-10-13"),
                         ["Active Threat Preparedness", "Situational Awareness", "De-Escalation"])
        self.assertEqual(SS["hours"], "4")

    def test_detroit_and_other_dates_never_certified(self):
        for d in ("2026-10-10", "2026-10-06", "2026-10-14", "", None):
            self.assertFalse(sc.course_enabled_for_date(SS, d))
        self.assertIsNone(sc.known_course_names("Safety Summit", COURSES, "2026-10-10"))
        self.assertEqual(sc.event_topics(SS, "2026-10-10"), [])

    def test_combined_with_summit_blocked_on_other_dates(self):
        t = "Safety Summit + De-Escalation Techniques for Early Childhood Professionals"
        self.assertIsNone(sc.known_course_names(t, COURSES, "2026-10-10"))

    def test_normal_courses_unaffected(self):
        n = "De-Escalation Techniques for Early Childhood Professionals"
        self.assertEqual(sc.known_course_names(n, COURSES, "2026-10-10"), [n])
        self.assertEqual(sc.known_course_names(n, COURSES), [n])

    def test_scheduler_entry_cannot_drop_the_date_guard(self):
        class R:
            def raise_for_status(s): pass
            def json(s): return {"courses": {"Safety Summit": {"format": "in-person", "hours": "8", "title_lines": ["Safety Summit"]}}}
        courses, _ = cc.load_courses(url="http://x", get=lambda u, timeout: R(), path=os.path.join(ROOT, "courses.json"))
        self.assertEqual(courses["Safety Summit"]["event_dates"], SS["event_dates"])
        self.assertFalse(sc.course_enabled_for_date(courses["Safety Summit"], "2026-10-10"))

    def test_hours_resolve_from_topic_only_segment(self):
        sc._scheduler_sessions_cache = [{"id": 43, "date": "2026-10-13", "start_time": "09:00", "end_time": "17:00",
            "segments": [{"course_name": None, "topic_override": "Safety Summit", "start_time": "13:00", "end_time": "17:00"}]}]
        try:
            self.assertEqual(sc.resolve_course_hours("Safety Summit", "2026-10-13", "9"), "4")
        finally:
            sc._scheduler_sessions_cache = None

    def test_render_with_topics_produces_pdf(self):
        pdf = sc.render_cert("Maria Del Carmen Fernandez-Rodriguez", {**SS, "topics": sc.event_topics(SS, "2026-10-13")},
                             "October 13, 2026", "in-person")
        self.assertTrue(pdf.startswith(b"%PDF"))


if __name__ == "__main__":
    unittest.main()
