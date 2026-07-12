"""Stateful bridge between Tabbit Agent MCP calls and OpenAI Responses.

The Tabbit backend invokes the relay over HTTPS and keeps that MCP request
pending.  A Responses client receives the pending call, executes it locally,
and submits ``function_call_output``.  Resolving the pending HTTP request lets
the original Tabbit Agent task continue without inventing an unverified
WebSocket result message.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from core.tabbit_agent import AgentTaskRequest, TabbitAgentClient
from core.tabbit_client import TabbitClient


CALL_BATCH_WINDOW_SECONDS = 0.05
AGENT_CONTENT_LIMIT = 19_500
AGENT_PROMPT_RESERVE = 8_000
MAX_NATIVE_ROUTE_ATTEMPTS = 2
logger = logging.getLogger("tabbit2openai")


class ResponsesBridgeError(RuntimeError):
    """Base error for invalid or expired bridge operations."""


class BridgeSessionNotFound(ResponsesBridgeError):
    pass


class BridgeCallNotFound(ResponsesBridgeError):
    pass


@dataclass(frozen=True)
class BridgeFunctionCall:
    call_id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class BridgeTurn:
    kind: str
    function_calls: tuple[BridgeFunctionCall, ...] = ()
    text: str = ""
    error: str = ""


@dataclass
class PendingRelayCall:
    call_id: str
    name: str
    arguments: str
    result: asyncio.Future[str]


@dataclass
class BridgeSession:
    bridge_id: str
    model: str
    requested_model: str
    client: TabbitClient
    token_id: str = ""
    token_name: str = ""
    owns_client: bool = False
    allowed_tools: frozenset[str] = frozenset()
    created_at: float = field(default_factory=time.time)
    touched_at: float = field(default_factory=time.time)
    outcomes: asyncio.Queue[BridgeTurn] = field(default_factory=asyncio.Queue)
    pending_calls: dict[str, PendingRelayCall] = field(default_factory=dict)
    response_ids: set[str] = field(default_factory=set)
    dispatch_count: int = 0
    runner: asyncio.Task | None = None
    closed: bool = False


@dataclass(frozen=True)
class BridgeStartRequest:
    client: TabbitClient
    model: str
    requested_model: str
    prompt: str
    tools: list[dict[str, Any]]
    token_id: str = ""
    token_name: str = ""
    owns_client: bool = False


AgentFactory = Callable[[TabbitClient], TabbitAgentClient]


class ResponsesBridge:
    """Own active Agent tasks and their pending relay invocations."""

    def __init__(
        self,
        *,
        agent_factory: AgentFactory | None = None,
        relay_timeout_seconds: int = 300,
        session_ttl_seconds: int = 900,
    ):
        self._agent_factory = agent_factory or TabbitAgentClient
        self._relay_timeout_seconds = max(1, relay_timeout_seconds)
        self._session_ttl_seconds = max(30, session_ttl_seconds)
        self._sessions: dict[str, BridgeSession] = {}
        self._response_sessions: dict[str, str] = {}
        self._call_sessions: dict[str, str] = {}
        self._lock = asyncio.Lock()

    async def start(self, request: BridgeStartRequest) -> BridgeSession:
        await self.cleanup_expired()
        bridge_id = f"bridge_{uuid.uuid4().hex}"
        session = BridgeSession(
            bridge_id=bridge_id,
            model=request.model,
            requested_model=request.requested_model,
            client=request.client,
            token_id=request.token_id,
            token_name=request.token_name,
            owns_client=request.owns_client,
            allowed_tools=frozenset(extract_tool_names(request.tools)),
        )
        async with self._lock:
            self._sessions[bridge_id] = session
        session.runner = asyncio.create_task(
            self._run_agent(session, request.prompt, request.tools),
            name=f"tabb2-responses-{bridge_id}",
        )
        return session

    async def _run_agent(
        self,
        session: BridgeSession,
        prompt: str,
        tools: list[dict[str, Any]],
    ) -> None:
        try:
            retry_native_tools: tuple[str, ...] = ()
            for attempt in range(1, MAX_NATIVE_ROUTE_ATTEMPTS + 1):
                text_parts: list[str] = []
                final_text = ""
                native_tools: set[str] = set()
                dispatch_count_before = session.dispatch_count
                chat_session_id = await session.client.create_chat_session()
                agent = self._agent_factory(session.client)
                content = build_relay_prompt(
                    prompt,
                    session.bridge_id,
                    tools,
                    retry_native_tools=retry_native_tools,
                )
                logger.info(
                    "agent relay prompt prepared: chars=%d tools=%d attempt=%d",
                    len(content),
                    len(session.allowed_tools),
                    attempt,
                )
                bootstrap = await agent.bootstrap_task(
                    AgentTaskRequest(
                        session_id=chat_session_id,
                        content=content,
                        model=session.model,
                    )
                )
                async for event in agent.run_task(bootstrap):
                    session.touched_at = time.time()
                    native_tools.update(
                        extract_native_agent_tool_names(event.type, event.data)
                    )
                    if event.type == "execute_content":
                        text = extract_agent_text(event.data)
                        if text:
                            text_parts.append(text)
                    elif event.type == "task_completed":
                        final_text = extract_agent_text(event.data)
                        break
                    elif event.type in {"error", "audit_failure", "task_limit"}:
                        error = extract_agent_error(event.type, event.data)
                        await session.outcomes.put(
                            BridgeTurn(kind="error", error=error)
                        )
                        self._fail_pending_calls(session, error)
                        return

                merged_text = merge_agent_text("".join(text_parts), final_text)
                dispatched = session.dispatch_count > dispatch_count_before
                if not should_retry_native_route(native_tools, dispatched, merged_text):
                    await session.outcomes.put(
                        BridgeTurn(kind="message", text=merged_text)
                    )
                    return

                retry_native_tools = tuple(sorted(native_tools)) or (
                    "cloud sandbox artifact",
                )
                logger.warning(
                    "agent native route rejected: bridge=%s attempt=%d tools=%s cloud_artifact=%s",
                    session.bridge_id[-8:],
                    attempt,
                    list(retry_native_tools),
                    claims_cloud_artifact(merged_text),
                )
                if attempt == MAX_NATIVE_ROUTE_ATTEMPTS:
                    error = (
                        "Tabbit native sandbox intercepted a client-side tool task; "
                        "no MCP dispatch reached the OpenCode workspace"
                    )
                    await session.outcomes.put(BridgeTurn(kind="error", error=error))
                    return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await session.outcomes.put(BridgeTurn(kind="error", error=str(exc)))
            self._fail_pending_calls(session, str(exc))
        finally:
            session.closed = True
            session.touched_at = time.time()

    async def relay_call(
        self,
        *,
        bridge_id: str,
        name: str,
        arguments: Any,
    ) -> str:
        session = self._sessions.get(bridge_id)
        if session is None or session.closed:
            raise BridgeSessionNotFound("bridge session is missing or closed")
        if not name or not isinstance(name, str):
            raise ResponsesBridgeError("relay tool name is required")
        if name not in session.allowed_tools:
            raise ResponsesBridgeError(f"relay tool is not allowed: {name}")
        arguments_json = normalize_arguments(arguments)
        call_id = f"call_{uuid.uuid4().hex}"
        session.dispatch_count += 1
        logger.info(
            "agent MCP dispatch accepted: bridge=%s tool=%s count=%d",
            bridge_id[-8:],
            name,
            session.dispatch_count,
        )
        loop = asyncio.get_running_loop()
        pending = PendingRelayCall(
            call_id=call_id,
            name=name,
            arguments=arguments_json,
            result=loop.create_future(),
        )
        session.pending_calls[call_id] = pending
        session.touched_at = time.time()
        self._call_sessions[call_id] = bridge_id
        await session.outcomes.put(
            BridgeTurn(
                kind="function_call",
                function_calls=(
                    BridgeFunctionCall(
                        call_id=call_id,
                        name=name,
                        arguments=arguments_json,
                    ),
                ),
            )
        )
        try:
            return await asyncio.wait_for(
                asyncio.shield(pending.result),
                timeout=self._relay_timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            raise ResponsesBridgeError("relay tool result timed out") from exc
        finally:
            session.pending_calls.pop(call_id, None)
            self._call_sessions.pop(call_id, None)
            session.touched_at = time.time()

    async def next_turn(self, session: BridgeSession) -> BridgeTurn:
        first = await session.outcomes.get()
        if first.kind != "function_call":
            return first
        calls = list(first.function_calls)
        while True:
            try:
                queued = await asyncio.wait_for(
                    session.outcomes.get(),
                    timeout=CALL_BATCH_WINDOW_SECONDS,
                )
            except asyncio.TimeoutError:
                break
            if queued.kind == "function_call":
                calls.extend(queued.function_calls)
            else:
                await session.outcomes.put(queued)
                break
        return BridgeTurn(kind="function_call", function_calls=tuple(calls))

    @staticmethod
    def _fail_pending_calls(session: BridgeSession, error: str) -> None:
        for pending in session.pending_calls.values():
            if not pending.result.done():
                pending.result.set_exception(ResponsesBridgeError(error))

    def session_for_continuation(
        self,
        *,
        previous_response_id: str | None,
        call_ids: list[str],
    ) -> BridgeSession:
        bridge_id = None
        if previous_response_id:
            bridge_id = self._response_sessions.get(previous_response_id)
        if bridge_id is None:
            candidates = {
                self._call_sessions[call_id]
                for call_id in call_ids
                if call_id in self._call_sessions
            }
            if len(candidates) == 1:
                bridge_id = candidates.pop()
        session = self._sessions.get(bridge_id or "")
        if session is None:
            raise BridgeSessionNotFound("previous response or call_id is unknown")
        return session

    def pending_session_for_call_ids(
        self,
        call_ids: list[str],
    ) -> BridgeSession | None:
        candidates = {
            self._call_sessions[call_id]
            for call_id in call_ids
            if call_id in self._call_sessions
        }
        if len(candidates) > 1:
            raise ResponsesBridgeError("tool outputs span multiple bridge sessions")
        if not candidates:
            return None
        return self._sessions.get(candidates.pop())

    def submit_outputs(
        self,
        session: BridgeSession,
        outputs: list[tuple[str, str]],
    ) -> None:
        provided = {call_id for call_id, _ in outputs}
        missing = set(session.pending_calls) - provided
        if missing:
            raise BridgeCallNotFound(
                "missing outputs for pending calls: " + ", ".join(sorted(missing))
            )
        for call_id, output in outputs:
            pending = session.pending_calls.get(call_id)
            if pending is None:
                raise BridgeCallNotFound(f"pending call not found: {call_id}")
            if pending.result.done():
                raise BridgeCallNotFound(f"pending call already completed: {call_id}")
            pending.result.set_result(output)
        session.touched_at = time.time()

    def bind_response(self, session: BridgeSession, response_id: str) -> None:
        session.response_ids.add(response_id)
        session.touched_at = time.time()
        self._response_sessions[response_id] = session.bridge_id

    async def cleanup_expired(self) -> None:
        cutoff = time.time() - self._session_ttl_seconds
        expired = [
            session
            for session in self._sessions.values()
            if session.touched_at < cutoff
        ]
        for session in expired:
            await self.close_session(session)

    async def close_session(self, session: BridgeSession) -> None:
        self._sessions.pop(session.bridge_id, None)
        for response_id in session.response_ids:
            self._response_sessions.pop(response_id, None)
        for call_id, pending in list(session.pending_calls.items()):
            self._call_sessions.pop(call_id, None)
            if not pending.result.done():
                pending.result.set_exception(
                    BridgeSessionNotFound("bridge session closed")
                )
        session.pending_calls.clear()
        if session.runner and not session.runner.done():
            session.runner.cancel()
            await asyncio.gather(session.runner, return_exceptions=True)
        if session.owns_client:
            await session.client.client.aclose()
        session.closed = True

    async def close_all(self) -> None:
        for session in list(self._sessions.values()):
            await self.close_session(session)


def normalize_arguments(arguments: Any) -> str:
    if isinstance(arguments, str):
        try:
            decoded = json.loads(arguments)
        except json.JSONDecodeError as exc:
            raise ResponsesBridgeError("relay arguments must be valid JSON") from exc
        return json.dumps(decoded, ensure_ascii=False, separators=(",", ":"))
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, (dict, list)):
        raise ResponsesBridgeError("relay arguments must be an object or array")
    return json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))


def build_relay_prompt(
    prompt: str,
    bridge_id: str,
    tools: list[dict[str, Any]],
    *,
    retry_native_tools: tuple[str, ...] = (),
) -> str:
    tool_specs = []
    for tool in tools:
        if tool.get("type") != "function":
            continue
        name = tool.get("name") or (tool.get("function") or {}).get("name")
        if not name:
            continue
        source = tool.get("function") or tool
        tool_specs.append(
            {
                "name": name,
                "description": source.get("description", ""),
                "parameters": source.get("parameters")
                or {"type": "object", "properties": {}},
            }
        )
    if not tool_specs:
        return truncate_middle(prompt, AGENT_CONTENT_LIMIT)
    retry_notice = ""
    if retry_native_tools:
        retry_notice = (
            "A previous attempt incorrectly used these Tabbit cloud tools: "
            f"{', '.join(retry_native_tools)}. Do not use them again. "
            "Retry the original task through MCP dispatch.\n"
        )
    relay_header = (
        "[LOCAL TOOL ROUTING - MANDATORY]\n"
        "Client tools are authoritative and execute in the user's actual OpenCode "
        "workspace. For filesystem, repository, shell, command, code execution, "
        "test, build, or git operations, you MUST call the MCP tool named dispatch. "
        "Do not use Tabbit built-in sandbox, E2B, code interpreter, terminal, "
        "browser, computer, or filesystem tools as substitutes. Never create or "
        "claim to create client files under /mnt/work, /tmp, or another cloud "
        "sandbox. Do not claim a local side effect unless dispatch returned success.\n"
        f"{retry_notice}"
        "For every client tool call, pass bridge_id exactly as provided, set name "
        "to the requested client tool name, and put its arguments object in "
        "arguments. Never invent a different bridge_id. After dispatch returns, "
        "continue the task using its result.\n\n"
        f"bridge_id: {bridge_id}\n"
        "Available client tools:\n"
    )
    user_prefix = "\n\nUser task:\n"
    prompt_budget = min(len(prompt), AGENT_PROMPT_RESERVE)
    fitted_prompt = truncate_middle(prompt, prompt_budget)
    tool_budget = max(
        256,
        AGENT_CONTENT_LIMIT
        - len(relay_header)
        - len(user_prefix)
        - len(fitted_prompt),
    )
    tool_catalog = fit_tool_catalog(tool_specs, tool_budget)
    content = f"{relay_header}{tool_catalog}{user_prefix}{fitted_prompt}"
    if len(content) > AGENT_CONTENT_LIMIT:
        fitted_prompt = truncate_middle(
            fitted_prompt,
            max(0, len(fitted_prompt) - (len(content) - AGENT_CONTENT_LIMIT)),
        )
        content = f"{relay_header}{tool_catalog}{user_prefix}{fitted_prompt}"
    return content[:AGENT_CONTENT_LIMIT]


def fit_tool_catalog(tool_specs: list[dict[str, Any]], budget: int) -> str:
    candidates = [
        tool_specs,
        [compact_tool_spec(spec) for spec in tool_specs],
        [tool_signature(spec) for spec in tool_specs],
        {"tool_names": [spec["name"] for spec in tool_specs]},
    ]
    for candidate in candidates:
        encoded = json.dumps(candidate, ensure_ascii=False, separators=(",", ":"))
        if len(encoded) <= budget:
            return encoded
    names = []
    for spec in tool_specs:
        candidate = json.dumps(
            {"tool_names": [*names, spec["name"]], "truncated": True},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        if len(candidate) > budget:
            break
        names.append(spec["name"])
    return json.dumps(
        {"tool_names": names, "truncated": True},
        ensure_ascii=False,
        separators=(",", ":"),
    )


def compact_tool_spec(spec: dict[str, Any]) -> dict[str, Any]:
    parameters = spec.get("parameters") or {}
    properties = parameters.get("properties") or {}
    compact_properties = {}
    for name, definition in properties.items():
        compact_properties[name] = compact_property(definition)
    result = {
        "name": spec["name"],
        "parameters": {
            "properties": compact_properties,
            "required": parameters.get("required") or [],
        },
    }
    description = str(spec.get("description") or "").strip()
    if description:
        result["description"] = description[:160]
    return result


def compact_property(definition: Any) -> dict[str, Any]:
    if not isinstance(definition, dict):
        return {"type": "any"}
    item = {"type": schema_type(definition)}
    description = str(definition.get("description") or "").strip()
    if description:
        item["description"] = description[:80]
    enum = definition.get("enum")
    if isinstance(enum, list) and enum:
        item["enum"] = enum[:8]
    return item


def tool_signature(spec: dict[str, Any]) -> dict[str, Any]:
    parameters = spec.get("parameters") or {}
    properties = parameters.get("properties") or {}
    return {
        "name": spec["name"],
        "arguments": {
            name: schema_type(definition) if isinstance(definition, dict) else "any"
            for name, definition in properties.items()
        },
        "required": parameters.get("required") or [],
    }


def schema_type(definition: dict[str, Any]) -> str:
    value = definition.get("type") or "any"
    if isinstance(value, list):
        return "|".join(str(item) for item in value)
    return str(value)


def truncate_middle(text: str, limit: int) -> str:
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    marker = "\n...[middle truncated to fit Tabbit gateway]...\n"
    if limit <= len(marker):
        return text[-limit:]
    head = (limit - len(marker)) // 3
    tail = limit - len(marker) - head
    return text[:head] + marker + text[-tail:]


def extract_tool_names(tools: list[dict[str, Any]]) -> list[str]:
    names = []
    for tool in tools:
        if tool.get("type") != "function":
            continue
        name = tool.get("name") or (tool.get("function") or {}).get("name")
        if isinstance(name, str) and name:
            names.append(name)
    return names


def extract_agent_text(data: dict[str, Any]) -> str:
    for key in ("content", "text", "execute_content", "result", "answer"):
        value = data.get(key)
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            nested = extract_agent_text(value)
            if nested:
                return nested
    return ""


def merge_agent_text(streamed: str, final_text: str) -> str:
    if not final_text:
        return streamed
    if not streamed or final_text.startswith(streamed):
        return final_text
    if streamed.endswith(final_text):
        return streamed
    return streamed + final_text


def extract_agent_tool_names(event_type: str, data: dict[str, Any]) -> set[str]:
    if event_type == "message_tool_calls":
        return extract_tool_call_list_names(data.get("tool_calls"))
    if event_type == "message_tool_call_delta":
        return compact_name_set(extract_tool_call_name(data))
    if event_type in {"tool_start", "tool_finish"}:
        return compact_name_set(data.get("tool_call_name") or data.get("name"))
    return set()


def extract_tool_call_list_names(value: Any) -> set[str]:
    if not isinstance(value, list):
        return set()
    return {
        name
        for tool_call in value
        if isinstance(tool_call, dict)
        for name in compact_name_set(extract_tool_call_name(tool_call))
    }


def extract_tool_call_name(tool_call: dict[str, Any]) -> Any:
    function = tool_call.get("function")
    function_name = function.get("name") if isinstance(function, dict) else None
    return function_name or tool_call.get("tool_call_name") or tool_call.get("name")


def compact_name_set(value: Any) -> set[str]:
    return {value} if isinstance(value, str) and value else set()


def extract_native_agent_tool_names(event_type: str, data: dict[str, Any]) -> set[str]:
    return {
        name
        for name in extract_agent_tool_names(event_type, data)
        if not is_dispatch_tool_name(name)
    }


def is_dispatch_tool_name(name: str) -> bool:
    normalized = name.strip().lower()
    return normalized == "dispatch" or normalized.endswith(("__dispatch", ".dispatch"))


def claims_cloud_artifact(text: str) -> bool:
    lowered = text.lower()
    if not any(marker in lowered for marker in ("/mnt/work", "e2b", "cloud sandbox")):
        return False
    action_markers = (
        "created",
        "generated",
        "saved",
        "written",
        "located",
        "file",
        "已创建",
        "已生成",
        "已保存",
        "写入",
        "文件",
    )
    return any(marker in lowered for marker in action_markers)


def should_retry_native_route(
    native_tools: set[str],
    dispatched: bool,
    final_text: str,
) -> bool:
    if dispatched:
        return False
    return bool(native_tools) or claims_cloud_artifact(final_text)


def extract_agent_error(event_type: str, data: dict[str, Any]) -> str:
    message = data.get("message") or data.get("error") or data.get("content")
    return f"Tabbit Agent {event_type}: {message or 'unknown error'}"
