import unittest

from core.tool_events import (
    NativeToolAggregator,
    ToolOrigin,
    classify_tool_origin,
    parse_arguments,
)


class ToolEventClassificationTest(unittest.TestCase):
    def test_classifies_local_alias_tools(self):
        self.assertEqual(
            classify_tool_origin("cc_Write", {"cc_Write": "Write"}),
            ToolOrigin.LOCAL,
        )
        self.assertEqual(classify_tool_origin("cc_Bash"), ToolOrigin.LOCAL)

    def test_classifies_known_tabbit_native_tools(self):
        self.assertEqual(
            classify_tool_origin("parallel_web_search"),
            ToolOrigin.NATIVE,
        )
        self.assertEqual(
            classify_tool_origin("browser_task_tool"),
            ToolOrigin.NATIVE,
        )

    def test_unknown_tool_name_is_unknown(self):
        self.assertEqual(classify_tool_origin("mystery_tool"), ToolOrigin.UNKNOWN)

    def test_parse_arguments_accepts_json_string_and_dict(self):
        self.assertEqual(parse_arguments('{"query":"news"}'), {"query": "news"})
        self.assertEqual(parse_arguments({"query": "news"}), {"query": "news"})
        self.assertEqual(parse_arguments("{broken"), {})


class NativeToolAggregatorTest(unittest.TestCase):
    def test_aggregates_native_tool_lifecycle(self):
        agg = NativeToolAggregator()
        call_id = "call_search_1"

        agg.consume(
            "message_tool_calls",
            {
                "tool_calls": [
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": "parallel_web_search",
                            "arguments": '{"query":"today tech news"}',
                        },
                    }
                ]
            },
            local_name_map={"cc_Write": "Write"},
        )
        agg.consume(
            "tool_start",
            {
                "tool_call_id": call_id,
                "tool_call_name": "parallel_web_search",
            },
        )
        agg.consume(
            "tool_finish",
            {
                "tool_call_id": call_id,
                "tool_call_name": "parallel_web_search",
                "content": "result one\nresult two",
            },
        )

        fields = agg.to_log_fields()

        self.assertEqual(fields["native_tools_count"], 1)
        self.assertEqual(fields["native_tool_names"], ["parallel_web_search"])
        self.assertEqual(fields["native_tools_status"], ["success"])
        self.assertGreater(fields["native_tools_result_chars"], 0)

    def test_ignores_local_alias_tool_calls(self):
        agg = NativeToolAggregator()

        agg.consume(
            "message_tool_calls",
            {
                "tool_calls": [
                    {
                        "id": "call_write",
                        "type": "function",
                        "function": {
                            "name": "cc_Write",
                            "arguments": '{"file_path":"/tmp/a","content":"x"}',
                        },
                    }
                ]
            },
            local_name_map={"cc_Write": "Write"},
        )

        self.assertEqual(agg.to_log_fields()["native_tools_count"], 0)
