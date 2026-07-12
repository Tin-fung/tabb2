import unittest

from fastapi import HTTPException

import routes.openai_compat as openai_compat
from core.model_registry import ModelRegistry, normalize_model_alias
from core.tabbit_client import resolve_model


class FakeConfig:
    def __init__(self, api_key="", local_tools_enabled=False):
        self.api_key = api_key
        self.local_tools_enabled = local_tools_enabled

    def get(self, *keys, default=None):
        if keys == ("proxy", "api_key"):
            return self.api_key
        if keys == ("proxy", "local_tools_enabled"):
            return self.local_tools_enabled
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

    def test_required_local_tools_reject_uncertified_model(self):
        tools = [function_tool("Write")]
        normalized = openai_compat._select_openai_tools(tools, "required")

        with self.assertRaises(HTTPException) as ctx:
            openai_compat._apply_openai_tool_policy(
                "GPT-5.5",
                normalized,
                "required",
                local_fallback_enabled=True,
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

    def test_required_search_tool_degrades_to_native_enhanced(self):
        tools = [function_tool("search")]
        normalized = openai_compat._select_openai_tools(tools, "required")

        selected = openai_compat._apply_openai_tool_policy(
            "DeepSeek-V4-Pro",
            normalized,
            "required",
        )

        self.assertEqual(selected, [])

    def test_required_local_tool_rejects_when_fallback_disabled(self):
        tools = [function_tool("Write")]
        normalized = openai_compat._select_openai_tools(tools, "required")

        with self.assertRaises(HTTPException) as ctx:
            openai_compat._apply_openai_tool_policy(
                "DeepSeek-V4-Pro",
                normalized,
                "required",
            )

        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("local tool mode is disabled", ctx.exception.detail)

    def test_required_local_tool_allowed_when_header_enables_fallback(self):
        tools = [function_tool("Write")]
        normalized = openai_compat._select_openai_tools(tools, "required")

        selected = openai_compat._apply_openai_tool_policy(
            "DeepSeek-V4-Pro",
            normalized,
            "required",
            local_fallback_enabled=True,
        )

        self.assertEqual(selected, normalized)

    def test_local_tools_header_or_config_enables_fallback(self):
        old_cfg = openai_compat._cfg
        try:
            openai_compat._cfg = FakeConfig(local_tools_enabled=False)
            self.assertTrue(openai_compat._local_tools_enabled_from_config_or_header("true"))
            self.assertFalse(openai_compat._local_tools_enabled_from_config_or_header(None))

            openai_compat._cfg = FakeConfig(local_tools_enabled=True)
            self.assertTrue(openai_compat._local_tools_enabled_from_config_or_header(None))
        finally:
            openai_compat._cfg = old_cfg


class ModelAliasResolutionTest(unittest.TestCase):
    def build_registry(self):
        registry = ModelRegistry()
        registry._cache, registry._models_meta = registry._build_alias_map(
            [
                {
                    "name": "GPT-5.6 Sol",
                    "display_name": "GPT-5.6 Sol",
                    "supports_tools": True,
                },
                {
                    "name": "Default",
                    "display_name": "Default",
                    "supports_tools": True,
                },
            ]
        )
        return registry

    def test_alias_normalization_ignores_case_and_all_whitespace(self):
        self.assertEqual(normalize_model_alias(" GPT-5.6\t Sol \n"), "gpt-5.6sol")

    def test_display_name_from_models_endpoint_resolves_exactly(self):
        registry = self.build_registry()

        self.assertEqual(registry.resolve("GPT-5.6 Sol"), "GPT-5.6 Sol")
        self.assertEqual(registry.resolve("gpt-5.6sol"), "GPT-5.6 Sol")

    def test_stale_dynamic_cache_still_resolves_known_explicit_model(self):
        registry = self.build_registry()
        registry._expires_at = 0

        self.assertFalse(registry.ready)
        self.assertEqual(
            resolve_model("GPT-5.6 Sol", registry, "Default"),
            "GPT-5.6 Sol",
        )

    def test_unknown_model_still_falls_back_to_default(self):
        registry = self.build_registry()
        registry._expires_at = 0

        self.assertEqual(resolve_model("unknown-model", registry, None), "Default")
