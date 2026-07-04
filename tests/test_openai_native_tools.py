import unittest

import routes.openai_compat as openai_compat


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


class FakeLocalToolClient:
    async def create_chat_session(self):
        return "session-1"

    async def send_message(self, *args, **kwargs):
        yield {
            "event": "message_tool_calls",
            "data": {
                "tool_calls": [
                    {
                        "id": "call_local",
                        "type": "function",
                        "function": {
                            "name": "cc_search",
                            "arguments": '{"query":"today tech news"}',
                        },
                    }
                ]
            },
        }
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
        if keys == ("proxy", "system_prompt"):
            return ""
        if keys == ("claude", "default_model"):
            return None
        return default


class FakeTokenManager:
    has_tokens = True

    def __init__(self, client=None):
        self.client = client or FakeClient()
        self.successes = []
        self.errors = []

    async def get_next(self):
        return {"id": "token-1", "name": "primary"}, self.client

    def report_success(self, token_id):
        self.successes.append(token_id)

    def report_error(self, token_id):
        self.errors.append(token_id)


class FakeRegistry:
    ready = True

    def has_alias(self, model):
        return True

    def resolve(self, model):
        return model


class OpenAINativeToolsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.old_tm = openai_compat._tm
        self.old_cfg = openai_compat._cfg
        self.old_logs = openai_compat._logs
        self.old_get_registry = openai_compat.get_registry
        openai_compat._tm = None
        openai_compat._cfg = FakeConfig()
        openai_compat._logs = FakeLogs()

    async def asyncTearDown(self):
        openai_compat._tm = self.old_tm
        openai_compat._cfg = self.old_cfg
        openai_compat._logs = self.old_logs
        openai_compat.get_registry = self.old_get_registry

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

    async def test_non_stream_native_tool_is_logged_but_not_emitted_as_openai_tool_call(self):
        tm = FakeTokenManager()
        openai_compat._tm = tm
        req = openai_compat.ChatCompletionRequest(
            model="Default",
            stream=False,
            messages=[openai_compat.ChatMessage(role="user", content="search")],
        )

        response = await openai_compat.chat_completions(
            req,
            authorization="Bearer sk-proxy",
        )
        message = response["choices"][0]["message"]
        log_data = openai_compat._logs.entries[0].to_dict()

        self.assertNotIn("tool_calls", message)
        self.assertEqual(message["content"], "Here is the answer.")
        self.assertEqual(log_data["native_tools_count"], 1)
        self.assertEqual(log_data["native_tool_names"], ["parallel_web_search"])
        self.assertEqual(tm.successes, ["token-1"])

    async def test_non_stream_local_alias_tool_still_emits_openai_tool_call(self):
        openai_compat.get_registry = lambda: FakeRegistry()
        openai_compat._tm = FakeTokenManager(client=FakeLocalToolClient())
        req = openai_compat.ChatCompletionRequest(
            model="DeepSeek-V4-Pro",
            stream=False,
            messages=[openai_compat.ChatMessage(role="user", content="search")],
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "search",
                        "description": "search the web",
                        "parameters": {
                            "type": "object",
                            "properties": {"query": {"type": "string"}},
                            "required": ["query"],
                        },
                    },
                }
            ],
            tool_choice="required",
        )

        response = await openai_compat.chat_completions(
            req,
            authorization="Bearer sk-proxy",
        )
        message = response["choices"][0]["message"]
        log_data = openai_compat._logs.entries[0].to_dict()

        self.assertEqual(message["tool_calls"][0]["function"]["name"], "search")
        self.assertEqual(response["choices"][0]["finish_reason"], "tool_calls")
        self.assertEqual(log_data["native_tools_count"], 0)
