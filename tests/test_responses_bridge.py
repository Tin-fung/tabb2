import asyncio
import json
import unittest

from core.responses_bridge import (
    AGENT_CONTENT_LIMIT,
    BridgeCallNotFound,
    BridgeStartRequest,
    ResponsesBridge,
    build_relay_prompt,
    merge_agent_text,
)
from core.tabbit_agent import AgentEvent, AgentTaskBootstrap


class FakeHTTPClient:
    def __init__(self):
        self.closed = False

    async def aclose(self):
        self.closed = True


class FakeTabbitClient:
    def __init__(self):
        self.client = FakeHTTPClient()

    async def create_chat_session(self):
        return "session-1"


class FakeAgent:
    def __init__(self, client):
        self.client = client
        self.release = asyncio.Event()
        self.tool_result = ""
        self.failure = ""
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
        await self.release.wait()
        if self.failure:
            yield AgentEvent(type="error", data={"message": self.failure})
            return
        yield AgentEvent(
            type="task_completed",
            data={"content": f"finished:{self.tool_result}"},
        )


class ResponsesBridgeTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.agents = []

        def factory(client):
            agent = FakeAgent(client)
            self.agents.append(agent)
            return agent

        self.bridge = ResponsesBridge(
            agent_factory=factory,
            relay_timeout_seconds=5,
        )

    async def asyncTearDown(self):
        await self.bridge.close_all()

    async def test_pending_relay_call_round_trips_through_responses_turns(self):
        session = await self.bridge.start(
            BridgeStartRequest(
                client=FakeTabbitClient(),
                model="Default",
                requested_model="best",
                prompt="read a file",
                tools=[
                    {
                        "type": "function",
                        "name": "read_file",
                        "parameters": {
                            "type": "object",
                            "properties": {"path": {"type": "string"}},
                        },
                    }
                ],
            )
        )

        async def invoke_relay():
            result = await self.bridge.relay_call(
                bridge_id=session.bridge_id,
                name="read_file",
                arguments={"path": "/tmp/example"},
            )
            self.agents[0].tool_result = result
            self.agents[0].release.set()
            return result

        relay_task = asyncio.create_task(invoke_relay())
        first = await self.bridge.next_turn(session)

        self.assertEqual(first.kind, "function_call")
        call = first.function_calls[0]
        self.assertEqual(call.name, "read_file")
        self.assertEqual(json.loads(call.arguments), {"path": "/tmp/example"})

        response_id = "resp_first"
        self.bridge.bind_response(session, response_id)
        resumed = self.bridge.session_for_continuation(
            previous_response_id=response_id,
            call_ids=[call.call_id],
        )
        self.assertIs(resumed, session)
        self.bridge.submit_outputs(session, [(call.call_id, "file contents")])

        self.assertEqual(await relay_task, "file contents")
        final = await self.bridge.next_turn(session)
        self.assertEqual(final.kind, "message")
        self.assertEqual(final.text, "finished:file contents")

    async def test_unknown_call_output_is_rejected(self):
        session = await self.bridge.start(
            BridgeStartRequest(
                client=FakeTabbitClient(),
                model="Default",
                requested_model="best",
                prompt="hello",
                tools=[],
            )
        )

        with self.assertRaises(BridgeCallNotFound):
            self.bridge.submit_outputs(session, [("call_missing", "result")])

    async def test_partial_parallel_outputs_are_rejected_before_resuming(self):
        from core.responses_bridge import PendingRelayCall

        session = await self.bridge.start(
            BridgeStartRequest(
                client=FakeTabbitClient(),
                model="Default",
                requested_model="best",
                prompt="hello",
                tools=[
                    {"type": "function", "name": "first"},
                    {"type": "function", "name": "second"},
                ],
            )
        )
        loop = asyncio.get_running_loop()
        session.pending_calls = {
            "call_first": PendingRelayCall(
                "call_first", "first", "{}", loop.create_future()
            ),
            "call_second": PendingRelayCall(
                "call_second", "second", "{}", loop.create_future()
            ),
        }

        with self.assertRaisesRegex(Exception, "call_second"):
            self.bridge.submit_outputs(
                session, [("call_first", "first result")]
            )
        self.assertFalse(session.pending_calls["call_first"].result.done())
        for pending in session.pending_calls.values():
            pending.result.cancel()
        session.pending_calls.clear()

    async def test_parallel_relay_calls_are_batched_into_one_turn(self):
        session = await self.bridge.start(
            BridgeStartRequest(
                client=FakeTabbitClient(),
                model="Default",
                requested_model="best",
                prompt="hello",
                tools=[
                    {"type": "function", "name": "first"},
                    {"type": "function", "name": "second"},
                ],
            )
        )
        relay_tasks = [
            asyncio.create_task(
                self.bridge.relay_call(
                    bridge_id=session.bridge_id,
                    name=name,
                    arguments={"value": name},
                )
            )
            for name in ("first", "second")
        ]

        turn = await self.bridge.next_turn(session)

        self.assertEqual(
            {call.name for call in turn.function_calls}, {"first", "second"}
        )
        self.bridge.submit_outputs(
            session,
            [(call.call_id, f"result:{call.name}") for call in turn.function_calls],
        )
        self.assertEqual(
            set(await asyncio.gather(*relay_tasks)),
            {"result:first", "result:second"},
        )

    async def test_relay_rejects_tool_not_declared_by_responses_client(self):
        session = await self.bridge.start(
            BridgeStartRequest(
                client=FakeTabbitClient(),
                model="Default",
                requested_model="best",
                prompt="hello",
                tools=[{"type": "function", "name": "read_file"}],
            )
        )

        with self.assertRaisesRegex(Exception, "not allowed"):
            await self.bridge.relay_call(
                bridge_id=session.bridge_id,
                name="shell",
                arguments={"cmd": "rm -rf /"},
            )

    async def test_agent_failure_immediately_fails_pending_relay(self):
        session = await self.bridge.start(
            BridgeStartRequest(
                client=FakeTabbitClient(),
                model="Default",
                requested_model="best",
                prompt="hello",
                tools=[{"type": "function", "name": "read_file"}],
            )
        )
        relay_task = asyncio.create_task(
            self.bridge.relay_call(
                bridge_id=session.bridge_id,
                name="read_file",
                arguments={"path": "/tmp/example"},
            )
        )
        await self.bridge.next_turn(session)
        self.agents[0].failure = "websocket closed"
        self.agents[0].release.set()

        error_turn = await self.bridge.next_turn(session)
        self.assertEqual(error_turn.kind, "error")
        with self.assertRaisesRegex(Exception, "websocket closed"):
            await relay_task

    def test_prompt_injects_bridge_id_and_client_tool_schema(self):
        prompt = build_relay_prompt(
            "do it",
            "bridge_123",
            [{"type": "function", "name": "shell", "parameters": {"type": "object"}}],
        )

        self.assertIn("bridge_123", prompt)
        self.assertIn('"name":"shell"', prompt)
        self.assertIn("MCP tool named dispatch", prompt)

    def test_prompt_without_tools_is_unchanged(self):
        self.assertEqual(build_relay_prompt("hello", "bridge_1", []), "hello")

    def test_final_agent_content_does_not_duplicate_streamed_text(self):
        self.assertEqual(merge_agent_text("READY", "READY"), "READY")
        self.assertEqual(merge_agent_text("REA", "READY"), "READY")
        self.assertEqual(merge_agent_text("prefix READY", "READY"), "prefix READY")

    def test_large_open_code_tool_catalog_fits_agent_gateway_limit(self):
        tools = []
        for index in range(80):
            tools.append(
                {
                    "type": "function",
                    "name": f"tool_{index}",
                    "description": "d" * 500,
                    "parameters": {
                        "type": "object",
                        "properties": {
                            f"argument_{item}": {
                                "type": "string",
                                "description": "p" * 500,
                            }
                            for item in range(12)
                        },
                        "required": ["argument_0"],
                    },
                }
            )
        prompt = "SYSTEM:" + "s" * 12_000 + "\nUSER:KEEP_THIS_TASK"

        content = build_relay_prompt(prompt, "bridge_large", tools)

        self.assertLessEqual(len(content), AGENT_CONTENT_LIMIT)
        self.assertIn("bridge_large", content)
        self.assertIn("tool_0", content)
        self.assertIn("tool_79", content)
        self.assertIn("KEEP_THIS_TASK", content)


if __name__ == "__main__":
    unittest.main()
