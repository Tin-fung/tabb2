from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


DEFAULT_CERTIFIED_LOCAL_TOOL_MODELS = frozenset(
    {
        "Kimi-K2.7-Code",
        "MiniMax-M3",
        "DeepSeek-V4-Pro",
        "DeepSeek-V4-Flash",
        "Kimi-K2.6",
        "GLM-5.1",
        "GPT-5.2-Chat",
        "Claude-Haiku-4.5",
        "DeepSeek-V3.2",
        "Kimi-K2.5",
        "Qwen3.5-Plus",
        "Doubao-Seed-1.8",
    }
)


class ToolMode(str, Enum):
    CHAT_ONLY = "chat_only"
    LOCAL_TOOLS = "local_tools"
    NATIVE_ENHANCED = "native_enhanced"
    DUAL = "dual"


class ToolKind(str, Enum):
    NATIVE_EQUIVALENT = "native_equivalent"
    LOCAL_ONLY = "local_only"
    UNKNOWN = "unknown"


NATIVE_EQUIVALENT_TOOL_NAMES = frozenset(
    {
        "search",
        "web_search",
        "parallel_web_search",
        "browser",
        "browser_task",
        "browser_task_tool",
        "fetch",
        "fetch_url",
        "web_fetch",
        "memory",
        "memory_search",
        "show_widget",
        "widget",
    }
)

LOCAL_ONLY_TOOL_NAMES = frozenset(
    {
        "read",
        "write",
        "edit",
        "multiedit",
        "multi_edit",
        "bash",
        "ls",
        "glob",
        "grep",
        "todowrite",
        "todo_write",
    }
)


def _tool_name(tool: dict) -> str:
    return str(tool.get("name") or "")


def classify_client_tool(name: str, description: str = "") -> ToolKind:
    normalized = (name or "").strip().lower().replace("-", "_")
    if normalized in NATIVE_EQUIVALENT_TOOL_NAMES:
        return ToolKind.NATIVE_EQUIVALENT
    desc = (description or "").lower()
    if any(
        word in normalized or word in desc
        for word in ("search", "browser", "fetch", "memory", "widget")
    ):
        return ToolKind.NATIVE_EQUIVALENT
    if normalized in LOCAL_ONLY_TOOL_NAMES:
        return ToolKind.LOCAL_ONLY
    return ToolKind.UNKNOWN


@dataclass(frozen=True)
class ToolModeDecision:
    mode: ToolMode
    local_tools_enabled: bool
    selected_tools: list[dict] | None = None
    native_equivalent_tools: list[str] | None = None
    ignored_local_tools: list[str] | None = None
    reject: bool = False
    reject_status: int = 0
    reject_detail: str = ""


def decide_tool_mode(
    model: str,
    has_tools: bool,
    required: bool = False,
    certified_models: set[str] | frozenset[str] | None = None,
    tools: list[dict] | None = None,
    local_fallback_enabled: bool = False,
) -> ToolModeDecision:
    certified = certified_models or DEFAULT_CERTIFIED_LOCAL_TOOL_MODELS
    if not has_tools:
        return ToolModeDecision(
            mode=ToolMode.NATIVE_ENHANCED,
            local_tools_enabled=False,
            selected_tools=[],
            native_equivalent_tools=[],
            ignored_local_tools=[],
        )

    selected_tools = []
    native_equivalent_tools = []
    local_or_unknown_tools = []
    for tool in tools or []:
        name = _tool_name(tool)
        kind = classify_client_tool(name, str(tool.get("description") or ""))
        if kind == ToolKind.NATIVE_EQUIVALENT:
            native_equivalent_tools.append(name)
        else:
            selected_tools.append(tool)
            local_or_unknown_tools.append(name)

    if not selected_tools:
        return ToolModeDecision(
            mode=ToolMode.NATIVE_ENHANCED,
            local_tools_enabled=False,
            selected_tools=[],
            native_equivalent_tools=native_equivalent_tools,
            ignored_local_tools=[],
        )

    if not local_fallback_enabled:
        if required:
            return ToolModeDecision(
                mode=ToolMode.NATIVE_ENHANCED,
                local_tools_enabled=False,
                selected_tools=[],
                native_equivalent_tools=native_equivalent_tools,
                ignored_local_tools=local_or_unknown_tools,
                reject=True,
                reject_status=400,
                reject_detail="local tool mode is disabled by default",
            )
        return ToolModeDecision(
            mode=ToolMode.NATIVE_ENHANCED,
            local_tools_enabled=False,
            selected_tools=[],
            native_equivalent_tools=native_equivalent_tools,
            ignored_local_tools=local_or_unknown_tools,
        )

    if model in certified:
        return ToolModeDecision(
            mode=ToolMode.DUAL,
            local_tools_enabled=True,
            selected_tools=selected_tools,
            native_equivalent_tools=native_equivalent_tools,
            ignored_local_tools=[],
        )
    if required:
        return ToolModeDecision(
            mode=ToolMode.NATIVE_ENHANCED,
            local_tools_enabled=False,
            selected_tools=[],
            native_equivalent_tools=native_equivalent_tools,
            ignored_local_tools=local_or_unknown_tools,
            reject=True,
            reject_status=400,
            reject_detail=f"model {model} is not certified for local tool mode",
        )
    return ToolModeDecision(
        mode=ToolMode.NATIVE_ENHANCED,
        local_tools_enabled=False,
        selected_tools=[],
        native_equivalent_tools=native_equivalent_tools,
        ignored_local_tools=local_or_unknown_tools,
    )
