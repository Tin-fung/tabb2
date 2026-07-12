import json
import os
import hashlib
import secrets
import copy
import logging
from pathlib import Path

import bcrypt

logger = logging.getLogger("tabbit2openai")

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"
CURRENT_BROWSER_VERSION = "1.4.46"
CURRENT_SPARKLE_VERSION = 10104046

DEFAULT_CONFIG = {
    "server": {"host": "0.0.0.0", "port": 8800},
    "admin": {"password_hash": "", "salt": "", "jwt_secret": ""},
    "tabbit": {
        "base_url": "https://web.tabbit.ai",
        "client_id": "e7fa44387b1238ef1f6f",
        "browser_version": CURRENT_BROWSER_VERSION,
        "sparkle_version": CURRENT_SPARKLE_VERSION,
        # 默认浏览器标记：编进 unique-uuid 第 5 位，让上游按 Pro 会员发权益。
        # 算法移植自 web 端 eN(isDefault)。True=伪装默认浏览器领免费 Pro。
        "default_browser": True,
        # SSL 验证：默认启用；仅在本地调试/抓包时显式关闭
        "verify_ssl": True,
    },
    "trusted_proxies": [],
    "tokens": [],
    "proxy": {"api_key": "", "system_prompt": ""},
    "responses": {
        "relay_token": "",
        "relay_timeout_seconds": 300,
        "session_ttl_seconds": 900,
    },
    "claude": {"default_model": "best", "system_prompt": ""},
    "logging": {"max_entries": 500},
}


def _deep_merge(base: dict, override: dict) -> dict:
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def hash_password(password: str) -> str:
    """使用 bcrypt 哈希密码（返回 bcrypt 完整哈希，含 salt）"""
    salt = bcrypt.gensalt(rounds=12)
    hashed = bcrypt.hashpw(password.encode(), salt).decode()
    return hashed


def verify_password_hash(password: str, hashed: str) -> bool:
    """验证密码是否匹配 bcrypt 哈希"""
    try:
        return bcrypt.checkpw(password.encode(), hashed.encode())
    except Exception:
        return False


def generate_initial_password() -> str:
    """生成随机初始密码"""
    return secrets.token_urlsafe(16)


class ConfigManager:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else CONFIG_PATH
        self.config = self._load()

    def _load(self) -> dict:
        if self.path.exists():
            with open(self.path, "r", encoding="utf-8") as f:
                saved = json.load(f)
            config = _deep_merge(copy.deepcopy(DEFAULT_CONFIG), saved)
            # 迁移：强制升级上游协议相关字段（旧配置值已失效，会导致 493/492）
            self._migrate_tabbit_config(config)
            # 迁移：旧版 SHA-256 密码哈希升级为 bcrypt
            self._migrate_password_hash(config)
            self._ensure_admin_security(config)
            self._ensure_responses_security(config)
            # 确保新字段被写入
            self._save(config)
            return config

        config = copy.deepcopy(DEFAULT_CONFIG)
        config["admin"]["jwt_secret"] = secrets.token_hex(32)
        self._ensure_responses_security(config)
        # 首次启动生成随机密码
        initial_pw = generate_initial_password()
        config["admin"]["password_hash"] = hash_password(initial_pw)
        config["admin"]["salt"] = ""  # bcrypt 自带 salt，此字段保留兼容
        self._save(config)
        # 输出到控制台，让用户记录
        logger.warning("=" * 60)
        logger.warning("⚠️  首次启动，管理员密码: %s", initial_pw)
        logger.warning("⚠️  请登录后立即修改密码！")
        logger.warning("=" * 60)
        print(f"\n{'=' * 60}")
        print(f"⚠️  首次启动，管理员密码: {initial_pw}")
        print(f"⚠️  请登录后立即修改密码！")
        print(f"{'=' * 60}\n")
        return config

    def _migrate_tabbit_config(self, config: dict):
        """迁移 tabbit 配置：旧域名 web.tabbitbrowser.com 已废弃，
        旧版本号(1.1)会导致 x-req-ctx 编码错误触发 493。
        强制升级到新值。"""
        tabbit = config.setdefault("tabbit", {})
        # 旧域名 → 新域名
        if tabbit.get("base_url") in (None, "https://web.tabbitbrowser.com"):
            tabbit["base_url"] = "https://web.tabbit.ai"
        # 已确认失效的版本会触发 493，迁移到当前官方客户端版本。
        if tabbit.get("browser_version") in (None, "1.1", "145", "1.1.39"):
            tabbit["browser_version"] = CURRENT_BROWSER_VERSION
        if tabbit.get("sparkle_version") in (None, 0, 10101039):
            tabbit["sparkle_version"] = CURRENT_SPARKLE_VERSION
        # default_browser 默认开启（领免费 Pro 会员）
        if tabbit.get("default_browser") is None:
            tabbit["default_browser"] = True
        if tabbit.get("verify_ssl") is None:
            tabbit["verify_ssl"] = True

    def _ensure_admin_security(self, config: dict):
        admin = config.setdefault("admin", {})
        if not admin.get("jwt_secret"):
            admin["jwt_secret"] = secrets.token_hex(32)

    def _ensure_responses_security(self, config: dict):
        responses = config.setdefault("responses", {})
        if not responses.get("relay_token"):
            responses["relay_token"] = secrets.token_urlsafe(32)

    def _migrate_password_hash(self, config: dict):
        """迁移旧版 SHA-256 密码哈希为 bcrypt

        旧格式: password_hash = sha256(password + salt)
        新格式: password_hash = bcrypt(password)，salt 字段留空
        """
        admin = config.get("admin", {})
        old_salt = admin.get("salt", "")
        pw_hash = admin.get("password_hash", "")

        # 如果 salt 非空，说明是旧版 SHA-256 哈希
        if old_salt and pw_hash:
            logger.info("检测到旧版 SHA-256 密码哈希，需要升级为 bcrypt")
            logger.info("请通过管理面板修改密码以自动完成迁移")
            # 不自动迁移（因为无法反推原始密码），提示用户修改密码
            # 保留旧哈希，用户修改密码时会自动使用 bcrypt
            return

    def _save(self, config: dict | None = None):
        if config is None:
            config = self.config
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)

    def save(self):
        self._save()

    def get(self, *keys, default=None):
        val = self.config
        for k in keys:
            if isinstance(val, dict):
                val = val.get(k)
            else:
                return default
            if val is None:
                return default
        return val

    def set_val(self, *keys_and_value):
        """set_val('server', 'port', 8800) — 最后一个参数是值"""
        keys = keys_and_value[:-1]
        value = keys_and_value[-1]
        d = self.config
        for k in keys[:-1]:
            d = d.setdefault(k, {})
        d[keys[-1]] = value
        self.save()
