# Dual Tool Plane Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement Dual Tool Plane foundations so tabb2 preserves stable chat, keeps local client tool calls separate from Tabbit native tools, and records native Tabbit tool activity.

**Architecture:** Add a shared tool event module that classifies `cc_` local tools versus Tabbit native tools and aggregates native `message_tool_calls/tool_start/tool_finish` events. Wire that aggregator into Claude and OpenAI streaming paths without exposing native tools as client-executable tool calls. Add backend log fields first; model gating and UI can build on this foundation.

**Tech Stack:** Python 3.11+/FastAPI, standard-library `dataclasses`, existing unittest test suite, existing SSE route code in `routes/claude_api.py` and `routes/openai_compat.py`.

## Global Constraints

- Do not promise full Claude Code or opencode backend equivalence.
- Do not make Tabbit native tools appear as client-executable Bash, Write, Read, Edit, or LS tools.
- Native Tabbit tools are used as upstream enhancement, not exposed as client executable tools.
- Local tool support remains experimental and model-gated.
- The first implementation prioritizes backend correctness over Admin UI presentation.
- Debug native tool streaming is opt-in and secondary to backend logs.
- Do not expose raw Tabbit cookies, tokens, request headers, or full upstream payloads in debug streams.

---

## File Structure

- Create `core/tool_events.py`: shared tool origin classification, JSON argument parsing, native tool aggregation, and log-field serialization.
- Create `tests/test_tool_events.py`: unit tests for local/native classification and native aggregation.
- Modify `core/log_store.py`: add optional native tool summary fields to `LogEntry`.
- Create `tests/test_log_store.py`: regression test that log entries expose native tool fields.
- Modify `routes/claude_api.py`: instantiate `NativeToolAggregator` inside `_stream_claude_response`, feed upstream tool events into it, and include summary fields in request logs.
- Create `tests/test_claude_native_tools.py`: offline route-level test that native tool events are logged but not emitted as Claude `tool_use`.
- Modify `routes/openai_compat.py`: instantiate `NativeToolAggregator` inside `_stream_handler`, feed upstream tool events into it, and include summary fields in request logs.
- Create `tests/test_openai_native_tools.py`: offline route-level test that native tool events are logged but not emitted as OpenAI `tool_calls`.
- Create `core/tool_policy.py`: model capability gate for local client tool simulation.
- Create `tests/test_tool_policy.py`: unit tests for certified and uncertified model decisions.
- Modify `README.md` and `TOOL_USE_REPORT.md`: document Dual Tool Plane positioning and current limits after implementation behavior is in place.

---

### Task 1: Shared Tool Event Model

**Files:**
- Create: `core/tool_events.py`
- Test: `tests/test_tool_events.py`

**Interfaces:**
- Produces: `classify_tool_origin(name: str, local_name_map: dict[str, str] | None = None) -> ToolOrigin`
- Produces: `parse_arguments(raw: object) -> dict`
- Produces: `NativeToolAggregator.consume(event_name: str, data: dict, local_name_map: dict[str, str] | None = None) -> None`
- Produces: `NativeToolAggregator.to_log_fields() -> dict`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_tool_events.py`:

```python
import unittest

from core.tool_events import (
    NativeToolAggregator,
    ToolOrigin,
    classify_tool_origin,
    parse_arguments,
)


class ToolEventClassificationTest(unittest.TestCase):
    def test_classifies_local_alias_tools(self):
        self.assertEqual(
            classify_tool_origin("cc_Write", {"cc_Write": "Write"}),
            ToolOrigin.LOCAL,
        )
        self.assertEqual(classify_tool_origin("cc_Bash"), ToolOrigin.LOCAL)

    def test_classifies_known_tabbit_native_tools(self):
        self.assertEqual(
            classify_tool_origin("parallel_web_search"),
            ToolOrigin.NATIVE,
        )
        self.assertEqual(
            classify_tool_origin("browser_task_tool"),
            ToolOrigin.NATIVE,
        )

    def test_unknown_tool_name_is_unknown(self):
        self.assertEqual(classify_tool_origin("mystery_tool"), ToolOrigin.UNKNOWN)

    def test_parse_arguments_accepts_json_string_and_dict(self):
        self.assertEqual(parse_arguments('{"query":"news"}'), {"query": "news"})
        self.assertEqual(parse_arguments({"query": "news"}), {"query": "news"})
        self.assertEqual(parse_arguments("{broken"), {})


