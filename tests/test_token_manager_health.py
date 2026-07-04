import time
import unittest
import base64
import json
from datetime import datetime, timezone
from unittest.mock import patch

from core.token_manager import TokenManager, token_expiration_metadata


class FakeConfig:
    def __init__(self, tokens):
        self.tokens = tokens
        self.save_count = 0

    def get(self, *keys, default=None):
        if keys == ("tokens",):
            return self.tokens
        if keys == ("tabbit", "base_url"):
            return "https://tabbit.example"
        if keys == ("tabbit", "client_id"):
            return "client-id"
        if keys == ("tabbit", "browser_version"):
            return "1.1.39"
        if keys == ("tabbit", "sparkle_version"):
            return 10101039
        if keys == ("tabbit", "default_browser"):
            return True
        if keys == ("tabbit", "verify_ssl"):
            return False
        return default

    def save(self):
        self.save_count += 1


def iso_from_epoch(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def parse_iso(value):
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()


def jwt_with_payload(payload):
    def enc(data):
        raw = json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return f"{enc({'alg': 'none', 'typ': 'JWT'})}.{enc(payload)}.sig"


def token(token_id, **overrides):
    base = {
        "id": token_id,
        "name": token_id,
        "value": f"value-{token_id}",
        "enabled": True,
        "status": "unknown",
        "error_count": 0,
        "total_requests": 0,
    }
    base.update(overrides)
    return base


class TokenManagerHealthTest(unittest.IsolatedAsyncioTestCase):
    def test_token_expiration_metadata_reads_jwt_exp(self):
        exp = int(time.time()) + 3600
        value = f"{jwt_with_payload({'exp': exp, 'email': 'boss@example.com'})}|next|device"

        meta = token_expiration_metadata(value, now=exp - 60)

        self.assertEqual(meta["expires_at"], iso_from_epoch(exp))
        self.assertEqual(meta["expires_in_seconds"], 60)
        self.assertFalse(meta["expired"])

    def test_token_expiration_metadata_marks_expired_tokens(self):
        exp = int(time.time()) - 5
        value = jwt_with_payload({"exp": exp})

        meta = token_expiration_metadata(value, now=exp + 5)

        self.assertEqual(meta["expires_at"], iso_from_epoch(exp))
        self.assertEqual(meta["expires_in_seconds"], 0)
        self.assertTrue(meta["expired"])

    async def test_get_next_skips_persisted_cooldown_and_clears_expired_state(self):
        now = time.time()
        tokens = [
            token("cooling", status="cooldown", cooldown_until=iso_from_epoch(now + 120)),
            token("expired", status="cooldown", error_count=2, cooldown_until=iso_from_epoch(now - 1)),
        ]
        manager = TokenManager(FakeConfig(tokens))

        with patch("core.token_manager.TabbitClient", return_value=object()):
            picked, client = await manager.get_next()

        self.assertEqual(picked["id"], "expired")
        self.assertIsNotNone(client)
        self.assertEqual(tokens[1]["status"], "unknown")
        self.assertEqual(tokens[1]["error_count"], 0)
        self.assertIsNone(tokens[1].get("cooldown_until"))
        self.assertEqual(manager.config.save_count, 1)

    async def test_get_next_skips_tokens_that_need_login(self):
        tokens = [
            token("auth", status="needs_login", last_status_code=401),
            token("ready"),
        ]
        manager = TokenManager(FakeConfig(tokens))

        with patch("core.token_manager.TabbitClient", return_value=object()):
            picked, client = await manager.get_next()

        self.assertEqual(picked["id"], "ready")
        self.assertIsNotNone(client)

    def test_get_token_status_clears_expired_persisted_cooldown(self):
        tokens = [
            token(
                "expired",
                status="cooldown",
                error_count=2,
                cooldown_until=iso_from_epoch(time.time() - 1),
            )
        ]
        manager = TokenManager(FakeConfig(tokens))

        status = manager.get_token_status("expired")

        self.assertEqual(status, "unknown")
        self.assertEqual(tokens[0]["status"], "unknown")
        self.assertEqual(tokens[0]["error_count"], 0)
        self.assertIsNone(tokens[0].get("cooldown_until"))
        self.assertEqual(manager.config.save_count, 1)

    def test_report_error_persists_retry_after_cooldown_for_rate_limit(self):
        tokens = [token("a")]
        manager = TokenManager(FakeConfig(tokens))
        before = time.time()

        manager.report_error("a", Exception("rate limited"), status_code=429, retry_after=120)

        updated = tokens[0]
        cooldown_until = parse_iso(updated["cooldown_until"])
        self.assertEqual(updated["status"], "cooldown")
        self.assertEqual(updated["error_count"], 1)
        self.assertEqual(updated["total_requests"], 1)
        self.assertEqual(updated["last_status_code"], 429)
        self.assertIn("rate limited", updated["last_error"])
        self.assertGreaterEqual(cooldown_until, before + 119)
        self.assertLessEqual(cooldown_until, before + 121)

    def test_report_error_reads_status_and_retry_after_from_tabbit_api_error(self):
        from core.tabbit_client import TabbitAPIError

        tokens = [token("a")]
        manager = TokenManager(FakeConfig(tokens))
        before = time.time()
        error = TabbitAPIError(
            "Tabbit API error 429: quota exceeded",
            status_code=429,
            headers={"Retry-After": "90"},
        )

        manager.report_error("a", error)

        updated = tokens[0]
        cooldown_until = parse_iso(updated["cooldown_until"])
        self.assertEqual(updated["status"], "cooldown")
        self.assertEqual(updated["last_status_code"], 429)
        self.assertGreaterEqual(cooldown_until, before + 89)
        self.assertLessEqual(cooldown_until, before + 91)

    def test_report_error_marks_auth_failures_as_needing_login(self):
        from core.tabbit_client import TabbitAPIError

        tokens = [token("a")]
        manager = TokenManager(FakeConfig(tokens))
        error = TabbitAPIError("Tabbit API error 401: unauthorized", status_code=401)

        manager.report_error("a", error)

        updated = tokens[0]
        self.assertEqual(updated["status"], "needs_login")
        self.assertEqual(updated["last_status_code"], 401)
        self.assertIn("unauthorized", updated["last_error"])
        self.assertIsNone(updated.get("cooldown_until"))
        self.assertNotIn("a", manager._cooldowns)

    def test_report_error_uses_short_cooldown_for_transient_upstream_errors(self):
        tokens = [token("a")]
        manager = TokenManager(FakeConfig(tokens))
        before = time.time()

        manager.report_error("a", Exception("upstream unavailable"), status_code=500)

        updated = tokens[0]
        cooldown_until = parse_iso(updated["cooldown_until"])
        self.assertEqual(updated["status"], "cooldown")
        self.assertEqual(updated["last_status_code"], 500)
        self.assertGreaterEqual(cooldown_until, before + 59)
        self.assertLessEqual(cooldown_until, before + 61)

    def test_report_error_keeps_generic_errors_on_legacy_threshold(self):
        tokens = [token("a")]
        manager = TokenManager(FakeConfig(tokens))

        manager.report_error("a", Exception("generic failure"))
        manager.report_error("a", Exception("generic failure"))

        self.assertEqual(tokens[0]["status"], "error")
        self.assertEqual(tokens[0]["error_count"], 2)
        self.assertIsNone(tokens[0].get("cooldown_until"))

        manager.report_error("a", Exception("generic failure"))

        self.assertEqual(tokens[0]["status"], "cooldown")
        self.assertIsNotNone(tokens[0].get("cooldown_until"))

    def test_report_success_clears_error_and_cooldown_state(self):
        tokens = [
            token(
                "a",
                status="cooldown",
                error_count=2,
                cooldown_until=iso_from_epoch(time.time() + 120),
                last_error="boom",
                last_error_at=iso_from_epoch(time.time()),
                last_status_code=500,
            )
        ]
        manager = TokenManager(FakeConfig(tokens))

        manager.report_success("a")

        updated = tokens[0]
        self.assertEqual(updated["status"], "active")
        self.assertEqual(updated["error_count"], 0)
        self.assertIsNone(updated.get("cooldown_until"))
        self.assertIsNone(updated.get("last_error"))
        self.assertIsNone(updated.get("last_error_at"))
        self.assertIsNone(updated.get("last_status_code"))

    def test_update_token_cookies_persists_refreshed_auth_cookies(self):
        tokens = [token("a", value="old-jwt|old-next|device-1")]
        manager = TokenManager(FakeConfig(tokens))

        changed = manager.update_token_cookies(
            "a",
            {"token": "new-jwt", "next-auth.session-token": "new-next"},
        )

        self.assertTrue(changed)
        self.assertEqual(tokens[0]["value"], "new-jwt|new-next|device-1")
        self.assertEqual(manager.config.save_count, 1)

    def test_update_token_cookies_preserves_missing_parts(self):
        tokens = [token("a", value="old-jwt|old-next|device-1")]
        manager = TokenManager(FakeConfig(tokens))

        changed = manager.update_token_cookies("a", {"token": "new-jwt"})

        self.assertTrue(changed)
        self.assertEqual(tokens[0]["value"], "new-jwt|old-next|device-1")
        self.assertEqual(manager.config.save_count, 1)

    def test_update_token_cookies_ignores_empty_or_unchanged_cookie_values(self):
        tokens = [token("a", value="old-jwt|old-next|device-1")]
        manager = TokenManager(FakeConfig(tokens))

        changed = manager.update_token_cookies(
            "a",
            {"token": "", "next-auth.session-token": "old-next"},
        )

        self.assertFalse(changed)
        self.assertEqual(tokens[0]["value"], "old-jwt|old-next|device-1")
        self.assertEqual(manager.config.save_count, 0)

    async def test_refresh_token_from_client_exports_client_cookie_jar(self):
        from core.tabbit_client import TabbitClient

        tokens = [token("a", value="old-jwt|old-next|device-1")]
        manager = TokenManager(FakeConfig(tokens))
        client = TabbitClient("old-jwt|old-next|device-1", "https://web.tabbit.ai")
        try:
            client.client.cookies.set("token", "jar-jwt", domain="web.tabbit.ai")
            client.client.cookies.set(
                "next-auth.session-token",
                "jar-next",
                domain="web.tabbit.ai",
            )

            changed = manager.refresh_token_from_client("a", client)
        finally:
            await client.client.aclose()

        self.assertTrue(changed)
        self.assertEqual(tokens[0]["value"], "jar-jwt|jar-next|device-1")
