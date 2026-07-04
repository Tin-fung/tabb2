from pathlib import Path
import re
import unittest


STATIC_INDEX = Path(__file__).resolve().parents[1] / "static" / "index.html"


class AdminDashboardXssTest(unittest.TestCase):
    def test_recent_logs_escape_untrusted_text_fields(self):
        html = STATIC_INDEX.read_text(encoding="utf-8")
        match = re.search(
            r"s\.recent_logs\.map\(l => `(?P<row>.*?)`\)\.join\(''\)",
            html,
            re.DOTALL,
        )

        self.assertIsNotNone(match, "Dashboard recent logs row template not found")
        row_template = match.group("row")

        self.assertIn("${esc(l.model)}", row_template)
        self.assertIn("${esc(l.token_name)}", row_template)
        self.assertNotIn("${l.model}", row_template)
        self.assertNotIn("${l.token_name}", row_template)

    def test_status_badge_escapes_display_text(self):
        html = STATIC_INDEX.read_text(encoding="utf-8")
        match = re.search(
            r"function statusBadge\(s\) \{(?P<body>.*?)\n\}",
            html,
            re.DOTALL,
        )

        self.assertIsNotNone(match, "statusBadge helper not found")
        body = match.group("body")

        self.assertIn("${esc(s)}", body)
        self.assertNotIn(">${s}</span>", body)
