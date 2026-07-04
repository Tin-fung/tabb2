import unittest

from fastapi import HTTPException

import routes.openai_compat as openai_compat


class FakeConfig:
    def __init__(self, api_key=""):
        self.api_key = api_key

    def get(self, *keys, default=None):
        if keys == ("proxy", "api_key"):
            return self.api_key
        return default


class FakeTokenManager:
    has_tokens = True

    def __init__(self):
        self.get_next_called = False

    async def get_next(self):
        self.get_next_called = True
        return {"id": "token-1", "name": "primary"}, object()


def function_tool(name):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": f"{name} description",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    }


class OpenAICompatAuthTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.old_cfg = openai_compat._cfg
        self.old_tm = openai_compat._tm

    async def asyncTearDown(self):
        openai_compat._cfg = self.old_cfg
        openai_compat._tm = self.old_tm

    async def test_token_pool_requires_proxy_api_key(self):
        tm = FakeTokenManager()
        openai_compat._tm = tm
        openai_compat._cfg = FakeConfig(api_key="")

        with self.assertRaises(HTTPException) as ctx:
            await openai_compat._get_client_and_token(None)

        self.assertEqual(ctx.exception.status_code, 401)
        self.assertIn("api key", ctx.exception.detail.lower())
        self.assertFalse(tm.get_next_called)


class OpenAICompatToolChoiceTest(unittest.TestCase):
    def test_tool_choice_none_disables_tools(self):
        tools = [function_tool("search"), function_tool("write_file")]

        selected = openai_compat._select_openai_tools(tools, "none")

        self.assertEqual(selected, [])

    def test_tool_choice_function_limits_exposed_tool(self):
        tools = [function_tool("search"), function_tool("write_file")]

        selected = openai_compat._select_openai_tools(
            tools,
            {"type": "function", "function": {"name": "search"}},
        )

        self.assertEqual([tool["name"] for tool in selected], ["search"])
        self.assertEqual(selected[0]["input_schema"]["required"], ["query"])

    def test_tool_choice_unknown_function_rejects_request(self):
        tools = [function_tool("search")]

        with self.assertRaises(HTTPException) as ctx:
            openai_compat._select_openai_tools(
                tools,
                {"type": "function", "function": {"name": "missing"}},
            )

        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("unknown tool", ctx.exception.detail.lower())

    def test_required_tools_reject_uncertified_model(self):
        tools = [function_tool("search")]
        normalized = openai_compat._select_openai_tools(tools, "required")

        with self.assertRaises(HTTPException) as ctx:
            openai_compat._apply_openai_tool_policy(
                "GPT-5.5",
                normalized,
                "required",
            )

        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("not certified", ctx.exception.detail)

    def test_optional_tools_degrade_for_uncertified_model(self):
        tools = [function_tool("search")]
        normalized = openai_compat._select_openai_tools(tools, "auto")

        selected = openai_compat._apply_openai_tool_policy(
            "GPT-5.5",
            normalized,
            "auto",
        )

        self.assertEqual(selected, [])

    def test_certified_model_keeps_tools_enabled(self):
        tools = [function_tool("search")]
        normalized = openai_compat._select_openai_tools(tools, "required")

        selected = openai_compat._apply_openai_tool_policy(
            "DeepSeek-V4-Pro",
            normalized,
            "required",
        )

        self.assertEqual(selected, normalized)
