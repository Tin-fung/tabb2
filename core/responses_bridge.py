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
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from core.tabbit_agent import AgentTaskRequest, TabbitAgentClient
from core.tabbit_client import TabbitClient


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
    owns_client: bool = False
    allowed_tools: frozenset[str] = frozenset()
    created_at: float = field(default_factory=time.time)
    touched_at: float = field(default_factory=time.time)
    outcomes: asyncio.Queue[BridgeTurn] = field(default_factory=asyncio.Queue)
    pending_calls: dict[str, PendingRelayCall] = field(default_factory=dict)
    response_ids: set[str] = field(default_factory=set)
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
        text_parts: list[str] = []
        try:
            chat_session_id = await session.client.create_chat_session()
            agent = self._agent_factory(session.client)
            content = build_relay_prompt(prompt, session.bridge_id, tools)
            bootstrap = await agent.bootstrap_task(
                AgentTaskRequest(
                    session_id=chat_session_id,
                    content=content,
                    model=session.model,
                )
            )
            async for event in agent.run_task(bootstrap):
                session.touched_at = time.time()
                if event.type == "execute_content":
                    text = extract_agent_text(event.data)
                    if text:
                        text_parts.append(text)
                elif event.type == "task_completed":
                    final_text = extract_agent_text(event.data)
                    if final_text:
                        text_parts.append(final_text)
                    await session.outcomes.put(
                        BridgeTurn(kind="message", text="".join(text_parts))
                    )
                    return
                elif event.type in {"error", "audit_failure", "task_limit"}:
                    error = extract_agent_error(event.type, event.data)
                    await session.outcomes.put(
                        BridgeTurn(kind="error", error=error)
                    )
                    self._fail_pending_calls(session, error)
                    return
            await session.outcomes.put(
                BridgeTurn(kind="message", text="".join(text_parts))
            )
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
                queued = session.outcomes.get_nowait()
            except asyncio.QueueEmpty:
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

    def submit_outputs(
        self,
        session: BridgeSession,
        outputs: list[tuple[str, str]],
    ) -> None:
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
        return prompt
    relay_rules = (
        "You can call client-side tools through the MCP tool named dispatch. "
        "For every tool call, pass bridge_id exactly as provided, set name to "
        "the requested client tool name, and put its arguments object in "
        "arguments. Never invent a different bridge_id. After dispatch returns, "
        "continue the task using its result.\n\n"
        f"bridge_id: {bridge_id}\n"
        "Available client tools:\n"
        f"{json.dumps(tool_specs, ensure_ascii=False, separators=(',', ':'))}"
    )
    return f"{relay_rules}\n\nUser task:\n{prompt}"


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


def extract_agent_error(event_type: str, data: dict[str, Any]) -> str:
    message = data.get("message") or data.get("error") or data.get("content")
    return f"Tabbit Agent {event_type}: {message or 'unknown error'}"
