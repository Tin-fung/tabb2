# Native-First Tool Policy Design

Date: 2026-07-04
Project: tabb2
Status: approved design

## 1. Context

tabb2 exposes Tabbit through OpenAI-compatible and Anthropic-compatible APIs. The
previous tool strategy optimized for Claude Code and opencode: when a client
sent tools, tabb2 tried to translate those tools into a text protocol and asked
the upstream model to emit compatible local tool calls.

That direction is no longer the primary product goal. The preferred behavior is
to restore Tabbit official-client behavior first. For prompts such as "search
today's hot technology news", tabb2 should let Tabbit upstream use its native
tools, such as `parallel_web_search`, and return the final answer. It should not
try to call Claude Code, opencode, or OpenAI client-side search tools first.

## 2. Product Direction

tabb2 adopts a Native-first tool policy:

1. Native Tabbit behavior is the default.
   - Requests are sent upstream as normal chat whenever possible.
   - Client-provided search, browser, memory, or widget tools are treated as
     native-equivalent hints and are not exposed to the model through the local
     compatibility prompt.
   - Tabbit native SSE events are observed and logged.

2. Local compatibility tools are opt-in fallback.
   - Local tool simulation remains available for non-native actions such as
     `Read`, `Write`, `Edit`, `Bash`, and `LS`.
   - It is disabled by default.
   - It is enabled only when a request or configuration explicitly allows local
     fallback and the selected model is certified for the legacy protocol.

3. Native tools are never disguised as client-executable tools.
   - `parallel_web_search`, `browser_task_tool`, `memory_search`, `show_widget`,
     and compatible native names stay upstream-managed.
   - They may enrich the final answer and logs.
   - They should not be converted into Claude `tool_use` or OpenAI `tool_calls`
     for the downstream client to execute.

## 3. Goals

- Make official Tabbit native behavior the default for OpenAI and Claude
  compatibility routes.
- Prevent client-side search/browser tools from stealing work that Tabbit native
  tools can do better.
- Preserve a controlled escape hatch for local file or shell tools when the user
  explicitly asks for that mode.
- Keep native tool observability in request logs.
- Provide clear errors when a client requires local tools while local fallback is
  disabled.

## 4. Non-Goals

- Do not promise full Claude Code or opencode backend equivalence.
- Do not pass arbitrary external tool schemas into a supposed native Tabbit tool
  API; no such upstream external-tool field has been proven.
- Do not make Tabbit native tools appear as local `Bash`, `Write`, `Read`,
  `Edit`, or `LS` calls.
- Do not remove the legacy local compatibility implementation in this change.

## 5. Tool Classification

The policy splits incoming client tools into two groups.

Native-equivalent tools:

- Names or descriptions that clearly represent web search, browsing, URL fetch,
  memory retrieval, or UI widget display.
- Examples: `search`, `web_search`, `parallel_web_search`, `browser`,
  `browser_task`, `fetch_url`, `web_fetch`, `memory_search`, `show_widget`.
- These tools are filtered out of the local compatibility path. The request
  falls back to normal Tabbit chat so upstream native tools can trigger.

Local-only tools:

- Tools that require execution by the downstream client or local machine.
- Examples: `Read`, `Write`, `Edit`, `MultiEdit`, `Bash`, `LS`, `Glob`, `Grep`,
  `TodoWrite`.
- These tools can use the existing `cc_` alias and XML compatibility protocol
  only when explicit local fallback is enabled and the model is certified.

Unknown tools:

- Tools that are not confidently native-equivalent or known local-only.
- Default behavior is conservative: local fallback must be explicitly enabled.
  Otherwise required unknown tools are rejected and optional unknown tools are
  ignored for local compatibility.

## 6. Policy Rules

The default mode is `native_enhanced`.

When a request has no tools:

- Send normal chat upstream.
- Collect native tool events for logs.

When a request has only native-equivalent tools:

- Remove those tools from the local compatibility path.
- Send normal chat upstream.
- Do not return client-executable tool calls.
- Log any upstream native tool activity.

When a request has local-only or unknown tools:

- If local fallback is not enabled:
  - Optional tools are ignored for local compatibility and the request proceeds
    as native-enhanced chat.
  - Required tools return a clear 400 error explaining that local tool mode is
    disabled by default.
