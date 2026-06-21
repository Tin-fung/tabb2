import json
import os
import hashlib
import secrets
import copy
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"

DEFAULT_CONFIG = {
    "server": {"host": "0.0.0.0", "port": 8800},
    "admin": {"password_hash": "", "salt": "", "jwt_secret": ""},
    "tabbit": {
        "base_url": "https://web.tabbit.ai",
        "client_id": "e7fa44387b1238ef1f6f",
        "browser_version": "1.1.39",
        "sparkle_version": 10101039,
        # 默认浏览器标记：编进 unique-uuid 第 5 位，让上游按 Pro 会员发权益。
        # 算法移植自 web 端 eN(isDefault)。True=伪装默认浏览器领免费 Pro。
        "default_browser": True,
    },
    "tokens": [],
    "proxy": {"api_key": "", "system_prompt": ""},
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


def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    if salt is None:
        salt = secrets.token_hex(16)
    hashed = hashlib.sha256((password + salt).encode()).hexdigest()
    return hashed, salt


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
            # 确保新字段被写入
            self._save(config)
            return config

        config = copy.deepcopy(DEFAULT_CONFIG)
        config["admin"]["jwt_secret"] = secrets.token_hex(32)
        pw_hash, salt = hash_password("admin")
        config["admin"]["password_hash"] = pw_hash
        config["admin"]["salt"] = salt
        self._save(config)
        return config

    def _migrate_tabbit_config(self, config: dict):
        """迁移 tabbit 配置：旧域名 web.tabbitbrowser.com 已废弃，
        旧版本号(1.1)会导致 x-req-ctx 编码错误触发 493。
        强制升级到新值。"""
        tabbit = config.setdefault("tabbit", {})
        # 旧域名 → 新域名
        if tabbit.get("base_url") in (None, "https://web.tabbitbrowser.com"):
            tabbit["base_url"] = "https://web.tabbit.ai"
        # 旧版本号 1.1 → 1.1.39（x-req-ctx 编码需要完整三段版本号）
        if tabbit.get("browser_version") in (None, "1.1", "145"):
            tabbit["browser_version"] = "1.1.39"
        # sparkle_version 默认值
        if not tabbit.get("sparkle_version"):
            tabbit["sparkle_version"] = 10101039
        # default_browser 默认开启（领免费 Pro 会员）
        if tabbit.get("default_browser") is None:
            tabbit["default_browser"] = True

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
