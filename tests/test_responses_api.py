import json
import unittest

from fastapi import HTTPException

from core.responses_bridge import BridgeFunctionCall, BridgeTurn
from routes import responses_api


class FakeConfig:
    def get(self, *keys, default=None):
        values = {
            ("responses", "relay_token"): "relay-secret",
            ("responses", "relay_timeout_seconds"): 30,
            ("responses", "session_ttl_seconds"): 60,
        }
        return values.get(keys, default)


class ResponsesAPITest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        responses_api._cfg = FakeConfig()

    def test_function_call_response_matches_responses_shape(self):
        response = responses_api.build_response(
            "resp_1",
            "best",
            BridgeTurn(
                kind="function_call",
                function_calls=(
                    BridgeFunctionCall(
                        call_id="call_1",
                        name="shell",
                        arguments='{"cmd":"pwd"}',
                    ),
                ),
            ),
        )

        item = response["output"][0]
        self.assertEqual(response["object"], "response")
        self.assertEqual(response["status"], "completed")
        self.assertEqual(item["type"], "function_call")
        self.assertEqual(item["call_id"], "call_1")
        self.assertEqual(item["name"], "shell")

    def test_extracts_function_call_outputs(self):
        outputs = responses_api.extract_function_call_outputs(
            [
                {
                    "type": "function_call_output",
                    "call_id": "call_1",
                    "output": {"ok": True},
                }
            ]
        )

        self.assertEqual(outputs, [("call_1", '{"ok":true}')])

    def test_stream_event_has_named_event_and_json_type(self):
        encoded = responses_api.sse_event(
            "response.output_item.done",
            {"item": {"type": "function_call"}},
        )

        lines = encoded.strip().splitlines()
        self.assertEqual(lines[0], "event: response.output_item.done")
        payload = json.loads(lines[1][6:])
        self.assertEqual(payload["type"], "response.output_item.done")

    async def test_mcp_initialize_and_tools_list(self):
        init_response = await responses_api.mcp_relay(
            responses_api.MCPRequest(
                id=1,
                method="initialize",
                params={"protocolVersion": "2025-03-26"},
            ),
            authorization="Bearer relay-secret",
        )
        init_payload = json.loads(init_response.body)
        self.assertEqual(init_payload["result"]["protocolVersion"], "2025-03-26")

        tools_response = await responses_api.mcp_relay(
            responses_api.MCPRequest(id=2, method="tools/list"),
            authorization="Bearer relay-secret",
        )
        tools_payload = json.loads(tools_response.body)
        tool = tools_payload["result"]["tools"][0]
        self.assertEqual(tool["name"], "client_tool_dispatch")
        self.assertIn("client_tool_name", tool["inputSchema"]["required"])

    async def test_mcp_rejects_invalid_relay_token(self):
        with self.assertRaises(HTTPException) as ctx:
            await responses_api.mcp_relay(
                responses_api.MCPRequest(id=1, method="ping"),
                authorization="Bearer wrong",
            )

        self.assertEqual(ctx.exception.status_code, 401)

    async def test_mcp_dispatch_accepts_new_and_legacy_argument_shapes(self):
        class FakeBridge:
            def __init__(self):
                self.calls = []

            async def relay_call(self, **kwargs):
                self.calls.append(kwargs)
                return "tool result"

        original = responses_api._bridge
        bridge = FakeBridge()
        responses_api._bridge = bridge
        try:
            current = await responses_api.mcp_relay(
                responses_api.MCPRequest(
                    id=1,
                    method="tools/call",
                    params={
                        "name": "client_tool_dispatch",
                        "arguments": {
                            "bridge_id": "bridge_current",
                            "client_tool_name": "write",
                            "arguments": {"path": "test.txt"},
                        },
                    },
                ),
                authorization="Bearer relay-secret",
            )
            legacy = await responses_api.mcp_relay(
                responses_api.MCPRequest(
                    id=2,
                    method="tools/call",
                    params={
                        "name": "dispatch",
                        "arguments": {
                            "bridge_id": "bridge_legacy",
                            "name": "read",
                            "arguments": {"path": "test.txt"},
                        },
                    },
                ),
                authorization="Bearer relay-secret",
            )

            self.assertEqual(json.loads(current.body)["result"]["isError"], False)
            self.assertEqual(json.loads(legacy.body)["result"]["isError"], False)
            self.assertEqual(
                [call["name"] for call in bridge.calls],
                ["write", "read"],
            )
        finally:
            responses_api._bridge = original


if __name__ == "__main__":
    unittest.main()
