import json
import time
import uuid
import hmac
import asyncio
import logging
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Header
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from core.tabbit_client import MODEL_MAP, TabbitClient, resolve_model
from core.token_manager import TokenManager
from core.log_store import LogStore, LogEntry
from core.config import ConfigManager
from core.model_registry import get_registry
from core.tool_events import NativeToolAggregator
from core.tool_policy import decide_tool_mode
from core.claude_compat import (
    MAX_CONTENT_LEN,
    ToolifyParser,
    alias_tools,
    build_tool_name_map,
    build_tool_prompt,
    compress_content,
    estimate_tokens,
    random_trigger_signal,
)

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


def _refresh_token_from_client(token_id: str, client: TabbitClient) -> None:
    if not token_id or not _tm:
        return
    refresher = getattr(_tm, "refresh_token_from_client", None)
    if callable(refresher):
        refresher(token_id, client)


class ChatMessage(BaseModel):
    # content 兼容字符串和多模态数组（Cherry Studio 等客户端可能发数组）
    model_config = {"extra": "ignore"}
    role: str
    content: str | list | None = None
    tool_calls: list[dict] | None = None
    tool_call_id: str | None = None

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
    tools: list[dict] | None = None
    tool_choice: Any = None
    stream_options: dict | None = None


def _normalize_openai_tools(tools: list[dict] | None) -> list[dict]:
    """OpenAI tools → 内部 Claude-style tools."""
    normalized = []
    for tool in tools or []:
        if tool.get("type") == "function":
            fn = tool.get("function") or {}
            name = fn.get("name")
            if not name:
                continue
            normalized.append(
                {
                    "name": name,
                    "description": fn.get("description", ""),
                    "input_schema": fn.get("parameters")
                    or {"type": "object", "properties": {}},
                }
            )
        elif tool.get("name"):
            normalized.append(
                {
                    "name": tool.get("name"),
                    "description": tool.get("description", ""),
                    "input_schema": tool.get("input_schema")
                    or tool.get("parameters")
                    or {"type": "object", "properties": {}},
                }
            )
    return normalized


def _select_openai_tools(tools: list[dict] | None, tool_choice: Any = None) -> list[dict]:
    """Normalize OpenAI tools and apply the subset of tool_choice we can enforce.

    Tabbit does not expose a native OpenAI tool_choice control, so the adapter
    enforces choices by deciding which tool definitions are shown to the model.
    """
    normalized = _normalize_openai_tools(tools)
    if not normalized:
        if isinstance(tool_choice, dict):
            raise HTTPException(status_code=400, detail="unknown tool in tool_choice")
        return []

    if tool_choice in (None, "auto", "required"):
        return normalized
    if tool_choice == "none":
        return []

    if isinstance(tool_choice, dict):
        if tool_choice.get("type") != "function":
            raise HTTPException(status_code=400, detail="unsupported tool_choice")
        fn = tool_choice.get("function") or {}
        name = fn.get("name")
        for tool in normalized:
            if tool.get("name") == name:
                return [tool]
        raise HTTPException(status_code=400, detail=f"unknown tool in tool_choice: {name}")

    raise HTTPException(status_code=400, detail="unsupported tool_choice")


def _openai_tool_choice_required(tool_choice: Any = None) -> bool:
    return tool_choice == "required" or isinstance(tool_choice, dict)


def _local_tools_enabled_from_config_or_header(header_value: str | None) -> bool:
    if isinstance(header_value, str) and header_value.strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    ):
        return True
    return bool(_cfg and _cfg.get("proxy", "local_tools_enabled", default=False))


def _apply_openai_tool_policy(
    tabbit_model: str,
    tools: list[dict],
    tool_choice: Any = None,
    local_fallback_enabled: bool = False,
) -> list[dict]:
    decision = decide_tool_mode(
        tabbit_model,
        has_tools=bool(tools),
        required=_openai_tool_choice_required(tool_choice),
        tools=tools,
        local_fallback_enabled=local_fallback_enabled,
    )
    if decision.reject:
        raise HTTPException(
            status_code=decision.reject_status,
            detail=decision.reject_detail,
        )
    if decision.native_equivalent_tools or decision.ignored_local_tools:
        logger.info(
            "openai tool policy: mode=%s local=%s native=%s ignored=%s",
            decision.mode,
            decision.local_tools_enabled,
            decision.native_equivalent_tools,
            decision.ignored_local_tools,
        )
    return decision.selected_tools or []


