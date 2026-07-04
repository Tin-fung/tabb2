import unittest

from core.tool_policy import ToolKind, ToolMode, classify_client_tool, decide_tool_mode


def tool(name, description=""):
    return {
        "name": name,
        "description": description,
        "input_schema": {"type": "object"},
    }


class ToolPolicyTest(unittest.TestCase):
    def test_certified_model_can_use_dual_mode_when_fallback_enabled(self):
        tools = [tool("Write")]
        decision = decide_tool_mode(
            "DeepSeek-V4-Pro",
            has_tools=True,
            required=True,
            tools=tools,
            local_fallback_enabled=True,
        )

        self.assertEqual(decision.mode, ToolMode.DUAL)
        self.assertTrue(decision.local_tools_enabled)
        self.assertFalse(decision.reject)
        self.assertEqual(decision.selected_tools, tools)

    def test_uncertified_required_tools_are_rejected(self):
        decision = decide_tool_mode(
            "GPT-5.5",
            has_tools=True,
            required=True,
            tools=[tool("Write")],
            local_fallback_enabled=True,
        )

        self.assertTrue(decision.reject)
        self.assertEqual(decision.reject_status, 400)
        self.assertIn("not certified", decision.reject_detail)
        self.assertFalse(decision.local_tools_enabled)

    def test_uncertified_optional_tools_degrade_to_native_enhanced(self):
        decision = decide_tool_mode("GPT-5.5", has_tools=True, required=False, tools=[tool("Write")])

        self.assertFalse(decision.reject)
        self.assertEqual(decision.mode, ToolMode.NATIVE_ENHANCED)
        self.assertFalse(decision.local_tools_enabled)

    def test_plain_chat_uses_native_enhanced_observation(self):
        decision = decide_tool_mode("GPT-5.5", has_tools=False)

        self.assertFalse(decision.reject)
        self.assertEqual(decision.mode, ToolMode.NATIVE_ENHANCED)
        self.assertFalse(decision.local_tools_enabled)


class NativeFirstToolPolicyTest(unittest.TestCase):
    def test_search_tool_is_native_equivalent(self):
        self.assertEqual(classify_client_tool("search"), ToolKind.NATIVE_EQUIVALENT)
        self.assertEqual(classify_client_tool("web_search"), ToolKind.NATIVE_EQUIVALENT)
        self.assertEqual(
            classify_client_tool("fetch_url", "Fetch a URL"),
            ToolKind.NATIVE_EQUIVALENT,
        )

    def test_file_and_shell_tools_are_local_only(self):
        self.assertEqual(classify_client_tool("Write"), ToolKind.LOCAL_ONLY)
        self.assertEqual(classify_client_tool("Bash"), ToolKind.LOCAL_ONLY)

    def test_default_policy_filters_native_equivalent_tools(self):
        tools = [tool("search")]
        decision = decide_tool_mode(
            "DeepSeek-V4-Pro",
            has_tools=True,
            required=True,
            tools=tools,
        )

        self.assertEqual(decision.mode, ToolMode.NATIVE_ENHANCED)
        self.assertFalse(decision.local_tools_enabled)
        self.assertFalse(decision.reject)
        self.assertEqual(decision.selected_tools, [])
        self.assertEqual(decision.native_equivalent_tools, ["search"])

    def test_required_local_tool_rejects_when_fallback_disabled(self):
        tools = [tool("Write")]
        decision = decide_tool_mode(
            "DeepSeek-V4-Pro",
            has_tools=True,
            required=True,
            tools=tools,
        )

        self.assertTrue(decision.reject)
        self.assertEqual(decision.reject_status, 400)
        self.assertIn("local tool mode is disabled", decision.reject_detail)

    def test_local_tool_allowed_when_fallback_enabled_and_model_certified(self):
        tools = [tool("Write")]
        decision = decide_tool_mode(
            "DeepSeek-V4-Pro",
            has_tools=True,
            required=True,
            tools=tools,
            local_fallback_enabled=True,
        )

        self.assertEqual(decision.mode, ToolMode.DUAL)
        self.assertTrue(decision.local_tools_enabled)
        self.assertEqual(decision.selected_tools, tools)
