"""Replay a Tabbit native-tool SSE lifecycle through the shared aggregator.

This is an offline smoke test for the Native Tool Plane. It does not require a
live Tabbit token or a running local server. The fixture mirrors the important
shape captured from Tabbit native search:

  message_tool_calls -> tool_start -> tool_finish

Usage:
  .venv/bin/python scripts/verify_native_tool_replay.py
  .venv/bin/python scripts/verify_native_tool_replay.py --json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Iterable

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.tool_events import NativeToolAggregator  # noqa: E402


SAMPLE_NATIVE_TOOL_EVENTS = [
    {
        "event": "message_tool_calls",
        "data": {
            "tool_calls": [
                {
                    "id": "call_native_search_1",
                    "type": "function",
                    "function": {
                        "name": "parallel_web_search",
                        "arguments": json.dumps(
                            {"query": "today technology news"},
                            ensure_ascii=False,
                        ),
                    },
                }
            ]
        },
    },
    {
        "event": "tool_start",
        "data": {
            "tool_call_id": "call_native_search_1",
            "tool_call_name": "parallel_web_search",
        },
    },
    {
        "event": "tool_finish",
        "data": {
            "tool_call_id": "call_native_search_1",
            "tool_call_name": "parallel_web_search",
            "content": (
                "Search result 1: model vendors announced new coding features.\n"
                "Search result 2: browser-native AI assistants expanded tool support."
            ),
        },
    },
]


def replay_native_tool_events(events: Iterable[dict]) -> dict:
    aggregator = NativeToolAggregator()
    for event in events:
        aggregator.consume(
            event.get("event", ""),
            event.get("data", {}) or {},
            local_name_map={},
        )
    return aggregator.to_log_fields()


def validate_native_tool_summary(summary: dict) -> None:
    names = summary.get("native_tool_names") or []
    statuses = summary.get("native_tools_status") or []
    if summary.get("native_tools_count") != 1:
        raise AssertionError("expected exactly one parallel_web_search native tool event")
    if "parallel_web_search" not in names:
        raise AssertionError("expected parallel_web_search native tool")
    if statuses != ["success"]:
        raise AssertionError(f"expected successful native tool status, got {statuses}")
    if summary.get("native_tools_result_chars", 0) <= 0:
        raise AssertionError("expected non-empty native tool result")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Replay Tabbit native-tool SSE events through NativeToolAggregator.",
    )
    parser.add_argument("--json", action="store_true", help="print JSON summary")
    args = parser.parse_args(argv)

    summary = replay_native_tool_events(SAMPLE_NATIVE_TOOL_EVENTS)
    validate_native_tool_summary(summary)

    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print("Native tool replay smoke ok")
        print(f"tools: {', '.join(summary['native_tool_names'])}")
        print(f"status: {', '.join(summary['native_tools_status'])}")
        print(f"result_chars: {summary['native_tools_result_chars']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