def _tool_result_text(message: ChatMessage) -> str:
    content = message.text_content()
    status = "error" if content.lower().startswith(("error", "failed")) else "success"
    tool_id = message.tool_call_id or ""
    result = (
        f'<tool_result id="{tool_id}" status="{status}">\n'
        f"{content}\n"
        "</tool_result>"
    )
    if status == "success":
        result += (
            "\nThe tool call above completed successfully. If its output contains "
            "the information requested by the user, answer the user now. Do not "
            "repeat the same write/create/read operation."
        )
    return result


def _assistant_tool_calls_text(
    message: ChatMessage,
    trigger_signal: str | None,
    name_map: dict[str, str],
) -> str:
    parts = []
    if message.text_content():
        parts.append(message.text_content())
    reverse = {v: k for k, v in name_map.items()}
    for call in message.tool_calls or []:
        fn = call.get("function") or {}
        name = fn.get("name", "")
        invoke_name = reverse.get(name, name)
        raw_args = fn.get("arguments") or "{}"
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
        except Exception:
            args = {}
        param_lines = []
        for key, value in (args or {}).items():
            str_val = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            param_lines.append(f'<parameter name="{key}">{str_val}</parameter>')
        trigger = f"{trigger_signal}\n" if trigger_signal else ""
        parts.append(
            f'{trigger}<invoke name="{invoke_name}">\n'
            + "\n".join(param_lines)
            + "\n</invoke>"
        )
    return "\n".join(parts)


def _build_content(
    messages: list[ChatMessage],
    tools: list[dict] | None = None,
    trigger_signal: str | None = None,
    name_map: dict[str, str] | None = None,
) -> tuple[str, list, str]:
    """构建发送内容，返回 (content, references, task_name)。

    超长时自动分流：System段+最近消息留 content，旧历史入 references，
    绕过 20421 网关限制（与 Claude 端点共用 build_content_with_refs）。
    """
    from core.claude_compat import build_content_with_refs
    system_prompt = _cfg.get("proxy", "system_prompt") if _cfg else ""
    name_map = name_map or {}
    if len(messages) == 1 and not system_prompt and not tools:
        # 单条短消息快速路径，但仍要过截断闸（单条也可能超长，如贴大段代码）
        text = messages[0].text_content()
        if len(text) > MAX_CONTENT_LEN:
            # 单条无历史可分流，硬压缩兜底
            text = compress_content([text], MAX_CONTENT_LEN)
        return text, [], "chat"
    parts = []
    if system_prompt:
        parts.append(f"[System]: {system_prompt}")
    if tools and trigger_signal:
        parts.append(f"[System]: {build_tool_prompt(alias_tools(tools), trigger_signal)}")
    for m in messages:
        if m.role == "tool":
            label = "User"
            content = _tool_result_text(m)
        elif m.role == "assistant" and m.tool_calls:
            label = "Assistant"
            content = _assistant_tool_calls_text(m, trigger_signal, name_map)
        else:
            label = {"user": "User", "assistant": "Assistant", "system": "System"}.get(
                m.role, m.role.capitalize()
            )
            content = m.text_content()
        parts.append(f"[{label}]: {content}")
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
        if not api_key:
            raise HTTPException(status_code=401, detail="proxy api key required")
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
    import random
    if random.random() < 0.01:
        _fallback_clients.cleanup()

    return client, "bearer", ""