class NativeToolAggregatorTest(unittest.TestCase):
    def test_aggregates_native_tool_lifecycle(self):
        agg = NativeToolAggregator()
        call_id = "call_search_1"

        agg.consume(
            "message_tool_calls",
            {
                "tool_calls": [
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": "parallel_web_search",
                            "arguments": '{"query":"today tech news"}',
                        },
                    }
                ]
            },
            local_name_map={"cc_Write": "Write"},
        )
        agg.consume(
            "tool_start",
            {
                "tool_call_id": call_id,
                "tool_call_name": "parallel_web_search",
            },
        )
        agg.consume(
            "tool_finish",
            {
                "tool_call_id": call_id,
                "tool_call_name": "parallel_web_search",
                "content": "result one\nresult two",
            },
        )

        fields = agg.to_log_fields()

        self.assertEqual(fields["native_tools_count"], 1)
        self.assertEqual(fields["native_tool_names"], ["parallel_web_search"])
        self.assertEqual(fields["native_tools_status"], ["success"])
        self.assertGreater(fields["native_tools_result_chars"], 0)

    def test_ignores_local_alias_tool_calls(self):
        agg = NativeToolAggregator()

        agg.consume(
            "message_tool_calls",
            {
                "tool_calls": [
                    {
                        "id": "call_write",
                        "type": "function",
                        "function": {
                            "name": "cc_Write",
                            "arguments": '{"file_path":"/tmp/a","content":"x"}',
                        },
                    }
                ]
            },
            local_name_map={"cc_Write": "Write"},
        )

        self.assertEqual(agg.to_log_fields()["native_tools_count"], 0)
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
.venv/bin/python -m unittest tests.test_tool_events
```

Expected: FAIL with `ModuleNotFoundError: No module named 'core.tool_events'`.

- [ ] **Step 3: Write minimal implementation**

Create `core/tool_events.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
.venv/bin/python -m unittest tests.test_tool_events
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add core/tool_events.py tests/test_tool_events.py
git commit -m "feat(tools): add native tool event aggregation"
```

---

### Task 2: Native Tool Fields in Request Logs

**Files:**
- Modify: `core/log_store.py`
- Test: `tests/test_log_store.py`

**Interfaces:**
- Consumes: `NativeToolAggregator.to_log_fields() -> dict`
- Produces: `LogEntry(..., native_tools: dict | None = None)`
- Produces: `LogEntry.to_dict()` includes native tool summary fields.

- [ ] **Step 1: Write the failing test**

Create `tests/test_log_store.py`:

```python
import unittest

from core.log_store import LogEntry


