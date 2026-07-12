# Core design

## Goals

- Keep upstream Tabbit protocol mechanics separate from public API adapters.
- Preserve structured tool events and identifiers without converting them to
  text prematurely.
- Make authentication refresh, version headers, and error handling consistent
  across routes.
- Keep new protocol paths additive until their compatibility surface is proven.

## Non-goals

- `core` does not decide public API authorization or rate limits.
- Agent transport does not execute arbitrary Codex-local tools by itself.
- It does not expose upstream sandbox credentials to downstream clients.

## Architecture

```text
routes/ ──> token_manager ──> TabbitClient ──> Tabbit HTTP APIs
                         └──> TabbitAgentClient
                               ├── signed /chat/send
                               └── /api/agent/v2/ws
                                      │
Tabbit MCP HTTPS ──> ResponsesBridge ─┬──> /v1/responses (Codex)
                                      └──> /v1/chat/completions
```

## Key decisions

### Separate Agent transport from `TabbitClient`

Options considered:

1. Add Task/WebSocket methods directly to the already large `TabbitClient`.
2. Wrap `TabbitClient` with a dedicated `TabbitAgentClient`.

The wrapper was selected because Task mode has a different lifecycle and
security boundary: short-lived HMAC signing, a bootstrap SSE stream, and a
long-lived bidirectional WebSocket. It reuses cookies, version headers, and
server-time synchronization without expanding the chat client further.

### Keep Responses bridging outside the transport

The transport emits typed upstream events and stops at terminal Agent events.
Mapping those events to OpenAI Responses and persisting pending tool calls is a
separate state-machine concern. This separation keeps Agent protocol tests
independent of downstream API policy.

`ResponsesBridge` owns the long-lived Agent runner, response/task/call
identifier mappings, and pending MCP result futures. The MCP request remains
blocked until the Responses client returns the matching result, so the bridge
does not depend on an unverified Agent WebSocket result message.

When client tools are supplied, they are authoritative for workspace side
effects. The relay prompt forbids Tabbit's native E2B/sandbox tools from
substituting for client filesystem, shell, code, test, build, or git tools.
The bridge observes Agent tool events and treats non-relay calls as
upstream-native routes, except for Tabbit's internal `plan_track` and
`termination` lifecycle tools. The public relay tool is named
`client_tool_dispatch`, with legacy `dispatch` accepted only for in-flight
compatibility, so the model cannot confuse the relay transport name with a
client tool name. If a task finishes through a real native tool without any MCP
dispatch, the bridge retries once with an explicit correction; a second
violation is returned as an error instead of falsely claiming that a cloud
`/mnt/work` artifact exists in the user's local workspace.

Responses continuations use `previous_response_id` or `call_id`. Chat
Completions has no response continuation identifier, so its adapter resolves
the session through the pending `tool_call_id`. Historical tool messages whose
IDs are no longer pending stay ordinary conversation history and do not reopen
an expired bridge session.

Task mode still requires a server-created chat session. The bridge reuses
`TabbitClient.create_chat_session()` before the signed `/chat/send` bootstrap;
an arbitrary client-generated UUID is rejected upstream as `chat.notFound`.

### Explicit dependency injection for protocol tests

Clock, nonce, and WebSocket connector dependencies are injectable as one
runtime object. Tests can verify exact HMAC bytes and event preservation without
network access, while production uses the official runtime behavior.

## Security

- Cookie and authorization values are passed only to HTTP/WebSocket clients.
- Signing keys are cached in memory for a bounded interval and never persisted.
- WebSocket message size is capped and non-object JSON is ignored.
- TLS verification is enabled by default. The unverified SSL context is used
  only when the existing explicit debug setting disables verification.
- Raw `agent_e2b_session_ready` events can contain short-lived credentials;
  callers must redact them before logging or returning diagnostics.
- The `d41d8...` entity key is a literal compatibility constant copied from the
  official request shape. It is not a security hash and no input is hashed with
  MD5.

## Known limitations

- Model selection and billing are separate upstream concerns. The bridge sends
  the resolved `selected_model` requested by the client. Dynamic aliases use a
  shared case/whitespace normalization function and known stale-cache entries
  remain resolvable, preventing model ids such as `GPT-5.6 Sol` from silently
  falling back to `Default`. Tabbit does not return the actual Task-mode model,
  so this field is still an upstream request hint rather than a verified
  execution identity. Tabbit records MCP Task-mode execution under the `agent`
  quota scene even when `Default` is free in ordinary chat. New user turns
  create new Agent tasks; function-call result continuations stay on the
  existing task.
- `responses_bridge.py` remains above the preferred module-size threshold while
  the Agent state machine, native-route guard, prompt fitting, and reference
  packaging are still evolving together. The prompt/reference helpers are pure
  and independently tested so they can be extracted after the upstream
  protocol stabilizes without changing the active-session state machine.
- The HTTPS MCP relay must be published by the operator and configured in the
  Tabbit account before Codex tool calls can complete.
- Bridge state is process-local; use one worker. Shared multi-process task state
  will require a durable store.
- The upstream maximum duration for a blocked MCP call is not yet measured.
- Requests cancelled while waiting for the next Agent outcome close their
  bridge session so abandoned HTTP clients do not leak WebSocket tasks.
- Agent relay prompts are capped below the verified `/chat/send` content limit.
  Large OpenCode/Codex tool catalogs degrade from full schemas to compact
  parameter summaries and finally tool signatures instead of triggering 492.
- Agent bridge requests restore the direct-chat long-context bypass. When the
  client conversation exceeds the main prompt budget, `content` keeps the
  routing policy plus a head/tail compact view while one DOM `reference`
  carries the complete untruncated client context. Short requests do not add a
  reference. Tool-result continuations remain on the same Agent task, so the
  reference is attached only at bootstrap.
- Native-tool rejection is a routing guard, not an upstream capability switch:
  Tabbit currently exposes no verified `tool_choice` field that disables its
  native sandbox. A model that ignores both guarded attempts produces an
  explicit bridge error and requires further upstream protocol work.
- The current official client version must remain synchronized to avoid 493.

## Change history

- 2026-07-12: added the official signed Task-mode and Agent v2 transport.
- 2026-07-12: added the authenticated MCP relay and stateful Responses bridge.
- 2026-07-12: reused the bridge for Chat Completions tool calls and tool-result
  continuations, including SSE `delta.tool_calls` output.
- 2026-07-12: made client tools authoritative in Agent bridge mode, added
  native sandbox interception detection, one corrective retry, and MCP method
  observability without logging arguments or credentials.
