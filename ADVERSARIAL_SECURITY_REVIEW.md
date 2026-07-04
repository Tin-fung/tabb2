# Tabbit2API 对抗式安全审查报告

审查日期：2026-07-04  
项目路径：`/Users/tin/project/tabbit/tabb2`  
审查对象：Python/FastAPI 版 Tabbit2API 网关、Admin 面板、OpenAI/Claude 兼容 API、配置与部署边界  
审查说明：本报告只覆盖 `tabb2`，不包含 `tabbit2api-ref` 参考项目。

## 0. 修复进展

- 2026-07-04：OpenAI 兼容接口 `/v1/chat/completions` 已改为 token 池非空且 `proxy.api_key` 为空时 fail-closed，并补充离线回归测试。
- 2026-07-04：Claude 兼容接口 `/v1/messages` 已改为 token 池非空且 `proxy.api_key` 为空时 fail-closed，并补充离线回归测试。
- 2026-07-04：OpenAI 兼容接口已补充 `tool_choice="none"` 与指定 function 的最小兼容处理。
- 2026-07-04：Dashboard 最近请求日志已对 `model`、`token_name` 与 `statusBadge` 显示文本使用 HTML escape，并补充离线回归测试。
- 2026-07-04：限流只在直连来源命中 `trusted_proxies` 时信任 `X-Forwarded-For` / `X-Real-IP`，并补充回归测试。
- 2026-07-04：默认启用上游 TLS 证书校验；旧配置显式设置 `verify_ssl=false` 时仍保留，便于本地抓包。
- 2026-07-04：已有配置若 `admin.jwt_secret` 为空会自动生成；Admin JWT 验证强制要求 `role=admin`。
- 2026-07-04：应用层新增 ASGI receive 累计请求体大小限制，覆盖无 `Content-Length` 的请求。

## 1. 执行摘要

本轮审查按“入口梳理 → source 到 sink 追踪 → 对抗反证 → 证据收口”的方式进行。核心结论是：项目的主要风险不在复杂代码执行链，而在默认运行边界偏开放。当前高优先级鉴权、Dashboard XSS、转发头信任、TLS 默认值、Admin JWT 与无 `Content-Length` 请求体限制已完成代码级修复。

最高优先级修复项状态：

1. `proxy.api_key` 为空且 token 池非空时，禁止 `/v1/*` 使用本地 token 池：已修复。
2. Dashboard 最近请求日志对 `model`、`token_name`、`status` 等字段 escape：已修复。
3. 限流逻辑默认不信任 `X-Forwarded-For` / `X-Real-IP`：已修复。
4. 默认启用上游 TLS 证书校验：已修复，新配置默认 `verify_ssl=true`。
5. 启动时补齐空 `admin.jwt_secret`，并校验 `role == "admin"`：已修复。

## 2. 威胁模型

### 资产

- Tabbit 账号 Token：存储在 `config.json` / `data/config.json`，请求上游时作为 cookie 使用。
- Admin JWT：前端存放在 `localStorage.admin_token`。
- 管理后台能力：Token CRUD、上游登录换 token、额度/重置券/签到操作、设置修改。
- 代理 API 能力：`/v1/chat/completions`、`/v1/messages` 等端点可消耗 Tabbit 额度。

### 主要攻击者

- 局域网内未授权用户。
- 误暴露到公网后的匿名请求者。
- 能调用兼容 API 但不应影响 admin UI 的普通 API 使用者。
- 上游链路中间人或被配置为恶意的上游地址。

### 信任边界

- 外部 HTTP 请求到 FastAPI 路由。
- Admin 页面 DOM 渲染边界。
- 本地配置文件到运行态鉴权逻辑。
- Tabbit2API 到 Tabbit 上游 HTTPS 请求。
- 反向代理头到真实客户端 IP 的转换边界。

## 3. Findings

### P0-01: Token 池非空且 `proxy.api_key` 为空时，兼容 API 对所有请求放行

严重度：P0 / Critical  
类别：Broken Authentication / Unrestricted Resource Consumption  
文件：

- `routes/openai_compat.py:243`
- `routes/claude_api.py:86`
- `core/config.py:15`
- `config.json.example:1`

#### 证据

OpenAI 兼容路由中，当 token 池非空时只在 `api_key` 有值时验证：

```python
if _tm.has_tokens:
    api_key = _cfg.get("proxy", "api_key")
    if api_key:
        bearer = (authorization or "").replace("Bearer ", "")
        if not hmac.compare_digest(bearer, api_key):
            raise HTTPException(status_code=401, detail="invalid api key")
    token_info, client = await _tm.get_next()
```