class LogEntryNativeToolsTest(unittest.TestCase):
    def test_to_dict_includes_native_tool_summary(self):
        entry = LogEntry(
            model="DeepSeek-V4-Pro",
            token_name="primary",
            stream=True,
            status="success",
            duration=1.25,
            native_tools={
                "native_tools_count": 1,
                "native_tool_names": ["parallel_web_search"],
                "native_tools_status": ["success"],
                "native_tools_duration_ms": 42,
                "native_tools_result_chars": 128,
            },
        )

        data = entry.to_dict()

        self.assertEqual(data["native_tools_count"], 1)
        self.assertEqual(data["native_tool_names"], ["parallel_web_search"])
        self.assertEqual(data["native_tools_status"], ["success"])
        self.assertEqual(data["native_tools_duration_ms"], 42)
        self.assertEqual(data["native_tools_result_chars"], 128)
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
.venv/bin/python -m unittest tests.test_log_store
```

Expected: FAIL with `TypeError: LogEntry.__init__() got an unexpected keyword argument 'native_tools'`.

- [ ] **Step 3: Write minimal implementation**

Modify `core/log_store.py`:

```python
class LogEntry:
    __slots__ = (
        "timestamp",
        "model",
        "token_name",
        "stream",
        "status",
        "duration",
        "error",
        "native_tools",
    )

    def __init__(
        self,
        model: str,
        token_name: str,
        stream: bool,
        status: str = "pending",
        duration: float = 0,
        error: str = "",
        native_tools: dict | None = None,
    ):
        self.timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.model = model
        self.token_name = token_name
        self.stream = stream
        self.status = status
        self.duration = round(duration, 2)
        self.error = error
        self.native_tools = native_tools or {}

    def to_dict(self) -> dict:
        native_tools = {
            "native_tools_count": 0,
            "native_tool_names": [],
            "native_tools_status": [],
            "native_tools_duration_ms": 0,
            "native_tools_result_chars": 0,
        }
        native_tools.update(self.native_tools)
        return {
            "timestamp": self.timestamp,
            "model": self.model,
            "token_name": self.token_name,
            "stream": self.stream,
            "status": self.status,
            "duration": self.duration,
            "error": self.error,
            **native_tools,
        }
```

- [ ] **Step 4: Run test to verify it passes**

Run:

```bash
.venv/bin/python -m unittest tests.test_log_store
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add core/log_store.py tests/test_log_store.py
git commit -m "feat(logs): include native tool summaries"
```

---

### Task 3: Claude Route Native Tool Aggregation

**Files:**
- Modify: `routes/claude_api.py`
- Test: `tests/test_claude_native_tools.py`

**Interfaces:**
- Consumes: `NativeToolAggregator.consume(...)`
- Consumes: `NativeToolAggregator.to_log_fields()`
- Produces: Claude streaming path logs native tool summaries without emitting native `tool_use`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_claude_native_tools.py`:

```python
import unittest

import routes.claude_api as claude_api


class FakeClient:
    async def send_message(self, *args, **kwargs):
        yield {
            "event": "message_tool_calls",
            "data": {
                "tool_calls": [
                    {
                        "id": "call_native",
                        "type": "function",
                        "function": {
                            "name": "parallel_web_search",
                            "arguments": '{"query":"today tech news"}',
                        },
                    }
                ]
            },
        }
        yield {
            "event": "tool_start",
            "data": {
                "tool_call_id": "call_native",
                "tool_call_name": "parallel_web_search",
            },
        }
        yield {
            "event": "tool_finish",
            "data": {
                "tool_call_id": "call_native",
                "tool_call_name": "parallel_web_search",
                "content": "search result text",
            },
        }
        yield {"event": "message_chunk", "data": {"content": "Here is the answer."}}
        yield {"event": "finish", "data": {}}


class FakeLogs:
    def __init__(self):
        self.entries = []

    def add(self, entry):
        self.entries.append(entry)


class ClaudeNativeToolsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.old_tm = claude_api._tm
        self.old_logs = claude_api._logs
        claude_api._tm = None
        claude_api._logs = FakeLogs()

    async def asyncTearDown(self):
        claude_api._tm = self.old_tm
        claude_api._logs = self.old_logs

    async def test_native_tool_is_logged_but_not_emitted_as_tool_use(self):
        body = {"model": "claude-proxy", "messages": [{"role": "user", "content": "search"}]}
        chunks = [
            line
            async for line in claude_api._stream_claude_response(
                FakeClient(),
                "session-1",
                "search",
                "DeepSeek-V4-Pro",
                body,
                "primary",
                "",
            )
        ]
        stream = "".join(chunks)
        log_data = claude_api._logs.entries[0].to_dict()

        self.assertNotIn('"type": "tool_use"', stream)
        self.assertIn("Here is the answer.", stream)
        self.assertEqual(log_data["native_tools_count"], 1)
        self.assertEqual(log_data["native_tool_names"], ["parallel_web_search"])
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
.venv/bin/python -m unittest tests.test_claude_native_tools
```

