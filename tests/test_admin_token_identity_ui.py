from pathlib import Path
import unittest


STATIC_INDEX = Path(__file__).resolve().parents[1] / "static" / "index.html"


class AdminTokenIdentityUiTest(unittest.TestCase):
    def test_tokens_table_uses_display_name_and_account_label(self):
        html = STATIC_INDEX.read_text(encoding="utf-8")

        self.assertIn("function tokenDisplayName(t)", html)
        self.assertIn("function tokenMetaLine(t)", html)
        self.assertIn("${esc(tokenDisplayName(t))}", html)
        self.assertIn("t.account_label", html)

    def test_google_login_uses_backend_suggested_name_when_manual_name_empty(self):
        html = STATIC_INDEX.read_text(encoding="utf-8")

        self.assertIn("data.suggested_name", html)
        self.assertIn("data.account_label", html)
        self.assertIn("识别账号", html)

    def test_google_login_keeps_manual_name_before_showing_loading_state(self):
        html = STATIC_INDEX.read_text(encoding="utf-8")
        function_start = html.index("async function exchangeGoogleToken")
        manual_name_index = html.index("const manualName", function_start)
        loading_index = html.index("step1.innerHTML = '<div", function_start)

        self.assertLess(manual_name_index, loading_index)

    def test_token_action_arguments_are_json_encoded(self):
        html = STATIC_INDEX.read_text(encoding="utf-8")

        self.assertIn("function jsArg(value)", html)
        self.assertIn("testToken(${jsArg(t.id)})", html)
        self.assertIn("deleteToken(${jsArg(t.id)}, ${jsArg(t.name)})", html)
        self.assertIn("saveGoogleToken(${jsArg(name)}, ${jsArg(data.token_value)})", html)
