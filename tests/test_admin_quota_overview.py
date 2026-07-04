import unittest

from routes.admin_api import build_quota_overview


class FakeClient:
    def __init__(self, name, fail=None):
        self.name = name
        self.fail = set(fail or [])

    async def get_quota_usage(self):
        if "quota" in self.fail:
            raise RuntimeError("quota failed")
        return {"usage_percentage": "12.5%", "member_level": "pro"}

    async def get_coupon_list(self, coupon_type="weekly_reset_coupon", status=1):
        if "coupons" in self.fail:
            raise RuntimeError("coupons failed")
        return {"coupons": [{"coupon_code": f"{self.name}-coupon"}]}

    async def get_sign_in_status(self):
        if "sign_in" in self.fail:
            raise RuntimeError("sign-in failed")
        return {"results": [{"scene_code": "daily_sign_in", "signed_today": False}]}

    async def get_usage_records(self, page=1, limit=50):
        if "records" in self.fail:
            raise RuntimeError("records failed")
        return {"records": [{"request_time": "2026-07-04", "consume_percentage": "-1%"}]}


class AdminQuotaOverviewTest(unittest.IsolatedAsyncioTestCase):
    async def test_overview_includes_enabled_tokens_only_and_omits_values(self):
        tokens = [
            {"id": "t1", "name": "Account 1", "value": "secret-1", "enabled": True},
            {"id": "t2", "name": "Disabled", "value": "secret-2", "enabled": False},
        ]

        async def client_for_token(token_id):
            return tokens[0], FakeClient(token_id)

        result = await build_quota_overview(tokens, client_for_token)

        self.assertTrue(result["ok"])
        self.assertEqual([a["token_id"] for a in result["accounts"]], ["t1"])
        self.assertNotIn("value", result["accounts"][0])
        self.assertEqual(result["accounts"][0]["quota"]["data"]["member_level"], "pro")
        self.assertEqual(result["usage_records"][0]["token_name"], "Account 1")

    async def test_subsection_failure_does_not_fail_whole_overview(self):
        tokens = [{"id": "t1", "name": "Account 1", "enabled": True}]

        async def client_for_token(token_id):
            return tokens[0], FakeClient(token_id, fail={"coupons", "records"})

        result = await build_quota_overview(tokens, client_for_token)
        account = result["accounts"][0]

        self.assertTrue(result["ok"])
        self.assertTrue(account["quota"]["ok"])
        self.assertFalse(account["coupons"]["ok"])
        self.assertIn("coupons failed", account["coupons"]["error"])
        self.assertFalse(result["usage_records"][0]["ok"])
