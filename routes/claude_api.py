"""
Claude Messages API 路由 (/v1/messages)
为 Claude Code 提供 Anthropic Messages API 兼容端点。
"""

import json
import time
import uuid
import math
import hmac
import logging
from typing import Optional

from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import StreamingResponse

from core.config import ConfigManager
from core.tabbit_client import TabbitClient, resolve_model
from core.token_manager import TokenManager
from core.log_store import LogStore, LogEntry
from core.model_registry import get_registry
from core.tool_events import NativeToolAggregator
from core.tool_policy import decide_tool_mode
from core.claude_compat import (
    random_trigger_signal,
    map_claude_to_content,
    normalize_blocks,
    estimate_tokens,
    ToolifyParser,
    ClaudeSSEWriter,
)

logger = logging.getLogger("tabbit2openai")

router = APIRouter()

_tm: TokenManager | None = None
_cfg: ConfigManager | None = None
_logs: LogStore | None = None


class TTLCache:
    """带 TTL 的缓存，防止内存泄漏"""

    def __init__(self, ttl: int = 3600):
        self._cache: dict[str, tuple[TabbitClient, float]] = {}
        self._ttl = ttl

    def get(self, key: str) -> Optional[TabbitClient]:
        if key in self._cache:
            value, timestamp = self._cache[key]
            if time.time() - timestamp < self._ttl:
                return value
            del self._cache[key]
        return None

    def set(self, key: str, value: TabbitClient):
        self._cache[key] = (value, time.time())

    def cleanup(self):
        """清理过期缓存"""
        now = time.time()
        expired = [k for k, (_, ts) in self._cache.items() if now - ts >= self._ttl]
        for k in expired:
            del self._cache[k]


_fallback_clients = TTLCache(ttl=3600)  # 1 小时过期

def init(token_manager: TokenManager, config: ConfigManager, log_store: LogStore):
    global _tm, _cfg, _logs
    _tm = token_manager
    _cfg = config
    _logs = log_store


def _apply_claude_tool_policy(tabbit_model: str, body: dict) -> list[dict]:
    tools = body.get("tools", []) or []
    decision = decide_tool_mode(
        tabbit_model,
        has_tools=bool(tools),
        required=bool(tools),
    )
    if decision.reject:
        raise HTTPException(
            status_code=decision.reject_status,
            detail=decision.reject_detail,
        )
    if not decision.local_tools_enabled:
        body["tools"] = []
        return []
    return tools


async def _get_client_and_token(
    request: Request,
) -> tuple[TabbitClient, str, str]:
    """获取客户端实例，返回 (client, token_name, token_id)"""
    # 验证客户端 API key（使用 hmac.compare_digest 防止时序攻击）
    api_key = _cfg.get("proxy", "api_key") if _cfg else ""
    auth_header = request.headers.get("x-api-key") or request.headers.get(
        "authorization", ""
    )
    bearer = auth_header.replace("Bearer ", "") if auth_header.startswith("Bearer ") else auth_header

    if _tm and _tm.has_tokens:
        if not api_key:
            raise HTTPException(status_code=401, detail="proxy api key required")
        if api_key and not hmac.compare_digest(bearer, api_key):
            raise HTTPException(status_code=401, detail="invalid api key")
        token_info, client = await _tm.get_next()
        if token_info is None:
            raise HTTPException(
                status_code=503, detail="no available tokens (all cooling down)"
            )
        return client, token_info.get("name", "unknown"), token_info["id"]

    # fallback
    token = bearer
    if not token:
        raise HTTPException(status_code=401, detail="missing token")

    client = _fallback_clients.get(token)
    if client is None:
        client = TabbitClient(
            token,
            _cfg.get("tabbit", "base_url") if _cfg else None,
            _cfg.get("tabbit", "client_id") if _cfg else None,
            _cfg.get("tabbit", "browser_version") if _cfg else None,
            _cfg.get("tabbit", "sparkle_version") if _cfg else None,
            _cfg.get("tabbit", "default_browser", default=True) if _cfg else True,
        )
        _fallback_clients.set(token, client)

    # 定期清理过期缓存（1% 概率触发）
    import random
    if random.random() < 0.01:
        _fallback_clients.cleanup()

    return client, "bearer", ""


