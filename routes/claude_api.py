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
from core.tabbit_client import TabbitClient, MODEL_MAP
from core.token_manager import TokenManager
from core.log_store import LogStore, LogEntry
from core.model_registry import get_registry
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

# Claude 模型名 → Tabbit 模型名映射
CLAUDE_MODEL_MAP = {
    "claude-opus-4-6": "best",
    "claude-sonnet-4-6": "best",
    "claude-sonnet-4-5": "best",
    "claude-haiku-4-5": "best",
    "claude-3-5-sonnet": "best",
    "claude-3-5-haiku": "best",
}


def init(token_manager: TokenManager, config: ConfigManager, log_store: LogStore):
    global _tm, _cfg, _logs
    _tm = token_manager
    _cfg = config
    _logs = log_store


def _resolve_tabbit_model(model: str) -> str:
    """将请求中的模型名映射到 Tabbit 模型"""
    # 优先用动态模型注册表
    registry = get_registry()
    if registry and registry.ready:
        # 精确匹配动态清单
        if registry.has_alias(model):
            return registry.resolve(model)
        # Claude 模型名 → best → 动态解析
        for prefix, target in CLAUDE_MODEL_MAP.items():
            if model.startswith(prefix):
                return registry.resolve(target)
        # config 默认模型
        default = _cfg.get("claude", "default_model") if _cfg else None
        if default and registry.has_alias(default):
            return registry.resolve(default)
        return registry.resolve(model)  # 兜底 Default
    # 动态注册表不可用时，用静态 MODEL_MAP
    if model in MODEL_MAP:
        return MODEL_MAP[model]
    for prefix, target in CLAUDE_MODEL_MAP.items():
        if model.startswith(prefix):
            return MODEL_MAP.get(target, "Default")
    default = _cfg.get("claude", "default_model") if _cfg else None
    if default and default in MODEL_MAP:
        return MODEL_MAP[default]
    return "Default"


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
    if time.time() % 100 < 1:
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

    # 解析器配置
    tools = body.get("tools", [])
    has_tools = len(tools) > 0
    trigger_signal = body.get("_trigger_signal")  # 在调用前注入
    thinking_enabled = (
        body.get("thinking", {}).get("type") == "enabled"
        if isinstance(body.get("thinking"), dict)
        else False
    )
    parser = ToolifyParser(trigger_signal, thinking_enabled)

    # message_start
    yield writer.init_event()

    start_time = time.time()
    error_msg = ""

    try:
        async for event in client.send_message(session_id, content, tabbit_model, references=references, task_name=task_name):
            et = event["event"]
            ed = event["data"]

            if et == "message_chunk" and "content" in ed:
                text = ed["content"]
                for char in text:
                    parser.feed_char(char)
                    events = parser.consume_events()
                    if events:
                        for line in writer.handle_events(events):
                            yield line
            elif et in ("message_finish", "finish"):
                break

        # 流结束
        parser.finish()
        final_events = parser.consume_events()
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
        final_events = parser.consume_events()
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
    tabbit_model = _resolve_tabbit_model(body.get("model", "best"))

    # 工具调用准备
    tools = body.get("tools", [])
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

    try:
        async for event in client.send_message(session_id, content, tabbit_model, references=references, task_name=task_name):
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