- If local fallback is enabled:
  - Allow local compatibility only for certified models.
  - Reject required tools on uncertified models with a clear 400 error.
  - Use the existing `cc_` alias, XML prompt, parser, and route emitters.

When a request mixes native-equivalent and local-only tools:

- Filter native-equivalent tools out of the compatibility prompt.
- Apply the local fallback rules only to the remaining local-only or unknown
  tools.
- If no local tools remain, proceed as native-enhanced chat.

## 7. Explicit Fallback Controls

Local fallback can be enabled by either configuration or request metadata.

Controls:

- Config: `proxy.local_tools_enabled`, default `false`.
- OpenAI request header: `x-tabbit-local-tools: true`.
- Claude request header: `x-tabbit-local-tools: true`.

Request header should enable fallback for that request only. Configuration
should enable fallback globally for deployments that still need Claude
Code/opencode style use.

## 8. Route Behavior

OpenAI route:

- Normalize `tools` and `tool_choice` as today.
- Classify selected tools before building content.
- Build the Tabbit prompt with local tool instructions only for tools that
  survive the Native-first policy.
- For native-enhanced mode, return normal OpenAI assistant text with
  `finish_reason: "stop"` unless the final upstream text itself ends otherwise.

Claude route:

- Classify Claude `tools` before calling `map_claude_to_content`.
- Remove native-equivalent tools from local compatibility.
- Do not set `_trigger_signal` unless local fallback tools remain.
- For native-enhanced mode, return normal Claude text content and `end_turn`.

Both routes:

- Continue feeding upstream `message_tool_calls`, `tool_start`, and
  `tool_finish` into `NativeToolAggregator`.
- Do not emit native Tabbit tools as downstream tool calls.
- Keep existing local tool parsing for explicit fallback mode.

## 9. Error Handling

- Required native-equivalent tools do not produce a local tool call. They are
  interpreted as a request for native-enhanced behavior.
- Required local-only tools with fallback disabled return HTTP 400.
- Required local-only tools with fallback enabled but uncertified model return
  HTTP 400.
- Optional local-only or unknown tools with fallback disabled are ignored for
  local compatibility and logged at info level.
- Malformed native tool events should not fail the chat response; they should
  produce safe log fields with partial data.

## 10. Observability

Existing native tool log fields remain:

- `native_tools_count`
- `native_tool_names`
- `native_tools_status`
- `native_tools_duration_ms`
- `native_tools_result_chars`

The initial implementation should also log enough policy detail at info level to
verify routing decisions during tests and live smoke runs:

- `tool_mode`
- `local_tools_enabled`
- `native_equivalent_tools`
- `ignored_local_tools`

Persisting these policy details in Admin UI log entries is out of scope for the
first implementation unless route tests require it.

## 11. Testing Strategy

Unit tests:

- Native-equivalent search tools are classified correctly.
- Local-only tools are classified separately.
- Default policy disables local fallback.
- Required native-equivalent tools become native-enhanced chat instead of local
  tool calls.
- Required local-only tools return 400 when fallback is disabled.
- Local-only tools are allowed only when fallback is enabled and the model is
  certified.

Route tests:

- OpenAI request with `search` tool does not inject local tool prompt.
- Claude request with `web_search` tool does not inject local tool prompt.
- OpenAI and Claude local tool requests require explicit fallback.
- Native upstream `parallel_web_search` events are logged but not emitted as
  downstream executable tool calls.

Smoke tests:

- Run a prompt such as "搜索今天热点科技新闻" against Default or another model
  that triggers Tabbit native search.
- Verify the response is normal text.
- Verify admin logs record `parallel_web_search` native activity.
- Verify no Claude/OpenAI client-side search tool call is emitted.

## 12. Rollout

1. Add classification and policy tests.
2. Implement policy helpers without changing route behavior.
3. Wire OpenAI route to Native-first filtering.
4. Wire Claude route to Native-first filtering.
5. Update README and tool report language.
6. Run full unit, compile, dependency, and native replay verification.

The legacy compatibility path remains available behind explicit fallback, so
deployments that still need local tools can opt in deliberately.
