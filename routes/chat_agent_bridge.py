"""Chat Completions adapter for the stateful Tabbit Agent MCP bridge."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass
from typing import Any, AsyncGenerator

from fastapi import HTTPException
from fastapi.responses import StreamingResponse

from core.config import ConfigManager
from core.log_store import LogEntry, LogStore
from core.model_registry import get_registry
from core.responses_bridge import (
    BridgeSession,
    BridgeStartRequest,
    BridgeTurn,
    ResponsesBridge,
    ResponsesBridgeError,
)
from core.tabbit_client import resolve_model
from core.token_manager import TokenManager


_bridge: ResponsesBridge | None = None
_cfg: ConfigManager | None = None
_tm: TokenManager | None = None
_logs: LogStore | None = None


@dataclass(frozen=True)
class ChatStreamContext:
    bridge: ResponsesBridge
    session: BridgeSession
    completion_id: str
    model: str
    token_name: str
    started_at: float
    include_usage: bool


def init(
    bridge: ResponsesBridge,
    token_manager: TokenManager,
    config: ConfigManager,
    log_store: LogStore,
) -> None:
    global _bridge, _cfg, _tm, _logs
    _bridge = bridge
    _cfg = config
    _tm = token_manager
    _logs = log_store


def get_bridge() -> ResponsesBridge:
    if _bridge is None:
        raise RuntimeError("Chat Agent bridge is not initialized")
    return _bridge


def is_initialized() -> bool:
    return _bridge is not None


def should_handle(req: Any) -> bool:
    call_ids = [call_id for call_id, _ in extract_tool_outputs(req.messages)]
    if call_ids and get_bridge().pending_session_for_call_ids(call_ids):
        return True
    return bool(req.tools) and req.tool_choice != "none"


async def handle(req: Any, authorization: str | None):
    bridge = get_bridge()
    start = time.time()
    try:
        session, token_name = await prepare_session(
            bridge, req, authorization
        )
    except HTTPException:
        raise
    except ResponsesBridgeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    completion_id = f"chatcmpl-{uuid.uuid4().hex}"
    include_usage = bool(
        req.stream_options and req.stream_options.get("include_usage")
    )
    if req.stream:
        context = ChatStreamContext(
            bridge=bridge,
            session=session,
            completion_id=completion_id,
            model=req.model,
            token_name=token_name,
            started_at=start,
            include_usage=include_usage,
        )
        return StreamingResponse(
            stream_chat_response(context),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    try:
        turn = await bridge.next_turn(session)
    except asyncio.CancelledError:
        await bridge.close_session(session)
        raise
    if turn.kind == "error":
        report_error(session, turn.error)
        log_turn(req.model, token_name, False, start, turn.error)
        raise HTTPException(status_code=502, detail=turn.error)
    report_success(session, turn)
    log_turn(req.model, token_name, False, start, "")
    return build_chat_completion(completion_id, req.model, turn)


async def prepare_session(
    bridge: ResponsesBridge,
    req: Any,
    authorization: str | None,
) -> tuple[BridgeSession, str]:
    session = continuation_session(bridge, req.messages)
    if session is not None:
        resume_session(bridge, session, req.messages)
        return session, session.token_name or "bridge"
    return await start_session(bridge, req, authorization)


def resume_session(
    bridge: ResponsesBridge,
    session: BridgeSession,
    messages: list[Any],
) -> None:
    outputs = [
        (call_id, output)
        for call_id, output in extract_tool_outputs(messages)
        if call_id in session.pending_calls
    ]
    if not outputs:
        raise HTTPException(status_code=400, detail="no pending tool outputs")
    bridge.submit_outputs(session, outputs)


async def start_session(
    bridge: ResponsesBridge,
    req: Any,
    authorization: str | None,
) -> tuple[BridgeSession, str]:
    tools = select_chat_tools(req.tools or [], req.tool_choice)
    if not tools:
        raise HTTPException(status_code=400, detail="no callable tools selected")
    from routes import openai_compat

    client, token_name, token_id = await openai_compat.get_client_and_token(
        authorization
    )
    default_model = _cfg.get("claude", "default_model") if _cfg else None
    model = resolve_model(req.model, get_registry(), default_model)
    prompt = tool_requirement_prompt(req, extract_chat_prompt(req.messages))
    session = await bridge.start(
        BridgeStartRequest(
            client=client,
            model=model,
            requested_model=req.model,
            prompt=prompt,
            tools=tools,
            token_id=token_id,
            token_name=token_name,
        )
    )
    return session, token_name


def tool_requirement_prompt(req: Any, prompt: str) -> str:
    if req.tool_choice != "required" and not isinstance(req.tool_choice, dict):
        return prompt
    return (
        "[TOOL REQUIREMENT]\nYou must call one of the supplied client tools "
        "before producing a final answer.\n\n" + prompt
    )


def continuation_session(
    bridge: ResponsesBridge,
    messages: list[Any],
) -> BridgeSession | None:
    call_ids = [call_id for call_id, _ in extract_tool_outputs(messages)]
    return bridge.pending_session_for_call_ids(call_ids) if call_ids else None


def extract_tool_outputs(messages: list[Any]) -> list[tuple[str, str]]:
    outputs = []
    for message in messages:
        if getattr(message, "role", "") != "tool":
            continue
        call_id = getattr(message, "tool_call_id", None)
        if not isinstance(call_id, str) or not call_id:
            continue
        outputs.append((call_id, message.text_content()))
    return outputs


def select_chat_tools(tools: list[dict], tool_choice: Any) -> list[dict]:
    valid = [tool for tool in tools if valid_function_tool(tool)]
    if isinstance(tool_choice, dict) and tool_choice.get("type") == "function":
        name = (tool_choice.get("function") or {}).get("name")
        selected = [tool for tool in valid if tool["function"].get("name") == name]
        if selected:
            return selected
        raise HTTPException(status_code=400, detail=f"unknown tool in tool_choice: {name}")
    if tool_choice not in (None, "auto", "required", "none"):
        raise HTTPException(status_code=400, detail="unsupported tool_choice")
    return [] if tool_choice == "none" else valid


def valid_function_tool(tool: dict) -> bool:
    function = tool.get("function") or {}
    return tool.get("type") == "function" and bool(function.get("name"))


def extract_chat_prompt(messages: list[Any]) -> str:
    parts = []
    for message in messages:
        role = getattr(message, "role", "user")
        text = message.text_content()
        if role == "tool":
            call_id = getattr(message, "tool_call_id", "") or "unknown"
            if text:
                parts.append(f"[TOOL RESULT {call_id}]\n{text}")
            continue
        if text:
            parts.append(f"[{str(role).upper()}]\n{text}")
    prompt = "\n\n".join(parts).strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="Chat Completions input has no text")
    return prompt


def build_chat_completion(
    completion_id: str,
    model: str,
    turn: BridgeTurn,
) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": turn.text}
    finish_reason = "stop"
    if turn.kind == "function_call":
        message["content"] = None
        message["tool_calls"] = [chat_tool_call(call) for call in turn.function_calls]
        finish_reason = "tool_calls"
    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "logprobs": None,
                "finish_reason": finish_reason,
            }
        ],
        "usage": zero_usage(),
    }


def chat_tool_call(call: Any) -> dict[str, Any]:
    return {
        "id": call.call_id,
        "type": "function",
        "function": {"name": call.name, "arguments": call.arguments},
    }


async def stream_chat_response(
    context: ChatStreamContext,
) -> AsyncGenerator[str, None]:
    yield chat_chunk(context.completion_id, context.model, {"role": "assistant"})
    try:
        try:
            turn = await context.bridge.next_turn(context.session)
        except asyncio.CancelledError:
            await context.bridge.close_session(context.session)
            raise
        if turn.kind == "error":
            report_error(context.session, turn.error)
            log_turn(
                context.model,
                context.token_name,
                True,
                context.started_at,
                turn.error,
            )
            yield "data: " + json.dumps(
                {"error": {"message": turn.error, "type": "upstream_error"}},
                ensure_ascii=False,
                separators=(",", ":"),
            ) + "\n\n"
            yield "data: [DONE]\n\n"
            return
        report_success(context.session, turn)
        for frame in chat_turn_frames(
            context.completion_id, context.model, turn, context.include_usage
        ):
            yield frame
        log_turn(
            context.model, context.token_name, True, context.started_at, ""
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        report_error(context.session, str(exc))
        log_turn(
            context.model,
            context.token_name,
            True,
            context.started_at,
            str(exc),
        )
        yield "data: " + json.dumps(
            {"error": {"message": str(exc), "type": "bridge_error"}},
            ensure_ascii=False,
            separators=(",", ":"),
        ) + "\n\n"
        yield "data: [DONE]\n\n"


def chat_turn_frames(
    completion_id: str,
    model: str,
    turn: BridgeTurn,
    include_usage: bool = False,
) -> list[str]:
    frames = []
    finish_reason = "stop"
    if turn.kind == "function_call":
        finish_reason = "tool_calls"
        for index, call in enumerate(turn.function_calls):
            frames.append(
                chat_chunk(
                    completion_id,
                    model,
                    {
                        "tool_calls": [
                            {
                                "index": index,
                                **chat_tool_call(call),
                            }
                        ]
                    },
                )
            )
    elif turn.text:
        frames.append(chat_chunk(completion_id, model, {"content": turn.text}))
    frames.append(chat_chunk(completion_id, model, {}, finish_reason))
    if include_usage:
        frames.append(chat_usage_chunk(completion_id, model))
    frames.append("data: [DONE]\n\n")
    return frames


def chat_chunk(
    completion_id: str,
    model: str,
    delta: dict[str, Any],
    finish_reason: str | None = None,
) -> str:
    payload = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": delta,
                "logprobs": None,
                "finish_reason": finish_reason,
            }
        ],
    }
    return encode_sse(payload)


def chat_usage_chunk(completion_id: str, model: str) -> str:
    return encode_sse(
        {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [],
            "usage": zero_usage(),
        }
    )


def encode_sse(payload: dict[str, Any]) -> str:
    return "data: " + json.dumps(
        payload, ensure_ascii=False, separators=(",", ":")
    ) + "\n\n"


def zero_usage() -> dict[str, int]:
    return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}


def report_success(session: BridgeSession, turn: BridgeTurn) -> None:
    if session.token_id and _tm and turn.kind == "message":
        _tm.report_success(session.token_id)


def report_error(session: BridgeSession, error: str) -> None:
    if session.token_id and _tm:
        _tm.report_error(session.token_id, error)


def log_turn(
    model: str,
    token_name: str,
    stream: bool,
    start: float,
    error: str,
) -> None:
    if _logs:
        _logs.add(
            LogEntry(
                model=model,
                token_name=token_name,
                stream=stream,
                status="error" if error else "success",
                duration=time.time() - start,
                error=error,
            )
        )