class OpenAISSEWriter:
    def __init__(
        self,
        completion_id: str,
        model: str,
        input_tokens: int = 0,
        include_usage: bool = False,
    ):
        self.completion_id = completion_id
        self.model = model
        self.created = int(time.time())
        self.input_tokens = max(1, input_tokens)
        self.output_tokens = 0
        self.include_usage = include_usage
        self.has_tool_call = False
        self.finished = False
        self.tool_index = 0
        self.emitted_tool_signatures: set[str] = set()
        self.started_tool_ids: set[str] = set()
        self.tool_arg_buffers: dict[str, str] = {}
        self.pending_text = ""
        self.suppressed_text = False
        self.emitted_content = False

    def init_event(self) -> str:
        return self._chunk(
            [
                {
                    "index": 0,
                    "delta": {"role": "assistant"},
                    "logprobs": None,
                    "finish_reason": None,
                }
            ]
        )

    def handle_events(self, events: list[dict]) -> list[str]:
        output = []
        for event in events:
            etype = event["type"]
            if etype == "text":
                output.extend(self._queue_text(event["content"]))
            elif etype == "tool_call":
                self.has_tool_call = True
                self.pending_text = ""
                output.extend(self.emit_tool_call_final(event["call"]))
            elif etype == "end":
                output.extend(self.finish())
        return output

    def _queue_text(self, text: str) -> list[str]:
        from core.claude_compat import _clean_tool_protocol_residue

        original = text
        text = _clean_tool_protocol_residue(text)
        if not text:
            if original and original.strip():
                self.suppressed_text = True
            return []
        if self.has_tool_call:
            return []
        return [self._content_delta(text)]

    def _flush_pending_text(self) -> list[str]:
        if not self.pending_text or self.has_tool_call:
            self.pending_text = ""
            return []
        text = self.pending_text
        self.pending_text = ""
        return [self._content_delta(text)]

    @staticmethod
    def _stringify_arguments(args: Any) -> str:
        if isinstance(args, str):
            return args
        return json.dumps(args or {}, ensure_ascii=False)

    def emit_tool_call_delta(
        self,
        *,
        call_id: str,
        index: int,
        name: str | None = None,
        arguments_delta: str | None = None,
        call_type: str = "function",
    ) -> list[str]:
        self.has_tool_call = True
        self.pending_text = ""
        call_id = call_id or f"call_{uuid.uuid4().hex}"
        first_delta = call_id not in self.started_tool_ids
        self.started_tool_ids.add(call_id)
        if arguments_delta:
            self.tool_arg_buffers[call_id] = self.tool_arg_buffers.get(call_id, "") + arguments_delta
            self.output_tokens += estimate_tokens(arguments_delta)

        function_delta = {}
        if first_delta and name:
            function_delta["name"] = name
        if arguments_delta:
            function_delta["arguments"] = arguments_delta

        tool_delta = {"index": index, "function": function_delta}
        if first_delta:
            tool_delta.update({"id": call_id, "type": call_type})

        if not function_delta and not first_delta:
            return []

        return [
            self._chunk(
                [
                    {
                        "index": 0,
                        "delta": {"tool_calls": [tool_delta]},
                        "logprobs": None,
                        "finish_reason": None,
                    }
                ]
            )
        ]

    def emit_tool_call_final(self, call: dict) -> list[str]:
        call_id = call.get("id") or f"call_{uuid.uuid4().hex}"
        idx = call.get("index")
        if idx is None:
            idx = self.tool_index
            self.tool_index += 1
        name = call.get("name", "")
        args = self._stringify_arguments(call.get("arguments", {}))

        if call_id in self.started_tool_ids:
            already = self.tool_arg_buffers.get(call_id, "")
            remaining = ""
            if args.startswith(already):
                remaining = args[len(already):]
            elif not already:
                remaining = args
            if remaining:
                return self.emit_tool_call_delta(
                    call_id=call_id,
                    index=idx,
                    arguments_delta=remaining,
                )
            return []

        signature = json.dumps(
            {"name": name, "arguments": call.get("arguments", {})},
            ensure_ascii=False,
            sort_keys=True,
        )
        if signature in self.emitted_tool_signatures:
            logger.info("skip duplicate openai tool_call: %s", name)
            return []
        self.emitted_tool_signatures.add(signature)
        self.output_tokens += estimate_tokens(args)

        return [
            self._chunk(
                [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": idx,
                                    "id": call_id,
                                    "type": "function",
                                    "function": {"name": name, "arguments": args},
                                }
                            ]
                        },
                        "logprobs": None,
                        "finish_reason": None,
                    }
                ]
            )
        ]

    def finish(self) -> list[str]:
        if self.finished:
            return []
        self.finished = True
        lines = []
        if not self.has_tool_call and not self.pending_text and self.suppressed_text:
            self.pending_text = "这轮没有产生有效工具调用，请重试一次。"
        lines.extend(self._flush_pending_text())
        finish_reason = "tool_calls" if self.has_tool_call else "stop"
        lines.append(
            self._chunk(
                [
                    {
                        "index": 0,
                        "delta": {},
                        "logprobs": None,
                        "finish_reason": finish_reason,
                    }
                ]
            )
        )
        if self.include_usage:
            lines.append(self.usage_event())
        return lines

    def _content_delta(self, text: str) -> str:
        if text:
            self.emitted_content = True
            self.output_tokens += estimate_tokens(text)
        return self._chunk(
            [
                {
                    "index": 0,
                    "delta": {"content": text},
                    "logprobs": None,
                    "finish_reason": None,
                }
            ]
        )

    def usage_event(self) -> str:
        usage = {
            "prompt_tokens": self.input_tokens,
            "completion_tokens": max(1, self.output_tokens),
            "total_tokens": self.input_tokens + max(1, self.output_tokens),
        }
        return self._sse(
            {
                "id": self.completion_id,
                "object": "chat.completion.chunk",
                "created": self.created,
                "model": self.model,
                "system_fingerprint": None,
                "choices": [],
                "usage": usage,
            }
        )

    def _chunk(self, choices: list[dict]) -> str:
        return self._sse(
            {
                "id": self.completion_id,
                "object": "chat.completion.chunk",
                "created": self.created,
                "model": self.model,
                "system_fingerprint": None,
                "choices": choices,
            }
        )

    @staticmethod
    def _sse(data: dict) -> str:
        return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"

    def has_pending_or_emitted_output(self) -> bool:
        return bool(
            self.has_tool_call
            or self.pending_text
            or self.emitted_content
            or self.suppressed_text
        )


