# Dual Tool Plane Design

Date: 2026-07-04
Project: tabb2
Status: draft for user review

## 1. Context

tabb2 exposes Tabbit as OpenAI-compatible and Anthropic-compatible APIs. The
current tool implementation has two different behaviors mixed together:

- Local client tools, such as Claude Code or opencode tools like Write, Read,
  Edit, Bash, and LS, are simulated by injecting a text protocol into the
  upstream prompt. The adapter aliases these tools with a `cc_` prefix and then
  parses either text `<invoke>` blocks or Tabbit `message_tool_calls` events back
  into OpenAI or Claude tool calls.
- Tabbit native tools, such as `parallel_web_search`, `browser_task_tool`,
  `memory_search`, and `show_widget`, are executed by Tabbit upstream itself and
  reported through SSE events like `message_tool_calls`, `tool_start`, and
  `tool_finish`.

The current routes mostly forward only aliased `cc_` tool calls. Native Tabbit
tools are visible in upstream events but are not treated as a first-class
capability. This makes the project appear to have a broken generic tool layer,
when the better product shape is a stable chat proxy with two explicit tool
planes.

## 2. Product Direction

tabb2 should adopt a Dual Tool Plane architecture:

1. Chat Core
   - Always provide reliable OpenAI and Anthropic chat compatibility.
   - Chat behavior must not depend on tool support.
   - If tool mode is unavailable, requests should degrade clearly instead of
     pretending that tools are fully supported.

2. Local Tool Plane
   - Supports client-owned tools from Claude Code, opencode, and OpenAI tool
     callers.
   - Uses the existing `cc_` alias strategy and text-protocol parser.
   - Is enabled only for certified models that pass tool-loop evaluation.
   - Is best-effort, because Tabbit upstream does not expose a native external
     tools API.

3. Tabbit Native Tool Plane
   - Uses Tabbit upstream's own server-side tools.
   - Tracks native tool lifecycle events and results.
   - Does not present native tools as local client tools, because the client did
     not execute them.
   - Can enrich final answers, logs, diagnostics, and optional debug streams.

## 3. Goals

- Preserve reliable chat compatibility for `/v1/chat/completions` and
  `/v1/messages`.
- Keep external client tool support, but gate it behind model certification and
  explicit mode selection.
- Capture Tabbit native tool events instead of dropping them.
- Share tool event handling between OpenAI and Claude routes.
- Make tool behavior observable in logs and diagnostics.
- Add offline tests for native tool aggregation, local tool separation, and SSE
  ordering.

## 4. Non-Goals

- Do not promise full Claude Code or opencode backend equivalence.
- Do not claim that all Tabbit models support external local tools.
- Do not make Tabbit native tools appear as client-executable Bash, Write, Read,
  Edit, or LS tools.
- Do not depend on an upstream native external tools field unless a real Tabbit
  payload proves it exists.
- Do not remove the existing parser before replacement tests are in place.

## 5. Architecture

### 5.1 Components

`core/tool_events.py`

- Defines normalized internal event types:
  - `ToolCallStarted`
  - `ToolCallDelta`
  - `ToolCallCompleted`
  - `ToolCallFailed`
  - `ToolResultAvailable`
- Defines tool origin:
  - `local`: aliased `cc_` tools that must be returned to the client as
    tool calls.
  - `native`: Tabbit upstream tools that execute server-side.
  - `unknown`: events that cannot be classified safely.

`NativeToolAggregator`

- Groups upstream events by `tool_call_id`.
- Records `name`, `arguments`, `started_at`, `finished_at`, `status`, and
  result text or structured payload.
- Handles out-of-order or partial events safely.
- Produces compact summaries for logs and optional debug events.

`LocalToolBridge`

- Owns the existing `cc_` alias mapping, required-argument validation, and
  OpenAI or Claude tool call emission.
- Does not consume native Tabbit tool results.
- Applies model capability gates before local tool mode is enabled.

`ToolModePolicy`

- Decides what to do when a request contains tools:
  - `chat_only`: ignore or reject tools according to compatibility policy.
  - `local_tools`: allow local tool simulation only for certified models.
  - `native_enhanced`: allow Tabbit native tools to run as upstream behavior.
  - `dual`: allow both local tool simulation and native tool observation.
- Provides a consistent decision to both OpenAI and Claude routes.

`ToolEventSSEAdapter`

- Converts internal tool events into route-specific output only when needed.
- For local tools, emits OpenAI `tool_calls` or Claude `tool_use`.
- For native tools, normally emits no client-visible tool call.
- In debug mode, may emit metadata-only SSE events that do not break OpenAI or
  Claude clients.

### 5.2 Route Responsibilities

The routes should become thin consumers:

- Build request content and model selection.
- Ask `ToolModePolicy` which tool planes are enabled.
- Feed upstream SSE events into the shared tool event layer.
- Emit local client tool calls only through `LocalToolBridge`.
- Attach native tool summaries to logs and optional diagnostics.

OpenAI and Claude routes should not duplicate native tool parsing logic.

## 6. Data Flow

### 6.1 Plain Chat

1. Client sends a chat request without tools.
2. Route sends content to Tabbit.
3. Text deltas stream back normally.
4. Any accidental native tool events are collected for logs but not exposed as
   client tool calls.
5. Final response remains standard OpenAI or Claude output.

### 6.2 Local Tool Request

1. Client sends tools.
2. `ToolModePolicy` checks whether the requested model is certified for local
   tool simulation.
