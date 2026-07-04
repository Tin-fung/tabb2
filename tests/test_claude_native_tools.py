import unittest

import routes.claude_api as claude_api


class FakeClient:
    async def create_chat_session(self):
        return "session-1"

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


class FakeConfig:
    def get(self, *keys, default=None):
        if keys == ("proxy", "api_key"):
            return "sk-proxy"
        if keys == ("claude", "default_model"):
            return None
        if keys == ("claude", "system_prompt"):
            return ""
        return default


class FakeTokenManager:
    has_tokens = True

    def __init__(self):
        self.successes = []
        self.errors = []

    async def get_next(self):
        return {"id": "token-1", "name": "primary"}, FakeClient()

    def report_success(self, token_id):
        self.successes.append(token_id)

    def report_error(self, token_id):
        self.errors.append(token_id)


class FakeRequest:
    def __init__(self, body, headers=None):
        self._body = body
        self.headers = headers or {}

    async def json(self):
        return dict(self._body)


class ClaudeNativeToolsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.old_tm = claude_api._tm
        self.old_cfg = claude_api._cfg
        self.old_logs = claude_api._logs
        claude_api._tm = None
        claude_api._cfg = FakeConfig()
        claude_api._logs = FakeLogs()

    async def asyncTearDown(self):
        claude_api._tm = self.old_tm
        claude_api._cfg = self.old_cfg
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

    async def test_non_stream_native_tool_is_logged_but_not_emitted_as_tool_use(self):
        tm = FakeTokenManager()
        claude_api._tm = tm

        response = await claude_api.claude_messages(
            FakeRequest(
                {
                    "model": "claude-proxy",
                    "stream": False,
                    "messages": [{"role": "user", "content": "search"}],
                    "max_tokens": 1024,
                },
                headers={"x-api-key": "sk-proxy"},
            )
        )
        log_data = claude_api._logs.entries[0].to_dict()

        self.assertEqual(response["content"], [{"type": "text", "text": "Here is the answer."}])
        self.assertNotIn("tool_use", str(response["content"]))
        self.assertEqual(log_data["native_tools_count"], 1)
        self.assertEqual(log_data["native_tool_names"], ["parallel_web_search"])
        self.assertEqual(tm.successes, ["token-1"])
