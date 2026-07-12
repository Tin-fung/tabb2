"""OpenAI Responses API bridge backed by Tabbit Agent Task mode."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import time
import uuid
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from core.config import ConfigManager
from core.log_store import LogStore
from core.model_registry import get_registry
from core.responses_bridge import (
    BridgeCallNotFound,
    BridgeSession,
    BridgeSessionNotFound,
    BridgeStartRequest,
    BridgeTurn,
    ResponsesBridge,
    ResponsesBridgeError,
)
from core.tabbit_client import resolve_model
from core.token_manager import TokenManager
from routes import openai_compat


router = APIRouter()
logger = logging.getLogger("tabbit2openai")

_tm: TokenManager | None = None
_cfg: ConfigManager | None = None
_logs: LogStore | None = None
_bridge: ResponsesBridge | None = None


class ResponsesRequest(BaseModel):
    model_config = {"extra": "ignore"}
    model: str = "best"
    input: str | list[Any]
    instructions: str | None = None
    tools: list[dict[str, Any]] | None = None
    previous_response_id: str | None = None
    stream: bool = False


class MCPRequest(BaseModel):
    model_config = {"extra": "ignore"}
    jsonrpc: str = "2.0"
    id: str | int | None = None
    method: str
    params: dict[str, Any] | None = None


def init(token_manager: TokenManager, config: ConfigManager, log_store: LogStore):
    global _tm, _cfg, _logs, _bridge
    _tm = token_manager
    _cfg = config
    _logs = log_store
    _bridge = ResponsesBridge(
        relay_timeout_seconds=config.get(
            "responses", "relay_timeout_seconds", default=300
        ),
        session_ttl_seconds=config.get(
            "responses", "session_ttl_seconds", default=900
        ),
    )


def get_bridge() -> ResponsesBridge:
    if _bridge is None:
        raise RuntimeError("Responses API route is not initialized")
    return _bridge


@router.post("/v1/responses")
async def create_response(
    req: ResponsesRequest,
    authorization: str = Header(None),
):
    bridge = get_bridge()
    outputs = extract_function_call_outputs(req.input)
    try:
        if outputs:
            session = bridge.session_for_continuation(
                previous_response_id=req.previous_response_id,
                call_ids=[call_id for call_id, _ in outputs],
            )
            bridge.submit_outputs(session, outputs)
        else:
            client, token_name, token_id = await openai_compat.get_client_and_token(
                authorization
            )
            default_model = _cfg.get("claude", "default_model") if _cfg else None
            model = resolve_model(req.model, get_registry(), default_model)
            session = await bridge.start(
                BridgeStartRequest(
                    client=client,
                    model=model,
                    requested_model=req.model,
                    prompt=extract_prompt(req.input, req.instructions),
                    tools=req.tools or [],
                    token_id=token_id,
                    token_name=token_name,
                )
            )
    except (BridgeSessionNotFound, BridgeCallNotFound, ResponsesBridgeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    response_id = f"resp_{uuid.uuid4().hex}"
    bridge.bind_response(session, response_id)
    if req.stream:
        return StreamingResponse(
            stream_response(bridge, session, response_id),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    try:
        turn = await bridge.next_turn(session)
    except asyncio.CancelledError:
        await bridge.close_session(session)
        raise
    if turn.kind == "error":
        _report_session_error(session, turn.error)
        raise HTTPException(status_code=502, detail=turn.error)
    _report_session_success(session, turn)
    return build_response(response_id, session.requested_model, turn)


async def stream_response(
    bridge: ResponsesBridge,
    session: BridgeSession,
    response_id: str,
):
    created = response_envelope(
        response_id,
        session.requested_model,
        status="in_progress",
        output=[],
    )
    yield sse_event("response.created", {"response": created, "sequence_number": 0})
    try:
        try:
            turn = await bridge.next_turn(session)
        except asyncio.CancelledError:
            await bridge.close_session(session)
            raise
        if turn.kind == "error":
            _report_session_error(session, turn.error)
            failed = response_envelope(
                response_id, session.requested_model, status="failed", output=[]
            )
            failed["error"] = {"code": "upstream_error", "message": turn.error}
            yield sse_event(
                "response.failed", {"response": failed, "sequence_number": 1}
            )
            return
        _report_session_success(session, turn)
        output = build_output(turn)
        sequence = 1
        for index, item in enumerate(output):
            added = dict(item)
            added["status"] = "in_progress"
            yield sse_event(
                "response.output_item.added",
                {
                    "output_index": index,
                    "item": added,
                    "sequence_number": sequence,
                },
            )
            sequence += 1
            if item["type"] == "function_call":
                yield sse_event(
                    "response.function_call_arguments.done",
                    {
                        "item_id": item["id"],
                        "output_index": index,
                        "arguments": item["arguments"],
                        "sequence_number": sequence,
                    },
                )
                sequence += 1
            elif item["type"] == "message":
                text = item["content"][0]["text"]
                yield sse_event(
                    "response.output_text.delta",
                    {
                        "item_id": item["id"],
                        "output_index": index,
                        "content_index": 0,
                        "delta": text,
                        "sequence_number": sequence,
                    },
                )
                sequence += 1
                yield sse_event(
                    "response.output_text.done",
                    {
                        "item_id": item["id"],
                        "output_index": index,
                        "content_index": 0,
                        "text": text,
                        "sequence_number": sequence,
                    },
                )
                sequence += 1
            yield sse_event(
                "response.output_item.done",
                {
                    "output_index": index,
                    "item": item,
                    "sequence_number": sequence,
                },
            )
            sequence += 1
        completed = response_envelope(
            response_id,
            session.requested_model,
            status="completed",
            output=output,
        )
        yield sse_event(
            "response.completed",
            {"response": completed, "sequence_number": sequence},
        )
    except Exception as exc:
        _report_session_error(session, str(exc))
        failed = response_envelope(
            response_id, session.requested_model, status="failed", output=[]
        )
        failed["error"] = {"code": "bridge_error", "message": str(exc)}
        yield sse_event(
            "response.failed", {"response": failed, "sequence_number": 1}
        )


@router.post("/mcp/relay")
async def mcp_relay(
    request: MCPRequest,
    authorization: str = Header(None),
):
    verify_relay_auth(authorization)
    params = request.params or {}
    logger.info("MCP relay request: method=%s", request.method)
    if request.method == "initialize":
        requested_version = params.get("protocolVersion")
        result = {
            "protocolVersion": requested_version or "2025-03-26",
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "tabb2-responses-relay", "version": "0.1.0"},
        }
        return jsonrpc_result(request.id, result)
    if request.method in {"notifications/initialized", "notifications/cancelled"}:
        return Response(status_code=202)
    if request.method == "ping":
        return jsonrpc_result(request.id, {})
    if request.method == "tools/list":
        return jsonrpc_result(request.id, {"tools": [relay_tool_definition()]})
    if request.method == "tools/call":
        if params.get("name") != "dispatch":
            return jsonrpc_error(request.id, -32602, "unknown relay tool")
        arguments = params.get("arguments")
        if not isinstance(arguments, dict):
            return jsonrpc_error(request.id, -32602, "arguments must be an object")
        logger.info(
            "MCP dispatch request: bridge=%s tool=%s",
            str(arguments.get("bridge_id") or "")[-8:],
            str(arguments.get("name") or ""),
        )
        try:
            result = await get_bridge().relay_call(
                bridge_id=str(arguments.get("bridge_id") or ""),
                name=str(arguments.get("name") or ""),
                arguments=arguments.get("arguments", {}),
            )
        except ResponsesBridgeError as exc:
            return jsonrpc_result(
                request.id,
                {
                    "content": [{"type": "text", "text": str(exc)}],
                    "isError": True,
                },
            )
        return jsonrpc_result(
            request.id,
            {"content": [{"type": "text", "text": result}], "isError": False},
        )
    return jsonrpc_error(request.id, -32601, "method not found")


def verify_relay_auth(authorization: str | None) -> None:
    expected = _cfg.get("responses", "relay_token") if _cfg else ""
    supplied = ""
    if isinstance(authorization, str) and authorization.startswith("Bearer "):
        supplied = authorization[7:]
    if not expected or not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="invalid relay token")


def relay_tool_definition() -> dict[str, Any]:
    return {
        "name": "dispatch",
        "description": (
            "Dispatch one client-side function call and wait for its result. "
            "Use the bridge_id supplied in the task instructions verbatim."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "bridge_id": {"type": "string"},
                "name": {"type": "string"},
                "arguments": {"type": "object"},
            },
            "required": ["bridge_id", "name", "arguments"],
            "additionalProperties": False,
        },
    }


def extract_function_call_outputs(value: str | list[Any]) -> list[tuple[str, str]]:
    if not isinstance(value, list):
        return []
    outputs = []
    for item in value:
        if not isinstance(item, dict) or item.get("type") != "function_call_output":
            continue
        call_id = item.get("call_id")
        if not isinstance(call_id, str) or not call_id:
            raise HTTPException(status_code=400, detail="function_call_output.call_id required")
        output = item.get("output", "")
        if not isinstance(output, str):
            output = json.dumps(output, ensure_ascii=False, separators=(",", ":"))
        outputs.append((call_id, output))
    return outputs


def extract_prompt(value: str | list[Any], instructions: str | None) -> str:
    parts = []
    if instructions:
        parts.append(f"[INSTRUCTIONS]\n{instructions}")
    if isinstance(value, str):
        parts.append(value)
    else:
        for item in value:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "function_call_output":
                continue
            role = item.get("role") or "user"
            text = extract_content_text(item.get("content"))
            if text:
                parts.append(f"[{str(role).upper()}]\n{text}")
    prompt = "\n\n".join(parts).strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="Responses input has no text")
    return prompt


def extract_content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for part in content:
        if isinstance(part, str):
            parts.append(part)
        elif isinstance(part, dict):
            text = part.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "\n".join(parts)


def build_response(response_id: str, model: str, turn: BridgeTurn) -> dict[str, Any]:
    return response_envelope(
        response_id,
        model,
        status="completed",
        output=build_output(turn),
    )


def build_output(turn: BridgeTurn) -> list[dict[str, Any]]:
    if turn.kind == "function_call":
        return [
            {
                "id": f"fc_{uuid.uuid4().hex}",
                "type": "function_call",
                "status": "completed",
                "call_id": call.call_id,
                "name": call.name,
                "arguments": call.arguments,
            }
            for call in turn.function_calls
        ]
    return [
        {
            "id": f"msg_{uuid.uuid4().hex}",
            "type": "message",
            "status": "completed",
            "role": "assistant",
            "content": [
                {
                    "type": "output_text",
                    "annotations": [],
                    "text": turn.text,
                }
            ],
        }
    ]


def response_envelope(
    response_id: str,
    model: str,
    *,
    status: str,
    output: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "id": response_id,
        "object": "response",
        "created_at": int(time.time()),
        "status": status,
        "error": None,
        "incomplete_details": None,
        "instructions": None,
        "model": model,
        "output": output,
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
        "usage": {
            "input_tokens": 0,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": 0,
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": 0,
        },
    }


def sse_event(event_type: str, payload: dict[str, Any]) -> str:
    body = {"type": event_type, **payload}
    return f"event: {event_type}\ndata: {json.dumps(body, ensure_ascii=False, separators=(',', ':'))}\n\n"


def jsonrpc_result(request_id: str | int | None, result: Any) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": result})


def jsonrpc_error(
    request_id: str | int | None,
    code: int,
    message: str,
) -> JSONResponse:
    return JSONResponse(
        {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
    )


def _report_session_success(session: BridgeSession, turn: BridgeTurn) -> None:
    if session.token_id and _tm and turn.kind == "message":
        _tm.report_success(session.token_id)


def _report_session_error(session: BridgeSession, error: str) -> None:
    if session.token_id and _tm:
        _tm.report_error(session.token_id, error)
