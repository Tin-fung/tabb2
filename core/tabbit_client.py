import re
import json
import uuid
import time
import hashlib
import base64
import random
import secrets
import logging
import urllib.parse
from contextlib import suppress
from email.utils import parsedate_to_datetime
from typing import AsyncGenerator

import httpx

logger = logging.getLogger("tabbit2openai")

DEFAULT_BROWSER_VERSION = "1.4.46"
DEFAULT_SPARKLE_VERSION = 10104046


class TabbitAPIError(Exception):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        headers: dict | httpx.Headers | None = None,
        code: int | str | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.headers = headers or {}
        self.code = code


# ── unique-uuid 生成器（移植自 web 端 eN 算法） ──
# 前端把"是否默认浏览器"状态编码进 unique-uuid 的第 5 位：
#   - 默认浏览器 → 第 5 位 = "1"
#   - 非默认浏览器 → 第 5 位从 "023456789abcdef" 随机（绝不出现 "1"）
# 同时 8 个固定位置 [2,7,11,14,18,21,25,28] 填当前时间戳(16进制)，
# 后端据此校验时效性。把 is_default=True 即可让后端按 Pro 会员发权益。
_UUID_MARKER_POS = 5
_UUID_DEFAULT_MARKER = "1"
_UUID_TS_POSITIONS = [2, 7, 11, 14, 18, 21, 25, 28]
_UUID_HEX = "0123456789abcdef"
_UUID_HEX_NO_MARKER = _UUID_HEX.replace(_UUID_DEFAULT_MARKER, "")


def _gen_unique_uuid(is_default_browser: bool = True, ts: float | None = None) -> str:
    """生成 Tabbit 风格 unique-uuid，编码默认浏览器标记 + 时间戳。

    1:1 移植自 web 端 chunk eN(isDefault) 函数。
    ts: 生成时间戳位用的秒级时间，默认本地 time.time()。
        传入上游服务器时间可规避 vps 本地时钟漂移。
    """
    now = ts if ts is not None else time.time()
    # 当前秒级时间戳 → 8 位 16 进制（取末 8 位，不足前补 0）
    ts_hex = format(int(now), "x").zfill(len(_UUID_TS_POSITIONS))[-len(_UUID_TS_POSITIONS):]
    ts_map = {pos: ts_hex[i] for i, pos in enumerate(_UUID_TS_POSITIONS)}
    chars = []
    for i in range(32):
        if i == _UUID_MARKER_POS:
            # 标记位：默认浏览器放 "1"，否则从剔除 "1" 的字符集随机
            chars.append(_UUID_DEFAULT_MARKER if is_default_browser else random.choice(_UUID_HEX_NO_MARKER))
        elif i in ts_map:
            chars.append(ts_map[i])
        else:
            chars.append(random.choice(_UUID_HEX))
    raw = "".join(chars)
    return f"{raw[0:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:32]}"

MODEL_MAP = {
    "best": "Default",
    "gpt-5.2-chat": "gpt-5.2-chat",
    "gpt-5.1-chat": "gpt-5.1-chat",
    "gemini-3-pro": "gemini-3-pro-preview",
    "gemini-3-flash": "gemini-3-flash-preview",
    "gemini-2.5-flash": "gemini-2.5-flash",
    # Claude 模型族（CLAUDE_MODEL_MAP 映射的 target 必须在此注册）
    "Claude-Opus-4.8": "Claude-Opus-4.8",
    "Claude-Sonnet-4.6": "Claude-Sonnet-4.6",
    "Claude-Haiku-4.5": "Claude-Haiku-4.5",
    "glm-5": "glm-5",
    "glm-4.7": "glm-4.7",
    "deepseek-v3.2": "byteplus/deepseek-v3-2",
    "minimax-m2.7": "MiniMax-M2.7",
    "minimax-m2.5": "minimax-m2.5",
    "kimi-k2.5": "novita/kimi-k2.5",
    "qwen3-max": "qwen3-max",
    "doubao-seed-1.8": "byteplus/seed-1-8",
}


