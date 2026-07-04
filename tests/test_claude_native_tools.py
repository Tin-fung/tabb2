import unittest

import routes.claude_api as claude_api


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


class ClaudeNativeToolsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.old_tm = claude_api._tm
        self.old_logs = claude_api._logs
        claude_api._tm = None
        claude_api._logs = FakeLogs()

    async def asyncTearDown(self):
        claude_api._tm = self.old_tm
        claude_api._logs = self.old_logs

    async def test_native_tool_is_logged_but_not_emitted_as_tool_use(self):
        body = {"model": "claude-proxy", "messages": [{"role": "user", "content": "search"}]}
        chunks = [
            line
            async for line in claude_api._stream_claude_response(
                FakeClient(),
                "session-1",
                "search",
                "DeepSeek-V4-Pro",
                body,
                "primary",
                "",
            )
        ]
        stream = "".join(chunks)
        log_data = claude_api._logs.entries[0].to_dict()

        self.assertNotIn('"type": "tool_use"', stream)
        self.assertIn("Here is the answer.", stream)
        self.assertEqual(log_data["native_tools_count"], 1)
        self.assertEqual(log_data["native_tool_names"], ["parallel_web_search"])
