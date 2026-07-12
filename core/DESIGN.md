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

- The HTTPS MCP relay must be published by the operator and configured in the
  Tabbit account before Codex tool calls can complete.
- Bridge state is process-local; use one worker. Shared multi-process task state
  will require a durable store.
- The upstream maximum duration for a blocked MCP call is not yet measured.
- Requests cancelled while waiting for the next Agent outcome close their
  bridge session so abandoned HTTP clients do not leak WebSocket tasks.
- The current official client version must remain synchronized to avoid 493.

## Change history

- 2026-07-12: added the official signed Task-mode and Agent v2 transport.
- 2026-07-12: added the authenticated MCP relay and stateful Responses bridge.
- 2026-07-12: reused the bridge for Chat Completions tool calls and tool-result
  continuations, including SSE `delta.tool_calls` output.