def _required_by_name(tools: list[dict]) -> dict[str, set[str]]:
    return {
        t.get("name", ""): set((t.get("input_schema") or {}).get("required") or [])
        for t in tools
        if t.get("name")
    }


_ARG_ALIASES = {
    "filePath": ("file_path", "path", "filepath", "file"),
    "file_path": ("filePath", "path", "filepath", "file"),
    "path": ("filePath", "file_path", "filepath", "file"),
    "command": ("cmd", "shell_command", "bash_command"),
    "cmd": ("command", "shell_command", "bash_command"),
}


def _properties_by_name(tools: list[dict]) -> dict[str, set[str]]:
    return {
        t.get("name", ""): set(((t.get("input_schema") or {}).get("properties") or {}).keys())
        for t in tools
        if t.get("name")
    }


def _repair_arguments(name: str, args: dict, properties_by_name: dict[str, set[str]]) -> dict:
    """把模型常吐的参数别名修成客户端 schema 的真实参数名。"""
    if not isinstance(args, dict):
        return {}
    props = properties_by_name.get(name, set())
    if not props:
        return args

    repaired = dict(args)
    for prop in props:
        if prop in repaired and repaired.get(prop) not in (None, ""):
            continue
        for alias in _ARG_ALIASES.get(prop, ()):
            if alias in repaired and repaired.get(alias) not in (None, ""):
                repaired[prop] = repaired[alias]
                logger.info("repair openai tool arg: %s %s<- %s", name, prop, alias)
                break
        if prop == "description" and prop not in repaired and repaired.get("command"):
            repaired[prop] = f"Run shell command: {repaired['command']}"
            logger.info("repair openai tool arg: %s description<- command", name)
    return repaired


def _repair_tool_name(name: str, properties_by_name: dict[str, set[str]]) -> str:
    if name in properties_by_name:
        return name
    lower = (name or "").lower()
    for existing in properties_by_name:
        if existing.lower() == lower:
            logger.info("repair openai tool name: %s -> %s", name, existing)
            return existing
    return name


def _filter_tool_events(
    events: list[dict],
    required_by_name: dict[str, set[str]],
    properties_by_name: dict[str, set[str]],
) -> list[dict]:
    filtered = []
    for ev in events:
        if ev.get("type") != "tool_call":
            filtered.append(ev)
            continue
        call = ev.get("call") or {}
        name = _repair_tool_name(call.get("name", ""), properties_by_name)
        call["name"] = name
        args = _repair_arguments(name, call.get("arguments") or {}, properties_by_name)
        call["arguments"] = args
        ev["call"] = call
        missing = [
            key for key in required_by_name.get(name, set())
            if key not in args or args.get(key) in (None, "")
        ]
        if missing:
            logger.info(
                "skip invalid openai tool_call: %s missing=%s args=%s",
                name,
                ",".join(missing),
                list(args.keys()),
            )
            continue
        filtered.append(ev)
    return filtered


