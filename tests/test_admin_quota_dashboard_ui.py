from pathlib import Path
import unittest


STATIC_INDEX = Path(__file__).resolve().parents[1] / "static" / "index.html"


class AdminQuotaDashboardUiTest(unittest.TestCase):
    def test_quota_page_uses_overview_dashboard_not_selector_tabs(self):
        html = STATIC_INDEX.read_text(encoding="utf-8")

        self.assertIn("api('/quota/overview')", html)
        self.assertIn('id="quotaAccountGrid"', html)
        self.assertIn('id="usageRecordsSection"', html)
        self.assertNotIn('id="tokenSelector"', html)
        self.assertNotIn('class="quota-tab', html)

    def test_quota_dashboard_has_per_account_actions(self):
        html = STATIC_INDEX.read_text(encoding="utf-8")

        self.assertIn("function signInToken(tokenId)", html)
        self.assertIn("function claimCouponForToken(tokenId)", html)
        self.assertIn("function useCouponForToken(tokenId, couponCode)", html)
        self.assertIn("token_id=${encodeURIComponent(tokenId)}", html)

    def test_quota_dashboard_escapes_untrusted_fields(self):
        html = STATIC_INDEX.read_text(encoding="utf-8")

        self.assertIn("${esc(account.token_name || account.token_id)}", html)
        self.assertIn("${esc(c.coupon_code || '')}", html)
        self.assertIn(
            "${esc(rec.scene_group_name_display || rec.scene_group_name || '-')}",
            html,
        )

    def test_quota_dashboard_uses_account_identity_labels(self):
        html = STATIC_INDEX.read_text(encoding="utf-8")

        self.assertIn("function quotaAccountDisplayName(account)", html)
        self.assertIn("${esc(quotaAccountDisplayName(account))}", html)
        self.assertIn("account.account_label", html)

    def test_usage_records_do_not_format_missing_time(self):
        html = STATIC_INDEX.read_text(encoding="utf-8")

        self.assertIn("const when = rec.request_time || rec.created_at;", html)
        self.assertIn("${esc(when ? fmtTime(when) : '-')}", html)
