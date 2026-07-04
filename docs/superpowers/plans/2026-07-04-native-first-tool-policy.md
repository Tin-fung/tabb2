# Native-First Tool Policy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Tabbit native tools the default behavior and keep local client tool simulation disabled unless explicitly enabled.

**Architecture:** Add native-equivalent/local-only classification to `core/tool_policy.py`, return a filtered tool decision, and wire OpenAI/Claude routes so only explicit fallback tools reach the legacy `cc_` prompt path. Native Tabbit SSE events remain observed by `NativeToolAggregator` and are not emitted as downstream executable tool calls.

**Tech Stack:** Python 3.11+/FastAPI, existing `unittest` suite, existing OpenAI route in `routes/openai_compat.py`, existing Claude route in `routes/claude_api.py`.

## Global Constraints

- Native Tabbit behavior is the default.
- Local compatibility tools are disabled by default.
- Native-equivalent search/browser/memory/widget tools are filtered out of the local compatibility path.
- Local-only tools require explicit fallback and a certified model.
- Tabbit native tools must not be disguised as client-executable Claude `tool_use` or OpenAI `tool_calls`.
- Preserve the legacy local compatibility implementation behind explicit fallback.

---

## File Structure

- Modify `core/tool_policy.py`: classify client tool names, decide Native-first mode, and expose a filtered decision object.
- Modify `routes/openai_compat.py`: read the request header/config fallback flag, pass it into the policy, and only inject filtered local tools.
- Modify `routes/claude_api.py`: read the request header/config fallback flag, pass it into the policy, and only inject filtered local tools.
- Modify `tests/test_tool_policy.py`: add Native-first policy unit tests.
- Modify `tests/test_openai_compat.py`: add route helper tests for search tools and disabled fallback.
- Modify `tests/test_claude_api.py`: add route helper tests for search tools and disabled fallback.
- Modify `README.md` and `TOOL_USE_REPORT.md`: document Native-first default behavior.

---

### Task 1: Native-First Policy Helpers

**Files:**
- Modify: `core/tool_policy.py`
- Test: `tests/test_tool_policy.py`

**Interfaces:**
- Produces: `ToolKind` enum with `NATIVE_EQUIVALENT`, `LOCAL_ONLY`, and `UNKNOWN`
- Produces: `classify_client_tool(name: str, description: str = "") -> ToolKind`
- Extends: `ToolModeDecision` with `selected_tools`, `native_equivalent_tools`, `ignored_local_tools`, and `local_tools_enabled`
- Extends: `decide_tool_mode(model, has_tools, required=False, certified_models=None, tools=None, local_fallback_enabled=False) -> ToolModeDecision`

- [ ] **Step 1: Write failing policy tests**

Add these tests to `tests/test_tool_policy.py`:

```python
from core.tool_policy import ToolKind, classify_client_tool


def tool(name, description=""):
    return {"name": name, "description": description, "input_schema": {"type": "object"}}


class NativeFirstToolPolicyTest(unittest.TestCase):
    def test_search_tool_is_native_equivalent(self):
        self.assertEqual(classify_client_tool("search"), ToolKind.NATIVE_EQUIVALENT)
        self.assertEqual(classify_client_tool("web_search"), ToolKind.NATIVE_EQUIVALENT)
        self.assertEqual(classify_client_tool("fetch_url", "Fetch a URL"), ToolKind.NATIVE_EQUIVALENT)

    def test_file_and_shell_tools_are_local_only(self):
        self.assertEqual(classify_client_tool("Write"), ToolKind.LOCAL_ONLY)
        self.assertEqual(classify_client_tool("Bash"), ToolKind.LOCAL_ONLY)

    def test_default_policy_filters_native_equivalent_tools(self):
        tools = [tool("search")]
        decision = decide_tool_mode("DeepSeek-V4-Pro", has_tools=True, required=True, tools=tools)

        self.assertEqual(decision.mode, ToolMode.NATIVE_ENHANCED)
        self.assertFalse(decision.local_tools_enabled)
        self.assertFalse(decision.reject)
        self.assertEqual(decision.selected_tools, [])
        self.assertEqual(decision.native_equivalent_tools, ["search"])

    def test_required_local_tool_rejects_when_fallback_disabled(self):
        tools = [tool("Write")]
        decision = decide_tool_mode("DeepSeek-V4-Pro", has_tools=True, required=True, tools=tools)

        self.assertTrue(decision.reject)
        self.assertEqual(decision.reject_status, 400)
        self.assertIn("local tool mode is disabled", decision.reject_detail)

    def test_local_tool_allowed_when_fallback_enabled_and_model_certified(self):
        tools = [tool("Write")]
        decision = decide_tool_mode(
            "DeepSeek-V4-Pro",
            has_tools=True,
            required=True,
            tools=tools,
            local_fallback_enabled=True,
        )

        self.assertEqual(decision.mode, ToolMode.DUAL)
        self.assertTrue(decision.local_tools_enabled)
        self.assertEqual(decision.selected_tools, tools)
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
/Users/tin/project/tabbit/tabb2/.venv/bin/python -m unittest tests.test_tool_policy
```