def _estimate_input_tokens(body: dict) -> int:
    """估算输入 token 数"""
    total_text = ""
    # system
    system = body.get("system")
    if system:
        if isinstance(system, str):
            total_text += system
        elif isinstance(system, list):
            for b in system:
                if isinstance(b, dict):
                    total_text += b.get("text", "")
    # messages
    for msg in body.get("messages", []):
        content = msg.get("content", "")
        if isinstance(content, str):
            total_text += content
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    total_text += block.get("text", "")
                    total_text += block.get("thinking", "")
                    total_text += str(block.get("content", ""))
    # tools
    tools = body.get("tools", [])
    if tools:
        total_text += json.dumps(tools, ensure_ascii=False)

    return estimate_tokens(total_text)


def _parse_tool_delta_event(
    ed: dict,
    name_map: dict[str, str],
    pending_aliases: dict[str, str],
) -> dict | None:
    tool_call = ed.get("tool_call") if isinstance(ed.get("tool_call"), dict) else ed
    fn = tool_call.get("function") if isinstance(tool_call.get("function"), dict) else {}
    call_id = ed.get("tool_call_id") or ed.get("id") or tool_call.get("id") or f"call_{uuid.uuid4().hex}"
    raw_alias_name = (
        ed.get("tool_call_name")
        or fn.get("name")
        or tool_call.get("name")
        or ""
    )
    if raw_alias_name and raw_alias_name in name_map:
        pending_aliases[call_id] = raw_alias_name
    alias_name = raw_alias_name or pending_aliases.get(call_id, "")
    if not alias_name or alias_name not in name_map:
        return None

    arguments_delta = fn.get("arguments")
    if arguments_delta is None:
        arguments_delta = ed.get("arguments")
    if arguments_delta is not None and not isinstance(arguments_delta, str):
        arguments_delta = json.dumps(arguments_delta, ensure_ascii=False)

    return {
        "id": call_id,
        "name": name_map.get(alias_name, alias_name),
        "arguments_delta": arguments_delta or "",
    }


def _releasable_arguments_delta(
    delta: dict,
    required_by_name: dict[str, set[str]],
    pending_args: dict[str, str],
) -> str | None:
    """Return the delta safe to emit to Claude clients.

    Anthropic clients execute a tool block once it stops. If upstream sends a
    name-only or "{}" delta for a tool with required fields, emitting it early
    creates an invalid local tool call. For required tools, hold deltas until
    the cumulative JSON parses and contains every required field.
    """
    required = required_by_name.get(delta["name"], set())
    if not required:
        return delta["arguments_delta"]

    call_id = delta["id"]
    pending_args[call_id] = pending_args.get(call_id, "") + delta["arguments_delta"]
    try:
        parsed = json.loads(pending_args[call_id] or "{}")
    except Exception:
        return None
    if not isinstance(parsed, dict):
        return None
    missing = [
        key for key in required
        if key not in parsed or parsed.get(key) in (None, "")
    ]
    if missing:
        return None
    return pending_args.pop(call_id)


