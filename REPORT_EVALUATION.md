# Tabbit-Context-Analysis-Report.md 真伪评估

**评估日期**: 2026-06-22
**评估方法**: 真机抓包（mitmproxy 代理 Tabbit macOS 1.1.39 客户端）+ 本仙女此前实测铁证
**评估环境**: macOS Tabbit 1.1.39 (10101039)，账号 pro 会员，web.tabbit.ai 国际版

---

## 一、总判定

**报告整体可信度：高（~85%）。** 核心技术主张（请求格式、references 机制、492 边界）全部真机复现。主要缺陷是**样本单一**（只抓国内版 GLM-5.2）和**漏了前端限制**。

---

## 二、逐条验证

### 2.1 请求格式（报告 2.2 节）—— ✅ 完全属实

真机抓包 `POST https://web.tabbit.ai/api/v1/chat/completion`：

| 报告主张 | 真机 | 判定 |
|---|---|---|
| 端点 `/api/v1/chat/completion` | ✅ 一致 | 真 |
| `chat_session_id` / `message_id:null` | ✅ | 真 |
| `content` / `selected_model` | ✅ | 真 |
| `parallel_group_id:null` / `task_name:"chat"` | ✅ | 真 |
| `agent_mode:false` | ✅ | 真 |
| `metadatas.html_content` | ✅ `<p>...</p>` | 真 |
| `references:[]` | ✅ | 真 |
| `entity.key = d41d8cd98f00b204e9800998ecf8427e` | ✅ **完全一致**（md5 of empty） | 真 |
| `entity.extras:{type:"tab",url:""}` | ✅ | 真 |

### 2.2 请求头（报告 2.2 节）—— ✅ 全部属实

| 报告主张 | 真机抓包值 | 判定 |
|---|---|---|
| `x-nonce` (64 hex) | ✅ `cb60ac35...` | 真 |
| `trace-id` (UUID) | ✅ | 真 |
| `x-timestamp` (13位毫秒) | ✅ `1782130773825` | 真 |
| `unique-uuid` | ✅ | 真 |
| `x-req-ctx: MS4xLjM5KDEwMTAxMDM5KQ==` | ✅ **完全一致**（解码=`1.1.39(10101039)`) | 真 |
| `x-signature` (UUID) | ✅ | 真 |

### 2.3 References 机制（报告 2.3 节）—— ✅ 核心主张全部属实

真机 @ 引用文件 `测试文本.txt` 抓包：

```json
{
  "type": "document",
  "title": "测试文本.txt",
  "content": "",              ← 确实为空
  "metadata": {
    "file_id": "0bfc65e7e94a4500aa112a9a50b4eb9a"
  }
}
```

| 报告主张 | 真机 | 判定 |
|---|---|---|
| `type:"document"` | ✅ | 真 |
| `content:""`（空，服务端取） | ✅ | 真 |
| `metadata.file_id` 引用 | ✅ | 真 |
| `metadatas.html_content` 含 `<tab-mention-node>` | ✅ **完全一致** | 真 |
| `data-reference` 里 `content:"", path:file_id` | ✅ | 真 |
| 模型可读 references 完整内容 | ✅ 模型复述了文件内容 | 真 |

**补充发现的完整链路**（报告未详述）：
1. `POST /proxy/v0/cos/presigned-upload-url` → 返回腾讯云 COS 预签名 URL + file_id
2. 客户端直传文件到 `cos.ap-singapore.myqcloud.com`（新加坡节点）
3. `POST /proxy/v0/cos/complete-upload` → 通知服务端
4. chat/completion 的 references 里只带 file_id，content 为空

### 2.4 SSE 事件序列（报告 2.5 节）—— ✅ 属实

报告序列：`ready → message_start → thinking → thinking_finished → message_chunk → message_finish → title → finish → close`

真机（@ 引用触发 thinking）：`ready → message_start → thinking×40 → thinking_finished → message_chunk×N → message_finish → finish → close`

**差异**：未抓到 `title` 事件（可能在 message_finish 后异步推送，或本次未生成）。短消息无 thinking（因 Claude-Opus-4.8 的 use_thinking 配置/任务复杂度）。

### 2.5 492 边界 ~20421（报告 8.1 节）—— ✅ 属实（本仙女此前已验证）

本仙女此前的 4 模型二分探测（Claude-Opus-4.8 / GPT-5.5 / GLM-5.1 / Kimi-K2.6）边界全部 = 20421。本次未复测（避免烧额度），但报告与本仙女结论一致。

### 2.6 References 不受 20421 限制（报告 8.1 节）—— ✅ 属实

本仙女此前的金丝雀测试（`probe_bypass_verify.py`）：80000 字符 content 埋金丝雀，模型命中 → references 通道真实可读超长内容。报告说"7万+"，本仙女测到 80000，一致量级。

---

## 三、报告的缺陷与遗漏

### 3.1 🔴 严重遗漏：输入框前端 20000 字符硬限制

**报告完全没提**这个前端限制。真机验证：
- 输入框按 **JS 字符数（UTF-16 code units，中文算1）** 截断，精确 20000 字符
- 计数器显示 `20000/20000`
- 本仙女给的 24790 字符长文本，前端截断到 20000（精确到"品牌建设需要长期投入"末尾）
- 这是客户端 UI 限制，proxy 直打接口可绕过

**影响**：报告 6.1 节建议"主动利用 references"，但没说**正常用户在输入框就被 20000 卡死**，根本到不了 20421 网关限制。proxy 项目存在的意义之一就是绕过这个前端限制。