def _estimate_openai_input_tokens(messages: list[ChatMessage], tools: list[dict] | None) -> int:
    text = "\n".join(m.text_content() for m in messages)
    if tools:
        text += "\n" + json.dumps(tools, ensure_ascii=False)
    return estimate_tokens(text)


def _include_stream_usage(req: ChatCompletionRequest) -> bool:
    opts = req.stream_options or {}
    return bool(isinstance(opts, dict) and opts.get("include_usage"))


def _parse_tool_delta_event(ed: dict, name_map: dict[str, str] | None) -> dict | None:
    tool_call = ed.get("tool_call") if isinstance(ed.get("tool_call"), dict) else ed
    fn = tool_call.get("function") if isinstance(tool_call.get("function"), dict) else {}
    alias_name = (
        ed.get("tool_call_name")
        or fn.get("name")
        or tool_call.get("name")
        or ""
    )
    if not name_map or not alias_name or alias_name not in name_map:
        return None
    name = name_map[alias_name]
    arguments_delta = fn.get("arguments")
    if arguments_delta is None:
        arguments_delta = ed.get("arguments")
    if arguments_delta is not None and not isinstance(arguments_delta, str):
        arguments_delta = json.dumps(arguments_delta, ensure_ascii=False)

    try:
        index = int(ed.get("index", 0))
    except Exception:
        index = 0
    call_id = (
        ed.get("tool_call_id")
        or ed.get("id")
        or tool_call.get("id")
        or f"call_{uuid.uuid4().hex}"
    )
    return {
        "id": call_id,
        "index": index,
        "name": name,
        "arguments_delta": arguments_delta or "",
        "type": ed.get("type") or tool_call.get("type") or "function",
    }


def _resolve_model_name(model: str) -> str:
    registry = get_registry()
    if registry and registry.ready:
        return registry.resolve(model)
    return MODEL_MAP.get(model.lower(), model)


def _tool_fallback_model(requested_tabbit_model: str) -> str | None:
    """工具请求空回时的兜底模型。"""
    fallback = None
    if _cfg:
        fallback = _cfg.get("claude", "default_model") or _cfg.get("proxy", "default_model")
    fallback = fallback or "DeepSeek-V4-Pro"
    fallback_model = _resolve_model_name(fallback)
    if fallback_model == requested_tabbit_model:
        return None
    return fallback_model