Claude 兼容路由逻辑同类：

```python
if _tm and _tm.has_tokens:
    if api_key and not hmac.compare_digest(bearer, api_key):
        raise HTTPException(status_code=401, detail="invalid api key")
    token_info, client = await _tm.get_next()
```

本地当前配置经脱敏检查确认：

```text
server_host: 0.0.0.0
has_proxy_api_key: false
token_count: 1
enabled_token_count: 1
verify_ssl: false
```

#### 影响

如果服务监听在 `0.0.0.0` 且端口对其他机器可达，任意请求者可直接调用 `/v1/chat/completions` 或 `/v1/messages`，消耗 Tabbit 账号额度，并污染日志/状态。

#### 对抗验证

该 finding 不依赖上游是否可用。代码路径显示鉴权分支在 `api_key` 为空时不会拒绝请求，会直接进入 `_tm.get_next()` 并使用配置中的 token 池。

#### 修复建议

- 启动时若 `tokens` 非空且 `proxy.api_key` 为空，默认拒绝 `/v1/*`，返回明确错误。
- 仅在显式 `ALLOW_UNAUTHENTICATED_LOCAL_API=true` 且监听 `127.0.0.1` 时允许无代理 key。
- README 和 Admin Settings 中把“API Key 留空则不校验”改为强风险提示或移除。
- 增加回归测试：token 池非空、api_key 为空、请求 `/v1/chat/completions` 与 `/v1/messages` 应返回 401/403。

### P1-02: Dashboard 最近请求日志存在 DOM XSS

严重度：P1 / High  
类别：Stored DOM XSS  
当前状态：已修复（Dashboard 最近请求中的 `model`、`token_name` 与 `statusBadge` 显示文本已统一走 `esc()`，见 `tests/test_admin_dashboard_xss.py`）  
文件：

- `static/index.html:333`
- `static/index.html:335`
- `routes/openai_compat.py:991`
- `routes/claude_api.py:380`
- `core/log_store.py:27`

#### 证据

Dashboard 的最近请求表格未对日志字段统一转义：

```javascript
${s.recent_logs.map(l => `<tr class="border-b border-zinc-800/50 hover:bg-zinc-800/30">
  <td class="px-3 md:px-5 py-3 text-zinc-400 whitespace-nowrap">${fmtTime(l.timestamp)}</td>
  <td class="px-3 md:px-5 py-3">${l.model}</td>
  <td class="px-3 md:px-5 py-3 text-zinc-400">${l.token_name}</td>
  <td class="px-3 md:px-5 py-3">${statusBadge(l.status)}</td>
  <td class="px-3 md:px-5 py-3 text-zinc-400">${l.duration}s</td>
</tr>`).join('')}
```

Logs 页面同类字段使用了 `esc(l.model)`、`esc(l.token_name)`，说明项目已有 escape helper，但 Dashboard 漏用了。

`req.model` 会进入日志：

```python
LogEntry(
    model=req.model,
    token_name=token_name,
    stream=False,
    status="success" if not error_msg else "error",
)
```

#### 影响

能调用兼容 API 的请求者可通过 `model` 字段写入 HTML/JS 片段。管理员打开 Dashboard 后，浏览器会把未转义内容插入 `innerHTML`。由于 Admin JWT 存在 `localStorage.admin_token`，XSS 可能进一步调用 admin API 或窃取管理会话。

#### 对抗验证

数据流成立：

```text
POST /v1/chat/completions body.model
  -> routes/openai_compat.py LogEntry(model=req.model)
  -> admin_api.py /api/admin/status recent_logs
  -> static/index.html Dashboard innerHTML
```

Logs 页已 escape，不推翻 Dashboard 漏洞；它只说明修复模式已经存在。

#### 修复建议

- Dashboard 中 `l.model`、`l.token_name`、`l.status`、`l.duration` 等非静态字段统一使用 `esc()` 或安全 DOM API。
- `statusBadge(s)` 内部也应对显示文本 `s` 做 allowlist 或 escape。
- 增加 CSP，例如 `default-src 'self'; script-src 'self'`，并移除远程 Tailwind CDN 或给出受控构建产物。
- 增加前端回归：日志字段含 HTML 时，Dashboard 显示文本而不是执行。

### P1-03: 限流默认信任可伪造转发头，可绕过登录与 API 限流