# Claude 模型名 → Tabbit 模型名映射（Claude/OpenAI 两端共用）
# 按型号族映射（opus→Opus-4.8, sonnet→Sonnet-4.6, haiku→Haiku-4.5），
# 让用户选 opus 真用 premium Opus（消额度），选 haiku 用免费 Haiku。
# Tabbit 侧无 3.x/4.0-4.4，统一归到当前最新同族型号。
# 保留 3-5/3-7 老前缀作老客户端兼容层——Claude Code 旧版本或锁定旧模型名的
# 配置仍在发这些名，删了会静默降级到 Default（用户不知情）。
# 不含 claude-opus-3/claude-sonnet-3：Anthropic 从未发过此命名，死代码已清。
CLAUDE_MODEL_MAP = {
    # Opus 族 → Claude-Opus-4.8 (premium_only)
    "claude-opus-4": "Claude-Opus-4.8",
    # Sonnet 族 → Claude-Sonnet-4.6 (premium_only)
    "claude-sonnet-4": "Claude-Sonnet-4.6",
    "claude-3-7-sonnet": "Claude-Sonnet-4.6",
    "claude-3-5-sonnet": "Claude-Sonnet-4.6",
    # Haiku 族 → Claude-Haiku-4.5 (free_metered)
    "claude-haiku-4": "Claude-Haiku-4.5",
    "claude-3-5-haiku": "Claude-Haiku-4.5",
}


def resolve_model(
    model: str,
    registry=None,
    default_model: str | None = None,
) -> str:
    """将请求模型名解析为 Tabbit selected_model。

    Claude 端点和 OpenAI 端点共用此函数，保证映射行为一致。

    优先级：
    1. 动态注册表精确匹配
    2. CLAUDE_MODEL_MAP 前缀匹配 → 动态注册表解析
    3. 静态 MODEL_MAP 精确匹配
    4. CLAUDE_MODEL_MAP 前缀匹配 → 静态 MODEL_MAP 解析
    5. config 默认模型
    6. Default
    """
    if not model:
        model = "best"

    # 路径 1-2：动态注册表有匹配项时优先使用。
    # 已知 alias 可使用过期但真实拉取过的缓存；否则 TTL 到期到下一次刷新之间
    # 会把新模型静默降级成 Default。
    if registry:
        if registry.has_alias(model):
            return registry.resolve(model)
        for prefix, target in CLAUDE_MODEL_MAP.items():
            if model.startswith(prefix) and registry.has_alias(target):
                return registry.resolve(target)
        if default_model and registry.has_alias(default_model):
            return registry.resolve(default_model)
        if registry.ready:
            return registry.resolve(model)  # 动态清单在线但确实未知 → Default

    # 路径 3-4：动态注册表不可用，用静态 MODEL_MAP
    if model in MODEL_MAP:
        return MODEL_MAP[model]
    for prefix, target in CLAUDE_MODEL_MAP.items():
        if model.startswith(prefix):
            return MODEL_MAP.get(target, "Default")
    if default_model and default_model in MODEL_MAP:
        return MODEL_MAP[default_model]
    return "Default"