async def _stream_handler(
    client,
    session_id,
    content,
    tabbit_model,
    req_model,
    completion_id,
    token_name,
    token_id,
    references=None,
    task_name="chat",
    trigger_signal: str | None = None,
    name_map: dict[str, str] | None = None,
    tools: list[dict] | None = None,
    input_tokens: int = 0,
    include_usage: bool = False,
):
    start = time.time()
    error_msg = ""
    writer = OpenAISSEWriter(
        completion_id,
        req_model,
        input_tokens=input_tokens,
        include_usage=include_usage,
    )
    required = _required_by_name(tools or [])
    properties = _properties_by_name(tools or [])
    native_tools = NativeToolAggregator()

    async def run_attempt(attempt_session_id: str, attempt_model: str):
        parser = ToolifyParser(trigger_signal, False, name_map=name_map or {})
        async for event in client.send_message(attempt_session_id, content, attempt_model, references=references, task_name=task_name):
            et, ed = event["event"], event["data"]
            native_tools.consume(et, ed, local_name_map=name_map or {})
            if et == "message_chunk" and "content" in ed:
                text = ed["content"]
                if trigger_signal:
                    for char in text:
                        parser.feed_char(char)
                        events = _filter_tool_events(parser.consume_events(), required, properties)
                        for line in writer.handle_events(events):
                            yield line
                else:
                    for line in writer.handle_events([{"type": "text", "content": text}]):
                        yield line
            elif et == "message_tool_call_delta":
                delta = _parse_tool_delta_event(ed, name_map or {})
                if delta:
                    for line in writer.emit_tool_call_delta(
                        call_id=delta["id"],
                        index=delta["index"],
                        name=delta["name"],
                        arguments_delta=delta["arguments_delta"],
                        call_type=delta["type"],
                    ):
                        yield line
            elif et == "message_tool_calls":
                parser.flush_text()
                events = _filter_tool_events(parser.consume_events(), required, properties)
                for line in writer.handle_events(events):
                    yield line
                for idx, tc in enumerate(ed.get("tool_calls", [])):
                    fn = tc.get("function", {})
                    alias_name = fn.get("name", "")
                    if not name_map or alias_name not in name_map:
                        continue
                    orig_name = (name_map or {}).get(alias_name, alias_name)
                    try:
                        args = json.loads(fn.get("arguments", "{}")) if isinstance(fn.get("arguments"), str) else fn.get("arguments", {})
                    except Exception:
                        args = {}
                    events = _filter_tool_events(
                        [
                            {
                                "type": "tool_call",
                                "call": {
                                    "id": tc.get("id"),
                                    "index": tc.get("index", idx),
                                    "name": orig_name,
                                    "arguments": args,
                                },
                            }
                        ],
                        required,
                        properties,
                    )
                    for line in writer.handle_events(events):
                        yield line
                    if events:
                        logger.info("native tool_calls → openai tool_calls: %s", orig_name)
            elif et == "finish":
                break

        parser.finish()
        events = _filter_tool_events(parser.consume_events(), required, properties)
        events = [ev for ev in events if ev.get("type") != "end"]
        for line in writer.handle_events(events):
            yield line

    try:
        yield writer.init_event()

        async for line in run_attempt(session_id, tabbit_model):
            yield line
        if tools and not writer.has_pending_or_emitted_output():
            fallback_model = _tool_fallback_model(tabbit_model)
            if fallback_model:
                logger.info(
                    "openai tool empty response; retry with fallback model: %s -> %s",
                    tabbit_model,
                    fallback_model,
                )
                fallback_session_id = await client.create_chat_session()
                _refresh_token_from_client(token_id, client)
                async for line in run_attempt(fallback_session_id, fallback_model):
                    yield line
        for line in writer.handle_events([{"type": "end"}]):
            yield line
        yield "data: [DONE]\n\n"
        if token_id:
            _refresh_token_from_client(token_id, client)
            _tm.report_success(token_id)
    except Exception as e:
        error_msg = str(e)
        if token_id:
            _tm.report_error(token_id, e)
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
                native_tools=native_tools.to_log_fields(),
            )
        )


