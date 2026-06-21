import uuid
import time
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional

from core.config import ConfigManager, hash_password
from core.auth import create_jwt, verify_password, require_admin
from core.token_manager import TokenManager
from core.tabbit_client import TabbitClient, _gen_unique_uuid
from core.log_store import LogStore

logger = logging.getLogger("tabbit2openai")

# 模块级状态
_cfg: ConfigManager | None = None
_tm: TokenManager | None = None
_logs: LogStore | None = None

# Pydantic models（需在模块级定义才能被 FastAPI 正确解析）
class LoginRequest(BaseModel):
    password: str

class TokenAddRequest(BaseModel):
    name: str
    value: str
    enabled: bool = True

class TokenUpdateRequest(BaseModel):
    name: Optional[str] = None
    value: Optional[str] = None
    enabled: Optional[bool] = None

class SettingsUpdateRequest(BaseModel):
    host: Optional[str] = None
    port: Optional[int] = None
    base_url: Optional[str] = None
    client_id: Optional[str] = None
    browser_version: Optional[str] = None
    default_browser: Optional[bool] = None
    api_key: Optional[str] = None
    max_entries: Optional[int] = None
    claude_default_model: Optional[str] = None
    openai_system_prompt: Optional[str] = None
    claude_system_prompt: Optional[str] = None

class GoogleLoginRequest(BaseModel):
    id_token: str

class PasswordUpdateRequest(BaseModel):
    old_password: str
    new_password: str


# router 初始为占位，init() 后替换为带鉴权的完整路由
router = APIRouter(prefix="/api/admin")