Expected: FAIL because `native_tools_count` remains `0`.

- [ ] **Step 3: Write minimal implementation**

Modify `routes/claude_api.py`:

```python
from core.tool_events import NativeToolAggregator
```

Inside `_stream_claude_response`, after `writer = ClaudeSSEWriter(...)`:

```python
    native_tools = NativeToolAggregator()
```

Inside the upstream event loop, before local-specific handling:

```python
            native_tools.consume(et, ed, local_name_map=name_map)
```

Inside the `finally` block where `LogEntry` is created:

```python
                    native_tools=native_tools.to_log_fields(),
```

Keep the existing `if name_map and alias_name in name_map` guard for `message_tool_calls`. That guard is what prevents native tools from becoming Claude client `tool_use` blocks.

- [ ] **Step 4: Run test to verify it passes**

Run:

```bash
.venv/bin/python -m unittest tests.test_claude_native_tools
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add routes/claude_api.py tests/test_claude_native_tools.py
git commit -m "feat(claude): log native Tabbit tool events"
```

---

### Task 4: OpenAI Route Native Tool Aggregation

**Files:**
- Modify: `routes/openai_compat.py`
- Test: `tests/test_openai_native_tools.py`

**Interfaces:**
- Consumes: `NativeToolAggregator.consume(...)`
- Consumes: `NativeToolAggregator.to_log_fields()`
- Produces: OpenAI streaming path logs native tool summaries without emitting native `tool_calls`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_openai_native_tools.py`:

```python
import unittest

import routes.openai_compat as openai_compat


class FakeClient:
    async def send_message(self, *args, **kwargs):
        yield {
            "event": "message_tool_calls",
            "data": {
                "tool_calls": [
                    {
                        "id": "call_native",
                        "type": "function",
                        "function": {
                            "name": "parallel_web_search",
                            "arguments": '{"query":"today tech news"}',
                        },
                    }
                ]
            },
        }
        yield {
            "event": "tool_start",
            "data": {
                "tool_call_id": "call_native",
                "tool_call_name": "parallel_web_search",
            },
        }
        yield {
            "event": "tool_finish",
            "data": {
                "tool_call_id": "call_native",
                "tool_call_name": "parallel_web_search",
                "content": "search result text",
            },
        }
        yield {"event": "message_chunk", "data": {"content": "Here is the answer."}}
        yield {"event": "finish", "data": {}}


class FakeLogs:
    def __init__(self):
        self.entries = []

    def add(self, entry):
        self.entries.append(entry)


class OpenAINativeToolsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.old_tm = openai_compat._tm
        self.old_logs = openai_compat._logs
        openai_compat._tm = None
        openai_compat._logs = FakeLogs()

    async def asyncTearDown(self):
        openai_compat._tm = self.old_tm
        openai_compat._logs = self.old_logs

    async def test_native_tool_is_logged_but_not_emitted_as_openai_tool_call(self):
        chunks = [
            line
            async for line in openai_compat._stream_handler(
                FakeClient(),
                "session-1",
                "search",
                "DeepSeek-V4-Pro",
                "gpt-proxy",
                "chatcmpl-test",
                "primary",
                "",
            )
        ]
        stream = "".join(chunks)
        log_data = openai_compat._logs.entries[0].to_dict()

        self.assertNotIn('"tool_calls"', stream)
        self.assertIn("Here is the answer.", stream)
        self.assertEqual(log_data["native_tools_count"], 1)
        self.assertEqual(log_data["native_tool_names"], ["parallel_web_search"])
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
.venv/bin/python -m unittest tests.test_openai_native_tools
```

Expected: FAIL because `native_tools_count` remains `0`.

- [ ] **Step 3: Write minimal implementation**

Modify `routes/openai_compat.py`:

```python
from core.tool_events import NativeToolAggregator
```

Inside `_stream_handler`, after `properties = _properties_by_name(tools or [])`:

```python
    native_tools = NativeToolAggregator()
