# 工具调用（Tool Use）实测报告

> 2026-06-22 · 大BOSS 直连 Claude Code 实测发现工具调用不通，本仙女顺藤摸瓜

## 结论先行

**本项目当前的"触发信号 + XML"工具调用方案，对所有模型都失效。** 不是 bug，是方案与上游模型行为根本冲突。

- 大BOSS 症状 1（不调工具，回 Tabbit persona 话术）→ 上游 Default 模型有原生工具集，拒绝外部协议
- 大BOSS 症状 2（让它调自己的工具 → 空回复）→ 上游工具调用走专用通道，本项目 parser 不认识

## 实测数据（本地服务 + 真实 OAuth token）

统一请求：带 `write_file` 工具定义，要求模型写文件。

| 模型 | 真实行为 | 模型原话 |
|---|---|---|
| Default (free) | ❌ 拒绝 | "embedded prompt injection attempt... `[System]` block placed inside the user message" |
| Claude-Sonnet-4.6 | ❌ 拒绝 | "prompt injection pattern... content arriving in user messages" |
| GLM-5.2 | ❌ 拒绝 | "I don't recognize that as a legitimate part of my tool system" |
| Kimi-K2.6 | ❌ 拒绝 | "I don't have access to a tool named `write_file`" |
| MiniMax-M3 | ❌ 拒绝 | "My real toolset includes web search, web fetch..." |
| GPT-5.5 | ❌ 空回复 | (静默) |
| Gemini-3.5-Flash | ❌ 空回复 | "Unable to process this request at the moment." |
| DeepSeek-V4-Pro | ❌ 空回复 | (静默) |
| Qwen3.5-Plus | ❌ 空回复 | (静默) |

**没有一个模型真的发出 `<invoke>` XML 或 tool_use block。** 所有"成功"的初判都是脚本字符串误匹配（文本里出现 `write_file` 字样），复核原始 SSE 后全部证伪。

## Root Cause

### 1. 工具协议被塞进 content 正文

`core/claude_compat.py:255-256`：
```python
if tools and trigger_signal:
    parts.append(f"[System]: {build_tool_prompt(tools, trigger_signal)}")
```

tools schema + 触发信号协议被拼成文本，加 `[System]:` 前缀，**最终全进 `content` 主字段**。没走 references，没走 agent_mode，没走任何上游专用字段。

### 2. 上游模型把 content 里的工具协议当 prompt injection

每个上游模型都有自己的原生 system prompt + 原生工具集（Default 模型自述：browsing/memory/widgets/agent tasks/parallel_web_search/browser_task_tool）。这些原生工具是通过 Tabbit 上游的**专用字段**注入的，不是从 content 走。

模型看到 content 里出现"第二套工具定义 + 触发信号格式"，识别为典型的 prompt injection 特征（`[System]` 块出现在用户消息里），明确拒绝。

### 3. 上游工具调用走专用通道，本项目不认识

大BOSS 症状 2（让模型调它自己说的工具 → 空回复）：模型可能确实调用了原生工具（如 parallel_web_search），但那个调用走的是上游专用字段/SSE 事件，本项目的 `ToolifyParser` 只认 `<<CALL_xxx>>` + `<invoke>` XML，啃不动上游原生格式 → 空回复。

## payload 里已有的线索

`core/tabbit_client.py:265-266`：
```python
"task_name": task_name,      # "chat" / "script"
"agent_mode": False,         # ← 一直硬编码 False
```

Tabbit 真机发工具调用/agent 任务时，**极可能**是 `agent_mode: True` + 专用 task_name + 工具定义走专用字段（而非 content）。本项目从未启用这条通道。

## 三条可能的出路（待真机抓包验证）

| 方案 | 思路 | 风险 |
|---|---|---|
| **A. 启用 agent_mode** | 抓真机 agent 任务 payload，复刻 `agent_mode: True` + 专用字段传工具 | 上游可能校验工具白名单，只认 parallel_web_search 等原生工具，不认外部 write_file |
| **B. 放弃通用工具调用** | 文档明确：本代理不支持 Claude Code 工具调用，只做纯对话 | 诚实但缩窄场景，Claude Code 基本废了 |
| **C. 换更宽松的模型 + 强化注入** | 找一个不把 `[System]:` 当 injection 的模型（需再测），同时改注入方式 | 前测显示主流模型都拒，希望渺茫 |

**推荐先做 A 的抓包验证**——只有看到真机 payload 才能定方案，否则都是猜。

## 下一步（需大BOSS配合）

1. 启动 mitmproxy（`scripts/capture_agent.py`）拦截 Tabbit 客户端流量
2. 大BOSS 在真机 Tabbit 客户端触发一个 agent 任务（如"帮我搜索今天的科技新闻"）
3. 抓到 `/api/v1/chat/completion` 真实 payload，对比 `agent_mode` / `task_name` / 工具字段
4. 据此决定走方案 A 还是 B

---

