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


@dataclass(frozen=True)
class ToolModeDecision:
    mode: ToolMode
    local_tools_enabled: bool
    reject: bool = False
    reject_status: int = 0
    reject_detail: str = ""


def decide_tool_mode(
    model: str,
    has_tools: bool,
    required: bool = False,
    certified_models: set[str] | frozenset[str] | None = None,
) -> ToolModeDecision:
    certified = certified_models or DEFAULT_CERTIFIED_LOCAL_TOOL_MODELS
    if not has_tools:
        return ToolModeDecision(
            mode=ToolMode.NATIVE_ENHANCED,
            local_tools_enabled=False,
        )
    if model in certified:
        return ToolModeDecision(
            mode=ToolMode.DUAL,
            local_tools_enabled=True,
        )
    if required:
        return ToolModeDecision(
            mode=ToolMode.NATIVE_ENHANCED,
            local_tools_enabled=False,
            reject=True,
            reject_status=400,
            reject_detail=f"model {model} is not certified for local tool mode",
        )
    return ToolModeDecision(
        mode=ToolMode.NATIVE_ENHANCED,
        local_tools_enabled=False,
    )
