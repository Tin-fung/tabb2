import re
import json
import uuid
import hashlib
import base64
import urllib.parse
from typing import AsyncGenerator

import httpx

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
    def __init__(self, token_str: str, base_url: str | None = None, client_id: str | None = None, browser_version: str | None = None, sparkle_version: int | None = None):
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

        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=15, read=120, write=15, pool=15),
            follow_redirects=False,
            verify=False,
        )

    def _extract_user_id(self, token: str) -> str:
        try:
            payload = json.loads(
                base64.urlsafe_b64decode(token.split(".")[1] + "==")
            )
            return payload.get("id", payload.get("sub", str(uuid.uuid4())))
        except Exception:
            return str(uuid.uuid4())

    def _get_headers(self, referer_path: str = "/newtab") -> dict:
        # x-req-ctx 是上游版本校验的关键头（base64 编码的 "版本(sparkle_version)"）
        # 解码即 "1.1.39(10101039)"，缺这个头会触发 493 "浏览器版本过低"
        x_req_ctx = base64.b64encode(
            f"{self.browser_version}({self.sparkle_version})".encode()
        ).decode()
        return {
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

        新版上游用 /api/v1/session 创建会话（POST），返回 JSON 含 id。
        兼容旧 RSC 方式作为 fallback。
        """
        headers = {
            **self._get_headers("/session/new"),
            "Content-Type": "application/json",
        }
        # 新接口：POST /api/v1/session
        try:
            resp = await self.client.post(
                f"{self.base_url}/api/v1/session",
                json={"title": "New Chat"},
                headers=headers,
                cookies=self._get_cookies(),
            )
            if resp.status_code == 200:
                data = resp.json()
                # 尝试常见字段
                sid = (
                    data.get("id")
                    or data.get("session_id")
                    or (data.get("data") or {}).get("id")
                    or (data.get("data") or {}).get("session_id")
                )
                if sid:
                    return sid
        except Exception:
            pass

        # fallback: 旧 RSC 方式（web.tabbitbrowser.com）
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
        old_headers = {
            **self._get_headers("/chat/new"),
            "rsc": "1",
            "next-router-state-tree": urllib.parse.quote(json.dumps(router_state)),
        }
        resp = await self.client.get(
            f"{self.base_url}/chat/new",
            params={"_rsc": "auto"},
            headers=old_headers,
            cookies=self._get_cookies(),
        )
        text = resp.text
        match = re.search(
            r"/(?:chat|session)/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
            text,
        )
        if match:
            return match.group(1)
        raise Exception("Failed to extract chat session_id from response")

    async def send_message(
        self, session_id: str, content: str, model: str
    ) -> AsyncGenerator[dict, None]:
        """向会话发送消息，流式返回上游 SSE 事件

        真实 endpoint: POST /api/v1/chat/completion（抓包确认）
        x-req-ctx 头是版本校验关键，缺则 493。
        """
        payload = {
            "chat_session_id": session_id,
            "message_id": None,
            "content": content,
            "selected_model": model,
            "parallel_group_id": None,
            "task_name": "chat",
            "agent_mode": False,
            "metadatas": {"html_content": f"<p>{content}</p>"},
            "references": [],
            "entity": {
                "key": hashlib.md5(b"").hexdigest(),
                "extras": {"type": "tab", "url": ""},
            },
        }

        headers = {
            **self._get_headers(f"/session/{session_id}"),
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
            if resp.status_code != 200:
                body = await resp.aread()
                raise Exception(
                    f"Tabbit API error {resp.status_code}: {body.decode()}"
                )

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
                        raise Exception(
                            f"Tabbit upstream error {code}: {msg}"
                        )
                    yield {"event": current_event, "data": data}
