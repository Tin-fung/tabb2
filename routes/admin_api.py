import uuid
import time
import logging
import base64
import json
import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional

from core.config import ConfigManager, hash_password
from core.auth import create_jwt, verify_password, require_admin
from core.token_manager import TokenManager, token_expiration_metadata
from core.tabbit_client import TabbitClient
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
    sparkle_version: Optional[int] = None
    default_browser: Optional[bool] = None
    verify_ssl: Optional[bool] = None
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


GENERIC_TOKEN_NAME_RE = re.compile(r"^google\s+account(?:\s*(?:#\s*)?\d+)?$", re.IGNORECASE)


def _decode_jwt_payload(token: str) -> dict:
    parts = (token or "").split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1]
    payload += "=" * ((4 - len(payload) % 4) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def token_account_label(token_value: str) -> str:
    """Best-effort account label extracted from Google/Tabbit JWT payloads.

    This is for admin display only; authentication still relies on the token
    value returned by Tabbit.
    """
    first_token = (token_value or "").split("|", 1)[0]
    payload = _decode_jwt_payload(first_token)
    for key in ("email", "name", "preferred_username", "login", "id", "sub"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def is_generic_token_name(name: str | None) -> bool:
    return not (name or "").strip() or bool(GENERIC_TOKEN_NAME_RE.match((name or "").strip()))


def _unique_token_name(base: str, existing_tokens: list[dict]) -> str:
    candidate = (base or "Google Account").strip()
    existing = set()
    for token in existing_tokens:
        name = (token.get("name") or "").strip()
        if name:
            existing.add(name.lower())
        account_label = token_account_label(token.get("value", ""))
        if account_label:
            existing.add(account_label.lower())
    if candidate.lower() not in existing:
        return candidate
    index = 2
    while f"{candidate} #{index}".lower() in existing:
        index += 1
    return f"{candidate} #{index}"


def suggest_token_name(
    requested_name: str | None,
    token_value: str,
    existing_tokens: list[dict],
    *,
    identity_token: str | None = None,
) -> str:
    requested = (requested_name or "").strip()
    if requested and not is_generic_token_name(requested):
        return requested
    label = token_account_label(identity_token or "") or token_account_label(token_value)
    return _unique_token_name(label or "Google Account", existing_tokens)


# router 初始为占位，init() 后替换为带鉴权的完整路由
router = APIRouter(prefix="/api/admin")


def _get_admin_client(token_id: str | None = None):
    """获取 admin 用的 (token_info, client)，复用 TokenManager 缓存。
    不传 token_id 取第一个 enabled token。"""
    tokens = _cfg.get("tokens", default=[]) or []
    if token_id:
        info, client = _tm.get_client_for_token(token_id)
        if not info:
            return None, None
        return info, client
    target = next((t for t in tokens if t.get("enabled", True)), None)
    if not target:
        return None, None
    info, client = _tm.get_client_for_token(target["id"])
    return info, client


async def _safe_overview_call(label: str, func):
    try:
        return {"ok": True, "data": await func()}
    except Exception as e:
        return {"ok": False, "error": str(e), "data": None, "label": label}


async def build_quota_overview(
    tokens: list[dict],
    client_for_token,
    usage_limit: int = 20,
) -> dict:
    enabled_tokens = [t for t in tokens if t.get("enabled", True)]
    accounts = []
    usage_records = []

    for token in enabled_tokens:
        token_id = token.get("id", "")
        token_name = token.get("name", "")
        account_label = token_account_label(token.get("value", ""))
        display_name = (
            account_label
            if account_label and is_generic_token_name(token_name)
            else token_name
        )
        info, client = await client_for_token(token_id)
        account = {
            "token_id": token_id,
            "token_name": token_name,
            "account_label": account_label,
            "display_name": display_name,
            "enabled": token.get("enabled", True),
            "status": token.get("status", "unknown"),
        }

        if not client:
            unavailable = {
                "ok": False,
                "error": "client not available",
                "data": None,
            }
            account.update(
                {
                    "quota": unavailable,
                    "coupons": unavailable,
                    "sign_in": unavailable,
                }
            )
            usage_records.append(
                {
                    "token_id": token_id,
                    "token_name": token_name,
                    "account_label": account_label,
                    "display_name": display_name,
                    "ok": False,
                    "error": "client not available",
                    "records": [],
                }
            )
            accounts.append(account)
            continue

        account["quota"] = await _safe_overview_call("quota", client.get_quota_usage)
        account["coupons"] = await _safe_overview_call(
            "coupons",
            lambda: client.get_coupon_list(),
        )
        account["sign_in"] = await _safe_overview_call(
            "sign_in",
            client.get_sign_in_status,
        )

        try:
            records = await client.get_usage_records(page=1, limit=usage_limit)
            usage_records.append(
                {
                    "token_id": token_id,
                    "token_name": token_name,
                    "account_label": account_label,
                    "display_name": display_name,
                    "ok": True,
                    "records": (records or {}).get("records", []),
                }
            )
        except Exception as e:
            usage_records.append(
                {
                    "token_id": token_id,
                    "token_name": token_name,
                    "account_label": account_label,
                    "display_name": display_name,
                    "ok": False,
                    "error": str(e),
                    "records": [],
                }
            )

        accounts.append(account)

    return {"ok": True, "accounts": accounts, "usage_records": usage_records}


def _time_sync_status(offset: float) -> tuple[str, str]:
    abs_off = abs(offset)
    if abs_off < 60:
        return "pass", f"vps 与上游时钟偏差 {offset:+.1f}s（±60s 内，安全）"
    if abs_off < 300:
        return "warn", f"vps 与上游时钟偏差 {offset:+.1f}s（偏大，建议同步系统时钟）"
    return (
        "fail",
        f"vps 与上游时钟偏差 {offset:+.1f}s（过大，时间戳位校验会翻车！请同步系统时钟）",
    )


async def run_diagnose_model_registry_check(check, registry, token_value: str | None = None) -> None:
    from core.model_registry import MODELS_API_PATH

    if not registry:
        check("上游连通(模型配置)", "warn", "模型注册表未初始化")
        return

    if token_value:
        registry.update_token(token_value)

    ok = await registry.refresh(force=True)
    models = registry.list_models() if (ok or registry.ready) else []
    snapshot = registry.snapshot_info
    if models:
        from_snapshot = bool(snapshot.get("from_snapshot"))
        status = "warn" if from_snapshot else "pass"
        source = "快照兜底" if from_snapshot else "动态拉取"
        check(
            "上游连通(模型配置)",
            status,
            f"{MODELS_API_PATH} {source} {len(models)} 个模型",
        )
        return

    check("上游连通(模型配置)", "fail", f"{MODELS_API_PATH} 未能拉取模型清单")


async def run_diagnose_chat_session_check(
    check,
    client,
    *,
    token_id: str | None = None,
    token_manager=None,
) -> None:
    if not client:
        check("建会话", "fail", "client not available")
        check("时间同步", "warn", "未能创建会话，无法判断时钟偏差")
        return

    try:
        session_id = await client.create_chat_session()
        if token_id and token_manager:
            refresher = getattr(token_manager, "refresh_token_from_client", None)
            if callable(refresher):
                refresher(token_id, client)
        check("建会话", "pass", f"session_id={session_id}")

        if getattr(client, "_server_time_synced", False):
            status, detail = _time_sync_status(getattr(client, "_server_time_offset", 0.0))
            check("时间同步", status, detail)
        else:
            check("时间同步", "warn", "未能从上游 Date 头同步时间，无法判断时钟偏差")
    except Exception as e:
        check("建会话", "fail", f"失败: {e}")
        check("时间同步", "warn", "建会话失败，无法判断时钟偏差")


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
            account_label = token_account_label(v)
            info["account_label"] = account_label
            info["display_name"] = (
                account_label
                if account_label and is_generic_token_name(info.get("name"))
                else info.get("name", "")
            )
            info.update(token_expiration_metadata(v))
            info["needs_login"] = info["status"] == "needs_login"
            # 安全脱敏：只显示最后 4 位（类似信用卡显示方式）
            info["value_preview"] = "***" + v[-4:] if len(v) > 4 else "***"
            del info["value"]
            result.append(info)
        return {"tokens": result}

    @r.post("/tokens", dependencies=[Depends(admin_dep)])
    async def add_token(req: TokenAddRequest):
        tokens = _cfg.get("tokens", default=[])
        token_name = suggest_token_name(req.name, req.value, tokens)
        token_entry = {
            "id": str(uuid.uuid4()),
            "name": token_name,
            "value": req.value,
            "enabled": req.enabled,
            "added_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "last_used_at": None,
            "total_requests": 0,
            "error_count": 0,
            "status": "unknown",
        }
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
        verify_ssl = _cfg.get("tabbit", "verify_ssl", default=False)
        async with _httpx.AsyncClient(verify=verify_ssl, timeout=15) as hc:
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
        token_value = "|".join(parts)
        suggested_name = suggest_token_name(
            "",
            token_value,
            _cfg.get("tokens", default=[]) or [],
            identity_token=req.id_token,
        )

        return {
            "ok": True,
            "token_value": token_value,
            "suggested_name": suggested_name,
            "account_label": token_account_label(req.id_token) or token_account_label(token_value),
            "cookies": cookies,
            "body": body,
        }

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
        if req.sparkle_version is not None:
            _cfg.set_val("tabbit", "sparkle_version", req.sparkle_version)
        if req.default_browser is not None:
            _cfg.set_val("tabbit", "default_browser", req.default_browser)
        if req.verify_ssl is not None:
            _cfg.set_val("tabbit", "verify_ssl", req.verify_ssl)
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
        # 确保 registry 有认证 token（运行时可能新增了 token）
        if not registry._token_str:
            tokens = _cfg.get("tokens", default=[]) or []
            first = next((t["value"] for t in tokens if t.get("enabled", True)), None)
            if first:
                registry.update_token(first)
        ok = await registry.refresh(force=True)
        models = registry.list_models() if ok else []
        return {"ok": ok, "count": len(models), "models": models, "snapshot": registry.snapshot_info}

    @r.get("/models", dependencies=[Depends(admin_dep)])
    async def admin_list_models():
        """查看当前模型清单（动态 + 缓存状态）"""
        from core.model_registry import get_registry
        registry = get_registry()
        if not registry:
            return {"ready": False, "models": [], "snapshot": {"from_snapshot": False, "snapshot_age": -1, "snapshot_count": 0}}
        return {
            "ready": registry.ready,
            "count": len(registry.list_models()),
            "models": registry.list_models(),
            "snapshot": registry.snapshot_info,
        }

    # ── Password ──

    @r.put("/password", dependencies=[Depends(admin_dep)])
    async def update_password(req: PasswordUpdateRequest):
        if not verify_password(req.old_password, _cfg):
            raise HTTPException(status_code=401, detail="wrong old password")
        pw_hash = hash_password(req.new_password)
        _cfg.set_val("admin", "password_hash", pw_hash)
        _cfg.set_val("admin", "salt", "")  # bcrypt 自带 salt，清空旧字段
        return {"ok": True}

    # ── Logs ──

    @r.get("/logs", dependencies=[Depends(admin_dep)])
    async def get_logs(
        status: Optional[str] = None, page: int = 1, page_size: int = 50
    ):
        return _logs.query(status=status, page=page, page_size=page_size)

    # ── 版本检测（读 Sparkle appcast.xml，查最新版本）──
    @r.get("/version", dependencies=[Depends(admin_dep)])
    async def check_version():
        """查 Tabbit 最新版本（读 Sparkle appcast 更新清单）。

        appcast 地址：{base_url}/api/v0/upgrade/appcast.xml（从 Sparkle.framework
        二进制反查得到，无需认证，公开可读）。

        返回当前配置版本 vs appcast 最新版本，is_latest 判断是否需要同步。
        版本过期会导致 x-req-ctx 校验失败触发 493。
        """
        import httpx as _httpx
        import re as _re
        tabbit_cfg = _cfg.get("tabbit", default={}) or {}
        current_bv = tabbit_cfg.get("browser_version", "")
        current_sv = tabbit_cfg.get("sparkle_version")
        base_url = tabbit_cfg.get("base_url", "https://web.tabbit.ai")
        appcast_url = f"{base_url}/api/v0/upgrade/appcast.xml"

        try:
            verify_ssl = _cfg.get("tabbit", "verify_ssl", default=False)
            async with _httpx.AsyncClient(timeout=10, verify=verify_ssl) as hc:
                resp = await hc.get(appcast_url, headers={
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                    "Accept": "application/rss+xml,application/xml,*/*",
                })
            if resp.status_code != 200:
                return {"ok": False, "error": f"appcast 接口 {resp.status_code}",
                        "current": {"browser_version": current_bv, "sparkle_version": current_sv},
                        "appcast_url": appcast_url}
            xml = resp.text

            # 解析 appcast XML（Sparkle 格式）
            # <sparkle:shortVersionString>1.1.39</sparkle:shortVersionString>
            # <sparkle:version>10101039</sparkle:version>
            # <pubDate>Mon, 15 Jun 2026 ...</pubDate>
            # <enclosure url="...dmg" />
            latest_bv_m = _re.search(r"<sparkle:shortVersionString>([^<]+)</sparkle:shortVersionString>", xml)
            latest_sv_m = _re.search(r"<sparkle:version>([^<]+)</sparkle:version>", xml)
            pubdate_m = _re.search(r"<pubDate>([^<]+)</pubDate>", xml)
            enclosure_m = _re.search(r'url="([^"]+\.dmg[^"]*)"', xml)
            # 提取更新说明（title 里的文本）
            title_m = _re.search(r"<title>([^<]+)</title>", xml)
            # 第一个 <title> 是 channel 名（如 "tab"），item 的 title 在后面
            item_title_m = _re.findall(r"<title>([^<]+)</title>", xml)

            latest_bv = latest_bv_m.group(1).strip() if latest_bv_m else None
            latest_sv = latest_sv_m.group(1).strip() if latest_sv_m else None
            pubdate = pubdate_m.group(1).strip() if pubdate_m else None
            download_url = enclosure_m.group(1) if enclosure_m else None
            # 更新说明取第二个 title（第一个是 channel 名）
            release_notes = item_title_m[1][:300] if len(item_title_m) > 1 else None

            is_latest = None
            if latest_bv and latest_sv:
                is_latest = (str(latest_bv) == str(current_bv)
                             and str(latest_sv) == str(current_sv))

            return {
                "ok": True,
                "current": {"browser_version": current_bv, "sparkle_version": current_sv},
                "latest": {"browser_version": latest_bv, "sparkle_version": latest_sv},
                "is_latest": is_latest,
                "pub_date": pubdate,
                "download_url": download_url,
                "release_notes": release_notes,
                "appcast_url": appcast_url,
            }
        except Exception as e:
            return {"ok": False, "error": str(e),
                    "current": {"browser_version": current_bv, "sparkle_version": current_sv},
                    "appcast_url": appcast_url}

    # ── 额度查询（验证默认浏览器伪装是否生效 → Pro 5x quota）──
    @r.get("/quota/overview", dependencies=[Depends(admin_dep)])
    async def query_quota_overview():
        tokens = _cfg.get("tokens", default=[]) or []

        async def client_for_token(token_id):
            return _tm.get_client_for_token(token_id)

        return await build_quota_overview(tokens, client_for_token)

    @r.get("/quota", dependencies=[Depends(admin_dep)])
    async def query_quota(token_id: Optional[str] = None):
        """查询账号额度使用情况。

        不传 token_id：查所有 enabled token 的额度（对比各账号 Pro 状态）。
        传 token_id：只查指定 token。
        unique-uuid 第5位编码默认浏览器标记，后端据此发 Pro 权益（5x quota）。
        """
        tokens = _cfg.get("tokens", default=[]) or []
        if token_id:
            targets = [t for t in tokens if t["id"] == token_id]
            if not targets:
                raise HTTPException(status_code=404, detail="token not found")
        else:
            targets = [t for t in tokens if t.get("enabled", True)]

        if not targets:
            return {"ok": False, "error": "无可用 token", "results": []}

        results = []
        for t in targets:
            info, client = _tm.get_client_for_token(t["id"])
            if not client:
                results.append({
                    "token_id": t["id"],
                    "token_name": t.get("name", ""),
                    "ok": False,
                    "error": "client not available",
                })
                continue
            try:
                quota = await client.get_quota_usage()
                results.append({
                    "token_id": t["id"],
                    "token_name": t.get("name", ""),
                    "ok": True,
                    "quota": quota,
                })
            except Exception as e:
                results.append({
                    "token_id": t["id"],
                    "token_name": t.get("name", ""),
                    "ok": False,
                    "error": str(e),
                })
        return {"ok": True, "results": results}

    # ── 重置券列表查询 ──
    @r.get("/coupons", dependencies=[Depends(admin_dep)])
    async def query_coupons(token_id: Optional[str] = None, coupon_type: str = "weekly_reset_coupon", status: int = 1):
        """查询可用重置券列表。

        不传 token_id：查第一个 enabled token 的重置券。
        传 token_id：查指定 token 的重置券。
        """
        tokens = _cfg.get("tokens", default=[]) or []
        if token_id:
            targets = [t for t in tokens if t["id"] == token_id]
            if not targets:
                raise HTTPException(status_code=404, detail="token not found")
        else:
            targets = [t for t in tokens if t.get("enabled", True)]

        if not targets:
            return {"ok": False, "error": "无可用 token", "results": []}

        t = targets[0]
        info, client = _get_admin_client(t["id"])
        if not client:
            return {"ok": False, "error": "client not available"}
        try:
            coupons = await client.get_coupon_list(coupon_type=coupon_type, status=status)
            return {"ok": True, "token_id": t["id"], "token_name": t.get("name", ""), "coupons": coupons}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ── 领取重置券（参与活动）──
    @r.post("/coupons/claim", dependencies=[Depends(admin_dep)])
    async def claim_coupon(token_id: Optional[str] = None):
        """参与活动领取重置券。

        不传 token_id：用第一个 enabled token 领取。
        传 token_id：用指定 token 领取。
        """
        tokens = _cfg.get("tokens", default=[]) or []
        if token_id:
            targets = [t for t in tokens if t["id"] == token_id]
            if not targets:
                raise HTTPException(status_code=404, detail="token not found")
        else:
            targets = [t for t in tokens if t.get("enabled", True)]

        if not targets:
            return {"ok": False, "error": "无可用 token"}

        t = targets[0]
        info, client = _get_admin_client(t["id"])
        if not client:
            return {"ok": False, "error": "client not available"}
        try:
            result = await client.participate_activity()
            return {"ok": True, "token_id": t["id"], "token_name": t.get("name", ""), "result": result}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ── 使用重置券 ──
    @r.post("/coupons/use", dependencies=[Depends(admin_dep)])
    async def use_coupon_endpoint(coupon_code: str, token_id: Optional[str] = None):
        """使用指定的重置券。

        coupon_code: 优惠券码（从 /coupons 接口获取）
        """
        tokens = _cfg.get("tokens", default=[]) or []
        if token_id:
            targets = [t for t in tokens if t["id"] == token_id]
            if not targets:
                raise HTTPException(status_code=404, detail="token not found")
        else:
            targets = [t for t in tokens if t.get("enabled", True)]

        if not targets:
            return {"ok": False, "error": "无可用 token"}

        t = targets[0]
        info, client = _get_admin_client(t["id"])
        if not client:
            return {"ok": False, "error": "client not available"}
        try:
            result = await client.use_coupon(coupon_code)
            return {"ok": True, "token_id": t["id"], "token_name": t.get("name", ""), "result": result}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ── 重置券商品信息 ──
    @r.get("/coupons/sku", dependencies=[Depends(admin_dep)])
    async def get_coupon_sku():
        """获取重置券商品信息（价格等）。"""
        tokens = _cfg.get("tokens", default=[]) or []
        targets = [t for t in tokens if t.get("enabled", True)]

        if not targets:
            return {"ok": False, "error": "无可用 token"}

        t = targets[0]
        info, client = _get_admin_client(t["id"])
        if not client:
            return {"ok": False, "error": "client not available"}
        try:
            sku = await client.get_reset_coupon_sku()
            return {"ok": True, "sku": sku}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ── 签到状态查询 ──
    @r.get("/sign-in/status", dependencies=[Depends(admin_dep)])
    async def get_sign_in_status_endpoint(token_id: Optional[str] = None):
        """查询签到状态。"""
        tokens = _cfg.get("tokens", default=[]) or []
        if token_id:
            targets = [t for t in tokens if t["id"] == token_id]
            if not targets:
                raise HTTPException(status_code=404, detail="token not found")
        else:
            targets = [t for t in tokens if t.get("enabled", True)]

        if not targets:
            return {"ok": False, "error": "无可用 token"}

        t = targets[0]
        info, client = _get_admin_client(t["id"])
        if not client:
            return {"ok": False, "error": "client not available"}
        try:
            status = await client.get_sign_in_status()
            return {"ok": True, "token_id": t["id"], "token_name": t.get("name", ""), "status": status}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ── 执行签到 ──
    @r.post("/sign-in", dependencies=[Depends(admin_dep)])
    async def sign_in_endpoint(token_id: Optional[str] = None):
        """执行每日签到，可获得用量奖励。"""
        tokens = _cfg.get("tokens", default=[]) or []
        if token_id:
            targets = [t for t in tokens if t["id"] == token_id]
            if not targets:
                raise HTTPException(status_code=404, detail="token not found")
        else:
            targets = [t for t in tokens if t.get("enabled", True)]

        if not targets:
            return {"ok": False, "error": "无可用 token"}

        t = targets[0]
        info, client = _get_admin_client(t["id"])
        if not client:
            return {"ok": False, "error": "client not available"}
        try:
            result = await client.sign_in()
            return {"ok": True, "token_id": t["id"], "token_name": t.get("name", ""), "result": result}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ── 额度池详情查询 ──
    @r.get("/quota/pools", dependencies=[Depends(admin_dep)])
    async def query_quota_pools(token_id: Optional[str] = None):
        """查询额度池详情。"""
        tokens = _cfg.get("tokens", default=[]) or []
        if token_id:
            targets = [t for t in tokens if t["id"] == token_id]
            if not targets:
                raise HTTPException(status_code=404, detail="token not found")
        else:
            targets = [t for t in tokens if t.get("enabled", True)]

        if not targets:
            return {"ok": False, "error": "无可用 token", "results": []}

        results = []
        for t in targets:
            info, client = _tm.get_client_for_token(t["id"])
            if not client:
                results.append({
                    "token_id": t["id"],
                    "token_name": t.get("name", ""),
                    "ok": False,
                    "error": "client not available",
                })
                continue
            try:
                pools = await client.get_quota_pools()
                results.append({
                    "token_id": t["id"],
                    "token_name": t.get("name", ""),
                    "ok": True,
                    "pools": pools,
                })
            except Exception as e:
                results.append({
                    "token_id": t["id"],
                    "token_name": t.get("name", ""),
                    "ok": False,
                    "error": str(e),
                })
        return {"ok": True, "results": results}

    # ── 使用记录查询 ──
    @r.get("/usage-records", dependencies=[Depends(admin_dep)])
    async def query_usage_records(token_id: Optional[str] = None, page: int = 1, limit: int = 50):
        """查询额度使用记录。"""
        tokens = _cfg.get("tokens", default=[]) or []
        if token_id:
            targets = [t for t in tokens if t["id"] == token_id]
            if not targets:
                raise HTTPException(status_code=404, detail="token not found")
        else:
            targets = [t for t in tokens if t.get("enabled", True)]

        if not targets:
            return {"ok": False, "error": "无可用 token", "results": []}

        t = targets[0]
        info, client = _get_admin_client(t["id"])
        if not client:
            return {"ok": False, "error": "client not available"}
        try:
            records = await client.get_usage_records(page=page, limit=limit)
            return {"ok": True, "token_id": t["id"], "token_name": t.get("name", ""), "records": records}
        except Exception as e:
            return {"ok": False, "error": str(e)}

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

        # 1f. 版本同步检查（读 appcast.xml 对比最新版本）
        try:
            import re as _re
            appcast_url = f"{base_url}/api/v0/upgrade/appcast.xml"
            verify_ssl = _cfg.get("tabbit", "verify_ssl", default=False)
            async with _httpx.AsyncClient(timeout=8, verify=verify_ssl) as hc:
                vr = await hc.get(appcast_url, headers={
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                    "Accept": "application/rss+xml,application/xml,*/*",
                })
                if vr.status_code == 200:
                    xml = vr.text
                    latest_bv_m = _re.search(r"<sparkle:shortVersionString>([^<]+)</sparkle:shortVersionString>", xml)
                    latest_sv_m = _re.search(r"<sparkle:version>([^<]+)</sparkle:version>", xml)
                    if latest_bv_m and latest_sv_m:
                        latest_bv = latest_bv_m.group(1).strip()
                        latest_sv = latest_sv_m.group(1).strip()
                        if str(latest_bv) == str(browser_version) and str(latest_sv) == str(sparkle):
                            check("版本同步", "pass",
                                  f"当前 {browser_version}({sparkle}) 已是最新（appcast: {latest_bv}({latest_sv})）")
                        else:
                            check("版本同步", "warn",
                                  f"Tabbit 已更新！当前 {browser_version}({sparkle}) → 最新 {latest_bv}({latest_sv})。"
                                  f"请在 Settings 更新 browser_version/sparkle_version，否则 x-req-ctx 会触发 493")
                    else:
                        check("版本同步", "warn", f"appcast 解析失败，原始: {xml[:120]}")
                else:
                    check("版本同步", "warn", f"appcast 接口 {vr.status_code}")
        except Exception as e:
            check("版本同步", "warn", f"版本检测异常: {e}")

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

        # 3. 上游连通性 + 协议自检（用真实业务链路）
        if tokens:
            token_info = next((t for t in tokens if t.get("enabled", True)), tokens[0])
            info, client = _get_admin_client(token_info["id"])
            await run_diagnose_chat_session_check(
                check,
                client,
                token_id=token_info["id"],
                token_manager=_tm,
            )

            # 模型诊断复用动态注册表，避免旧 /api/v0/chat/models 缓存清单误导。
            from core.model_registry import get_registry
            await run_diagnose_model_registry_check(
                check,
                get_registry(),
                token_value=token_info.get("value", ""),
            )

        # 4. 动态模型注册表缓存状态
        from core.model_registry import get_registry
        registry = get_registry()
        if registry and registry.ready:
            check("模型注册表", "pass", f"已缓存 {len(registry.list_models())} 个模型")
        elif registry:
            check("模型注册表", "warn", "未就绪（拉取失败或未初始化）")
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