async def _stream_claude_response(
    client: TabbitClient,
    session_id: str,
    content: str,
    tabbit_model: str,
    body: dict,
    token_name: str,
    token_id: str,
    references: list | None = None,
    task_name: str = "chat",
):
    """流式生成 Claude SSE 响应"""
    request_id = uuid.uuid4().hex[:12]
    model = body.get("model", "claude-proxy")
    input_tokens = _estimate_input_tokens(body)

    writer = ClaudeSSEWriter(request_id, model, input_tokens)
    native_tools = NativeToolAggregator()

    # 解析器配置
    tools = body.get("tools", [])
    has_tools = len(tools) > 0
    trigger_signal = body.get("_trigger_signal")  # 在调用前注入
    thinking_enabled = (
        body.get("thinking", {}).get("type") == "enabled"
        if isinstance(body.get("thinking"), dict)
        else False
    )
    # 别名→原名映射（map_claude_to_content 注入时建的），parser 解析 invoke 后转回原名
    name_map = body.get("_tool_name_map", {})
    required_by_name = {
        t.get("name", ""): set((t.get("input_schema") or {}).get("required") or [])
        for t in tools
        if t.get("name")
    }

    def _valid_tool_event(ev: dict) -> bool:
        if ev.get("type") != "tool_call":
            return True
        call = ev.get("call") or {}
        name = call.get("name", "")
        args = call.get("arguments") or {}
        missing = [
            key for key in required_by_name.get(name, set())
            if key not in args or args.get(key) in (None, "")
        ]
        if missing:
            logger.info(
                "skip invalid tool_call: %s missing=%s args=%s",
                name, ",".join(missing), list(args.keys()),
            )
            return False
        return True

    def _filter_tool_events(events: list[dict]) -> list[dict]:
        return [ev for ev in events if _valid_tool_event(ev)]

    parser = ToolifyParser(trigger_signal, thinking_enabled, name_map=name_map)
    pending_delta_aliases: dict[str, str] = {}
    pending_delta_args: dict[str, str] = {}

    # message_start
    yield writer.init_event()

    start_time = time.time()
    error_msg = ""

    try:
        async for event in client.send_message(session_id, content, tabbit_model, references=references, task_name=task_name):
            et = event["event"]
            ed = event["data"]
            native_tools.consume(et, ed, local_name_map=name_map)

            if et == "message_chunk" and "content" in ed:
                text = ed["content"]
                for char in text:
                    parser.feed_char(char)
                    events = _filter_tool_events(parser.consume_events())
                    if events:
                        for line in writer.handle_events(events):
                            yield line
            elif et == "message_tool_call_delta":
                delta = _parse_tool_delta_event(ed, name_map, pending_delta_aliases)
                if delta:
                    arguments_delta = _releasable_arguments_delta(
                        delta,
                        required_by_name,
                        pending_delta_args,
                    )
                    if arguments_delta is None:
                        continue
                    for line in writer.emit_tool_call_delta(
                        call_id=delta["id"],
                        name=delta["name"],
                        arguments_delta=arguments_delta,
                    ):
                        yield line
            elif et == "message_tool_calls":
                # 上游原生工具调用通道：Tabbit 服务端把工具调用强制走此事件，
                # 不走 <<CALL>> 文本协议（实测：模型调 cc_Write 时大多走这条）。
                # 解析 tool_calls 数组，转成 Claude tool_use block。
                # 先 flush parser 里残留的文本（模型可能在调工具前说了两句）。
                # 注意：只能 flush_text，不能 finish——finish 会触发 end 事件，
                # writer 提前发 message_delta/message_stop，导致 tool_use block
                # 排在 message_stop 之后，Claude Code 收到 stop 就不执行工具了。
                parser.flush_text()
                for ev in _filter_tool_events(parser.consume_events()):
                    for line in writer.handle_events([ev]):
                        yield line
                for tc in ed.get("tool_calls", []):
                    fn = tc.get("function", {})
                    alias_name = fn.get("name", "")
                    # 别名转回原名（cc_Write → Write）
                    orig_name = name_map.get(alias_name, alias_name) if name_map else alias_name
                    try:
                        args = json.loads(fn.get("arguments", "{}")) if isinstance(fn.get("arguments"), str) else fn.get("arguments", {})
                    except Exception:
                        args = {}
                    # 只转发生过的别名工具调用；browser_task_tool 等原生工具不转发
                    # （那些是 Tabbit 内置的，Claude Code 不认）
                    if name_map and alias_name in name_map:
                        missing = [
                            key for key in required_by_name.get(orig_name, set())
                            if key not in args or args.get(key) in (None, "")
                        ]
                        if missing:
                            logger.info(
                                "skip invalid native tool_call: %s missing=%s args=%s",
                                orig_name, ",".join(missing), list(args.keys()),
                            )
                            continue
                        for line in writer.handle_events([{"type": "tool_call", "call": {"id": tc.get("id"), "name": orig_name, "arguments": args}}]):
                            yield line
                        logger.info("native tool_calls → tool_use: %s", orig_name)
            elif et == "finish":
                # finish = 整轮结束。message_finish 只是单条子消息结束（上游可能
                # 连续发多个 message_tool_calls + message_finish，每个工具调用后跟一个
                # message_finish），不能在此 break，否则后续工具调用全丢。
                break

        # 流结束
        parser.finish()
        final_events = _filter_tool_events(parser.consume_events())
        if final_events:
            for line in writer.handle_events(final_events):
                yield line

        if token_id and _tm:
            _tm.report_success(token_id)

    except Exception as e:
        error_msg = str(e)
        if token_id and _tm:
            _tm.report_error(token_id)
        # 尝试发送错误后仍然关闭流
        parser.finish()
        final_events = _filter_tool_events(parser.consume_events())
        if final_events:
            for line in writer.handle_events(final_events):
                yield line
    finally:
        duration = time.time() - start_time
        if _logs:
            _logs.add(
                LogEntry(
                    model=body.get("model", "unknown"),
                    token_name=token_name,
                    stream=True,
                    status="success" if not error_msg else "error",
                    duration=duration,
                    error=error_msg,
                    native_tools=native_tools.to_log_fields(),
                )
            )