```

Inside `run_attempt`, before local-specific handling:

```python
            native_tools.consume(et, ed, local_name_map=name_map or {})
```

Inside the `finally` block where `LogEntry` is created:

```python
                native_tools=native_tools.to_log_fields(),
```

Keep the existing `if name_map and alias_name not in name_map: continue` guard for native `message_tool_calls`. That prevents native tools from becoming OpenAI client `tool_calls`.

- [ ] **Step 4: Run test to verify it passes**

Run:

```bash
.venv/bin/python -m unittest tests.test_openai_native_tools
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add routes/openai_compat.py tests/test_openai_native_tools.py
git commit -m "feat(openai): log native Tabbit tool events"
```

---

### Task 5: Model Capability Policy Gate

**Files:**
- Create: `core/tool_policy.py`
- Test: `tests/test_tool_policy.py`
- Modify: `routes/openai_compat.py`
- Modify: `routes/claude_api.py`

**Interfaces:**
- Produces: `ToolModeDecision`
- Produces: `decide_tool_mode(model: str, has_tools: bool, required: bool = False, certified_models: set[str] | None = None) -> ToolModeDecision`
- Consumes: route tool preparation in OpenAI and Claude endpoints.

- [ ] **Step 1: Write the failing policy tests**

Create `tests/test_tool_policy.py`:

```python
import unittest

from core.tool_policy import ToolMode, decide_tool_mode


class ToolPolicyTest(unittest.TestCase):
    def test_certified_model_can_use_dual_mode(self):
        decision = decide_tool_mode("DeepSeek-V4-Pro", has_tools=True, required=True)

        self.assertEqual(decision.mode, ToolMode.DUAL)
        self.assertTrue(decision.local_tools_enabled)
        self.assertFalse(decision.reject)

    def test_uncertified_required_tools_are_rejected(self):
        decision = decide_tool_mode("GPT-5.5", has_tools=True, required=True)

        self.assertTrue(decision.reject)
        self.assertEqual(decision.reject_status, 400)
        self.assertIn("not certified", decision.reject_detail)
        self.assertFalse(decision.local_tools_enabled)

    def test_uncertified_optional_tools_degrade_to_native_enhanced(self):
        decision = decide_tool_mode("GPT-5.5", has_tools=True, required=False)

        self.assertFalse(decision.reject)
        self.assertEqual(decision.mode, ToolMode.NATIVE_ENHANCED)
        self.assertFalse(decision.local_tools_enabled)

    def test_plain_chat_uses_native_enhanced_observation(self):
        decision = decide_tool_mode("GPT-5.5", has_tools=False)

        self.assertFalse(decision.reject)
        self.assertEqual(decision.mode, ToolMode.NATIVE_ENHANCED)
        self.assertFalse(decision.local_tools_enabled)
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
.venv/bin/python -m unittest tests.test_tool_policy
```

Expected: FAIL with `ModuleNotFoundError: No module named 'core.tool_policy'`.

- [ ] **Step 3: Write minimal policy implementation**

Create `core/tool_policy.py`:

```python
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
```

- [ ] **Step 4: Wire policy into OpenAI route**

Modify `routes/openai_compat.py`:

```python
from core.tool_policy import decide_tool_mode
```

Inside `chat_completions`, immediately after `tools = _select_openai_tools(req.tools, req.tool_choice)`:

```python
    tool_required = req.tool_choice == "required"
    if isinstance(req.tool_choice, dict):
        tool_required = True
    tool_decision = decide_tool_mode(
        tabbit_model,
        has_tools=bool(tools),
        required=tool_required,
    )
    if tool_decision.reject:
        raise HTTPException(
            status_code=tool_decision.reject_status,
            detail=tool_decision.reject_detail,
        )
    if not tool_decision.local_tools_enabled:
        tools = []
