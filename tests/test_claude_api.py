import unittest

from fastapi import HTTPException

import routes.claude_api as claude_api


class FakeConfig:
    def __init__(self, api_key=""):
        self.api_key = api_key

    def get(self, *keys, default=None):
        if keys == ("proxy", "api_key"):
            return self.api_key
        return default


class FakeRequest:
    def __init__(self, headers=None):
        self.headers = headers or {}


class FakeTokenManager:
    has_tokens = True

    def __init__(self):
        self.get_next_called = False

    async def get_next(self):
        self.get_next_called = True
        return {"id": "token-1", "name": "primary"}, object()


class ClaudeApiAuthTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.old_cfg = claude_api._cfg
        self.old_tm = claude_api._tm

    async def asyncTearDown(self):
        claude_api._cfg = self.old_cfg
        claude_api._tm = self.old_tm

    async def test_token_pool_requires_proxy_api_key(self):
        tm = FakeTokenManager()
        claude_api._tm = tm
        claude_api._cfg = FakeConfig(api_key="")

        with self.assertRaises(HTTPException) as ctx:
            await claude_api._get_client_and_token(FakeRequest())

        self.assertEqual(ctx.exception.status_code, 401)
        self.assertIn("api key", ctx.exception.detail.lower())
        self.assertFalse(tm.get_next_called)

    async def test_token_pool_accepts_configured_x_api_key(self):
        tm = FakeTokenManager()
        claude_api._tm = tm
        claude_api._cfg = FakeConfig(api_key="proxy-secret")

        client, token_name, token_id = await claude_api._get_client_and_token(
            FakeRequest({"x-api-key": "proxy-secret"})
        )

        self.assertIsNotNone(client)
        self.assertEqual(token_name, "primary")
        self.assertEqual(token_id, "token-1")
        self.assertTrue(tm.get_next_called)


class ClaudeApiToolPolicyTest(unittest.TestCase):
    def test_tools_reject_uncertified_model(self):
        body = {
            "tools": [
                {
                    "name": "Write",
                    "description": "Write a file",
                    "input_schema": {"type": "object", "properties": {}},
                }
            ]
        }

        with self.assertRaises(HTTPException) as ctx:
            claude_api._apply_claude_tool_policy("GPT-5.5", body)

        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("not certified", ctx.exception.detail)

    def test_certified_model_keeps_tools_enabled(self):
        tools = [
            {
                "name": "Write",
                "description": "Write a file",
                "input_schema": {"type": "object", "properties": {}},
            }
        ]
        body = {"tools": tools}

        selected = claude_api._apply_claude_tool_policy("DeepSeek-V4-Pro", body)

        self.assertEqual(selected, tools)
        self.assertEqual(body["tools"], tools)