Expected: FAIL because `ToolKind` and `classify_client_tool` do not exist and `ToolModeDecision` lacks the new fields.

- [ ] **Step 3: Implement minimal policy**

Update `core/tool_policy.py` with:

```python
class ToolKind(str, Enum):
    NATIVE_EQUIVALENT = "native_equivalent"
    LOCAL_ONLY = "local_only"
    UNKNOWN = "unknown"


NATIVE_EQUIVALENT_TOOL_NAMES = frozenset({
    "search", "web_search", "parallel_web_search", "browser", "browser_task",
    "browser_task_tool", "fetch", "fetch_url", "web_fetch", "memory_search",
    "memory", "show_widget", "widget",
})

LOCAL_ONLY_TOOL_NAMES = frozenset({
    "read", "write", "edit", "multiedit", "bash", "ls", "glob", "grep",
    "todowrite",
})


def classify_client_tool(name: str, description: str = "") -> ToolKind:
    normalized = (name or "").strip().lower().replace("-", "_")
    if normalized in NATIVE_EQUIVALENT_TOOL_NAMES:
        return ToolKind.NATIVE_EQUIVALENT
    desc = (description or "").lower()
    if any(word in normalized or word in desc for word in ("search", "browser", "fetch", "memory", "widget")):
        return ToolKind.NATIVE_EQUIVALENT
    if normalized in LOCAL_ONLY_TOOL_NAMES:
        return ToolKind.LOCAL_ONLY
    return ToolKind.UNKNOWN
```

Extend `ToolModeDecision` and `decide_tool_mode()` so default fallback is disabled, native-equivalent tools are filtered, and local-only required tools reject unless `local_fallback_enabled=True`.

- [ ] **Step 4: Run test to verify it passes**

Run:

```bash
/Users/tin/project/tabbit/tabb2/.venv/bin/python -m unittest tests.test_tool_policy
```

Expected: PASS.

---

### Task 2: OpenAI Route Uses Native-First Filtering

**Files:**
- Modify: `routes/openai_compat.py`
- Test: `tests/test_openai_compat.py`

**Interfaces:**
- Consumes: `decide_tool_mode(..., tools=tools, local_fallback_enabled=bool)`
- Produces: `_local_tools_enabled_from_config_or_header(header_value: str | None) -> bool`
- Updates: `_apply_openai_tool_policy(tabbit_model, tools, tool_choice=None, local_fallback_enabled=False) -> list[dict]`

- [ ] **Step 1: Write failing OpenAI tests**

Add these tests to `OpenAICompatToolChoiceTest` in `tests/test_openai_compat.py`:

```python
    def test_required_search_tool_degrades_to_native_enhanced(self):
        tools = [function_tool("search")]
        normalized = openai_compat._select_openai_tools(tools, "required")

        selected = openai_compat._apply_openai_tool_policy(
            "DeepSeek-V4-Pro",
            normalized,
            "required",
        )

        self.assertEqual(selected, [])

    def test_required_local_tool_rejects_when_fallback_disabled(self):
        tools = [function_tool("Write")]
        normalized = openai_compat._select_openai_tools(tools, "required")

        with self.assertRaises(HTTPException) as ctx:
            openai_compat._apply_openai_tool_policy(
                "DeepSeek-V4-Pro",
                normalized,
                "required",
            )

        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("local tool mode is disabled", ctx.exception.detail)

    def test_required_local_tool_allowed_when_header_enables_fallback(self):
        tools = [function_tool("Write")]
        normalized = openai_compat._select_openai_tools(tools, "required")

        selected = openai_compat._apply_openai_tool_policy(
            "DeepSeek-V4-Pro",
            normalized,
            "required",
            local_fallback_enabled=True,
        )

        self.assertEqual(selected, normalized)
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
/Users/tin/project/tabbit/tabb2/.venv/bin/python -m unittest tests.test_openai_compat
```

Expected: FAIL because search required currently stays enabled and local fallback parameter is missing.

- [ ] **Step 3: Implement OpenAI filtering**

Update `_apply_openai_tool_policy()` to pass `tools=tools` and `local_fallback_enabled=local_fallback_enabled` into `decide_tool_mode()` and return `decision.selected_tools`.

Add:

```python
def _local_tools_enabled_from_config_or_header(header_value: str | None) -> bool:
    if isinstance(header_value, str) and header_value.strip().lower() in ("1", "true", "yes", "on"):
        return True
    return bool(_cfg and _cfg.get("proxy", "local_tools_enabled", default=False))
```

Update `chat_completions()` to accept `x_tabbit_local_tools: str | None = Header(None)` and pass the helper result into `_apply_openai_tool_policy()`.

- [ ] **Step 4: Run test to verify it passes**

Run:

```bash
/Users/tin/project/tabbit/tabb2/.venv/bin/python -m unittest tests.test_openai_compat
```

Expected: PASS.

---

### Task 3: Claude Route Uses Native-First Filtering

