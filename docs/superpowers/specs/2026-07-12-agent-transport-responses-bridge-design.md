# Tabbit Agent Transport and Responses Bridge Design

## Problem

`tabb2` currently sends chat traffic directly to
`/api/v1|v2/chat/completion`. That path supports Tabbit chat tools, but it
bypasses the official Agent conversion layer used by Tabbit's "Task" mode.
As a result, setting `agent_mode=true` on the direct completion endpoint does
not create a usable browser/agent task and configured MCP servers are not
injected into the model runtime.

Codex also requires protocol-level Responses tool calls. Text that merely
resembles XML or JSON tool calls cannot provide the required
`function_call.call_id -> function_call_output.call_id` loop.

## Verified Official Flow

The current official client uses this sequence:

1. Fetch `GET /chat/sign-key`.
2. Serialize the Task-mode request as compact JSON.
3. Sign `<timestamp>.<nonce>.<sha256(body)>` with HMAC-SHA256.
4. Send `POST /chat/send` with:
   - `x-timestamp`: milliseconds since epoch
   - `x-signature`: UUID nonce
   - `x-nonce`: HMAC hex digest
   - current `x-req-ctx` and `unique-uuid` headers
5. Read `browser_use_start` from SSE. It contains `task_id`,
   `request_message_id`, `assistant_message_id`, audit state, and
   `needs_agent`.
6. Connect to `wss://web.tabbit.ai/api/agent/v2/ws` with Tabbit cookies.
7. Send `start_task` with the session/task/request identifiers.
8. Consume Agent events such as `tool_calls`, `tool_finish`,
   `execute_content`, and `task_completed`.

For backend-executed MCP calls, `tool_calls` contains native structured fields:

```json
{
  "id": "call_...",
  "function": {
    "name": "mcp__namespace__tool_name",
    "arguments": "{...}"
  },
  "mcp_name": "configured server name",
  "mcp_tool_name": "remote tool name"
}
```

The official frontend intentionally does not execute calls containing
`mcp_name` or names beginning with `mcp__`; Tabbit's backend executes them and
emits `tool_finish`.

## Phase 1: Agent Transport

Add a standalone `TabbitAgentClient` that wraps an authenticated
`TabbitClient` and owns:

- signing-key retrieval and bounded caching;
- deterministic compact request serialization;
- HMAC header generation;
- `/chat/send` SSE parsing and `browser_use_start` validation;
- authenticated Agent WebSocket connection;
- application heartbeat messages;
- typed bootstrap/event records;
- termination on `task_completed`, `error`, `audit_failure`, or `task_limit`.

This is additive. Existing Chat Completions and Claude routes remain unchanged.

## Phase 2: Responses Bridge (implemented, experimental)

The implementation adds `/v1/responses` and `/mcp/relay`:

1. Start a Tabbit Agent task.
2. Detect calls belonging to a dedicated HTTPS MCP relay.
3. Convert the relay call into an OpenAI Responses `function_call`, preserving
   or deterministically mapping its `call_id`.
4. Persist bridge state keyed by response/task/call identifiers.
5. Accept a later `function_call_output` and deliver it to the pending relay
   invocation.
6. Resume the same Tabbit Agent task until another tool call or final output.

The relay is required because Tabbit's MCP executor can only reach HTTPS
servers; it cannot call a Codex process on localhost directly.

## Phase 3: Chat Completions Bridge (implemented, experimental)

When a Chat Completions request supplies function tools, the route reuses the
same pending MCP state machine:

1. Map a pending relay invocation to `choices[0].message.tool_calls`.
2. For streaming requests, emit `choices[0].delta.tool_calls` and finish with
   `finish_reason=tool_calls`.
3. Locate the pending bridge session from a later `role=tool` message's
   `tool_call_id`.
4. Deliver the tool content to the blocked relay request and wait for the next
   tool call or final Agent message.

Chat requests without client function tools remain on the existing direct
completion path to minimize compatibility regressions.

## Security Boundaries

- Tabbit cookies, signing keys, sandbox credentials, and MCP authorization
  headers must never be logged.
- WebSocket events are untrusted upstream input and must be size-limited and
  JSON-validated.
- Only `wss` is accepted when the configured Tabbit base URL uses `https`.
- The relay authenticates every control/result request and binds it
  to one pending call to prevent cross-session result injection.
- Tool results remain data. They must not be interpolated into shell commands
  or executable templates by the transport.

## Compatibility and Rollback

- Existing Chat Completions and Claude behavior remains unchanged.
- Removing the new module and dependency restores the previous implementation.
- Agent transport is enabled by the explicit Responses route.
- The installed Tabbit client version observed during protocol verification was
  `1.4.46 (10104046)`. Older persisted defaults cause upstream error 493 and
  must be migrated.

## Remaining Risks

- The maximum duration for a blocked MCP invocation is not yet measured.
- Directly sending `tool_call_result` for an MCP call over Agent WebSocket is
  unverified; the design does not rely on it.
- Multi-process Responses bridge state will require Redis or another shared
  store before production deployment.
