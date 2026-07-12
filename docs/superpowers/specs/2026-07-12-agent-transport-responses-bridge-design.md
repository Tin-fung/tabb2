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

## Phase 4: Client-Authoritative Tool Routing

Tabbit Agent tasks can see both the configured MCP relay and Tabbit-native
cloud tools. For local coding clients this creates an unsafe ambiguity: a file
operation can succeed inside Tabbit's temporary E2B `/mnt/work` directory while
the client believes its own workspace changed.

When a Responses or Chat Completions request supplies client tools, the bridge
therefore treats those tools as authoritative:

1. The task prompt requires MCP `client_tool_dispatch` for filesystem,
   repository, shell,
   code execution, test, build, and git operations.
2. It explicitly prohibits native E2B/sandbox tools from substituting for a
   client tool or claiming local side effects.
3. Agent WebSocket tool events are inspected. `client_tool_dispatch` belongs
   to the relay; `plan_track` and `termination` are harmless Agent lifecycle
   controls; other tool names are upstream-native for this bridge path.
4. A native-only attempt is discarded and retried once with the observed tool
   names included in the correction.
5. A second native-only attempt fails explicitly. The bridge never reports a
   cloud sandbox artifact as a local OpenCode/Codex file.

The retry guard compensates for the absence of a verified upstream field that
fully disables native tools. It does not expose tool arguments, authorization
headers, cookies, or sandbox credentials in logs.

The relay previously used the generic name `dispatch` and an inner argument
also named `name`. With large OpenCode catalogs the model could set the target
tool to `dispatch` itself. The relay now exposes `client_tool_dispatch` and the
unambiguous `client_tool_name` field. The server still accepts the legacy shape
for already-running Agent tasks, but no longer advertises it.

## Phase 5: Agent Long-Context References

The `/chat/send` bootstrap enforces the same approximately 20,421-character
`content` limit as direct chat. Tool-enabled OpenCode requests need additional
space for routing policy and tool definitions, leaving an 8,000-character
client prompt budget.

To avoid losing middle conversation history, the bridge uses the proven
Tabbit reference bypass:

1. Short prompts remain unchanged and send no references.
2. Long prompts keep a head/tail compact view in `content`, together with the
   latest task, tool-routing policy, bridge identifier, and fitted tool catalog.
3. A DOM reference named `Complete client conversation context` carries the
   complete original client prompt without truncation.
4. The main content tells the Agent to consult that reference for omitted
   history while treating the latest task and routing policy as authoritative.
5. Logs record only content/reference character counts, never reference text.

References restore access to older facts and large documents, but they do not
guarantee equal attention. Current instructions, tool routing, and critical
constraints therefore remain visible in the main `content` field.

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
