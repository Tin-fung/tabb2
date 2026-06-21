"""动态模型注册表

启动时从 Tabbit 上游拉取真实模型清单，构建 alias → selected_model 映射，
带缓存与 TTL。

相比硬编码 MODEL_MAP 的好处：Tabbit 升级模型不用改代码。

关键发现（cmp_models.py 验证）:
- /proxy/v1/model_config/models 是最新清单（含 GLM-5.1/GPT-5.5 等新模型）
- /api/v0/chat/models 是缓存旧清单，模型少且滞后
- 上游 selectedModel 字段接受 display_name（如 "GLM-5.1"），验证可成功
- 接口 B 的模型只有 display_name 没有 name，故 selectedModel 用 display_name
"""
import time
import logging
from typing import Optional

import httpx

logger = logging.getLogger("tabbit2openai")

# 缓存 TTL（秒）：模型清单不常变，缓存 1 小时
MODEL_CACHE_TTL = 3600

# 上游模型清单接口（最新清单，含新模型）
MODELS_API_PATH = "/proxy/v1/model_config/models?a=0"


class ModelRegistry:
    """模型注册表：动态拉取 + 缓存 + fallback"""

    def __init__(self, base_url: str = "https://web.tabbit.ai"):
        self.base_url = base_url
        self._cache: Optional[dict] = None  # {alias: selected_model}
        self._models_meta: Optional[list] = None  # 完整模型元信息
        self._expires_at: float = 0

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

    async def refresh(self, force: bool = False) -> bool:
        """拉取最新模型清单，成功返回 True"""
        if not force and self._cache and time.time() < self._expires_at:
            return True
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(connect=10, read=20, write=10, pool=10),
                verify=False,
            ) as client:
                resp = await client.get(f"{self.base_url}{MODELS_API_PATH}")
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
            logger.info("model registry refreshed: %d models, %d aliases",
                        len(models_meta), len(alias_map))
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


# 全局单例
_registry: Optional[ModelRegistry] = None


def init_registry(base_url: str = "https://web.tabbit.ai") -> ModelRegistry:
    global _registry
    _registry = ModelRegistry(base_url)
    return _registry


def get_registry() -> Optional[ModelRegistry]:
    return _registry
