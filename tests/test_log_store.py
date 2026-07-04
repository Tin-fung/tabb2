import unittest

from core.log_store import LogEntry


class LogEntryNativeToolsTest(unittest.TestCase):
    def test_to_dict_includes_native_tool_summary(self):
        entry = LogEntry(
            model="DeepSeek-V4-Pro",
            token_name="primary",
            stream=True,
            status="success",
            duration=1.25,
            native_tools={
                "native_tools_count": 1,
                "native_tool_names": ["parallel_web_search"],
                "native_tools_status": ["success"],
                "native_tools_duration_ms": 42,
                "native_tools_result_chars": 128,
            },
        )

        data = entry.to_dict()

        self.assertEqual(data["native_tools_count"], 1)
        self.assertEqual(data["native_tool_names"], ["parallel_web_search"])
        self.assertEqual(data["native_tools_status"], ["success"])
        self.assertEqual(data["native_tools_duration_ms"], 42)
        self.assertEqual(data["native_tools_result_chars"], 128)
