import unittest

import routes.openai_compat as openai_compat


class FakeClient:
    async def send_message(self, *args, **kwargs):
        yield {
            "event": "message_tool_calls",
            "data": {
                "tool_calls": [
                    {
                        "id": "call_native",
                        "type": "function",
                        "function": {
                            "name": "parallel_web_search",
                            "arguments": '{"query":"today tech news"}',
                        },
                    }
                ]
            },
        }
        yield {
            "event": "tool_start",
            "data": {
                "tool_call_id": "call_native",
                "tool_call_name": "parallel_web_search",
            },
        }
        yield {
            "event": "tool_finish",
            "data": {
                "tool_call_id": "call_native",
                "tool_call_name": "parallel_web_search",
                "content": "search result text",
            },
        }
        yield {"event": "message_chunk", "data": {"content": "Here is the answer."}}
        yield {"event": "finish", "data": {}}


class FakeLogs:
    def __init__(self):
        self.entries = []

    def add(self, entry):
        self.entries.append(entry)


class OpenAINativeToolsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.old_tm = openai_compat._tm
        self.old_logs = openai_compat._logs
        openai_compat._tm = None
        openai_compat._logs = FakeLogs()

    async def asyncTearDown(self):
        openai_compat._tm = self.old_tm
        openai_compat._logs = self.old_logs

    async def test_native_tool_is_logged_but_not_emitted_as_openai_tool_call(self):
        chunks = [
            line
            async for line in openai_compat._stream_handler(
                FakeClient(),
                "session-1",
                "search",
                "DeepSeek-V4-Pro",
                "gpt-proxy",
                "chatcmpl-test",
                "primary",
                "",
            )
        ]
        stream = "".join(chunks)
        log_data = openai_compat._logs.entries[0].to_dict()

        self.assertNotIn('"tool_calls"', stream)
        self.assertIn("Here is the answer.", stream)
        self.assertEqual(log_data["native_tools_count"], 1)
        self.assertEqual(log_data["native_tool_names"], ["parallel_web_search"])
