from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


LOCAL_TOOL_PREFIX = "cc_"
NATIVE_TOOL_NAMES = frozenset(
    {
        "parallel_web_search",
        "browser_task_tool",
        "memory_search",
        "show_widget",
        "web_search",
    }
)


class ToolOrigin(str, Enum):
    LOCAL = "local"
    NATIVE = "native"
    UNKNOWN = "unknown"


def classify_tool_origin(
    name: str,
    local_name_map: dict[str, str] | None = None,
) -> ToolOrigin:
    if not name:
        return ToolOrigin.UNKNOWN
    if local_name_map and name in local_name_map:
        return ToolOrigin.LOCAL
    if name.startswith(LOCAL_TOOL_PREFIX):
        return ToolOrigin.LOCAL
    if name in NATIVE_TOOL_NAMES:
        return ToolOrigin.NATIVE
    return ToolOrigin.UNKNOWN


def parse_arguments(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw or "{}")
        except Exception:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _extract_result_text(data: dict) -> str:
    for key in ("content", "result", "text", "output"):
        value = data.get(key)
        if isinstance(value, str):
            return value
    return json.dumps(data, ensure_ascii=False)


@dataclass
class NativeToolRecord:
    id: str
    name: str
    arguments: dict = field(default_factory=dict)
    status: str = "pending"
    started_at: float | None = None
    finished_at: float | None = None
    result_text: str = ""

    def result_chars(self) -> int:
        return len(self.result_text or "")

    def duration_ms(self) -> int:
        if self.started_at is None or self.finished_at is None:
            return 0
        return max(0, int((self.finished_at - self.started_at) * 1000))


class NativeToolAggregator:
    def __init__(self):
        self._records: dict[str, NativeToolRecord] = {}

    def consume(
        self,
        event_name: str,
        data: dict,
        local_name_map: dict[str, str] | None = None,
    ) -> None:
        if event_name == "message_tool_calls":
            for tool_call in data.get("tool_calls", []) or []:
                fn = tool_call.get("function") or {}
                name = fn.get("name") or tool_call.get("name") or ""
                if classify_tool_origin(name, local_name_map) != ToolOrigin.NATIVE:
                    continue
                call_id = tool_call.get("id") or data.get("tool_call_id") or name
                record = self._records.get(call_id)
                if record is None:
                    record = NativeToolRecord(id=call_id, name=name)
                    self._records[call_id] = record
                record.name = name
                record.arguments = parse_arguments(fn.get("arguments"))
            return

        if event_name not in ("tool_start", "tool_finish"):
            return

        name = data.get("tool_call_name") or data.get("name") or ""
        if classify_tool_origin(name, local_name_map) != ToolOrigin.NATIVE:
            return
        call_id = data.get("tool_call_id") or data.get("id") or name
        record = self._records.get(call_id)
        if record is None:
            record = NativeToolRecord(id=call_id, name=name)
            self._records[call_id] = record
        record.name = name
        now = time.time()
        if event_name == "tool_start":
            record.status = "running"
            record.started_at = record.started_at or now
        else:
            record.status = "success" if not data.get("error") else "error"
            record.finished_at = now
            record.result_text = _extract_result_text(data)

    def records(self) -> list[NativeToolRecord]:
        return list(self._records.values())

    def to_log_fields(self) -> dict:
        records = self.records()
        return {
            "native_tools_count": len(records),
            "native_tool_names": [r.name for r in records],
            "native_tools_status": [r.status for r in records],
            "native_tools_duration_ms": sum(r.duration_ms() for r in records),
            "native_tools_result_chars": sum(r.result_chars() for r in records),
        }