3. If certified, tools are aliased as `cc_*` before prompt injection.
4. Upstream output is parsed from text protocol or `message_tool_calls`.
5. Only calls matching the alias map are emitted as client tool calls.
6. Client executes the tools and returns tool results in the next turn.
7. Tool results are converted back into content with aliased names for the next
   upstream request.

### 6.3 Native Tool Enhanced Request

1. Client sends a normal chat request, or a local tool request where Tabbit also
   triggers native tools.
2. Upstream emits native events such as `parallel_web_search`.
3. `NativeToolAggregator` records start, finish, arguments, duration, and result.
4. The route lets Tabbit continue producing the final answer.
5. The client receives normal text output.
6. Logs and diagnostics include native tool summaries and result sizes.

### 6.4 Debug Native Tool Streaming

Debug mode is opt-in. It can be enabled by config or a request header.

- Claude route may emit adapter-specific events such as
  `event: tabbit_tool_start` and `event: tabbit_tool_finish`.
- OpenAI route may emit metadata chunks only if they are known not to break
  target clients. Otherwise debug information stays in logs.
- Debug mode must never turn native tools into executable client tool calls.

## 7. Model Capability Policy

Local tool simulation must be gated by a capability matrix. The matrix should
record:

- model name
- local tool protocol pass rate
- required-argument reliability
- multi-round loop reliability
- last probe time
- probe version

Initial policy:

- Certified models can enter `local_tools` or `dual`.
- Uncertified models default to `chat_only` or `native_enhanced`.
- Requests with `tool_choice="required"` on uncertified models should fail fast
  with a clear 400 error.
- Requests with optional tools may degrade to chat with a warning in logs.

The existing probe scripts can seed this matrix, but implementation should use a
small structured file or config field rather than hard-coded comments.

## 8. Error Handling

- Missing or malformed native tool finish events should not fail the chat
  response. They should produce a native tool warning in logs.
- Invalid local tool calls should be filtered before emitting to clients.
- If local tool mode produces no useful output, the adapter may retry with a
  configured certified fallback model, but only once per request.
- If retry fails, return a normal assistant message or a protocol-compatible
  error rather than leaking tool protocol fragments.
- Upstream `error` events remain fatal for the route, as they are today.

## 9. Observability

Add native tool summary fields to request logs:

- `native_tools_count`
- `native_tool_names`
- `native_tools_status`
- `native_tools_duration_ms`
- `native_tools_result_chars`

Admin UI can show a compact native tools column or expandable details later.
The first implementation only needs backend log data and tests.

## 10. Security

- Native tool results are untrusted upstream data. Escape them in Admin UI and
  avoid directly rendering HTML.
- Native browsing/search results should not be passed to client tool executors.
- Local tool execution remains the client's responsibility. The server should
  only emit tool call requests in the client's expected protocol.
- Do not expose raw Tabbit cookies, tokens, request headers, or full upstream
  payloads in debug streams.
- If native tool results are stored, apply size limits and truncation.

## 11. Testing Strategy

Offline unit tests:

- Classify `cc_Write` and `cc_Bash` as local tools.
- Classify `parallel_web_search` and `browser_task_tool` as native tools.
- Aggregate native `message_tool_calls`, `tool_start`, and `tool_finish` by id.
- Ensure native tools do not produce Claude `tool_use` blocks.
- Ensure local tools still produce Claude `tool_use` blocks.
- Ensure local tools still produce OpenAI `tool_calls`.
- Ensure `message_stop` is not emitted before local tool blocks finish.

Integration tests:

- Replay captured Tabbit native tool SSE from `logs/capture_agent_evidence.log`.
- Verify log summaries include native tool name, status, duration, and result
  length.
- Verify plain chat output stays protocol-compatible.
- Verify uncertified model plus required tools fails clearly.

Live smoke tests:

- Run a native-search style prompt against a model known to trigger
  `parallel_web_search`.
- Run a certified local-tool model through one write/read loop.
- Run a chat-only model with tools and confirm safe degradation.

## 12. Rollout Plan

Phase 1: shared event model and aggregator

- Add `core/tool_events.py`.
- Add offline tests for classification and aggregation.
- Do not change route behavior yet.

Phase 2: route integration

- Wire native aggregation into Claude route.
- Wire native aggregation into OpenAI route.
- Preserve existing local tool behavior.
- Add log summaries.

Phase 3: policy gate

- Add model capability config.
- Enforce local tool mode only for certified models.
- Add clear behavior for required versus optional tools.

Phase 4: diagnostics and docs

- Add optional debug native tool events.
- Update README and TOOL_USE_REPORT.
- Add Admin UI display only after backend behavior is stable.

## 13. Open Decisions

These decisions are resolved for the first implementation:

- Native Tabbit tools are used as upstream enhancement, not exposed as client
  executable tools.
- Local tool support remains experimental and model-gated.
- Debug native tool streaming is opt-in and secondary to backend logs.
- The first implementation will prioritize backend correctness over Admin UI
  presentation.

## 14. Acceptance Criteria

- Existing chat tests continue to pass.
- Existing local tool parser tests continue to pass or are replaced by stricter
  equivalents.
- Captured native tool SSE can be replayed and aggregated without producing
  client local tool calls.
- `cc_` local tool calls are still emitted to Claude and OpenAI clients.
- A request log can show which Tabbit native tools ran.
- Uncertified models cannot silently enter required local tool mode.
- No raw native tool result is rendered in Admin UI without escaping.