严重度：P1 / High  
类别：Broken Rate Limiting / Security Misconfiguration  
当前状态：已修复（默认使用直连 IP；仅 `trusted_proxies` 命中的代理来源才读取转发头，见 `tests/test_security_hardening.py`）  
文件：`tabbit2api.py:101`

#### 证据

限流 key 默认优先使用请求头：

```python
client_ip = (
    request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    or request.headers.get("x-real-ip", "").strip()
    or (request.client.host if request.client else "unknown")
)
```

随后登录与 API 限流均基于 `client_ip`：

```python
if request.url.path == "/api/admin/login" and request.method == "POST":
    ...
elif request.url.path.startswith("/v1/"):
    ...
```

#### 影响

攻击者可每次请求伪造不同 `X-Forwarded-For`，绕过：

- `/api/admin/login` 的 5 次 / 15 分钟限制。
- `/v1/*` 的 60 次 / 分钟限制。

如果 P0-01 同时存在，攻击者可以绕过 API 限流持续消耗 token 池。

#### 对抗验证

代码中没有检查请求是否来自可信反向代理，也没有固定代理 IP allowlist。因此任意直连请求也可控制限流 key。

#### 修复建议

- 默认使用 `request.client.host`。
- 新增 `TRUSTED_PROXY_CIDRS` / `trusted_proxies` 配置，仅当直接来源 IP 命中可信代理时读取转发头。
- 登录失败计数建议同时按 IP、账号/全局维度限流。
- 多进程或生产部署使用 Redis/外部限流器。

### P1-04: 上游 TLS 证书校验默认关闭

严重度：P1 / High，若只在本地调试则可降为 P2  
类别：Sensitive Token Exposure / MITM  
当前状态：已修复默认值（`DEFAULT_CONFIG` 与 `config.json.example` 默认 `verify_ssl=true`；旧配置显式 `false` 保留以避免破坏本地抓包）  
文件：

- `core/config.py:26`
- `core/tabbit_client.py:167`
- `core/model_registry.py:243`
- `routes/admin_api.py:215`

#### 证据

默认配置：

```python
"verify_ssl": False
```

Tabbit 上游请求使用该值：

```python
self.client = httpx.AsyncClient(..., verify=verify_ssl)
```

模型注册表和 Google 登录换取 Tabbit Token 的请求也使用 `verify_ssl`。

#### 影响

Tabbit Token 会以 cookie 形式发往上游。证书校验关闭时，链路中间人可窃取 token 或篡改模型清单/上游响应。当前本地配置也确认 `verify_ssl=false`。

#### 对抗验证

该 finding 是配置与代码路径风险，不要求真实 MITM。请求构造点均从配置读取 `verify_ssl`，默认与当前运行状态均为关闭。

#### 修复建议

- 默认 `verify_ssl=true`。
- 仅允许通过显式 dev/debug 开关关闭，并在启动日志中输出强警告。
- Admin UI 中把“关闭方便调试”改为生产危险配置提示。
- 对 `base_url` 做 allowlist 或至少限制 scheme 为 `https`。

### P2-05: 空 `admin.jwt_secret` 会导致 Admin JWT 可伪造，且未校验 role

严重度：P2 / Medium  
类别：JWT Misconfiguration  
当前状态：已修复（已有配置空 `jwt_secret` 自动生成；JWT payload 必须 `role=admin`，见 `tests/test_security_hardening.py`）  
文件：

- `config.json.example:7`
- `core/config.py:71`
- `core/auth.py:16`
- `core/auth.py:26`
- `core/auth.py:54`

#### 证据

首次生成配置时会填充 `jwt_secret`，但如果配置文件已存在且 `admin.jwt_secret` 为空，加载流程不会自动补齐：

```python
if self.path.exists():
    saved = json.load(f)
    config = _deep_merge(copy.deepcopy(DEFAULT_CONFIG), saved)
    ...
    self._save(config)
    return config
```

JWT 验证只校验签名和过期时间：

```python
payload = jwt.decode(token, secret, algorithms=["HS256"])
return payload
```

Admin 依赖没有校验 `role`：

```python
return verify_jwt(token, config)
```

#### 对抗验证

本轮使用空 secret 生成 HS256 JWT 并调用 `verify_jwt`，验证结果被接受。payload 中 `role` 即使不是 `admin` 也能通过，因为依赖未检查角色。

#### 影响

当前真实配置已存在非空 `jwt_secret`，所以这不是当前实例立即可利用的漏洞。但复制 `config.json.example`、迁移旧配置、手工清空字段时，会形成 admin JWT 伪造风险。

#### 修复建议

