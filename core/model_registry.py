"""动态模型注册表

启动时从 Tabbit 上游拉取真实模型清单，构建 alias → selected_model 映射，
带缓存与 TTL。每次成功拉取落盘快照（data/models_snapshot.json），
注册表全挂时读快照兜底，不再直接 503。

相比硬编码 MODEL_MAP 的好处：Tabbit 升级模型不用改代码。
快照兜底的好处：上游不可达时仍能返回上次真实拉取的清单，
且新模型上线后下次刷新自动进快照，无需手维护静态列表。

关键发现（cmp_models.py 验证）:
- /proxy/v1/model_config/models 是最新清单（含 GLM-5.1/GPT-5.5 等新模型）
- /api/v0/chat/models 是缓存旧清单，模型少且滞后
- 上游 selectedModel 字段接受 display_name（如 "GLM-5.1"），验证可成功
- 接口 B 的模型只有 display_name 没有 name，故 selectedModel 用 display_name
"""
import asyncio
import time
import logging
import json
from pathlib import Path
from typing import Optional

import httpx

logger = logging.getLogger("tabbit2openai")

# 缓存 TTL（秒）：模型清单不常变，缓存 1 小时
MODEL_CACHE_TTL = 3600
# 旧缓存宽限窗（秒）：拉取失败但有过期缓存时，顺延这么久仍视为可用，
# 避免每请求都重打上游。期间后台可重试刷新。
STALE_CACHE_GRACE = 300

# 快照落盘路径：每次成功拉取写这里，全挂时读这里兜底
SNAPSHOT_PATH = Path(__file__).resolve().parent.parent / "data" / "models_snapshot.json"


async def _async_sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)

# 上游模型清单接口（最新清单，含新模型）
MODELS_API_PATH = "/proxy/v1/model_config/models?a=0"


