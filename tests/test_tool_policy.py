import unittest

from core.tool_policy import ToolMode, decide_tool_mode


class ToolPolicyTest(unittest.TestCase):
    def test_certified_model_can_use_dual_mode(self):
        decision = decide_tool_mode("DeepSeek-V4-Pro", has_tools=True, required=True)

        self.assertEqual(decision.mode, ToolMode.DUAL)
        self.assertTrue(decision.local_tools_enabled)
        self.assertFalse(decision.reject)

    def test_uncertified_required_tools_are_rejected(self):
        decision = decide_tool_mode("GPT-5.5", has_tools=True, required=True)

        self.assertTrue(decision.reject)
        self.assertEqual(decision.reject_status, 400)
        self.assertIn("not certified", decision.reject_detail)
        self.assertFalse(decision.local_tools_enabled)

    def test_uncertified_optional_tools_degrade_to_native_enhanced(self):
        decision = decide_tool_mode("GPT-5.5", has_tools=True, required=False)

        self.assertFalse(decision.reject)
        self.assertEqual(decision.mode, ToolMode.NATIVE_ENHANCED)
        self.assertFalse(decision.local_tools_enabled)

    def test_plain_chat_uses_native_enhanced_observation(self):
        decision = decide_tool_mode("GPT-5.5", has_tools=False)

        self.assertFalse(decision.reject)
        self.assertEqual(decision.mode, ToolMode.NATIVE_ENHANCED)
        self.assertFalse(decision.local_tools_enabled)
