import json
import tempfile
import unittest
from pathlib import Path

from fastapi import HTTPException
from jose import jwt

from core.auth import verify_jwt
from core.config import ConfigManager, DEFAULT_CONFIG, hash_password
from tabbit2api import RequestBodyTooLarge, client_ip_for_request, limited_receive


class FakeConfig:
    def __init__(self, secret="secret"):
        self.secret = secret

    def get(self, *keys, default=None):
        if keys == ("admin", "jwt_secret"):
            return self.secret
        return default


class FakeClient:
    def __init__(self, host):
        self.host = host


class FakeRequest:
    def __init__(self, host, headers=None):
        self.client = FakeClient(host)
        self.headers = headers or {}


class SecurityHardeningTest(unittest.IsolatedAsyncioTestCase):
    def test_default_config_enables_tls_verification(self):
        self.assertIs(DEFAULT_CONFIG["tabbit"]["verify_ssl"], True)

    def test_existing_empty_jwt_secret_is_generated(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "admin": {
                            "password_hash": hash_password("admin"),
                            "salt": "",
                            "jwt_secret": "",
                        }
                    }
                ),
                encoding="utf-8",
            )

            cfg = ConfigManager(path)

            self.assertTrue(cfg.get("admin", "jwt_secret"))
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertTrue(saved["admin"]["jwt_secret"])

    def test_existing_empty_responses_relay_token_is_generated(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "admin": {
                            "password_hash": hash_password("admin"),
                            "salt": "",
                            "jwt_secret": "secret",
                        },
                        "responses": {"relay_token": ""},
                    }
                ),
                encoding="utf-8",
            )

            cfg = ConfigManager(path)

            token = cfg.get("responses", "relay_token")
            self.assertGreaterEqual(len(token), 32)
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["responses"]["relay_token"], token)

    def test_stale_tabbit_protocol_version_is_migrated(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "admin": {
                            "password_hash": hash_password("admin"),
                            "salt": "",
                            "jwt_secret": "secret",
                        },
                        "tabbit": {
                            "browser_version": "1.1.39",
                            "sparkle_version": 10101039,
                        },
                    }
                ),
                encoding="utf-8",
            )

            cfg = ConfigManager(path)

            self.assertEqual(cfg.get("tabbit", "browser_version"), "1.4.46")
            self.assertEqual(cfg.get("tabbit", "sparkle_version"), 10104046)

    def test_verify_jwt_rejects_non_admin_role(self):
        token = jwt.encode(
            {"role": "user", "exp": 4102444800},
            "secret",
            algorithm="HS256",
        )

        with self.assertRaises(HTTPException) as ctx:
            verify_jwt(token, FakeConfig("secret"))

        self.assertEqual(ctx.exception.status_code, 401)
        self.assertIn("admin", ctx.exception.detail.lower())

    def test_client_ip_ignores_forwarded_headers_without_trusted_proxy(self):
        request = FakeRequest(
            "203.0.113.10",
            headers={"x-forwarded-for": "198.51.100.99", "x-real-ip": "198.51.100.88"},
        )

        self.assertEqual(client_ip_for_request(request, []), "203.0.113.10")

    def test_client_ip_uses_forwarded_headers_from_trusted_proxy(self):
        request = FakeRequest(
            "10.0.0.5",
            headers={"x-forwarded-for": "198.51.100.99, 10.0.0.5"},
        )

        self.assertEqual(client_ip_for_request(request, ["10.0.0.0/24"]), "198.51.100.99")

    async def test_limited_receive_rejects_body_without_content_length(self):
        messages = iter(
            [
                {"type": "http.request", "body": b"12345", "more_body": True},
                {"type": "http.request", "body": b"67890", "more_body": False},
            ]
        )

        async def receive():
            return next(messages)

        guarded = limited_receive(receive, limit=8)
        await guarded()
        with self.assertRaises(RequestBodyTooLarge):
            await guarded()
