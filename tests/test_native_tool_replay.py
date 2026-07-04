import unittest

from scripts.verify_native_tool_replay import (
    SAMPLE_NATIVE_TOOL_EVENTS,
    replay_native_tool_events,
    validate_native_tool_summary,
)


class NativeToolReplayVerifierTest(unittest.TestCase):
    def test_sample_native_tool_replay_records_parallel_web_search(self):
        summary = replay_native_tool_events(SAMPLE_NATIVE_TOOL_EVENTS)

        self.assertEqual(summary["native_tools_count"], 1)
        self.assertEqual(summary["native_tool_names"], ["parallel_web_search"])
        self.assertEqual(summary["native_tools_status"], ["success"])
        self.assertGreater(summary["native_tools_result_chars"], 0)

    def test_validator_rejects_missing_native_tool(self):
        with self.assertRaises(AssertionError) as ctx:
            validate_native_tool_summary(
                {
                    "native_tools_count": 0,
                    "native_tool_names": [],
                    "native_tools_status": [],
                    "native_tools_duration_ms": 0,
                    "native_tools_result_chars": 0,
                }
            )

        self.assertIn("parallel_web_search", str(ctx.exception))
