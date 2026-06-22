import re
import json
import uuid
import time
import hashlib
import base64
import random
import logging
import urllib.parse
from email.utils import parsedate_to_datetime
from typing import AsyncGenerator

import httpx

logger = logging.getLogger("tabbit2openai")

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
    "claude-sonnet-4.5": "claude-sonnet-4-5@20250929",
    "claude-haiku-4.5": "claude-haiku-4-5@20251001",
    "glm-5": "glm-5",
    "glm-4.7": "glm-4.7",
    "deepseek-v3.2": "byteplus/deepseek-v3-2",
    "minimax-m2.7": "MiniMax-M2.7",
    "minimax-m2.5": "minimax-m2.5",
    "kimi-k2.5": "novita/kimi-k2.5",
    "qwen3-max": "qwen3-max",
    "doubao-seed-1.8": "byteplus/seed-1-8",
}


class TabbitClient:
    def __init__(self, token_str: str, base_url: str | None = None, client_id: str | None = None, browser_version: str | None = None, sparkle_version: int | None = None, default_browser: bool = True, verify_ssl: bool = False):
        parts = token_str.split("|")
        self.jwt_token = parts[0]
        self.next_auth = parts[1] if len(parts) > 1 else None
        self.device_id = parts[2] if len(parts) > 2 else str(uuid.uuid4())
        self.user_id = self._extract_user_id(self.jwt_token)
        self.base_url = base_url or "https://web.tabbit.ai"
        self.client_id = client_id or "e7fa44387b1238ef1f6f"
        # 浏览器版本号 + sparkle_version，用于 x-req-ctx 头绕过上游版本校验（code 493）
        # x-req-ctx = base64("版本号(sparkle_version)")，如 base64("1.1.39(10101039)")
        self.browser_version = browser_version or "1.1.39"
        self.sparkle_version = sparkle_version or 10101039
        # 默认浏览器标记：编进 unique-uuid 第 5 位，后端据此发 Pro 会员权益
        # 移植自 web 端 eN(isDefault) 算法。设 True 即让后端按默认浏览器用户对待。
        self.default_browser = default_browser
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
        """创建聊天会话，返回 session_id

        用 RSC 方式 GET /chat/new，从响应里提取 session_id。
        v10 验证: 此方式在 web.tabbit.ai 上可成功建会话。
        """
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
        headers = {
            **self._get_headers("/chat/new"),
            "rsc": "1",
            "next-router-state-tree": urllib.parse.quote(json.dumps(router_state)),
        }
        resp = await self.client.get(
            f"{self.base_url}/chat/new",
            params={"_rsc": "auto"},
            headers=headers,
            cookies=self._get_cookies(),
        )
        self._sync_server_time(resp)
        text = resp.text
        match = re.search(
            r"/(?:chat|session)/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
            text,
        )
        if match:
            return match.group(1)
        # 兜底：响应里直接找任意 UUID（v10 验证响应含裸 UUID）
        uuids = re.findall(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
            text,
        )
        if uuids:
            return uuids[0]
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

    async def send_message(
        self, session_id: str, content: str, model: str,
        references: list | None = None, task_name: str = "chat",
    ) -> AsyncGenerator[dict, None]:
        """向会话发送消息，流式返回上游 SSE 事件

        真实 endpoint: POST /api/v1/chat/completion（抓包确认）
        x-req-ctx 头是版本校验关键，缺则 493。

        references: 超长内容分流通道。网关只校验 content 主字段(≤20421)，
            references[].content 不受限制（实测模型可读到 7万+字符）。
            传非空 list 即启用分流，绕过 2万字符天花板。
        task_name: "chat"(默认) / "script"。分流时仍用 chat 保持语义。
        """
        payload = {
            "chat_session_id": session_id,
            "message_id": None,
            "content": content,
            "selected_model": model,
            "parallel_group_id": None,
            "task_name": task_name,
            "agent_mode": False,
            "metadatas": {"html_content": f"<p>{content}</p>"},
            "references": references or [],
            "entity": {
                "key": hashlib.md5(b"").hexdigest(),
                "extras": {"type": "tab", "url": ""},
            },
        }

        headers = {
            **self._get_headers(f"/session/{session_id}", with_uuid=True),
            "accept": "text/event-stream",
            "Content-Type": "application/json",
        }

        async with self.client.stream(
            "POST",
            f"{self.base_url}/api/v1/chat/completion",
            json=payload,
            headers=headers,
            cookies=self._get_cookies(),
        ) as resp:
            # 流式响应头在进入上下文时就可读，同步服务器时间（供下次请求用）
            self._sync_server_time(resp)
            if resp.status_code != 200:
                body = await resp.aread()
                raise Exception(
                    f"Tabbit API error {resp.status_code}: {body.decode()}"
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
                        raise Exception(
                            f"Tabbit upstream error {code}: {msg}"
                        )
                    yield {"event": current_event, "data": data}