class ModelRegistry:
    """模型注册表：动态拉取 + 缓存 + fallback"""

    def __init__(self, base_url: str = "https://web.tabbit.ai", verify_ssl: bool = False, token_str: str | None = None, snapshot_path: str | Path | None = None):
        self.base_url = base_url
        self.verify_ssl = verify_ssl
        self._token_str = token_str  # 认证 token（JWT|NEXT_AUTH|DEVICE_ID 格式）
        self._cache: Optional[dict] = None  # {alias: selected_model}
        self._models_meta: Optional[list] = None  # 完整模型元信息
        self._expires_at: float = 0
        # 快照路径：可注入（测试用），默认 data/models_snapshot.json
        self._snapshot_path = Path(snapshot_path) if snapshot_path else SNAPSHOT_PATH
        # 标记当前内存数据是否来自快照（用于日志区分动态 vs 快照兜底）
        self._from_snapshot: bool = False

    def _build_alias_map(self, models: list) -> tuple[dict, list]:
        """从上游模型列表构建 alias → selected_model 映射

        selectedModel 用 display_name（上游验证接受）。
        alias 规则：
        1. display_name 小写去空格（如 "GPT-5.2-Chat"→"gpt-5.2-chat"）
        2. name 本身（如有，如 "glm-5"）
        3. display_name 原值（如 "GLM-5.1"）
        4. 特殊：Default/最佳/best → Default
        """
        alias_map = {}
        models_meta = []
        for m in models:
            name = m.get("name", "") or ""
            display = m.get("display_name", "") or ""
            if not display and not name:
                continue
            # selectedModel 优先用 display_name（接口 B 验证可用），无则用 name
            selected = display or name
            meta = {
                "id": name or display,  # id 给前端展示，name 优先
                "display_name": display,
                "selected_model": selected,
                "provider": m.get("provider", ""),
                "supports_images": m.get("supports_images", False),
                "supports_tools": m.get("supports_tools", False),
                "sort_order": m.get("sort_order", 0),
            }
            models_meta.append(meta)
            # alias: display_name 小写去空格
            if display:
                alias_display = display.lower().replace(" ", "")
                if alias_display:
                    alias_map[alias_display] = selected
                    alias_map[display] = selected  # 原值也收
            # alias: name 本身（小写）
            if name:
                alias_map[name.lower()] = selected
                alias_map[name] = selected
            # 特殊：Default/最佳/best 统一映射
            if display in ("Default", "最佳") or name == "best-model":
                alias_map["best"] = selected
                alias_map["default"] = selected
                alias_map["默认"] = selected
                alias_map["最佳"] = selected
        return alias_map, models_meta

    def update_token(self, token_str: str | None) -> None:
        """更新认证 token（运行时添加新 token 后调用）"""
        self._token_str = token_str

    def _save_snapshot(self) -> None:
        """把当前 models_meta 落盘成快照。

        每次成功拉取上游后调用。快照是上次真实拉取的清单，
        上游不可达时读它兜底，避免 /v1/models 直接 503。
        写失败只记日志，不影响主流程（快照是 best-effort 优化）。
        """
        if not self._models_meta:
            return
        try:
            self._snapshot_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "saved_at": time.time(),
                "models_meta": self._models_meta,
            }
            tmp = self._snapshot_path.with_suffix(self._snapshot_path.suffix + ".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self._snapshot_path)  # 原子替换，避免半写
            logger.info("model snapshot saved: %d models -> %s",
                        len(self._models_meta), self._snapshot_path)
        except Exception as e:
            logger.warning("model snapshot save failed: %s", e)

    def _load_snapshot(self) -> bool:
        """从盘读快照填充内存缓存，成功返回 True。

        仅在动态拉取全挂且无内存缓存时调用。快照没有 TTL（它就是兜底），
        但 _from_snapshot 标记会让 ready 检查走宽限窗逻辑。
        """
        try:
            if not self._snapshot_path.exists():
                return False
            payload = json.loads(self._snapshot_path.read_text(encoding="utf-8"))
            models_meta = payload.get("models_meta") if isinstance(payload, dict) else None
            if not isinstance(models_meta, list) or not models_meta:
                return False
            # 从 models_meta 反建 alias_map（meta 已含 selected_model）
            alias_map: dict = {}
            for m in models_meta:
                display = m.get("display_name", "") or ""
                name = m.get("id", "") or ""
                selected = m.get("selected_model") or display or name
                if display:
                    alias_map[display.lower().replace(" ", "")] = selected
                    alias_map[display] = selected
                if name:
                    alias_map[name.lower()] = selected
                    alias_map[name] = selected
                if display in ("Default", "最佳") or name == "best-model":
                    alias_map["best"] = selected
                    alias_map["default"] = selected
                    alias_map["默认"] = selected
                    alias_map["最佳"] = selected
            self._cache = alias_map
            self._models_meta = models_meta
            self._from_snapshot = True
            # 快照兜底也设宽限窗，期间后台可重试拉动态
            self._expires_at = time.time() + STALE_CACHE_GRACE
            saved_at = payload.get("saved_at")
            age = int(time.time() - saved_at) if isinstance(saved_at, (int, float)) else -1
            logger.warning("model registry using snapshot fallback: %d models (age %ss)",
                           len(models_meta), age)
            return True
        except Exception as e:
            logger.warning("model snapshot load failed: %s", e)
            return False

    async def refresh(self, force: bool = False) -> bool:
        """拉取最新模型清单，成功返回 True。

        失败时保留旧缓存（若有）——旧清单 > 过时静态 MODEL_MAP。
        启动路径带重试（_startup=True），缓解上游瞬时不可达导致启动即兜底。
        """
        if not force and self._cache and time.time() < self._expires_at:
            return True
        ok = await self._fetch_once()
        if ok:
            return True
        # 失败但有旧缓存：保留旧数据，TTL 顺延短窗，避免每请求都重打上游
        if self._cache:
            logger.warning("model registry refresh failed, keeping stale cache (%d models)",
                           len(self._models_meta or []))
            self._expires_at = time.time() + STALE_CACHE_GRACE
            return True
        # 无内存缓存，读快照兜底
        if self._load_snapshot():
            return True
        return False

    async def refresh_with_retry(self, retries: int = 2, delay: float = 1.5) -> bool:
        """带重试的刷新，用于启动。retries=2 共打 3 次。"""
        for attempt in range(retries + 1):
            if await self._fetch_once():
                return True
            if attempt < retries:
                logger.warning("model registry refresh attempt %d/%d failed, retrying...",
                               attempt + 1, retries + 1)
                await _async_sleep(delay)
        # 全部失败：若有旧缓存兜住，否则返回 False
        if self._cache:
            self._expires_at = time.time() + STALE_CACHE_GRACE
            return True
        # 无内存缓存，最后试一次读快照兜底——上游全挂时仍能返回上次真实清单
        if self._load_snapshot():
            return True
        return False

    async def _fetch_once(self) -> bool:
        """单次拉取，成功更新缓存并返回 True。"""
        try:
            # 构建认证 cookie（上游 /proxy 接口需要 JWT 认证）
            cookies = {}
            if self._token_str:
                import uuid as _uuid
                parts = self._token_str.split("|")
                jwt_token = parts[0]
                # 从 JWT 提取 user_id
                try:
                    import json, base64
                    payload = json.loads(base64.urlsafe_b64decode(jwt_token.split(".")[1] + "=="))
                    user_id = payload.get("id", payload.get("sub", ""))
                except Exception:
                    user_id = ""
                cookies = {
                    "token": jwt_token,
                    "user_id": user_id,
                    "managed": "tab_browser",
                    "NEXT_LOCALE": "zh",
                }
                if len(parts) > 1:
                    cookies["next-auth.session-token"] = parts[1]

            async with httpx.AsyncClient(
                timeout=httpx.Timeout(connect=10, read=20, write=10, pool=10),
                verify=self.verify_ssl,
            ) as client:
                resp = await client.get(f"{self.base_url}{MODELS_API_PATH}", cookies=cookies)
                if resp.status_code != 200:
                    logger.warning("model registry refresh failed: %s", resp.status_code)
                    return False
                data = resp.json()
            # 接口 B 返回 {"models": [...]}，接口 A 返回 {"supported_models": {provider: [...]}}
            models = []
            if isinstance(data.get("models"), list):
                models = data["models"]
            elif isinstance(data.get("supported_models"), dict):
                for provider, ms in data["supported_models"].items():
                    for m in ms:
                        models.append({**m, "provider": provider})
            if not models:
                logger.warning("model registry: empty models list")
                return False
            alias_map, models_meta = self._build_alias_map(models)
            self._cache = alias_map
            self._models_meta = models_meta
            self._expires_at = time.time() + MODEL_CACHE_TTL
            self._from_snapshot = False
            logger.info("model registry refreshed: %d models, %d aliases",
                        len(models_meta), len(alias_map))
            self._save_snapshot()  # 落盘最新清单，供下次全挂时兜底
            return True
        except Exception as e:
            logger.warning("model registry refresh error: %s", e)
            return False

    def resolve(self, alias: str) -> str:
        """将请求模型名解析为上游 selected_model，未命中返回 Default"""
        if not alias:
            return "Default"
        key = alias.lower().strip()
        if self._cache and key in self._cache:
            return self._cache[key]
        # 兜底：动态缓存不可用时，直接把原名当 selected_model 传
        return "Default"

    def has_alias(self, alias: str) -> bool:
        if not alias or not self._cache:
            return False
        return alias.lower().strip() in self._cache

    def list_models(self) -> list:
        """返回 OpenAI 格式的模型清单"""
        if not self._models_meta:
            return []
        return [
            {
                "id": m["id"],
                "object": "model",
                "owned_by": "tabbit",
                "tabbit_display_name": m["display_name"],
                "supports_images": m["supports_images"],
                "supports_tools": m["supports_tools"],
            }
            for m in sorted(self._models_meta, key=lambda x: x.get("sort_order", 0))
        ]

    @property
    def ready(self) -> bool:
        return self._cache is not None and time.time() < self._expires_at

    @property
    def snapshot_info(self) -> dict:
        """快照状态信息，供 admin UI 提示用。

        from_snapshot: 当前内存数据是否来自快照兜底（True=上游全挂在读快照）
        snapshot_age:  落盘快照距今秒数（-1=无快照文件）
        snapshot_count: 落盘快照模型数（0=无快照文件）
        """
        info = {"from_snapshot": self._from_snapshot, "snapshot_age": -1, "snapshot_count": 0}
        try:
            if self._snapshot_path.exists():
                payload = json.loads(self._snapshot_path.read_text(encoding="utf-8"))
                saved_at = payload.get("saved_at") if isinstance(payload, dict) else None
                meta = payload.get("models_meta") if isinstance(payload, dict) else None
                if isinstance(saved_at, (int, float)):
                    info["snapshot_age"] = int(time.time() - saved_at)
                if isinstance(meta, list):
                    info["snapshot_count"] = len(meta)
        except Exception:
            pass
        return info


# 全局单例
_registry: Optional[ModelRegistry] = None


def init_registry(base_url: str = "https://web.tabbit.ai", verify_ssl: bool = False, token_str: str | None = None) -> ModelRegistry:
    global _registry
    _registry = ModelRegistry(base_url, verify_ssl, token_str)
    return _registry


def get_registry() -> Optional[ModelRegistry]:
    return _registry