@router.post("/v1/messages")
async def claude_messages(request: Request):
    """Anthropic Messages API 兼容端点"""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")

    # 获取客户端
    client, token_name, token_id = await _get_client_and_token(request)

    # 模型映射
    default_model = _cfg.get("claude", "default_model") if _cfg else None
    tabbit_model = resolve_model(body.get("model", "best"), get_registry(), default_model)

    # 工具调用准备
    tools = _apply_claude_tool_policy(tabbit_model, body)
    trigger_signal = random_trigger_signal() if tools else None
    body["_trigger_signal"] = trigger_signal

    # 注入全局 Claude system prompt
    claude_system_prompt = _cfg.get("claude", "system_prompt") if _cfg else ""
    if claude_system_prompt:
        body["_injected_system_prompt"] = claude_system_prompt

    # 构建发送内容（超长时自动分流：content+references 绕过 20421 网关限制）
    content, references, task_name = map_claude_to_content(body, trigger_signal)

    # 创建聊天会话
    try:
        session_id = await client.create_chat_session()
    except Exception as e:
        if token_id and _tm:
            _tm.report_error(token_id)
        if _logs:
            _logs.add(
                LogEntry(
                    model=body.get("model", "unknown"),
                    token_name=token_name,
                    stream=True,
                    status="error",
                    error=str(e),
                )
            )
        raise HTTPException(status_code=502, detail=str(e))

    # Claude Code 总是 stream
    is_stream = body.get("stream", True)
    if is_stream:
        return StreamingResponse(
            _stream_claude_response(
                client, session_id, content, tabbit_model, body, token_name, token_id,
                references=references, task_name=task_name,
            ),
            media_type="text/event-stream",
            headers={
                "cache-control": "no-cache",
                "connection": "keep-alive",
            },
        )

    # 非流式（少见，但仍支持）
    request_id = uuid.uuid4().hex[:12]
    model = body.get("model", "claude-proxy")
    input_tokens = _estimate_input_tokens(body)
    full_text = ""
    start_time = time.time()
    error_msg = ""
    native_tools = NativeToolAggregator()

    try:
        async for event in client.send_message(session_id, content, tabbit_model, references=references, task_name=task_name):
            native_tools.consume(event["event"], event["data"], local_name_map=body.get("_tool_name_map", {}))
            if event["event"] == "message_chunk":
                full_text += event["data"].get("content", "")
        if token_id and _tm:
            _tm.report_success(token_id)
    except Exception as e:
        error_msg = str(e)
        if token_id and _tm:
            _tm.report_error(token_id)
        raise HTTPException(status_code=502, detail=str(e))
    finally:
        duration = time.time() - start_time
        if _logs:
            _logs.add(
                LogEntry(
                    model=model,
                    token_name=token_name,
                    stream=False,
                    status="success" if not error_msg else "error",
                    duration=duration,
                    error=error_msg,
                    native_tools=native_tools.to_log_fields(),
                )
            )

    output_tokens = estimate_tokens(full_text)
    return {
        "id": f"msg_{request_id}",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": full_text}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        },
    }


@router.post("/v1/messages/count_tokens")
async def count_tokens(request: Request):
    """Token 计数端点"""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")

    input_tokens = _estimate_input_tokens(body)
    return {"input_tokens": input_tokens}