### 3.2 🟡 样本单一：只抓国内版 GLM-5.2

报告基于 `web.tabbit.com`（国内版）+ `GLM-5.2`。真机抓包发现 Tabbit 框架二进制硬编码了 **5 个域名**：
- `web.tab-browser.com`
- `web.tabbit.com`（国内版，报告抓的）
- `web.tabbit.ai`（国际版，本机用的）
- `tabai-test.meituan.com`（美团测试环境）
- `tab-browser-test-sg.meituan.com`（新加坡测试）

国际版与国内版上游可能不同。报告结论能否套用国际版，需分别验证。

### 3.3 🟡 模型列表严重不完整

报告 2.4 节只写 `GLM-5.2`。真机抓 `/proxy/v1/model_config/models` 返回 **21 个模型**：
- Default / GLM-5.2 / GLM-5.1 / MiniMax-M3 / Claude-Opus-4.8 / Gemini-3.5-Flash / GPT-5.5 / DeepSeek-V4-Pro / DeepSeek-V4-Flash / Claude-Opus-4.7 / Kimi-K2.6 / GPT-5.4 / GPT-5.2-Chat / Gemini-3.1-Pro / Claude-Sonnet-4.6 / Claude-Haiku-4.5 / MiniMax-M2.7 / DeepSeek-V3.2 / Kimi-K2.5 / Qwen3.5-Plus / Doubao-Seed-1.8

每个模型有 `model_access_type`（free_unlimited / free_metered / premium_only）、`supports_images`、`supports_tools`、`support_thinking`、`use_thinking` 等字段。

### 3.4 🟡 Mac 版架构描述不准

报告 2.1 节描述的是 Windows 版（Chromium 148 + Tabb.dll 273MB）。Mac 版是 **Swift 壳 + Tabbit Framework（CEF/Chromium 内核）**，架构不同但 web 层一致。

### 3.5 🟢 报告未涉及的发现（本仙女补充）

- **MCP 支持**：`/proxy/mcp/servers`、`/proxy/mcp/recommendations` —— Tabbit 支持 MCP 服务器
- **Skill 服务**：cookie 里有 `skills.tabbit.ai` 和 `skills.tabbitbrowser.com` 两个 skill 域名（印证 system prompt 经 skill_id 注入的猜想）
- **配额接口**：`/api/commerce/quota/v1/usage` 返回会员等级、用量百分比、重置时间
- **oauth 流程**：`/proxy/v0/oauth/token` 用 JWT refresh_token 换 access_token
- **sentry 上报**：`sentry.tabbitbrowser.com`

---

## 四、报告"优化建议"评估

### 4.1 输出清理（报告 7.1.1）—— 🟡 部分成立

报告建议借鉴 ds2api 清理泄漏标记（空代码块、引用标记、think 标签）。

**评估**：本仙女项目当前确实无输出清理。但 Tabbit 上游返回的是标准 SSE，本次抓包未见标记泄漏。**是否需要清理取决于上游是否泄漏**——建议先抓更多场景（工具调用、长输出）确认泄漏是否真实发生，再决定加不加。

### 4.2 Thinking-Only 重试（报告 7.2.1）—— 🟡 部分成立

报告建议上游返回空内容时自动重试。

**评估**：本仙女项目此前确认上游偶有慢响应（27s/40s），但未确认"thinking-only 空响应"是否真实发生。ds2api 的机制是为 DeepSeek 量身定做，Tabbit 上游未必有同样问题。**建议先监控是否真出现空响应，再决定**。

### 4.3 主动分层 references（报告 7.3.2）—— ✅ 本仙女已实现

报告建议"主动利用 references，不等超长才分流"。

**评估**：本仙女项目的 `build_content_with_refs` 已经实现这个——超长时把旧历史塞 references（type:"dom"）。报告这条建议**本仙女已落地**，只是策略是"被动触发"而非"主动总是分流"。是否改成主动分流，取决于性能权衡（references 通道虽能塞超长，但模型对 references 的注意力可能低于 content）。

### 4.4 统一兼容层（报告 7.3.1）—— 🟢 远期建议

报告建议借鉴 ds2api 分层支持多协议。

**评估**：本仙女项目当前 Claude + OpenAI 双协议已够用。多协议统一层是远期事，当前无刚需。

---

## 五、结论

### 报告真正有价值的部分（真材实料）
1. ✅ 请求格式 / 请求头结构 —— 100% 准确
2. ✅ references 机制（document + file_id + COS 上传链路）—— 100% 准确
3. ✅ SSE 事件序列 —— 准确
4. ✅ 492 边界 ~20421 —— 与本仙女实测一致
5. ✅ references 不受 20421 限制 —— 与本仙女实测一致

### 报告的主要问题
1. 🔴 漏了输入框前端 20000 字符限制（关键遗漏）
2. 🟡 样本单一（只国内版 GLM-5.2，没覆盖国际版 + 21 个模型）
3. 🟡 Mac 架构描述不准（用 Windows 版套）
4. 🟡 优化建议部分是为 ds2api 量身的，套到 Tabbit 需重新评估必要性

### 给大BOSS的建议
- 报告可作为**请求格式 / references 机制的可靠参考**
- 但**不要**照搬"优化建议"——先验证必要性
- 本仙女项目当前实现（references 分流 + 18450 截断）已覆盖报告核心主张，且额外处理了报告漏掉的前端 20000 限制