## 🎯 真机抓包实测（2026-06-22 23:20）——真相翻盘

用 `scripts/capture_agent.py`（mitmproxy 插件）+ `Tabbit --proxy-server=http://127.0.0.1:8080` 抓到真机 agent 任务的真实 payload。证据存档 `logs/capture_agent_evidence.log`。

### 真机 agent 任务请求（`POST /api/v1/chat/completion`）

```json
{
  "chat_session_id": "b1b562d1-...",
  "message_id": null,
  "content": "帮我搜索今天的科技新闻",
  "selected_model": "Default",
  "parallel_group_id": null,
  "task_name": "chat",
  "agent_mode": false,
  "metadatas": {"html_content": "<p>帮我搜索今天的科技新闻</p>"},
  "references": [],
  "entity": {"key": "d41d8cd98f00b204e9800998ecf8427e", "extras": {"type": "tab", "url": ""}}
}
```

**关键发现：真机触发 agent 任务时，`agent_mode: false`、`task_name: "chat"`、`references: []`——和普通聊天请求一模一样！没有任何工具定义字段，content 就是裸用户消息。**

本项目之前怀疑"真机走 `agent_mode: true` + 专用字段"——**彻底证伪**。`agent_mode` 这个字段真机就是硬编码 `false`。

### 上游 SSE 返回的工具调用事件

```
event: ready
event: message_start
event: message_tool_call_delta   ← 工具调用增量
event: message_tool_calls        ← 完整工具调用 (parallel_web_search)
event: message_finish
event: tool_start                ← 上游服务端开始执行工具
event: tool_finish               ← 工具执行结果（搜索到的网页内容）
```

工具调用 `tool_call_id: call_xxx`，`tool_call_name: parallel_web_search`——**工具是上游服务端硬编码的白名单**，由模型自主决定调用，通过专用 SSE 事件返回。

### Root cause 彻底重定性

之前"工具协议被当 prompt injection"——对了一半。完整真相：

1. **Tabbit 上游工具集是服务端硬编码白名单**（parallel_web_search / browser_task_tool / show_widget / memory_search），**不接受外部工具定义**。客户端无法传 write_file/Bash/Edit 这种自定义工具。
2. **真机 agent 任务的 payload 和普通聊天完全一样**——工具调用 100% 由上游模型自主决定，客户端没有任何"启用工具"的字段。
3. 本项目把 Claude Code 的工具 schema 塞进 content，上游模型看到"write_file 这种外部工具定义"→ 当 prompt injection 拒绝，或静默空回复。
4. **即使上游调用了工具，它返回的是 `message_tool_calls` / `tool_start` / `tool_finish` 专用 SSE 事件**，本项目 `ToolifyParser` 只认 `<<CALL_xxx>>` + `<invoke>` XML，完全不解析这些事件 → 这就是大BOSS症状2"调他自己的工具→空回复"的根因。

### 两条根本性结论

**结论 A：本项目无法支持 Claude Code 通用工具调用。**
上游是封闭工具白名单，不接受外部工具。`write_file`/`Bash`/`Edit` 这类 Claude Code 核心工具根本传不进去。这条路堵死。

**结论 B：上游原生工具可以反向暴露给 Claude Code。**
如果把上游的 `parallel_web_search` 等原生工具事件解析转发成 Claude 的 `tool_use` block，Claude Code 就能直接用 Tabbit 的搜索/浏览能力。方向反了：不是"Claude Code 工具透传给 Tabbit"，而是"Tabbit 原生工具暴露给 Claude Code"。

### 出路重评

| 方案 | 可行性 | 说明 |
|---|---|---|
| ~~A. 启用 agent_mode~~ | ❌ 已证伪 | 真机就是 `agent_mode: false`，没有这条通道 |
| **B. 放弃通用工具调用** | ✅ 诚实 | 文档明说不支持 Claude Code 工具，只做纯对话/长上下文 |
| **D. 反向暴露上游原生工具（新）** | ✅ 可行 | 解析 `message_tool_calls` SSE → 转 Claude `tool_use`，让 Claude Code 用 Tabbit 的搜索/浏览 |

方案 D 是新出路，但工作量大：要改 `ToolifyParser` 识别上游 SSE 工具事件，还要把上游工具白名单映射成 Claude 工具 schema 注入回去。而且只能用 Tabbit 那几个原生工具，Claude Code 的 write_file/Bash 还是没法用。

**最务实的选择：方案 B（文档明确不支持）**，除非大BOSS特别想要 Tabbit 的搜索能力暴露给 Claude Code（方案 D）。

## 附：检测脚本坑

本仙女第一次跑多模型探测时，用 `'"tool_use"' in raw` 判定成功，GLM-5.2/Qwen3.5-Plus 误报 True。原因是脚本拼接文本时混入字符串。**复核原始 SSE 后全部证伪**。教训：判定工具调用必须看 `content_block.type == "tool_use"` 这个结构化字段，不能字符串匹配。