@router.post("/v1/chat/completions")
async def chat_completions(
    req: ChatCompletionRequest,
    authorization: str = Header(None),
    x_tabbit_local_tools: str | None = Header(None),
):
    client, token_name, token_id = await _get_client_and_token(authorization)
    # 模型解析：与 Claude 端点共用 resolve_model，保证映射行为一致
    default_model = _cfg.get("claude", "default_model") if _cfg else None
    tabbit_model = resolve_model(req.model, get_registry(), default_model)
    tools = _select_openai_tools(req.tools, req.tool_choice)
    tools = _apply_openai_tool_policy(
        tabbit_model,
        tools,
        req.tool_choice,
        local_fallback_enabled=_local_tools_enabled_from_config_or_header(
            x_tabbit_local_tools
        ),
    )
    trigger_signal = random_trigger_signal() if tools else None
    name_map = build_tool_name_map(tools) if tools else {}
    content, references, task_name = _build_content(
        req.messages,
        tools=tools,
        trigger_signal=trigger_signal,
        name_map=name_map,
    )
    input_tokens = _estimate_openai_input_tokens(req.messages, tools)

    try:
        session_id = await client.create_chat_session()
        _refresh_token_from_client(token_id, client)
    except Exception as e:
        if token_id:
            _tm.report_error(token_id, e)
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
                trigger_signal=trigger_signal,
                name_map=name_map,
                tools=tools,
                input_tokens=input_tokens,
                include_usage=_include_stream_usage(req),
            ),
            media_type="text/event-stream",
        )

    # 非流式
    start = time.time()
    full_text = ""
    tool_calls = []
    error_msg = ""
    parser = ToolifyParser(trigger_signal, False, name_map=name_map)
    required = _required_by_name(tools)
    properties = _properties_by_name(tools)
    native_tools = NativeToolAggregator()
    try:
        async for event in client.send_message(session_id, content, tabbit_model, references=references, task_name=task_name):
            et, ed = event["event"], event["data"]
            native_tools.consume(et, ed, local_name_map=name_map or {})
            if et == "message_chunk":
                text = ed.get("content", "")
                if trigger_signal:
                    for char in text:
                        parser.feed_char(char)
                        for ev in _filter_tool_events(parser.consume_events(), required, properties):
                            if ev.get("type") == "text":
                                full_text += ev.get("content", "")
                            elif ev.get("type") == "tool_call":
                                call = ev["call"]
                                tool_calls.append(
                                    {
                                        "id": f"call_{uuid.uuid4().hex}",
                                        "type": "function",
                                        "function": {
                                            "name": call.get("name", ""),
                                            "arguments": json.dumps(call.get("arguments", {}), ensure_ascii=False),
                                        },
                                    }
                                )
                else:
                    full_text += text
            elif et == "message_tool_calls":
                parser.flush_text()
                for ev in _filter_tool_events(parser.consume_events(), required, properties):
                    if ev.get("type") == "text":
                        full_text += ev.get("content", "")
                for idx, tc in enumerate(ed.get("tool_calls", [])):
                    fn = tc.get("function", {})
                    alias_name = fn.get("name", "")
                    if not name_map or alias_name not in name_map:
                        continue
                    orig_name = name_map.get(alias_name, alias_name)
                    try:
                        args = json.loads(fn.get("arguments", "{}")) if isinstance(fn.get("arguments"), str) else fn.get("arguments", {})
                    except Exception:
                        args = {}
                    events = _filter_tool_events(
                        [{"type": "tool_call", "call": {"name": orig_name, "arguments": args}}],
                        required,
                        properties,
                    )
                    for ev in events:
                        call = ev["call"]
                        tool_calls.append(
                            {
                                "id": tc.get("id") or f"call_{uuid.uuid4().hex}",
                                "type": "function",
                                "function": {
                                    "name": call.get("name", ""),
                                    "arguments": json.dumps(call.get("arguments", {}), ensure_ascii=False),
                                },
                            }
                        )
            elif et == "finish":
                break
        parser.finish()
        for ev in _filter_tool_events(parser.consume_events(), required, properties):
            if ev.get("type") == "text":
                full_text += ev.get("content", "")
            elif ev.get("type") == "tool_call":
                call = ev["call"]
                tool_calls.append(
                    {
                        "id": f"call_{uuid.uuid4().hex}",
                        "type": "function",
                        "function": {
                            "name": call.get("name", ""),
                            "arguments": json.dumps(call.get("arguments", {}), ensure_ascii=False),
                        },
                    }
                )
        if token_id:
            _refresh_token_from_client(token_id, client)
            _tm.report_success(token_id)
    except Exception as e:
        error_msg = str(e)
        if token_id:
            _tm.report_error(token_id, e)
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
                native_tools=native_tools.to_log_fields(),
            )
        )

    message = {"role": "assistant", "content": full_text}
    finish_reason = "stop"
    if tool_calls:
        message["tool_calls"] = tool_calls
        finish_reason = "tool_calls"

    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": req.model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": finish_reason,
            }
        ],
        "usage": {
            "prompt_tokens": input_tokens,
            "completion_tokens": estimate_tokens(full_text),
            "total_tokens": input_tokens + estimate_tokens(full_text),
        },
        "system_fingerprint": None,
    }


@router.get("/v1/models")
async def list_models():
    """返回动态拉取的模型清单，注册表挂时用落盘快照兜底。

    优先级：动态缓存（TTL 1h）→ 快照（上次成功拉取的清单）→ 503。
    快照是真实拉取过的清单，不是手维护的静态 MODEL_MAP，不会引入过时 id。
    上游全挂 + 无快照（首次启动从未拉到过）才返回 503。
    """
    registry = get_registry()
    # registry 不可用：先尝试刷新（含快照兜底），再判断
    if not registry or not registry.ready:
        if registry:
            # 同步触发一次刷新：成功则就绪，全挂则 refresh_with_retry 内部会读快照
            await registry.refresh_with_retry(retries=1)
    if registry and registry.ready:
        models = registry.list_models()
        if models:
            return {"object": "list", "data": models}
    # 动态 + 快照都拿不到，才 503
    raise HTTPException(
        status_code=503,
        detail="model registry not ready (upstream fetch failed and no snapshot). "
               "Refresh in admin UI: Settings → Models → Refresh.",
    )