**Files:**
- Modify: `routes/claude_api.py`
- Test: `tests/test_claude_api.py`

**Interfaces:**
- Consumes: `decide_tool_mode(..., tools=tools, local_fallback_enabled=bool)`
- Produces: `_local_tools_enabled_from_config_or_header(request: Request) -> bool`
- Updates: `_apply_claude_tool_policy(tabbit_model: str, body: dict, local_fallback_enabled: bool = False) -> list[dict]`

- [ ] **Step 1: Write failing Claude tests**

Add these tests to `ClaudeApiToolPolicyTest` in `tests/test_claude_api.py`:

```python
    def test_search_tool_degrades_to_native_enhanced(self):
        body = {
            "tools": [
                {
                    "name": "web_search",
                    "description": "Search the web",
                    "input_schema": {"type": "object", "properties": {}},
                }
            ]
        }

        selected = claude_api._apply_claude_tool_policy("DeepSeek-V4-Pro", body)

        self.assertEqual(selected, [])
        self.assertEqual(body["tools"], [])

    def test_local_tool_rejects_when_fallback_disabled(self):
        body = {
            "tools": [
                {
                    "name": "Write",
                    "description": "Write a file",
                    "input_schema": {"type": "object", "properties": {}},
                }
            ]
        }

        with self.assertRaises(HTTPException) as ctx:
            claude_api._apply_claude_tool_policy("DeepSeek-V4-Pro", body)

        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("local tool mode is disabled", ctx.exception.detail)

    def test_local_tool_allowed_when_fallback_enabled(self):
        tools = [
            {
                "name": "Write",
                "description": "Write a file",
                "input_schema": {"type": "object", "properties": {}},
            }
        ]
        body = {"tools": tools}

        selected = claude_api._apply_claude_tool_policy(
            "DeepSeek-V4-Pro",
            body,
            local_fallback_enabled=True,
        )

        self.assertEqual(selected, tools)
        self.assertEqual(body["tools"], tools)
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
/Users/tin/project/tabbit/tabb2/.venv/bin/python -m unittest tests.test_claude_api
```

Expected: FAIL because local tools are currently enabled for certified models by default.

- [ ] **Step 3: Implement Claude filtering**

Update `_apply_claude_tool_policy()` to pass `tools=tools` and `local_fallback_enabled=local_fallback_enabled` into `decide_tool_mode()`, set `body["tools"] = decision.selected_tools`, and return `decision.selected_tools`.

Add:

```python
def _local_tools_enabled_from_config_or_header(request: Request) -> bool:
    value = request.headers.get("x-tabbit-local-tools", "")
    if value.strip().lower() in ("1", "true", "yes", "on"):
        return True
    return bool(_cfg and _cfg.get("proxy", "local_tools_enabled", default=False))
```

Update `claude_messages()` to pass this helper into `_apply_claude_tool_policy()`.

- [ ] **Step 4: Run test to verify it passes**

Run:

```bash
/Users/tin/project/tabbit/tabb2/.venv/bin/python -m unittest tests.test_claude_api
```

Expected: PASS.

---

### Task 4: Documentation and Full Verification

**Files:**
- Modify: `README.md`
- Modify: `TOOL_USE_REPORT.md`

**Interfaces:**
- Documents: Native-first default and explicit local fallback controls.

- [ ] **Step 1: Update docs**

Update README Tool behavior to state:

```markdown
- Native Tabbit tools are the default path. Search/browser/memory-style client tools are treated as native-equivalent hints and are not converted into local tool calls.
- Local client tools such as Read/Write/Edit/Bash/LS are disabled by default and require explicit local fallback (`x-tabbit-local-tools: true` or `proxy.local_tools_enabled=true`) plus a certified model.
```

Update `TOOL_USE_REPORT.md` with a short 2026-07-04 note that the product direction is now Native-first, with legacy compatibility behind explicit fallback.

- [ ] **Step 2: Run focused tests**

Run:

```bash
/Users/tin/project/tabbit/tabb2/.venv/bin/python -m unittest tests.test_tool_policy tests.test_openai_compat tests.test_claude_api tests.test_openai_native_tools tests.test_claude_native_tools
```

Expected: PASS.

- [ ] **Step 3: Run full verification**

Run:

```bash
/Users/tin/project/tabbit/tabb2/.venv/bin/python -m unittest discover -s tests
python3 -m compileall -q tabbit2api.py core routes scripts tests
/Users/tin/project/tabbit/tabb2/.venv/bin/python -m pip check
git diff --check
```

Expected: all commands exit 0.

- [ ] **Step 4: Commit implementation**

Run:

```bash
git add core/tool_policy.py routes/openai_compat.py routes/claude_api.py tests/test_tool_policy.py tests/test_openai_compat.py tests/test_claude_api.py README.md TOOL_USE_REPORT.md docs/superpowers/plans/2026-07-04-native-first-tool-policy.md
git commit -m "feat(tools): prefer Tabbit native tool behavior"
```

Expected: one implementation commit on `native-first-tool-policy`.
