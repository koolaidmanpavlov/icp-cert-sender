"""python3 -m unittest tests.test_course_config   (no network)"""
import json, os, tempfile, unittest
import course_config as cc

FILE = {"Old Course": {"format": "in-person", "hours": "2", "title_lines": ["Old", "Course"]},
        "Shared": {"format": "in-person", "hours": "2", "title_lines": ["Shared"]}}
class R:
    def __init__(s, body, status=200): s.body, s.status = body, status
    def raise_for_status(s):
        if s.status >= 400: raise RuntimeError(f"HTTP {s.status}")
    def json(s): return s.body

def run(get):
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f: json.dump(FILE, f)
    try: return cc.load_courses(url="http://x", get=get, path=f.name)
    finally: os.unlink(f.name)

class T(unittest.TestCase):
    def test_new_course_in_scheduler_is_certifiable(self):
        new = {"format": "webinar", "hours": "1", "title_lines": ["New", "Course"]}
        courses, info = run(lambda u, timeout: R({"courses": {"New Course": new}, "uncertified": []}))
        self.assertEqual(courses["New Course"], new); self.assertIn("New Course", info["from_scheduler"])
    def test_scheduler_wins_and_file_only_courses_kept(self):
        upd = {"format": "webinar", "hours": "3", "title_lines": ["Shared v2"]}
        courses, info = run(lambda u, timeout: R({"courses": {"Shared": upd}}))
        self.assertEqual(courses["Shared"], upd); self.assertIn("Old Course", courses); self.assertIn("Old Course", info["only_in_file"])
    def test_scheduler_down_falls_back_to_file(self):
        def boom(u, timeout): raise ConnectionError("down")
        courses, info = run(boom)
        self.assertEqual(courses, FILE); self.assertFalse(info["api_ok"])
    def test_http_error_and_bad_shape_fall_back(self):
        self.assertEqual(run(lambda u, timeout: R({}, 500))[0], FILE)
        self.assertEqual(run(lambda u, timeout: R({"nope": 1}))[0], FILE)
    def test_invalid_entry_ignored_not_trusted(self):
        bad = {"format": "in-person", "hours": "abc", "title_lines": []}
        courses, _ = run(lambda u, timeout: R({"courses": {"Bad": bad}}))
        self.assertNotIn("Bad", courses)
    def test_uncertified_reported(self):
        _, info = run(lambda u, timeout: R({"courses": {}, "uncertified": ["No Cert"]}))
        self.assertEqual(info["uncertified"], ["No Cert"])
    def test_known_course_names_sees_new_course(self):
        import send_certs  # needs deps installed; skip if not
        courses, _ = run(lambda u, timeout: R({"courses": {"New Course": {"format": "webinar", "hours": "1", "title_lines": ["N"]}}}))
        self.assertEqual(send_certs.known_course_names("New Course + Old Course", courses), ["New Course", "Old Course"])
        self.assertIsNone(send_certs.known_course_names("Unregistered", courses))
if __name__ == "__main__": unittest.main()
