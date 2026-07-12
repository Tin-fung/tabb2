import asyncio
import json
import re
import unittest
from unittest.mock import AsyncMock, patch

from core.responses_bridge import (
    BridgeFunctionCall,
    BridgeTurn,
    ResponsesBridge,
)
from core.tabbit_agent import AgentEvent, AgentTaskBootstrap
from routes import chat_agent_bridge, openai_compat


class FakeHTTPClient:
    async def aclose(self):
        return None


class FakeTabbitClient:
    def __init__(self):
        self.client = FakeHTTPClient()

    async def create_chat_session(self):
        return "session-1"


class RelayAgent:
    def __init__(self, bridge):
        self.bridge = bridge
        self.request = None

    async def bootstrap_task(self, request):
        self.request = request
        return AgentTaskBootstrap(
            session_id=request.session_id,
            task_id="task-1",
            request_message_id="request-1",
            assistant_message_id="assistant-1",
            refine_query="",
            refine_audit_pass=True,
            needs_agent=True,
        )

    async def run_task(self, bootstrap):
        match = re.search(r"bridge_id: (bridge_[0-9a-f]+)", self.request.content)
        if not match:
            raise RuntimeError("bridge id missing from prompt")
        result = await self.bridge.relay_call(
            bridge_id=match.group(1),
            name="echo",
            arguments={"message": "TOOL_READY"},
        )
        yield AgentEvent(
            type="task_completed",
            data={"content": f"agent received:{result}"},
        )


class FakeConfig:
    def get(self, *keys, default=None):
        if keys == ("claude", "default_model"):
            return "best"
        return default


class FakeTokenManager:
    def __init__(self):
        self.successes = []
        self.errors = []

    def report_success(self, token_id):
        self.successes.append(token_id)

    def report_error(self, token_id, error):
        self.errors.append((token_id, str(error)))


class FakeLogs:
    def __init__(self):
        self.entries = []

    def add(self, entry):
        self.entries.append(entry)


class ChatAgentBridgeTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        holder = {}

        def factory(client):
            return RelayAgent(holder["bridge"])

        self.bridge = ResponsesBridge(agent_factory=factory, relay_timeout_seconds=5)
        holder["bridge"] = self.bridge
        self.tm = FakeTokenManager()
        self.logs = FakeLogs()
        chat_agent_bridge.init(self.bridge, self.tm, FakeConfig(), self.logs)
        self.client = FakeTabbitClient()

    async def asyncTearDown(self):
        await self.bridge.close_all()
        chat_agent_bridge._bridge = None
        chat_agent_bridge._cfg = None
        chat_agent_bridge._tm = None
        chat_agent_bridge._logs = None

    async def test_non_stream_chat_tool_call_and_tool_result_resume_same_agent(self):
        first_request = openai_compat.ChatCompletionRequest(
            model="best",
            messages=[openai_compat.ChatMessage(role="user", content="use echo")],
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "echo",
                        "description": "echo text",
                        "parameters": {
                            "type": "object",
                            "properties": {"message": {"type": "string"}},
                            "required": ["message"],
                        },
                    },
                }
            ],
        )
        provider = AsyncMock(return_value=(self.client, "account", "token-1"))
        with (
            patch("routes.openai_compat.get_client_and_token", provider),
            patch("routes.chat_agent_bridge.resolve_model", return_value="Default"),
        ):
            first = await openai_compat.chat_completions(
                first_request,
                authorization="Bearer proxy-key",
            )

        choice = first["choices"][0]
        self.assertEqual(choice["finish_reason"], "tool_calls")
        tool_call = choice["message"]["tool_calls"][0]
        self.assertEqual(tool_call["function"]["name"], "echo")
        self.assertEqual(
            json.loads(tool_call["function"]["arguments"]),
            {"message": "TOOL_READY"},
        )

        second_request = openai_compat.ChatCompletionRequest(
            model="best",
            messages=[
                openai_compat.ChatMessage(role="user", content="use echo"),
                openai_compat.ChatMessage(
                    role="assistant",
                    content=None,
                    tool_calls=[tool_call],
                ),
                openai_compat.ChatMessage(
                    role="tool",
                    content="LOCAL_RESULT",
                    tool_call_id=tool_call["id"],
                ),
            ],
        )
        second = await openai_compat.chat_completions(
            second_request,
            authorization="Bearer proxy-key",
        )

        final_choice = second["choices"][0]
        self.assertEqual(final_choice["finish_reason"], "stop")
        self.assertEqual(
            final_choice["message"]["content"],
            "agent received:LOCAL_RESULT",
        )
        self.assertEqual(provider.await_count, 1)
        self.assertEqual(self.tm.successes, ["token-1"])

    def test_stream_frames_emit_standard_tool_call_delta_and_finish_reason(self):
        frames = chat_agent_bridge.chat_turn_frames(
            "chatcmpl-1",
            "best",
            BridgeTurn(
                kind="function_call",
                function_calls=(
                    BridgeFunctionCall(
                        call_id="call-1",
                        name="shell",
                        arguments='{"cmd":"pwd"}',
                    ),
                ),
            ),
        )

        tool_payload = json.loads(frames[0][6:])
        finish_payload = json.loads(frames[1][6:])
        delta = tool_payload["choices"][0]["delta"]["tool_calls"][0]
        self.assertEqual(delta["id"], "call-1")
        self.assertEqual(delta["function"]["name"], "shell")
        self.assertEqual(
            finish_payload["choices"][0]["finish_reason"], "tool_calls"
        )
        self.assertEqual(frames[-1], "data: [DONE]\n\n")

    def test_old_tool_history_without_pending_call_does_not_force_bridge(self):
        request = openai_compat.ChatCompletionRequest(
            messages=[
                openai_compat.ChatMessage(
                    role="tool", content="old", tool_call_id="call-old"
                )
            ]
        )

        self.assertFalse(chat_agent_bridge.should_handle(request))


if __name__ == "__main__":
    unittest.main()
