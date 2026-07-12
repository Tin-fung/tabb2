import asyncio
import hashlib
import hmac
import json
import unittest

from core.tabbit_agent import (
    AgentTaskBootstrap,
    AgentTaskRequest,
    AgentTransportDependencies,
    TabbitAgentClient,
)


class FakeStreamResponse:
    def __init__(self, lines, status_code=200, headers=None):
        self._lines = lines
        self.status_code = status_code
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def aread(self):
        return b"upstream failure"


class FakeHTTPClient:
    def __init__(self, stream_response):
        self.stream_response = stream_response
        self.stream_calls = []
        self.get_calls = []

    async def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        return type("Response", (), {"status_code": 200, "text": "test-sign-key"})()

    def stream(self, method, url, **kwargs):
        self.stream_calls.append((method, url, kwargs))
        return self.stream_response


class FakeTabbitClient:
    def __init__(self, stream_response):
        self.base_url = "https://web.tabbit.ai"
        self.client = FakeHTTPClient(stream_response)

    def _get_headers(self, referer_path="/newtab", with_uuid=False):
        headers = {"x-req-ctx": "ctx", "referer": self.base_url + referer_path}
        if with_uuid:
            headers["unique-uuid"] = "unique"
        return headers

    def _get_cookies(self):
        return {"token": "secret", "user_id": "user"}

    def _sync_server_time(self, response):
        return None


class FakeWebSocket:
    def __init__(self, messages):
        self.messages = list(messages)
        self.sent = []

    async def send(self, message):
        self.sent.append(json.loads(message))

    async def recv(self):
        if not self.messages:
            await asyncio.sleep(3600)
        return json.dumps(self.messages.pop(0))


class FakeWebSocketContext:
    def __init__(self, websocket):
        self.websocket = websocket

    async def __aenter__(self):
        return self.websocket

    async def __aexit__(self, exc_type, exc, tb):
        return False


class TabbitAgentClientTest(unittest.IsolatedAsyncioTestCase):
    def test_build_signed_headers_matches_official_hmac_contract(self):
        client = TabbitAgentClient(
            FakeTabbitClient(FakeStreamResponse([])),
            dependencies=AgentTransportDependencies(
                clock_ms=lambda: 1_725_000_000_123,
                nonce_factory=lambda: "11111111-2222-4333-8444-555555555555",
            ),
        )
        body = '{"agent_mode":true}'

        headers = client.build_signed_headers(body, "test-sign-key", "/session/s1")

        digest = hashlib.sha256(body.encode()).hexdigest()
        canonical = (
            "1725000000123.11111111-2222-4333-8444-555555555555." + digest
        )
        expected = hmac.new(
            b"test-sign-key", canonical.encode(), hashlib.sha256
        ).hexdigest()
        self.assertEqual(headers["x-timestamp"], "1725000000123")
        self.assertEqual(
            headers["x-signature"], "11111111-2222-4333-8444-555555555555"
        )
        self.assertEqual(headers["x-nonce"], expected)
        self.assertEqual(headers["unique-uuid"], "unique")
        self.assertNotIn("token", json.dumps(headers).lower())

    async def test_bootstrap_task_parses_browser_use_start(self):
        payload = {
            "chat_session_id": "session-1",
            "request_message_id": "request-1",
            "assistant_message_id": "assistant-1",
            "task_id": "task-1",
            "refine_query": "I will do that",
            "refine_audit_pass": True,
            "needs_agent": True,
        }
        response = FakeStreamResponse(
            [
                "event: ready",
                'data: {"request_message_id":"request-1"}',
                "",
                "event: browser_use_start",
                "data: " + json.dumps(payload),
                "",
            ]
        )
        tabbit = FakeTabbitClient(response)
        client = TabbitAgentClient(tabbit)

        bootstrap = await client.bootstrap_task(AgentTaskRequest(
            session_id="session-1",
            content="use the tool",
            model="Default",
        ))

        self.assertEqual(bootstrap.task_id, "task-1")
        self.assertEqual(bootstrap.request_message_id, "request-1")
        self.assertEqual(bootstrap.assistant_message_id, "assistant-1")
        self.assertTrue(bootstrap.needs_agent)
        method, url, kwargs = tabbit.client.stream_calls[0]
        self.assertEqual((method, url), ("POST", "https://web.tabbit.ai/chat/send"))
        sent = json.loads(kwargs["content"])
        self.assertTrue(sent["agent_mode"])
        self.assertEqual(sent["content"], "use the tool")

    async def test_run_task_preserves_structured_mcp_call_and_stops(self):
        websocket = FakeWebSocket(
            [
                {
                    "type": "tool_calls",
                    "data": {
                        "assistant_message_id": "assistant-2",
                        "calls": [
                            {
                                "id": "call-1",
                                "type": "function",
                                "function": {
                                    "name": "mcp__relay__dispatch",
                                    "arguments": '{"name":"read_file"}',
                                },
                                "mcp_name": "relay",
                                "mcp_tool_name": "dispatch",
                            }
                        ],
                    },
                },
                {"type": "task_completed", "data": {"content": "done"}},
            ]
        )
        connect_calls = []

        def connect(url, **kwargs):
            connect_calls.append((url, kwargs))
            return FakeWebSocketContext(websocket)

        client = TabbitAgentClient(
            FakeTabbitClient(FakeStreamResponse([])),
            dependencies=AgentTransportDependencies(websocket_connect=connect),
        )
        bootstrap = AgentTaskBootstrap(
            session_id="session-1",
            task_id="task-1",
            request_message_id="request-1",
            assistant_message_id="assistant-1",
            refine_query="",
            refine_audit_pass=True,
            needs_agent=True,
        )

        events = [event async for event in client.run_task(bootstrap)]

        self.assertEqual(connect_calls[0][0], "wss://web.tabbit.ai/api/agent/v2/ws")
        self.assertIn("token=secret", connect_calls[0][1]["additional_headers"]["Cookie"])
        self.assertEqual(websocket.sent[0]["type"], "start_task")
        self.assertEqual(websocket.sent[0]["task_id"], "task-1")
        self.assertEqual(events[0].type, "tool_calls")
        call = events[0].data["calls"][0]
        self.assertEqual(call["id"], "call-1")
        self.assertEqual(call["mcp_name"], "relay")
        self.assertEqual(events[-1].type, "task_completed")


if __name__ == "__main__":
    unittest.main()
