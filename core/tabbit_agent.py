"""Official Tabbit Task-mode transport.

This module reproduces the conversion layer used by the current Tabbit
frontend: signed ``/chat/send`` bootstrap followed by the Agent v2 WebSocket.
It is intentionally route-agnostic so a future Responses bridge can own the
tool-call state machine without changing the existing chat endpoints.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import ssl
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, AsyncGenerator, Callable
from urllib.parse import urlparse, urlunparse

from core.tabbit_client import TabbitAPIError, TabbitClient


TERMINAL_AGENT_EVENTS = {"task_completed", "error", "audit_failure", "task_limit"}
DEFAULT_SIGN_KEY_TTL_SECONDS = 600
DEFAULT_HEARTBEAT_SECONDS = 20
DEFAULT_MAX_WEBSOCKET_BYTES = 16 * 1024 * 1024
EMPTY_ENTITY_KEY = "d41d8cd98f00b204e9800998ecf8427e"


@dataclass(frozen=True)
class AgentTaskBootstrap:
    session_id: str
    task_id: str
    request_message_id: str
    assistant_message_id: str
    refine_query: str
    refine_audit_pass: bool
    needs_agent: bool


@dataclass(frozen=True)
class AgentEvent:
    type: str
    data: dict[str, Any]
    session_id: str | None = None
    task_id: str | None = None


@dataclass(frozen=True)
class AgentTaskRequest:
    session_id: str
    content: str
    model: str
    references: list | None = None
    metadatas: dict | None = None
    message_id: str | None = None


@dataclass(frozen=True)
class AgentTransportOptions:
    sign_key_ttl_seconds: int = DEFAULT_SIGN_KEY_TTL_SECONDS
    heartbeat_seconds: int = DEFAULT_HEARTBEAT_SECONDS
    max_websocket_bytes: int = DEFAULT_MAX_WEBSOCKET_BYTES


@dataclass(frozen=True)
class AgentTransportDependencies:
    websocket_connect: Callable[..., Any] | None = None
    clock_ms: Callable[[], int] | None = None
    nonce_factory: Callable[[], str] | None = None


class TabbitAgentClient:
    """Client for Tabbit's signed Task-mode and Agent WebSocket protocols."""

    def __init__(
        self,
        tabbit_client: TabbitClient,
        *,
        options: AgentTransportOptions | None = None,
        dependencies: AgentTransportDependencies | None = None,
    ):
        options = options or AgentTransportOptions()
        dependencies = dependencies or AgentTransportDependencies()
        self.tabbit = tabbit_client
        self._websocket_connect = (
            dependencies.websocket_connect or self._default_websocket_connect
        )
        self._clock_ms = dependencies.clock_ms or (lambda: int(time.time() * 1000))
        self._nonce_factory = dependencies.nonce_factory or (lambda: str(uuid.uuid4()))
        self._sign_key_ttl_seconds = max(0, options.sign_key_ttl_seconds)
        self._heartbeat_seconds = max(1, options.heartbeat_seconds)
        self._max_websocket_bytes = max(1024, options.max_websocket_bytes)
        self._sign_key: str | None = None
        self._sign_key_expires_at = 0.0

    @staticmethod
    def _default_websocket_connect(url: str, **kwargs):
        try:
            from websockets.asyncio.client import connect
        except ImportError as exc:  # pragma: no cover - installation contract
            raise RuntimeError(
                "Agent transport requires the 'websockets' package"
            ) from exc
        return connect(url, **kwargs)

    async def get_signing_key(self, *, force_refresh: bool = False) -> str:
        now = time.monotonic()
        if (
            not force_refresh
            and self._sign_key
            and now < self._sign_key_expires_at
        ):
            return self._sign_key

        response = await self.tabbit.client.get(
            f"{self.tabbit.base_url}/chat/sign-key",
            headers=self.tabbit._get_headers("/newtab", with_uuid=False),
            cookies=self.tabbit._get_cookies(),
        )
        self.tabbit._sync_server_time(response)
        if response.status_code != 200:
            raise TabbitAPIError(
                f"Tabbit sign-key error {response.status_code}",
                status_code=response.status_code,
                headers=getattr(response, "headers", {}),
            )
        sign_key = (response.text or "").strip()
        if not sign_key:
            raise TabbitAPIError("Tabbit returned an empty chat signing key")
        self._sign_key = sign_key
        self._sign_key_expires_at = now + self._sign_key_ttl_seconds
        return sign_key

    def build_signed_headers(
        self,
        body: str,
        signing_key: str,
        referer_path: str,
    ) -> dict[str, str]:
        timestamp = str(self._clock_ms())
        nonce = self._nonce_factory()
        body_digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        canonical = f"{timestamp}.{nonce}.{body_digest}"
        signature = hmac.new(
            signing_key.encode("utf-8"),
            canonical.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return {
            **self.tabbit._get_headers(referer_path, with_uuid=True),
            "accept": "text/event-stream",
            "cache-control": "no-cache",
            "content-type": "application/json",
            "trace-id": str(uuid.uuid4()),
            # These names match the current official frontend, even though the
            # UUID is carried in x-signature and the digest in x-nonce.
            "x-timestamp": timestamp,
            "x-signature": nonce,
            "x-nonce": signature,
        }

    @staticmethod
    def _build_task_payload(request: AgentTaskRequest) -> dict[str, Any]:
        return {
            "chat_session_id": request.session_id,
            "message_id": request.message_id,
            "content": request.content,
            "selected_model": request.model,
            "parallel_group_id": None,
            "task_name": "chat",
            "agent_mode": True,
            "metadatas": request.metadatas
            or {"html_content": f"<p>{request.content}</p>"},
            "references": request.references or [],
            "entity": {
                # Fixed compatibility value used by the official frontend; it
                # isn't used for integrity, authentication, or hashing input.
                "key": EMPTY_ENTITY_KEY,
                "extras": {"type": "tab", "url": ""},
            },
        }

    async def bootstrap_task(
        self,
        request: AgentTaskRequest,
    ) -> AgentTaskBootstrap:
        payload = self._build_task_payload(request)
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        signing_key = await self.get_signing_key()
        headers = self.build_signed_headers(
            body, signing_key, f"/session/{request.session_id}"
        )

        async with self.tabbit.client.stream(
            "POST",
            f"{self.tabbit.base_url}/chat/send",
            content=body.encode("utf-8"),
            headers=headers,
            cookies=self.tabbit._get_cookies(),
        ) as response:
            self.tabbit._sync_server_time(response)
            if response.status_code != 200:
                raw = await response.aread()
                raise TabbitAPIError(
                    f"Tabbit chat/send error {response.status_code}: "
                    f"{raw.decode(errors='replace')[:500]}",
                    status_code=response.status_code,
                    headers=response.headers,
                )
            async for event_type, data in self._iter_sse(response):
                if event_type == "error":
                    code = data.get("code")
                    raise TabbitAPIError(
                        f"Tabbit Agent bootstrap error {code}: "
                        f"{data.get('message', 'unknown error')}",
                        status_code=code if isinstance(code, int) else None,
                        code=code,
                    )
                if event_type == "browser_use_start":
                    return self._parse_bootstrap(data, request.session_id)

        raise TabbitAPIError("Tabbit Agent bootstrap ended without browser_use_start")

    @staticmethod
    async def _iter_sse(response) -> AsyncGenerator[tuple[str, dict], None]:
        event_type: str | None = None
        data_lines: list[str] = []
        async for line in response.aiter_lines():
            if not line:
                if event_type and data_lines:
                    yield event_type, TabbitAgentClient._decode_sse_data(data_lines)
                event_type = None
                data_lines = []
                continue
            if line.startswith("event:"):
                event_type = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].strip())
        if event_type and data_lines:
            yield event_type, TabbitAgentClient._decode_sse_data(data_lines)

    @staticmethod
    def _decode_sse_data(data_lines: list[str]) -> dict:
        raw = "\n".join(data_lines)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {"content": raw}
        return data if isinstance(data, dict) else {"content": raw}

    @staticmethod
    def _parse_bootstrap(data: dict, fallback_session_id: str) -> AgentTaskBootstrap:
        task_id = data.get("task_id")
        request_message_id = data.get("request_message_id")
        assistant_message_id = data.get("assistant_message_id")
        if not all(isinstance(value, str) and value for value in (
            task_id,
            request_message_id,
            assistant_message_id,
        )):
            raise TabbitAPIError("Invalid browser_use_start payload")
        return AgentTaskBootstrap(
            session_id=data.get("chat_session_id") or fallback_session_id,
            task_id=task_id,
            request_message_id=request_message_id,
            assistant_message_id=assistant_message_id,
            refine_query=str(data.get("refine_query") or ""),
            refine_audit_pass=data.get("refine_audit_pass") is not False,
            needs_agent=data.get("needs_agent") is not False,
        )

    def _websocket_url(self) -> str:
        parsed = urlparse(self.tabbit.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("Tabbit base_url must be an absolute HTTP(S) URL")
        scheme = "wss" if parsed.scheme == "https" else "ws"
        return urlunparse((scheme, parsed.netloc, "/api/agent/v2/ws", "", "", ""))

    def _websocket_kwargs(self) -> dict[str, Any]:
        cookies = self.tabbit._get_cookies()
        cookie_header = "; ".join(f"{key}={value}" for key, value in cookies.items())
        kwargs: dict[str, Any] = {
            "origin": self.tabbit.base_url,
            "additional_headers": {
                "Cookie": cookie_header,
                "User-Agent": self.tabbit._get_headers().get("User-Agent", "Mozilla/5.0"),
            },
            "max_size": self._max_websocket_bytes,
            "ping_interval": None,
            "open_timeout": 20,
        }
        if self._websocket_url().startswith("wss://") and not getattr(
            self.tabbit, "verify_ssl", True
        ):
            # nosec B501: explicitly enabled only by the existing debug config.
            kwargs["ssl"] = ssl._create_unverified_context()
        return kwargs

    async def run_task(
        self,
        bootstrap: AgentTaskBootstrap,
        *,
        page_info_list: list[dict] | None = None,
    ) -> AsyncGenerator[AgentEvent, None]:
        connect = self._websocket_connect(
            self._websocket_url(), **self._websocket_kwargs()
        )
        async with connect as websocket:
            await websocket.send(
                json.dumps(
                    {
                        "type": "start_task",
                        "session_id": bootstrap.session_id,
                        "task_id": bootstrap.task_id,
                        "data": {
                            "user_message_id": bootstrap.request_message_id,
                            "page_info_list": page_info_list or [],
                        },
                        "timestamp": self._iso_timestamp(),
                    },
                    separators=(",", ":"),
                )
            )
            while True:
                try:
                    raw = await asyncio.wait_for(
                        websocket.recv(), timeout=self._heartbeat_seconds
                    )
                except asyncio.TimeoutError:
                    await websocket.send(
                        json.dumps(
                            {
                                "type": "ping",
                                "session_id": bootstrap.session_id,
                                "data": {"timestamp": self._iso_timestamp()},
                            },
                            separators=(",", ":"),
                        )
                    )
                    continue
                if not isinstance(raw, str):
                    continue
                try:
                    message = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(message, dict):
                    continue
                event_type = message.get("type")
                data = message.get("data")
                if not isinstance(event_type, str) or not isinstance(data, dict):
                    continue
                event = AgentEvent(
                    type=event_type,
                    data=data,
                    session_id=message.get("session_id"),
                    task_id=message.get("task_id"),
                )
                yield event
                if event_type in TERMINAL_AGENT_EVENTS:
                    return

    @staticmethod
    def _iso_timestamp() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
            "+00:00", "Z"
        )
