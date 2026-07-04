import time
import asyncio
import re
import json
import base64
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Optional

from core.config import ConfigManager
from core.tabbit_client import TabbitClient

COOLDOWN_SECONDS = 300  # 5 分钟冷却
MAX_CONSECUTIVE_ERRORS = 3
TRANSIENT_COOLDOWN_SECONDS = 60
AUTH_COOLDOWN_SECONDS = 1800
QUOTA_COOLDOWN_SECONDS = 300
MAX_QUOTA_COOLDOWN_SECONDS = 1800
MAX_RETRY_AFTER_SECONDS = 12 * 60 * 60
MAX_LAST_ERROR_LEN = 500
AUTH_NEEDS_LOGIN_STATUS_CODES = {401, 403}


def _format_ts(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _parse_ts(value) -> float:
    if value in (None, ""):
        return 0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return 0
        try:
            return float(raw)
        except ValueError:
            pass
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0
    return 0


def _parse_retry_after(value, now: float) -> int | None:
    if value in (None, ""):
        return None
    try:
        seconds = int(float(str(value).strip()))
        return max(0, min(seconds, MAX_RETRY_AFTER_SECONDS))
    except ValueError:
        pass
    try:
        retry_at = parsedate_to_datetime(str(value))
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        seconds = int(retry_at.timestamp() - now)
        return max(0, min(seconds, MAX_RETRY_AFTER_SECONDS))
    except Exception:
        return None


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


def token_expiration_metadata(token_value: str, now: float | None = None) -> dict:
    first_token = (token_value or "").split("|", 1)[0]
    payload = _decode_jwt_payload(first_token)
    raw_exp = payload.get("exp")
    try:
        exp = float(raw_exp)
    except (TypeError, ValueError):
        return {
            "expires_at": None,
            "expires_in_seconds": None,
            "expired": False,
        }
    if exp <= 0:
        return {
            "expires_at": None,
            "expires_in_seconds": None,
            "expired": False,
        }
    if exp > 10_000_000_000:
        exp = exp / 1000
    now_ts = time.time() if now is None else float(now)
    return {
        "expires_at": _format_ts(exp),
        "expires_in_seconds": max(0, int(exp - now_ts)),
        "expired": exp <= now_ts,
    }


def _error_headers(error) -> object | None:
    headers = getattr(error, "headers", None)
    if headers is not None:
        return headers
    response = getattr(error, "response", None)
    return getattr(response, "headers", None)


def _header_get(headers, name: str):
    if headers is None:
        return None
    getter = getattr(headers, "get", None)
    if callable(getter):
        return getter(name) or getter(name.lower())
    if isinstance(headers, dict):
        return headers.get(name) or headers.get(name.lower())
    return None


def _status_code_from_error(error, status_code: int | None = None) -> int | None:
    if status_code is not None:
        try:
            return int(status_code)
        except (TypeError, ValueError):
            return None
    if error is None:
        return None
    for attr in ("status_code", "status"):
        value = getattr(error, attr, None)
        if value is not None:
            try:
                return int(value)
            except (TypeError, ValueError):
                pass
    response = getattr(error, "response", None)
    value = getattr(response, "status_code", None)
    if value is not None:
        try:
            return int(value)
        except (TypeError, ValueError):
            pass
    match = re.search(r"(?:API error|upstream error|HTTP)\s+(\d{3})", str(error))
    if match:
        return int(match.group(1))
    return None


def _last_error_text(error) -> str | None:
    if error is None:
        return None
    text = str(error).strip()
    if not text:
        return None
    return text[:MAX_LAST_ERROR_LEN]


def _split_token_value(value: str) -> tuple[str, str | None, str | None]:
    parts = (value or "").split("|")
    jwt_token = parts[0] if parts else ""
    next_auth = parts[1] if len(parts) > 1 else None
    device_id = parts[2] if len(parts) > 2 else None
    return jwt_token, next_auth, device_id


def _build_token_value(jwt_token: str, next_auth: str | None, device_id: str | None) -> str:
    parts = [jwt_token]
    if next_auth is not None or device_id:
        parts.append(next_auth or "")
    if device_id:
        parts.append(device_id)
    return "|".join(parts)


def _non_empty_cookie(cookies: dict, name: str) -> str | None:
    value = cookies.get(name)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


class TokenManager:
    def __init__(self, config: ConfigManager):
        self.config = config
        self._clients: dict[str, TabbitClient] = {}
        self._last_token_id: str | None = None  # 上次使用的 token_id（稳定轮转）
        self._cooldowns: dict[str, float] = {}  # token_id -> 冷却截止时间戳
        self._lock = asyncio.Lock()

    @property
    def has_tokens(self) -> bool:
        return len(self.config.get("tokens", default=[])) > 0

    def _clear_expired_cooldown(self, token_info: dict, token_id: str) -> bool:
        self._cooldowns.pop(token_id, None)
        changed = False
        if token_info.get("status") != "unknown":
            token_info["status"] = "unknown"
            changed = True
        if token_info.get("error_count", 0) != 0:
            token_info["error_count"] = 0
            changed = True
        if token_info.get("cooldown_until") is not None:
            token_info["cooldown_until"] = None
            changed = True
        return changed

    def _get_available_tokens(self) -> list[dict]:
        tokens = self.config.get("tokens", default=[])
        now = time.time()
        available = []
        changed = False
        for t in tokens:
            if not t.get("enabled", True):
                continue
            if t.get("status") == "needs_login":
                continue
            token_id = t["id"]
            cooldown_until = max(
                self._cooldowns.get(token_id, 0),
                _parse_ts(t.get("cooldown_until")),
            )
            if cooldown_until > now:
                self._cooldowns[token_id] = cooldown_until
                continue
            if now >= cooldown_until:
                if cooldown_until > 0:
                    changed = self._clear_expired_cooldown(t, token_id) or changed
                available.append(t)
        if changed:
            self.config.save()
        return available

    def _get_client(self, token_info: dict) -> TabbitClient:
        tid = token_info["id"]
        if tid not in self._clients:
            self._clients[tid] = TabbitClient(
                token_info["value"],
                self.config.get("tabbit", "base_url"),
                self.config.get("tabbit", "client_id"),
                self.config.get("tabbit", "browser_version"),
                self.config.get("tabbit", "sparkle_version"),
                self.config.get("tabbit", "default_browser", default=True),
                verify_ssl=self.config.get("tabbit", "verify_ssl", default=False),
            )
        return self._clients[tid]

    async def get_next(self) -> tuple[Optional[dict], Optional[TabbitClient]]:
        async with self._lock:
            available = self._get_available_tokens()
            if not available:
                return None, None
            # 稳定轮转：找上次 token_id 的下一个位置
            if self._last_token_id:
                ids = [t["id"] for t in available]
                try:
                    idx = ids.index(self._last_token_id)
                    next_idx = (idx + 1) % len(available)
                except ValueError:
                    next_idx = 0
            else:
                next_idx = 0
            token_info = available[next_idx]
            self._last_token_id = token_info["id"]
            client = self._get_client(token_info)
            return token_info, client

    def get_client_for_token(self, token_id: str) -> tuple[Optional[dict], Optional[TabbitClient]]:
        """按 token_id 获取 client（复用缓存，供 admin API 使用）"""
        for t in self.config.get("tokens", default=[]):
            if t["id"] == token_id:
                client = self._get_client(t)
                return t, client
        return None, None

    def report_success(self, token_id: str):
        for t in self.config.get("tokens", default=[]):
            if t["id"] == token_id:
                self._cooldowns.pop(token_id, None)
                t["total_requests"] = t.get("total_requests", 0) + 1
                t["error_count"] = 0
                t["last_used_at"] = time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
                )
                t["status"] = "active"
                t["cooldown_until"] = None
                t["last_error"] = None
                t["last_error_at"] = None
                t["last_status_code"] = None
                break
        self.config.save()

    def _cooldown_seconds_for_error(
        self,
        error_count: int,
        status_code: int | None,
        retry_after_seconds: int | None,
    ) -> int:
        if retry_after_seconds and status_code in (429, 503):
            return retry_after_seconds
        if status_code == 402:
            return AUTH_COOLDOWN_SECONDS
        if status_code == 429:
            backoff_level = max(0, error_count - 1)
            return min(
                QUOTA_COOLDOWN_SECONDS * (2 ** backoff_level),
                MAX_QUOTA_COOLDOWN_SECONDS,
            )
        if status_code in (408, 500, 502, 503, 504):
            return TRANSIENT_COOLDOWN_SECONDS
        if error_count >= MAX_CONSECUTIVE_ERRORS:
            return COOLDOWN_SECONDS
        return 0

    def _sync_cached_client_value(self, token_id: str, value: str) -> None:
        client = self._clients.get(token_id)
        if not client:
            return
        updater = getattr(client, "update_auth_token_value", None)
        if callable(updater):
            updater(value)

    def report_error(
        self,
        token_id: str,
        error: Exception | str | None = None,
        *,
        status_code: int | None = None,
        retry_after=None,
    ):
        now = time.time()
        resolved_status_code = _status_code_from_error(error, status_code)
        headers = _error_headers(error)
        retry_after_value = retry_after
        if retry_after_value is None:
            retry_after_value = _header_get(headers, "Retry-After")
        retry_after_seconds = _parse_retry_after(retry_after_value, now)

        for t in self.config.get("tokens", default=[]):
            if t["id"] == token_id:
                t["error_count"] = t.get("error_count", 0) + 1
                t["total_requests"] = t.get("total_requests", 0) + 1
                if resolved_status_code in AUTH_NEEDS_LOGIN_STATUS_CODES:
                    self._cooldowns.pop(t["id"], None)
                    t["cooldown_until"] = None
                    t["status"] = "needs_login"
                else:
                    cooldown_seconds = self._cooldown_seconds_for_error(
                        t["error_count"],
                        resolved_status_code,
                        retry_after_seconds,
                    )
                    if cooldown_seconds > 0:
                        cooldown_until = now + cooldown_seconds
                        self._cooldowns[t["id"]] = cooldown_until
                        t["cooldown_until"] = _format_ts(cooldown_until)
                        t["status"] = "cooldown"
                    else:
                        t["status"] = "error"
                        t["cooldown_until"] = None
                t["last_error"] = _last_error_text(error)
                t["last_error_at"] = _format_ts(now)
                t["last_status_code"] = resolved_status_code
                break
        self.config.save()

    def update_token_cookies(self, token_id: str, cookies: dict) -> bool:
        new_jwt = _non_empty_cookie(cookies, "token")
        new_next_auth = _non_empty_cookie(cookies, "next-auth.session-token")
        if not new_jwt and not new_next_auth:
            return False

        for t in self.config.get("tokens", default=[]):
            if t["id"] != token_id:
                continue
            old_jwt, old_next_auth, old_device_id = _split_token_value(t.get("value", ""))
            jwt_token = new_jwt or old_jwt
            next_auth = new_next_auth if new_next_auth is not None else old_next_auth
            new_value = _build_token_value(jwt_token, next_auth, old_device_id)
            if new_value == t.get("value", ""):
                return False
            t["value"] = new_value
            self._sync_cached_client_value(token_id, new_value)
            self.config.save()
            return True
        return False

    def refresh_token_from_client(self, token_id: str, client: TabbitClient) -> bool:
        exporter = getattr(client, "export_auth_cookies", None)
        if not callable(exporter):
            return False
        cookies = exporter()
        if not cookies:
            return False
        return self.update_token_cookies(token_id, cookies)

    def remove_client(self, token_id: str):
        self._clients.pop(token_id, None)
        self._cooldowns.pop(token_id, None)

    def get_token_status(self, token_id: str) -> str:
        now = time.time()
        for t in self.config.get("tokens", default=[]):
            if t["id"] == token_id:
                if t.get("status") == "needs_login":
                    self._cooldowns.pop(token_id, None)
                    if t.get("cooldown_until") is not None:
                        t["cooldown_until"] = None
                        self.config.save()
                    return "needs_login"
                cooldown_until = max(
                    self._cooldowns.get(token_id, 0),
                    _parse_ts(t.get("cooldown_until")),
                )
                if now < cooldown_until:
                    self._cooldowns[token_id] = cooldown_until
                    return "cooldown"
                if cooldown_until > 0 and self._clear_expired_cooldown(t, token_id):
                    self.config.save()
                return t.get("status", "unknown")
        return "unknown"

    async def close_all(self):
        for client in self._clients.values():
            await client.client.aclose()
        self._clients.clear()