```

Keep `trigger_signal = random_trigger_signal() if tools else None` after this policy block, so optional tools on uncertified models degrade to chat/native observation.

- [ ] **Step 5: Wire policy into Claude route**

Modify `routes/claude_api.py`:

```python
from core.tool_policy import decide_tool_mode
```

Inside `claude_messages`, immediately after `tools = body.get("tools", [])`:

```python
    tool_decision = decide_tool_mode(
        tabbit_model,
        has_tools=bool(tools),
        required=bool(tools),
    )
    if tool_decision.reject:
        raise HTTPException(
            status_code=tool_decision.reject_status,
            detail=tool_decision.reject_detail,
        )
    if not tool_decision.local_tools_enabled:
        tools = []
        body["tools"] = []
```

Claude tool requests are treated as required because Anthropic clients send
`tools` when they expect a `tool_use` capable model.

- [ ] **Step 6: Run policy tests**

Run:

```bash
.venv/bin/python -m unittest tests.test_tool_policy
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add core/tool_policy.py tests/test_tool_policy.py routes/openai_compat.py routes/claude_api.py
git commit -m "feat(tools): gate local tools by model capability"
```

---

### Task 6: Documentation and Full Verification

**Files:**
- Modify: `README.md`
- Modify: `TOOL_USE_REPORT.md`

**Interfaces:**
- Consumes: completed behavior from Tasks 1-4.
- Produces: user-facing description of Dual Tool Plane behavior.

- [ ] **Step 1: Update README wording**

Add a short section under the feature list:

```markdown
### Tool behavior

tabb2 uses a Dual Tool Plane model:

- Local client tools from Claude Code, opencode, or OpenAI tool callers are best-effort and model-gated.
- Tabbit native tools such as search or browser tasks execute upstream inside Tabbit and are recorded as native tool activity.
- Native Tabbit tools are not exposed as local Bash/Write/Edit/Read tool calls.
```

- [ ] **Step 2: Update TOOL_USE_REPORT**

Append:

```markdown
## 2026-07-04 Dual Tool Plane direction

The project now separates tool behavior into two planes:

- Local Tool Plane: `cc_` aliased client tools returned to Claude/OpenAI clients as tool calls.
- Tabbit Native Tool Plane: upstream Tabbit tools such as `parallel_web_search` collected from SSE lifecycle events and logged as native tool activity.

Native Tabbit tools are not converted into local client tool calls, because they execute on Tabbit upstream rather than in Claude Code/opencode.
```

- [ ] **Step 3: Run full verification**

Run:

```bash
.venv/bin/python -m unittest discover -s tests
python3 -m compileall -q tabbit2api.py core routes scripts tests
.venv/bin/python -m pip check
```

Expected:

- unittest reports all tests OK.
- compileall exits 0 with no output.
- pip check prints `No broken requirements found.`

- [ ] **Step 4: Commit docs and final verification**

```bash
git add README.md TOOL_USE_REPORT.md
git commit -m "docs(tools): document dual tool planes"
```

---

## Self-Review

Spec coverage:

- Chat Core is preserved by adding aggregation without changing text output.
- Local Tool Plane remains separated by the existing `cc_` alias map guards.
- Native Tool Plane is implemented through `NativeToolAggregator`.
- Observability is covered by `LogEntry` native fields.
- Admin UI presentation is intentionally deferred per spec.
- Model capability policy is implemented through `core/tool_policy.py`.

Placeholder scan:

- No placeholder markers or vague edge-case instructions are used.

Type consistency:

- Every task uses `NativeToolAggregator.consume(event_name, data, local_name_map=None)`.
- Every task uses `NativeToolAggregator.to_log_fields()`.
- Every route passes `native_tools=...` into `LogEntry`.
