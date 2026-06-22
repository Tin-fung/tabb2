import json
import time
import uuid
import hmac
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Header
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from core.tabbit_client import TabbitClient, MODEL_MAP
from core.token_manager import TokenManager
from core.log_store import LogStore, LogEntry
from core.config import ConfigManager
from core.model_registry import get_registry
from core.claude_compat import MAX_CONTENT_LEN, compress_content

logger = logging.getLogger("tabbit2openai")

router = APIRouter()

# 这些在 tabbit2api.py 中通过 app.state 注入
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


class ChatMessage(BaseModel):
    # content 兼容字符串和多模态数组（Cherry Studio 等客户端可能发数组）
    role: str
    content: str | list | None = None

    def text_content(self) -> str:
        """提取纯文本内容，兼容字符串和数组格式"""
        if self.content is None:
            return ""
        if isinstance(self.content, str):
            return self.content
        if isinstance(self.content, list):
            parts = []
            for part in self.content:
                if isinstance(part, str):
                    parts.append(part)
                elif isinstance(part, dict):
                    # OpenAI 多模态: {"type":"text","text":"..."} 或 {"type":"image_url",...}
                    if part.get("type") == "text":
                        parts.append(part.get("text", ""))
            return "\n".join(parts)
        return str(self.content)


class ChatCompletionRequest(BaseModel):
    # 兼容客户端额外字段（temperature/max_tokens/top_p 等），忽略不用即可
    model_config = {"extra": "ignore"}
    model: str = "best"
    messages: list[ChatMessage]
    stream: bool = False


def _build_content(messages: list[ChatMessage]) -> tuple[str, list, str]:
    """构建发送内容，返回 (content, references, task_name)。

    超长时自动分流：System段+最近消息留 content，旧历史入 references，
    绕过 20421 网关限制（与 Claude 端点共用 build_content_with_refs）。
    """
    from core.claude_compat import build_content_with_refs
    system_prompt = _cfg.get("proxy", "system_prompt") if _cfg else ""
    if len(messages) == 1 and not system_prompt:
        # 单条短消息快速路径，但仍要过截断闸（单条也可能超长，如贴大段代码）
        text = messages[0].text_content()
        if len(text) > MAX_CONTENT_LEN:
            # 单条无历史可分流，硬压缩兜底
            text = compress_content([text], MAX_CONTENT_LEN)
        return text, [], "chat"
    parts = []
    if system_prompt:
        parts.append(f"[System]: {system_prompt}")
    for m in messages:
        label = {"user": "User", "assistant": "Assistant", "system": "System"}.get(
            m.role, m.role.capitalize()
        )
        parts.append(f"[{label}]: {m.text_content()}")
    text = "\n\n".join(parts) + "\n\n[Assistant]:"
    # 超长分流：content 过网关，旧历史入 references 绕过限制
    if len(text) > MAX_CONTENT_LEN:
        return build_content_with_refs(parts + ["[Assistant]:"], MAX_CONTENT_LEN)
    return text, [], "chat"


async def _get_client_and_token(
    authorization: str | None,
) -> tuple[TabbitClient, str, str]:
    """返回 (client, token_name, token_id)"""
    # 若 token 池非空，走轮询
    if _tm.has_tokens:
        # 校验 proxy api_key（使用 hmac.compare_digest 防止时序攻击）
        api_key = _cfg.get("proxy", "api_key")
        if api_key:
            bearer = (authorization or "").replace("Bearer ", "")
            if not hmac.compare_digest(bearer, api_key):
                raise HTTPException(status_code=401, detail="invalid api key")
        token_info, client = await _tm.get_next()
        if token_info is None:
            raise HTTPException(
                status_code=503, detail="no available tokens (all cooling down)"
            )
        return client, token_info.get("name", "unknown"), token_info["id"]

    # fallback: 从 Authorization 读 token（向后兼容）
    token = (authorization or "").replace("Bearer ", "")
    if not token:
        raise HTTPException(status_code=401, detail="missing token")

    client = _fallback_clients.get(token)
    if client is None:
        client = TabbitClient(
            token,
            _cfg.get("tabbit", "base_url"),
            _cfg.get("tabbit", "client_id"),
            _cfg.get("tabbit", "browser_version"),
            _cfg.get("tabbit", "sparkle_version"),
            _cfg.get("tabbit", "default_browser", default=True),
        )
        _fallback_clients.set(token, client)

    # 定期清理过期缓存（1% 概率触发）
    if time.time() % 100 < 1:
        _fallback_clients.cleanup()

    return client, "bearer", ""