class TabbitClient:
    def __init__(self, token_str: str, base_url: str | None = None, client_id: str | None = None, browser_version: str | None = None, sparkle_version: int | None = None, default_browser: bool = True, verify_ssl: bool = False):
        parts = token_str.split("|")
        self.jwt_token = parts[0]
        self.next_auth = parts[1] if len(parts) > 1 and parts[1] else None
        self.device_id = parts[2] if len(parts) > 2 else str(uuid.uuid4())
        self.user_id = self._extract_user_id(self.jwt_token)
        self.base_url = base_url or "https://web.tabbit.ai"
        self.client_id = client_id or "e7fa44387b1238ef1f6f"
        # 浏览器版本号 + sparkle_version，用于 x-req-ctx 头绕过上游版本校验（code 493）
        # x-req-ctx = base64("版本号(sparkle_version)")，如 base64("1.4.46(10104046)")
        self.browser_version = browser_version or DEFAULT_BROWSER_VERSION
        self.sparkle_version = sparkle_version or DEFAULT_SPARKLE_VERSION
        self.verify_ssl = verify_ssl
        # 默认浏览器标记：编进 unique-uuid 第 5 位，后端据此发 Pro 会员权益
        # 移植自 web 端 eN(isDefault) 算法。设 True 即让后端按默认浏览器用户对待。
        self.default_browser = default_browser
        # v2 是当前前端发现的更原生入口，但部分上游域/账号会返回 404。
        # 首次失败后缓存降级到 v1，避免每轮多打一枪。
        self._chat_completion_api_version = "v2"
        # 服务器时间偏移（秒）：server_time = local_time + _server_time_offset
        # 从上游响应 Date 头惰性同步，规避 vps 本地时钟漂移导致时间戳位校验失败。
        # 初始 0（未同步），首次请求后校正。
        self._server_time_offset: float = 0.0
        self._server_time_synced: bool = False

        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=15, read=120, write=15, pool=15),
            follow_redirects=True,
            verify=verify_ssl,
        )

    def update_auth_token_value(self, token_str: str) -> None:
        parts = (token_str or "").split("|")
        self.jwt_token = parts[0] if parts else ""
        self.next_auth = parts[1] if len(parts) > 1 and parts[1] else None
        if len(parts) > 2 and parts[2]:
            self.device_id = parts[2]
        self.user_id = self._extract_user_id(self.jwt_token)

    def _client_cookie_value(self, name: str) -> str | None:
        try:
            value = self.client.cookies.get(name)
            if value:
                return value
        except Exception:
            pass
        for cookie in self.client.cookies.jar:
            if cookie.name == name and cookie.value:
                return cookie.value
        return None

    def export_auth_cookies(self) -> dict:
        cookies = {}
        jwt_token = self._client_cookie_value("token")
        next_auth = self._client_cookie_value("next-auth.session-token")
        if jwt_token:
            cookies["token"] = jwt_token
        if next_auth:
            cookies["next-auth.session-token"] = next_auth
        return cookies

    def _extract_user_id(self, token: str) -> str:
        try:
            payload = json.loads(
                base64.urlsafe_b64decode(token.split(".")[1] + "==")
            )
            return payload.get("id", payload.get("sub", str(uuid.uuid4())))
        except Exception:
            return str(uuid.uuid4())

    def _sync_server_time(self, resp: httpx.Response) -> None:
        """从上游响应 Date 头同步服务器时间偏移。

        上游 unique-uuid 时间戳位用服务器时钟校验，vps 本地时钟漂移会翻车。
        从 Date 头取权威时间，算出 offset，后续生成 uuid 时用 local+offset 校正。
        响应网络往返有延迟，但时间戳位精度是秒，±几秒在容忍窗内。
        """
        date_header = resp.headers.get("date")
        if not date_header:
            return
        try:
            server_dt = parsedate_to_datetime(date_header)
            server_ts = server_dt.timestamp()
            # 用收到响应的本地时间近似请求时刻（往返延迟对称的话误差小）
            self._server_time_offset = server_ts - time.time()
            self._server_time_synced = True
        except Exception:
            pass

    def _server_ts(self) -> float:
        """返回校正后的服务器时间戳。未同步时退回本地时间。"""
        return time.time() + self._server_time_offset

    def _get_headers(self, referer_path: str = "/newtab", with_uuid: bool = False) -> dict:
        # x-req-ctx 是版本校验关键头（base64("版本(sparkle_version)")），缺则 493
        x_req_ctx = base64.b64encode(
            f"{self.browser_version}({self.sparkle_version})".encode()
        ).decode()
        headers = {
            "User-Agent": f"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
            "sec-ch-ua": '"Chromium";v="148", "Tabbit";v="148", "Not/A)Brand";v="99"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"macOS"',
            "x-chrome-id-consistency-request": (
                f"version=1,client_id={self.client_id},"
                f"device_id={self.device_id},sync_account_id={self.user_id},"
                "signin_mode=all_accounts,signout_mode=show_confirmation"
            ),
            "x-req-ctx": x_req_ctx,
            "origin": self.base_url,
            "referer": f"{self.base_url}{referer_path}",
        }
        # unique-uuid 编码"是否默认浏览器"状态 + 时间戳，后端据此发 Pro 会员权益。
        # 算法移植自 web 端 eN()：第 5 位 "1"=默认浏览器，8 个固定位填当前时间戳。
        # 时间戳用上游服务器时间（从 Date 头同步），规避 vps 时钟漂移。
        # 真机抓包确认：只有聊天(/api/v1/chat/completion) + 额度(/api/commerce/quota/v1/usage)
        # 接口带 unique-uuid，其他接口不带。精确复刻真机行为。
        if with_uuid:
            headers["unique-uuid"] = _gen_unique_uuid(self.default_browser, self._server_ts())
        return headers

    def _get_cookies(self) -> dict:
        cookies = {
            "token": self.jwt_token,
            "user_id": self.user_id,
            "managed": "tab_browser",
            "NEXT_LOCALE": "zh",
        }
        if self.next_auth:
            cookies["next-auth.session-token"] = self.next_auth
        return cookies

    async def create_chat_session(self) -> str:
        """创建聊天会话，返回 session_id。

        上游路由已迁移: /chat/new 现在 307 重定向到 /session/new，
        session_id 藏在 /session/new 响应的 NEXT_REDIRECT;/session/<UUID>;307 里。
        ⚠️ router_state 必须用 chat 子树 (实测: /session/new + session子树 = 500,
        /session/new + chat子树 = 200 含 UUID)。
        三级提取: NEXT_REDIRECT digest → 路径 UUID → 任意裸 UUID。
        """
        # router_state 保持 chat 子树 (实测唯一有效组合)
        router_state = [
            "",
            {
                "children": [
                    "chat",
                    {
                        "children": [
                            ["id", "new", "d"],
                            {"children": ["__PAGE__", {}, None, "refetch"]},
                            None,
                            None,
                        ]
                    },
                    None,
                    None,
                ]
            },
            None,
            None,
        ]

        async def _try(path: str) -> str | None:
            headers = {
                **self._get_headers(path),
                "rsc": "1",
                "next-router-state-tree": urllib.parse.quote(json.dumps(router_state)),
            }
            resp = await self.client.get(
                f"{self.base_url}{path}",
                params={"_rsc": "auto"},
                headers=headers,
                cookies=self._get_cookies(),
            )
            self._sync_server_time(resp)
            text = resp.text
            # 1️⃣ 最精准: NEXT_REDIRECT digest 里挖 /session/<UUID>
            m = re.search(
                r"NEXT_REDIRECT;[^;]*;/(?:chat|session)/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
                text,
            )
            if m:
                return m.group(1)
            # 2️⃣ 路径里的 UUID
            m = re.search(
                r"/(?:chat|session)/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
                text,
            )
            if m:
                return m.group(1)
            # 3️⃣ 兜底: 任意裸 UUID
            uuids = re.findall(
                r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                text,
            )
            if uuids:
                return uuids[0]
            return None

        # 主路径: /session/new (上游当前路由, 实测有效)
        sid = await _try("/session/new")
        if sid:
            return sid
        # fallback: /chat/new (老路由, 万一回归)
        sid = await _try("/chat/new")
        if sid:
            return sid
        raise Exception("Failed to extract chat session_id from response")

    async def get_quota_usage(self) -> dict:
        """查询当前账号额度使用情况（真机抓包确认带 unique-uuid）。

        /api/commerce/quota/v1/usage 是会员权益判定接口，unique-uuid 第5位
        编码默认浏览器状态，后端据此发 Pro 权益（5x quota）。
        可用于验证伪装是否生效。
        """
        headers = self._get_headers("/newtab", with_uuid=True)
        resp = await self.client.get(
            f"{self.base_url}/api/commerce/quota/v1/usage",
            params={"user_id": self.user_id, "timezone": "Asia/Shanghai"},
            headers=headers,
            cookies=self._get_cookies(),
        )
        self._sync_server_time(resp)
        if resp.status_code != 200:
            raise Exception(f"quota query failed: {resp.status_code} {resp.text[:200]}")
        return resp.json()

    async def get_quota_pools(self, cycle_offset: int = 0) -> dict:
        """查询额度池详情。

        /api/commerce/quota/v1/pools 返回额度池的详细信息，包括总配额、剩余配额、已用配额等。
        """
        headers = self._get_headers("/member/usage", with_uuid=False)
        resp = await self.client.get(
            f"{self.base_url}/api/commerce/quota/v1/pools",
            params={"user_id": self.user_id, "cycle_offset": cycle_offset},
            headers=headers,
            cookies=self._get_cookies(),
        )
        self._sync_server_time(resp)
        if resp.status_code != 200:
            raise Exception(f"quota pools query failed: {resp.status_code} {resp.text[:200]}")
        return resp.json()

    async def get_usage_records(self, scene_names: list[str] | None = None, page: int = 1, limit: int = 100) -> dict:
        """查询额度使用记录。

        /api/commerce/quota/v1/usage-records 返回额度使用记录列表。
        """
        if scene_names is None:
            scene_names = ["chat", "agent"]
        headers = self._get_headers("/member/usage", with_uuid=False)
        params = [
            ("user_id", self.user_id),
            ("page", str(page)),
            ("limit", str(limit)),
            ("timezone", "Asia/Shanghai"),
        ]
        for name in scene_names:
            params.append(("scene_name", name))
        resp = await self.client.get(
            f"{self.base_url}/api/commerce/quota/v1/usage-records",
            params=params,
            headers=headers,
            cookies=self._get_cookies(),
        )
        self._sync_server_time(resp)
        if resp.status_code != 200:
            raise Exception(f"usage records query failed: {resp.status_code} {resp.text[:200]}")
        return resp.json()

    async def get_coupon_list(self, coupon_type: str = "weekly_reset_coupon", status: int = 1, offset: int = 0, limit: int = 50) -> dict:
        """查询可用重置券列表。

        /api/commerce/benefit/v1/coupon/list 返回可用的重置券列表。
        coupon_type: 优惠券类型，默认为 weekly_reset_coupon（周重置券）
        status: 优惠券状态，1=可用
        """
        headers = self._get_headers("/member/usage", with_uuid=False)
        params = {
            "user_id": self.user_id,
            "coupon_type": coupon_type,
            "offset": offset,
            "limit": limit,
        }
        if status is not None:
            params["user_coupon_status"] = status
        resp = await self.client.get(
            f"{self.base_url}/api/commerce/benefit/v1/coupon/list",
            params=params,
            headers=headers,
            cookies=self._get_cookies(),
        )
        self._sync_server_time(resp)
        if resp.status_code != 200:
            raise Exception(f"coupon list query failed: {resp.status_code} {resp.text[:200]}")
        return resp.json()

    async def participate_activity(self, request_no: str | None = None) -> dict:
        """参与活动领取重置券。

        /api/commerce/activity/v1/participate 参与活动领取重置券。
        返回 participation_result: "success" | "already_participated" | "ACTIVITY_NOT_OPEN"
        """
        if request_no is None:
            request_no = f"claim_{int(time.time())}_{secrets.token_hex(4)}"
        headers = self._get_headers("/member/usage", with_uuid=False)
        headers["Content-Type"] = "application/json"
        resp = await self.client.post(
            f"{self.base_url}/api/commerce/activity/v1/participate",
            json={"user_id": self.user_id, "request_no": request_no},
            headers=headers,
            cookies=self._get_cookies(),
        )
        self._sync_server_time(resp)
        if resp.status_code != 200:
            raise Exception(f"participate activity failed: {resp.status_code} {resp.text[:200]}")
        return resp.json()

    async def use_coupon(self, coupon_code: str, request_no: str | None = None) -> dict:
        """使用重置券。

        /api/commerce/benefit/v1/coupon/use 使用指定的重置券。
        返回 use_result: "success" | "failed"
        """
        if request_no is None:
            request_no = f"req_{int(time.time())}_{secrets.token_hex(4)}"
        headers = self._get_headers("/member/usage", with_uuid=False)
        headers["Content-Type"] = "application/json"
        resp = await self.client.post(
            f"{self.base_url}/api/commerce/benefit/v1/coupon/use",
            json={
                "user_id": self.user_id,
                "coupon_code": coupon_code,
                "coupon_type": "weekly_reset_coupon",
                "request_no": request_no,
            },
            headers=headers,
            cookies=self._get_cookies(),
        )
        self._sync_server_time(resp)
        if resp.status_code != 200:
            raise Exception(f"use coupon failed: {resp.status_code} {resp.text[:200]}")
        return resp.json()

    async def get_reset_coupon_sku(self) -> dict:
        """获取重置券商品信息。

        /api/commerce/product/v1/sku/usage-reset-coupon 返回重置券的商品信息，包括价格等。
        """
        headers = self._get_headers("/member/usage", with_uuid=False)
        resp = await self.client.get(
            f"{self.base_url}/api/commerce/product/v1/sku/usage-reset-coupon",
            headers=headers,
            cookies=self._get_cookies(),
        )
        self._sync_server_time(resp)
        if resp.status_code != 200:
            raise Exception(f"reset coupon sku query failed: {resp.status_code} {resp.text[:200]}")
        return resp.json()

    async def create_product_checkout_session(self, product_sku_id: str, request_no: str | None = None) -> dict:
        """创建产品支付会话（用于购买重置券）。

        /api/commerce/payment/v1/product-checkout-sessions 创建支付会话，返回 checkoutUrl。
        """
        if request_no is None:
            request_no = f"coupon_{int(time.time())}_{secrets.token_hex(4)}"
        headers = self._get_headers("/member/usage", with_uuid=False)
        headers["Content-Type"] = "application/json"
        resp = await self.client.post(
            f"{self.base_url}/api/commerce/payment/v1/product-checkout-sessions",
            json={
                "user_id": self.user_id,
                "product_sku_id": product_sku_id,
                "request_no": request_no,
            },
            headers=headers,
            cookies=self._get_cookies(),
        )
        self._sync_server_time(resp)
        if resp.status_code != 200:
            raise Exception(f"create checkout session failed: {resp.status_code} {resp.text[:200]}")
        return resp.json()

    async def get_order_status(self, order_no: str) -> dict:
        """查询订单状态。

        /api/commerce/payment/v1/orders/{order_no} 查询订单支付状态。
        """
        headers = self._get_headers("/member/usage", with_uuid=False)
        resp = await self.client.get(
            f"{self.base_url}/api/commerce/payment/v1/orders/{order_no}",
            params={"user_id": self.user_id},
            headers=headers,
            cookies=self._get_cookies(),
        )
        self._sync_server_time(resp)
        if resp.status_code != 200:
            raise Exception(f"order status query failed: {resp.status_code} {resp.text[:200]}")
        return resp.json()

    async def get_sign_in_status(self) -> dict:
        """查询签到状态。

        /api/commerce/activity/v1/sign-in/status 查询每日签到状态。
        """
        headers = self._get_headers("/member/usage", with_uuid=False)
        resp = await self.client.get(
            f"{self.base_url}/api/commerce/activity/v1/sign-in/status",
            params={"scene_codes": ["daily_sign_in", "desktop_pet"]},
            headers=headers,
            cookies=self._get_cookies(),
        )
        self._sync_server_time(resp)
        if resp.status_code != 200:
            raise Exception(f"sign-in status query failed: {resp.status_code} {resp.text[:200]}")
        return resp.json()

    async def sign_in(self, request_no: str | None = None) -> dict:
        """执行签到。

        /api/commerce/activity/v1/sign-in 执行每日签到，可获得用量奖励。
        """
        if request_no is None:
            request_no = f"sign_{int(time.time())}_{secrets.token_hex(4)}"
        headers = self._get_headers("/member/usage", with_uuid=False)
        headers["Content-Type"] = "application/json"
        resp = await self.client.post(
            f"{self.base_url}/api/commerce/activity/v1/sign-in",
            json={
                "request_no": request_no,
                "scene_codes": ["daily_sign_in", "desktop_pet"],
                "usage_percentage": "0%",  # 会被服务端忽略
            },
            headers=headers,
            cookies=self._get_cookies(),
        )
        self._sync_server_time(resp)
        if resp.status_code != 200:
            raise Exception(f"sign-in failed: {resp.status_code} {resp.text[:200]}")
        return resp.json()

    async def send_message(
        self, session_id: str, content: str, model: str,
        references: list | None = None, task_name: str = "chat",
        api_version: str = "auto", force_execute: bool = False,
        client_turn_id: str | None = None, agent_mode: bool = False,
        parallel_group_id: str | None = None, metadatas: dict | None = None,
    ) -> AsyncGenerator[dict, None]:
        """向会话发送消息，流式返回上游 SSE 事件

        真实 endpoint: POST /api/v1|v2/chat/completion（抓包确认）
        x-req-ctx 头是版本校验关键，缺则 493。

        references: 超长内容分流通道。网关只校验 content 主字段(≤20421)，
            references[].content 不受限制（实测模型可读到 7万+字符）。
            传非空 list 即启用分流，绕过 2万字符天花板。
        task_name: "chat"(默认) / "script"。分流时仍用 chat 保持语义。
        api_version: 默认 auto，优先尝试 v2 并带 client_turn_id/stream_mode；
            如果 v2 入口不可用且尚未产生任何 SSE 事件，会自动退回 v1 并缓存降级。
        """
        primary_version = self._chat_completion_api_version if api_version == "auto" else api_version
        versions = [primary_version]
        if primary_version == "v2":
            versions.append("v1")

        last_error: Exception | None = None
        for version in versions:
            yielded = False
            stream = self._stream_chat_completion(
                session_id=session_id,
                content=content,
                model=model,
                references=references,
                task_name=task_name,
                api_version=version,
                force_execute=force_execute,
                client_turn_id=client_turn_id,
                agent_mode=agent_mode,
                parallel_group_id=parallel_group_id,
                metadatas=metadatas,
            )
            try:
                async for event in stream:
                    yielded = True
                    yield event
                return
            except Exception as e:
                last_error = e
                if (
                    version == "v2"
                    and not yielded
                    and self._should_retry_completion_with_v1(e)
                ):
                    self._chat_completion_api_version = "v1"
                    logger.warning("v2 chat completion failed before stream; fallback to v1: %s", e)
                    continue
                raise
            finally:
                with suppress(Exception):
                    await stream.aclose()

        if last_error:
            raise last_error

    def _build_chat_completion_payload(
        self,
        session_id: str,
        content: str,
        model: str,
        references: list | None,
        task_name: str,
        api_version: str,
        force_execute: bool,
        client_turn_id: str | None,
        agent_mode: bool,
        parallel_group_id: str | None,
        metadatas: dict | None,
    ) -> dict:
        payload = {
            "chat_session_id": session_id,
            "message_id": None,
            "content": content,
            "selected_model": model,
            "parallel_group_id": parallel_group_id,
            "task_name": task_name,
            "agent_mode": agent_mode,
            "metadatas": metadatas or {"html_content": f"<p>{content}</p>"},
            "references": references or [],
            "entity": {
                "key": hashlib.md5(b"").hexdigest(),
                "extras": {"type": "tab", "url": ""},
            },
        }
        if api_version == "v2":
            payload.update(
                {
                    "client_turn_id": client_turn_id or str(uuid.uuid4()),
                    "stream_mode": "sse",
                    "force_execute": force_execute,
                }
            )
        return payload

    @staticmethod
    def _should_retry_completion_with_v1(error: Exception) -> bool:
        msg = str(error)
        return any(
            marker in msg
            for marker in (
                "Tabbit API error 400",
                "Tabbit API error 404",
                "Tabbit API error 405",
                "Tabbit API error 422",
            )
        )

    async def _stream_chat_completion(
        self,
        session_id: str,
        content: str,
        model: str,
        references: list | None,
        task_name: str,
        api_version: str,
        force_execute: bool,
        client_turn_id: str | None,
        agent_mode: bool,
        parallel_group_id: str | None,
        metadatas: dict | None,
    ) -> AsyncGenerator[dict, None]:
        payload = self._build_chat_completion_payload(
            session_id=session_id,
            content=content,
            model=model,
            references=references,
            task_name=task_name,
            api_version=api_version,
            force_execute=force_execute,
            client_turn_id=client_turn_id,
            agent_mode=agent_mode,
            parallel_group_id=parallel_group_id,
            metadatas=metadatas,
        )

        headers = {
            **self._get_headers(f"/session/{session_id}", with_uuid=True),
            "accept": "text/event-stream",
            "Content-Type": "application/json",
        }

        async with self.client.stream(
            "POST",
            f"{self.base_url}/api/{api_version}/chat/completion",
            json=payload,
            headers=headers,
            cookies=self._get_cookies(),
        ) as resp:
            # 流式响应头在进入上下文时就可读，同步服务器时间（供下次请求用）
            self._sync_server_time(resp)
            if resp.status_code != 200:
                body = await resp.aread()
                raise TabbitAPIError(
                    f"Tabbit API error {resp.status_code}: {body.decode()}",
                    status_code=resp.status_code,
                    headers=resp.headers,
                )

            # 收集首个事件，用于诊断 492/493（身份/版本校验失败）
            first_events = []
            current_event = None
            async for line in resp.aiter_lines():
                if line.startswith("event:"):
                    current_event = line[len("event:") :].strip()
                elif line.startswith("data:") and current_event:
                    data_str = line[len("data:") :].strip()
                    try:
                        data = json.loads(data_str)
                    except Exception:
                        continue
                    # 上游 error 事件必须抛出，否则调用方会拿到空内容
                    if current_event == "error":
                        msg = data.get("message", "unknown upstream error")
                        code = data.get("code", "")
                        # 492/493 时附加诊断信息（content 长度/特征），定位触发条件
                        if code in (492, 493):
                            logger.warning(
                                "upstream error %s | content_len=%d has_tools=%s "
                                "model=%s session=%s unique_uuid=%s",
                                code, len(content),
                                "<invoke" in content or "[Tools]" in content,
                                model, session_id, headers.get("unique-uuid", "")[:8],
                            )
                        raise TabbitAPIError(
                            f"Tabbit upstream error {code}: {msg}",
                            status_code=code if isinstance(code, int) else None,
                            code=code,
                        )
                    yield {"event": current_event, "data": data}