- `_load()` 中发现 `admin.jwt_secret` 为空时自动生成并保存。
- `verify_jwt` 或 `require_admin` 中强制校验 `payload.get("role") == "admin"`。
- 增加启动自检：空 `password_hash`、空 `jwt_secret` 视为 fatal 或自动修复。
- 增加回归测试：空 secret 会被补齐；`role != admin` 被拒绝。

### P2-06: 10MB 请求体限制只依赖 `Content-Length`

严重度：P2 / Medium  
类别：Unrestricted Resource Consumption  
当前状态：已修复（ASGI receive 包装器累计 `http.request.body` 字节数，超限返回 413，见 `tests/test_security_hardening.py`）  
文件：`tabbit2api.py:112`

#### 证据

当前限制只检查 header：

```python
content_length = request.headers.get("content-length")
if content_length and int(content_length) > MAX_BODY_SIZE:
    return JSONResponse(status_code=413, ...)
```

如果请求没有 `Content-Length`，后续路由仍会执行 `request.json()` 或 Pydantic body parse。

#### 影响

chunked 或无长度请求可能绕过 10MB 限制，导致应用层读取超大 body，占用内存和 CPU。

#### 对抗验证

代码没有包裹 ASGI `receive` 流，也没有在读取 body 时累计长度。因此没有 `Content-Length` 时限制不生效。

#### 修复建议

- 在反向代理层配置 body size，例如 nginx `client_max_body_size`。
- 应用层实现 ASGI body limiter，累计读取字节超过阈值立即返回 413。
- 对 `/v1/*` 另设更贴近业务的消息长度、工具 schema 长度和历史条数限制。

## 4. 已确认的正向控制

- `config.json` 和 `data/` 被 `.gitignore` 排除，避免常规误提交本地 token。
- `.dockerignore` 排除了 `data/` 和 `config.json`，Docker build context 不会默认带入本地敏感配置。
- Token 列表接口返回时删除 `value`，只暴露 `value_preview`。
- Admin 大部分接口有 `require_admin` 依赖。
- Logs 页面关键字段已使用 `esc()`，可作为 Dashboard 修复参照。
- 密码新格式使用 bcrypt，首次启动生成随机初始密码。

## 5. 验证记录

已执行：

```bash
python3 -m compileall -q tabbit2api.py core routes scripts
.venv/bin/python -m pip check
jq '{server_host:.server.host, has_proxy_api_key:((.proxy.api_key // "")|length>0), token_count:(.tokens|length), enabled_token_count:([.tokens[]? | select(.enabled != false)]|length), verify_ssl:.tabbit.verify_ssl}' config.json
```

结果：

- Python 语法编译通过。
- `.venv` 依赖一致性检查通过：`No broken requirements found.`
- 本地配置确认 `host=0.0.0.0`、`proxy.api_key` 为空、存在 1 个 enabled token、`verify_ssl=false`。

未执行：

- `pip-audit`：本机未安装。
- `safety`：本机未安装。
- `semgrep`：本机未安装。
- 项目单元测试：仓库说明中未配置测试套件。

## 6. 建议修复顺序

### 第一批：立即阻断外部滥用

1. token 池非空但 `proxy.api_key` 为空时，`/v1/*` fail-closed：已完成。
2. 默认 host 改为 `127.0.0.1`：未硬改，避免破坏 Docker 默认可访问性；公网部署需由反代/防火墙收口。
3. 修复 `X-Forwarded-For` 信任边界：已完成。

### 第二批：阻断 admin 面板二次利用

1. Dashboard 日志字段全部 escape。
2. 给 admin 页面加 CSP，移除或固定远程 CDN 资源。
3. Admin JWT 从 `localStorage` 迁移到 HttpOnly cookie，或至少缩短有效期并增加 token rotation。

### 第三批：配置与供应链加固

1. 默认 `verify_ssl=true`：已完成。
2. 空 `jwt_secret` 自动生成并保存，`role != admin` 拒绝：已完成。
3. 增加 ASGI body limiter：已完成。
4. 引入基础测试与安全扫描：`pytest`、`pip-audit`、针对以上 findings 的回归测试。

## 7. 残余风险

- 本轮未对 Tabbit 上游协议本身做授权或合规判断，只审查本项目实现边界。
- 未进行真实浏览器 XSS 执行验证，结论基于代码级 source 到 sink 追踪。
- 未运行 SCA/CVE 审计，因为本机缺少 `pip-audit` / `safety`。
- 未对 `.code-abyss/`、`.omo/`、未跟踪脚本做内容审计；当前 git 状态显示它们为未跟踪项。