async def _stream_handler(client, session_id, content, tabbit_model, req_model, completion_id, token_name, token_id, references=None, task_name="chat"):
    start = time.time()
    error_msg = ""
    try:
        yield (
            f"data: {json.dumps({'id': completion_id, 'object': 'chat.completion.chunk', 'choices': [{'index': 0, 'delta': {'role': 'assistant', 'content': ''}, 'finish_reason': None}]})}\n\n"
        )

        async for event in client.send_message(session_id, content, tabbit_model, references=references, task_name=task_name):
            et, ed = event["event"], event["data"]
            if et == "message_chunk" and "content" in ed:
                chunk = {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": ed["content"]},
                            "finish_reason": None,
                        }
                    ],
                }
                yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
            elif et in ("message_finish", "finish"):
                yield (
                    f"data: {json.dumps({'id': completion_id, 'object': 'chat.completion.chunk', 'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]})}\n\n"
                )

        yield "data: [DONE]\n\n"
        if token_id:
            _tm.report_success(token_id)
    except Exception as e:
        error_msg = str(e)
        if token_id:
            _tm.report_error(token_id)
        raise
    finally:
        duration = time.time() - start
        _logs.add(
            LogEntry(
                model=req_model,
                token_name=token_name,
                stream=True,
                status="success" if not error_msg else "error",
                duration=duration,
                error=error_msg,
            )
        )


@router.post("/v1/chat/completions")
async def chat_completions(
    req: ChatCompletionRequest, authorization: str = Header(None)
):
    client, token_name, token_id = await _get_client_and_token(authorization)
    # 优先用动态模型注册表，未命中用静态 MODEL_MAP 兜底
    registry = get_registry()
    if registry and registry.ready:
        tabbit_model = registry.resolve(req.model)
    else:
        tabbit_model = MODEL_MAP.get(req.model.lower(), "Default")
    content, references, task_name = _build_content(req.messages)

    try:
        session_id = await client.create_chat_session()
    except Exception as e:
        if token_id:
            _tm.report_error(token_id)
        _logs.add(
            LogEntry(
                model=req.model,
                token_name=token_name,
                stream=req.stream,
                status="error",
                error=str(e),
            )
        )
        raise HTTPException(status_code=502, detail=str(e))

    completion_id = f"chatcmpl-{uuid.uuid4().hex}"

    if req.stream:
        return StreamingResponse(
            _stream_handler(
                client,
                session_id,
                content,
                tabbit_model,
                req.model,
                completion_id,
                token_name,
                token_id,
                references=references,
                task_name=task_name,
            ),
            media_type="text/event-stream",
        )

    # 非流式
    start = time.time()
    full_text = ""
    error_msg = ""
    try:
        async for event in client.send_message(session_id, content, tabbit_model, references=references, task_name=task_name):
            if event["event"] == "message_chunk":
                full_text += event["data"].get("content", "")
        if token_id:
            _tm.report_success(token_id)
    except Exception as e:
        error_msg = str(e)
        if token_id:
            _tm.report_error(token_id)
        raise HTTPException(status_code=502, detail=str(e))
    finally:
        duration = time.time() - start
        _logs.add(
            LogEntry(
                model=req.model,
                token_name=token_name,
                stream=False,
                status="success" if not error_msg else "error",
                duration=duration,
                error=error_msg,
            )
        )

    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": req.model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": full_text},
                "finish_reason": "stop",
            }
        ],
    }


@router.get("/v1/models")
async def list_models():
    # 优先返回动态拉取的模型清单
    registry = get_registry()
    if registry and registry.ready:
        models = registry.list_models()
        if models:
            return {"object": "list", "data": models}
    # 兜底：静态 MODEL_MAP
    return {
        "object": "list",
        "data": [
            {"id": k, "object": "model", "owned_by": "tabbit"}
            for k in MODEL_MAP.keys()
        ],
    }