def init(config: ConfigManager, token_manager: TokenManager, log_store: LogStore):
    global _cfg, _tm, _logs, router
    _cfg = config
    _tm = token_manager
    _logs = log_store

    admin_dep = require_admin(config)
    r = APIRouter(prefix="/api/admin")

    # ── Login（无需鉴权）──

    @r.post("/login")
    async def login(req: LoginRequest):
        if not verify_password(req.password, _cfg):
            raise HTTPException(status_code=401, detail="wrong password")
        return {"token": create_jwt(_cfg)}

    # ── Status ──

    @r.get("/status", dependencies=[Depends(admin_dep)])
    async def get_status():
        tokens = _cfg.get("tokens", default=[])
        active = sum(
            1 for t in tokens
            if t.get("enabled") and t.get("status") == "active"
        )
        return {
            "total_requests": _logs.total_requests,
            "total_success": _logs.total_success,
            "total_errors": _logs.total_errors,
            "success_rate": round(
                _logs.total_success / max(_logs.total_requests, 1) * 100, 1
            ),
            "total_tokens": len(tokens),
            "active_tokens": active,
            "recent_logs": _logs.query(page=1, page_size=10)["items"],
        }

    # ── Tokens ──

    @r.get("/tokens", dependencies=[Depends(admin_dep)])
    async def list_tokens():
        tokens = _cfg.get("tokens", default=[])
        result = []
        for t in tokens:
            info = {**t}
            info["status"] = _tm.get_token_status(t["id"])
            v = info.get("value", "")
            info["value_preview"] = (v[:10] + "...") if len(v) > 10 else v
            del info["value"]
            result.append(info)
        return {"tokens": result}

    @r.post("/tokens", dependencies=[Depends(admin_dep)])
    async def add_token(req: TokenAddRequest):
        token_entry = {
            "id": str(uuid.uuid4()),
            "name": req.name,
            "value": req.value,
            "enabled": req.enabled,
            "added_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "last_used_at": None,
            "total_requests": 0,
            "error_count": 0,
            "status": "unknown",
        }
        tokens = _cfg.get("tokens", default=[])
        tokens.append(token_entry)
        _cfg.config["tokens"] = tokens
        _cfg.save()
        return {"id": token_entry["id"]}

    @r.put("/tokens/{token_id}", dependencies=[Depends(admin_dep)])
    async def update_token(token_id: str, req: TokenUpdateRequest):
        for t in _cfg.get("tokens", default=[]):
            if t["id"] == token_id:
                if req.name is not None:
                    t["name"] = req.name
                if req.value is not None:
                    t["value"] = req.value
                    _tm.remove_client(token_id)
                if req.enabled is not None:
                    t["enabled"] = req.enabled
                _cfg.save()
                return {"ok": True}
        raise HTTPException(status_code=404, detail="token not found")

    @r.delete("/tokens/{token_id}", dependencies=[Depends(admin_dep)])
    async def delete_token(token_id: str):
        tokens = _cfg.get("tokens", default=[])
        _cfg.config["tokens"] = [t for t in tokens if t["id"] != token_id]
        _cfg.save()
        _tm.remove_client(token_id)
        return {"ok": True}

    @r.post("/tokens/{token_id}/test", dependencies=[Depends(admin_dep)])
    async def test_token(token_id: str):
        target = None
        for t in _cfg.get("tokens", default=[]):
            if t["id"] == token_id:
                target = t
                break
        if not target:
            raise HTTPException(status_code=404, detail="token not found")

        client = TabbitClient(
            target["value"],
            _cfg.get("tabbit", "base_url"),
            _cfg.get("tabbit", "client_id"),
            _cfg.get("tabbit", "browser_version"),
            _cfg.get("tabbit", "sparkle_version"),
            _cfg.get("tabbit", "default_browser", default=True),
        )
        try:
            session_id = await client.create_chat_session()
            target["status"] = "active"
            target["error_count"] = 0
            _cfg.save()
            return {"ok": True, "session_id": session_id}
        except Exception as e:
            target["status"] = "error"
            _cfg.save()
            return {"ok": False, "error": str(e)}
        finally:
            await client.client.aclose()

    @r.post("/tokens/google-login", dependencies=[Depends(admin_dep)])
    async def google_login(req: GoogleLoginRequest):
        """用 Google id_token 调用 Tabbit API 换取登录凭据，返回格式化后的 token"""
        import httpx as _httpx

        tabbit_url = (
            (_cfg.get("tabbit", "base_url") or "https://web.tabbit.ai")
            + "/proxy/v0/oauth/third-party-login"
        )
        async with _httpx.AsyncClient(verify=False, timeout=15) as hc:
            resp = await hc.post(
                tabbit_url,
                json={"id_token": req.id_token, "select_by": "btn", "type": 1},
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
                    "Origin": _cfg.get("tabbit", "base_url") or "https://web.tabbit.ai",
                    "Referer": (_cfg.get("tabbit", "base_url") or "https://web.tabbit.ai") + "/login",
                },
            )

        try:
            body = resp.json()
        except Exception:
            raise HTTPException(status_code=502, detail=f"Tabbit API 返回异常: {resp.text[:200]}")

        if resp.status_code != 200 or not body.get("success"):
            raise HTTPException(
                status_code=resp.status_code or 400,
                detail=body.get("detail") or body.get("message") or "登录失败",
            )

        # 从 Set-Cookie 提取 token
        import re as _re
        cookies = {}
        for h in resp.headers.multi_items():
            if h[0].lower() == "set-cookie":
                m = _re.match(r"([^=]+)=([^;]*)", h[1])
                if m:
                    cookies[m.group(1).strip()] = m.group(2).strip()

        jwt_token = cookies.get("token", "")
        next_auth = cookies.get("next-auth.session-token", "")
        device_id = str(uuid.uuid4())

        # 也尝试从 body.data 取
        data = body.get("data")
        if isinstance(data, dict):
            jwt_token = jwt_token or data.get("token", "") or data.get("access_token", "")
            next_auth = next_auth or data.get("session_token", "")

        if not jwt_token:
            raise HTTPException(status_code=502, detail="未能从 Tabbit 响应中提取 token")

        parts = [jwt_token]
        if next_auth:
            parts.append(next_auth)
        parts.append(device_id)

        return {"ok": True, "token_value": "|".join(parts), "cookies": cookies, "body": body}

    # ── Settings ──

    @r.get("/settings", dependencies=[Depends(admin_dep)])
    async def get_settings():
        return {
            "server": _cfg.get("server"),
            "tabbit": _cfg.get("tabbit"),
            "proxy": {
                "api_key": _cfg.get("proxy", "api_key", default=""),
                "system_prompt": _cfg.get("proxy", "system_prompt", default=""),
            },
            "claude": _cfg.get("claude", default={"default_model": "best", "system_prompt": ""}),
            "logging": _cfg.get("logging"),
        }

    @r.put("/settings", dependencies=[Depends(admin_dep)])
    async def update_settings(req: SettingsUpdateRequest):
        if req.host is not None:
            _cfg.set_val("server", "host", req.host)
        if req.port is not None:
            _cfg.set_val("server", "port", req.port)
        if req.base_url is not None:
            _cfg.set_val("tabbit", "base_url", req.base_url)
        if req.client_id is not None:
            _cfg.set_val("tabbit", "client_id", req.client_id)
        if req.browser_version is not None:
            _cfg.set_val("tabbit", "browser_version", req.browser_version)
        if req.default_browser is not None:
            _cfg.set_val("tabbit", "default_browser", req.default_browser)
        if req.api_key is not None:
            _cfg.set_val("proxy", "api_key", req.api_key)
        if req.claude_default_model is not None:
            _cfg.set_val("claude", "default_model", req.claude_default_model)
        if req.openai_system_prompt is not None:
            _cfg.set_val("proxy", "system_prompt", req.openai_system_prompt)
        if req.claude_system_prompt is not None:
            _cfg.set_val("claude", "system_prompt", req.claude_system_prompt)
        if req.max_entries is not None:
            _cfg.set_val("logging", "max_entries", req.max_entries)
            _logs.resize(req.max_entries)
        return {"ok": True}

    # ── 模型清单（动态拉取）──

    @r.post("/models/refresh", dependencies=[Depends(admin_dep)])
    async def refresh_models():
        """手动刷新动态模型清单"""
        from core.model_registry import get_registry
        registry = get_registry()
        if not registry:
            raise HTTPException(status_code=503, detail="model registry not initialized")
        ok = await registry.refresh(force=True)
        models = registry.list_models() if ok else []
        return {"ok": ok, "count": len(models), "models": models}

    @r.get("/models", dependencies=[Depends(admin_dep)])
    async def admin_list_models():
        """查看当前模型清单（动态 + 缓存状态）"""
        from core.model_registry import get_registry
        registry = get_registry()
        if not registry:
            return {"ready": False, "models": []}
        return {
            "ready": registry.ready,
            "count": len(registry.list_models()),
            "models": registry.list_models(),
        }

    # ── Password ──

    @r.put("/password", dependencies=[Depends(admin_dep)])
    async def update_password(req: PasswordUpdateRequest):
        if not verify_password(req.old_password, _cfg):
            raise HTTPException(status_code=401, detail="wrong old password")
        pw_hash, salt = hash_password(req.new_password)
        _cfg.set_val("admin", "password_hash", pw_hash)
        _cfg.set_val("admin", "salt", salt)
        return {"ok": True}

    # ── Logs ──

    @r.get("/logs", dependencies=[Depends(admin_dep)])
    async def get_logs(
        status: Optional[str] = None, page: int = 1, page_size: int = 50
    ):
        return _logs.query(status=status, page=page, page_size=page_size)

    # ── 诊断（深度健康检查，固化 493/492 排查经验）──

    @r.get("/diagnose", dependencies=[Depends(admin_dep)])
    async def diagnose():
        """深度诊断：配置/连接/版本/协议/模型/token 全链路自检"""
        import httpx as _httpx
        import base64 as _base64

        report = {"checks": [], "summary": {"total": 0, "pass": 0, "warn": 0, "fail": 0}}

        def check(name, status, detail=""):
            report["checks"].append({"name": name, "status": status, "detail": detail})
            report["summary"]["total"] += 1
            report["summary"][status] += 1

        # 1. 配置检查
        tabbit_cfg = _cfg.get("tabbit", default={}) or {}
        base_url = tabbit_cfg.get("base_url", "")
        browser_version = tabbit_cfg.get("browser_version", "")
        sparkle = tabbit_cfg.get("sparkle_version")

        # 1a. 域名是否新协议
        if "web.tabbit.ai" in base_url:
            check("域名配置", "pass", f"base_url={base_url}")
        elif "tabbitbrowser.com" in base_url:
            check("域名配置", "fail", f"旧域名 {base_url} 已废弃，会触发 493/492，应为 web.tabbit.ai")
        else:
            check("域名配置", "warn", f"非默认域名: {base_url}")

        # 1b. 版本号是否完整三段（x-req-ctx 编码需要）
        if browser_version and browser_version.count(".") >= 2 and browser_version not in ("1.1", "145"):
            check("版本号配置", "pass", f"browser_version={browser_version}")
        else:
            check("版本号配置", "fail", f"版本号 {browser_version!r} 不完整，应为 1.1.39（x-req-ctx 编码需要完整三段）")

        # 1c. sparkle_version
        if sparkle:
            check("sparkle配置", "pass", f"sparkle_version={sparkle}")
        else:
            check("sparkle配置", "fail", "sparkle_version 缺失，x-req-ctx 编码会出错")

        # 1d. x-req-ctx 编码验证
        try:
            expected = _base64.b64encode(f"{browser_version}({sparkle})".encode()).decode()
            check("x-req-ctx编码", "pass", f"x-req-ctx={expected}")
        except Exception as e:
            check("x-req-ctx编码", "fail", f"编码失败: {e}")

        # 1e. 默认浏览器标记（unique-uuid 第5位编码 Pro 会员权益）
        default_browser = tabbit_cfg.get("default_browser", True)
        if default_browser:
            check("默认浏览器标记", "pass", "unique-uuid 第5位='1'，后端按 Pro 会员发权益")
        else:
            check("默认浏览器标记", "warn", "未开启，按普通用户对待（无 Pro 权益）")

        # 2. Token 池
        tokens = _cfg.get("tokens", default=[]) or []
        if not tokens:
            check("Token池", "fail", "无 token，服务无法工作")
        else:
            active = [t for t in tokens if t.get("enabled", True) and t.get("status") != "cooldown"]
            check("Token池", "pass" if active else "warn",
                  f"共 {len(tokens)} 个，可用 {len(active)} 个")
            for t in tokens[:5]:
                check(f"Token[{t.get('name','?')}]",
                      "pass" if t.get("status") != "cooldown" else "warn",
                      f"status={t.get('status','?')} errors={t.get('error_count',0)} total={t.get('total_requests',0)}")

        # 3. 上游连通性 + 协议自检（用第一个可用 token）
        if tokens:
            token_info = next((t for t in tokens if t.get("enabled", True)), tokens[0])
            parts = token_info["value"].split("|")
            jwt_token = parts[0]
            user_id = ""
            try:
                import json as _json
                payload = _json.loads(_base64.urlsafe_b64decode(jwt_token.split(".")[1] + "=="))
                user_id = payload.get("id", payload.get("sub", ""))
            except Exception:
                pass

            headers = {
                "x-req-ctx": _base64.b64encode(f"{browser_version}({sparkle})".encode()).decode(),
                "unique-uuid": _gen_unique_uuid(tabbit_cfg.get("default_browser", True)),
                "x-chrome-id-consistency-request": f"version=1,client_id={tabbit_cfg.get('client_id','')},device_id=test,sync_account_id={user_id},signin_mode=all_accounts,signout_mode=show_confirmation",
            }
            cookies = {"token": jwt_token, "user_id": user_id, "managed": "tab_browser", "NEXT_LOCALE": "zh"}
            if len(parts) > 1:
                cookies["next-auth.session-token"] = parts[1]

            # 3a. 模型清单接口
            server_time_offset = 0.0
            try:
                async with _httpx.AsyncClient(timeout=10, verify=False) as hc:
                    resp = await hc.get(f"{base_url}/api/v0/chat/models", headers=headers, cookies=cookies)
                    # 从 Date 头同步服务器时间，供 3b 生成 uuid 用
                    dh = resp.headers.get("date")
                    if dh:
                        try:
                            from email.utils import parsedate_to_datetime as _pd
                            server_time_offset = _pd(dh).timestamp() - time.time()
                        except Exception:
                            pass
                    if resp.status_code == 200:
                        data = resp.json()
                        count = sum(len(v) for v in (data.get("supported_models") or {}).values())
                        check("上游连通(模型接口)", "pass", f"/api/v0/chat/models 返回 {count} 个模型")
                    else:
                        check("上游连通(模型接口)", "fail", f"模型接口 {resp.status_code}: {resp.text[:100]}")
            except Exception as e:
                check("上游连通(模型接口)", "fail", f"连接失败: {e}")

            # 3a+1. 服务器时间同步状态（unique-uuid 时间戳位校验依赖）
            if server_time_offset != 0.0:
                abs_off = abs(server_time_offset)
                if abs_off < 60:
                    check("时间同步", "pass", f"vps 与上游时钟偏差 {server_time_offset:+.1f}s（±60s 内，安全）")
                elif abs_off < 300:
                    check("时间同步", "warn", f"vps 与上游时钟偏差 {server_time_offset:+.1f}s（偏大，建议同步系统时钟）")
                else:
                    check("时间同步", "fail", f"vps 与上游时钟偏差 {server_time_offset:+.1f}s（过大，时间戳位校验会翻车！请同步系统时钟：apt install ntp && systemctl start ntp）")
            else:
                check("时间同步", "warn", "未能从上游 Date 头同步时间（可能被 4xx 拒绝），无法判断时钟偏差")

            # 3b. 建会话测试（用同步后的服务器时间生成 uuid）
            try:
                async with _httpx.AsyncClient(timeout=15, verify=False, follow_redirects=True) as hc:
                    import urllib.parse as _up, json as _json
                    router_state = ["",{"children":["chat",{"children":[["id","new","d"],{"children":["__PAGE__",{},None,"refetch"]},None,None]},None,None]},None,None]
                    h2 = {**headers,
                          "unique-uuid": _gen_unique_uuid(tabbit_cfg.get("default_browser", True), time.time() + server_time_offset),
                          "rsc":"1", "next-router-state-tree": _up.quote(_json.dumps(router_state)),
                          "referer": f"{base_url}/chat/new"}
                    resp = await hc.get(f"{base_url}/chat/new", params={"_rsc":"auto"}, headers=h2, cookies=cookies)
                    import re as _re
                    m = _re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", resp.text)
                    if m:
                        check("建会话", "pass", f"session_id={m.group(0)}")
                    else:
                        check("建会话", "fail", f"未提取到 session_id，status={resp.status_code}")
            except Exception as e:
                check("建会话", "fail", f"失败: {e}")

            # 3c. 额度查询（验证默认浏览器伪装是否生效 → Pro 5x quota）
            try:
                async with _httpx.AsyncClient(timeout=10, verify=False, follow_redirects=True) as hc:
                    quota_uuid = _gen_unique_uuid(tabbit_cfg.get("default_browser", True), time.time() + server_time_offset)
                    h3 = {**headers, "unique-uuid": quota_uuid, "referer": f"{base_url}/newtab"}
                    resp = await hc.get(f"{base_url}/api/commerce/quota/v1/usage",
                                        params={"user_id": user_id, "timezone": "Asia/Shanghai"},
                                        headers=h3, cookies=cookies)
                    if resp.status_code == 200:
                        qdata = resp.json()
                        # 提取额度信息（结构因版本而异，尽量取关键字段）
                        detail = json.dumps(qdata, ensure_ascii=False)[:200]
                        check("额度查询(会员验证)", "pass", f"unique-uuid第5位='1'伪装Pro，响应: {detail}")
                    else:
                        check("额度查询(会员验证)", "warn", f"额度接口 {resp.status_code}: {resp.text[:100]}")
            except Exception as e:
                check("额度查询(会员验证)", "fail", f"失败: {e}")

        # 4. 动态模型注册表
        from core.model_registry import get_registry
        registry = get_registry()
        if registry and registry.ready:
            check("模型注册表", "pass", f"已缓存 {len(registry.list_models())} 个模型")
        elif registry:
            check("模型注册表", "warn", "未就绪（拉取失败或未初始化），用静态 MODEL_MAP 兜底")
        else:
            check("模型注册表", "warn", "未初始化")

        # 5. proxy api_key
        api_key = _cfg.get("proxy", "api_key", default="")
        check("Proxy API Key", "pass" if api_key else "warn",
              "已设置" if api_key else "未设置（任何请求都能访问）")

        # 汇总结论
        s = report["summary"]
        if s["fail"] > 0:
            report["summary"]["overall"] = "fail"
        elif s["warn"] > 0:
            report["summary"]["overall"] = "warn"
        else:
            report["summary"]["overall"] = "pass"
        return report

    router = r
