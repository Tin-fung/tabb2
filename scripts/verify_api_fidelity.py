"""Live smoke test for native API fidelity.

Checks three things:
  1. Direct TabbitClient.send_message() works through the default v2 transport.
  2. /v1/chat/completions streaming emits OpenAI-like chunk fields and usage.
  3. /v1/messages streaming emits Anthropic-like event ordering.

This script intentionally avoids printing tokens or request credentials.

Usage:
  .venv/bin/python scripts/verify_api_fidelity.py --model DeepSeek-V4-Pro
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.claude_compat import map_claude_to_content, random_trigger_signal  # noqa: E402
from core.config import ConfigManager  # noqa: E402
from core.tabbit_client import TabbitClient  # noqa: E402


DEFAULT_SERVER = "http://localhost:8800"


def _load_config() -> ConfigManager:
    return ConfigManager()


def _first_token(cfg: ConfigManager) -> str:
    for token in cfg.get("tokens", default=[]) or []:
        if token.get("enabled", True) and token.get("value"):
            return token["value"]
    raise RuntimeError("no enabled token in config.json")


def _proxy_auth_headers(cfg: ConfigManager, *, anthropic: bool = False) -> dict:
    api_key = cfg.get("proxy", "api_key", default="")
    if api_key:
        return {"x-api-key": api_key} if anthropic else {"Authorization": f"Bearer {api_key}"}
    if cfg.get("tokens", default=[]):
        raise RuntimeError("proxy.api_key is required when verifying endpoints backed by the token pool")
    return {}


def _post_json_stream(url: str, body: dict, headers: dict | None = None, timeout: int = 180) -> list[dict]:
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    events = []
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        buffer = ""
        for chunk in iter(lambda: resp.read(1024), b""):
            buffer += chunk.decode("utf-8", errors="replace")
            while "\n\n" in buffer:
                raw, buffer = buffer.split("\n\n", 1)
                event_type = None
                data_lines = []
                for line in raw.splitlines():
                    if line.startswith("event: "):
                        event_type = line[7:].strip()
                    elif line.startswith("data: "):
                        data_lines.append(line[6:])
                if not data_lines:
                    continue
                data_str = "\n".join(data_lines)
                if data_str == "[DONE]":
                    events.append({"event": event_type or "done", "data": "[DONE]"})
                    continue
                try:
                    data = json.loads(data_str)
                except json.JSONDecodeError:
                    continue
                events.append({"event": event_type, "data": data})
    return events


def _health(server: str) -> dict:
    with urllib.request.urlopen(f"{server}/health", timeout=5) as resp:
        return json.loads(resp.read())


async def verify_direct_tabbit(cfg: ConfigManager, model: str) -> dict:
    token = _first_token(cfg)
    client = TabbitClient(
        token,
        base_url=cfg.get("tabbit", "base_url", default="https://web.tabbit.ai"),
        client_id=cfg.get("tabbit", "client_id"),
        browser_version=cfg.get("tabbit", "browser_version"),
        sparkle_version=cfg.get("tabbit", "sparkle_version"),
        default_browser=cfg.get("tabbit", "default_browser", default=True),
        verify_ssl=cfg.get("tabbit", "verify_ssl", default=False),
    )
    try:
        session_id = await client.create_chat_session()
        raw = []
        async for event in client.send_message(
            session_id,
            "Say 'API fidelity smoke ok' and nothing else.",
            model,
        ):
            raw.append(event)
            if event["event"] == "finish":
                break
        counts = {}
        text = ""
        for event in raw:
            counts[event["event"]] = counts.get(event["event"], 0) + 1
            if event["event"] == "message_chunk":
                text += event["data"].get("content", "")
        if not raw:
            raise AssertionError("direct upstream stream returned no events")
        if "error" in counts:
            raise AssertionError(f"direct upstream returned error event: {counts}")
        return {
            "ok": True,
            "events": counts,
            "text_preview": text[:120],
        }
    finally:
        await client.client.aclose()


def verify_openai_stream(server: str, model: str, cfg: ConfigManager) -> dict:
    events = _post_json_stream(
        f"{server}/v1/chat/completions",
        {
            "model": model,
            "stream": True,
            "stream_options": {"include_usage": True},
            "messages": [
                {
                    "role": "user",
                    "content": "Reply with exactly: OpenAI fidelity smoke ok",
                }
            ],
        },
        headers=_proxy_auth_headers(cfg),
    )
    chunks = [e["data"] for e in events if isinstance(e["data"], dict)]
    if not chunks:
        raise AssertionError("OpenAI stream returned no JSON chunks")
    first = chunks[0]
    for key in ("id", "object", "created", "model", "choices", "system_fingerprint"):
        if key not in first:
            raise AssertionError(f"OpenAI first chunk missing {key}")
    if first["choices"][0].get("delta", {}).get("role") != "assistant":
        raise AssertionError("OpenAI first chunk missing assistant role delta")
    if not any(chunk.get("choices") == [] and chunk.get("usage") for chunk in chunks):
        raise AssertionError("OpenAI stream missing include_usage usage chunk")
    if not any(e["data"] == "[DONE]" for e in events):
        raise AssertionError("OpenAI stream missing [DONE]")
    finish = [
        choice.get("finish_reason")
        for chunk in chunks
        for choice in chunk.get("choices", [])
        if choice.get("finish_reason")
    ]
    if not finish:
        raise AssertionError("OpenAI stream missing finish_reason")
    return {
        "ok": True,
        "chunks": len(chunks),
        "finish": finish[-1],
        "usage": next(chunk["usage"] for chunk in chunks if chunk.get("choices") == []),
    }


def verify_claude_stream(server: str, model: str, cfg: ConfigManager) -> dict:
    events = _post_json_stream(
        f"{server}/v1/messages",
        {
            "model": model,
            "max_tokens": 1024,
            "stream": True,
            "messages": [
                {
                    "role": "user",
                    "content": "Reply with exactly: Claude fidelity smoke ok",
                }
            ],
        },
        headers=_proxy_auth_headers(cfg, anthropic=True),
    )
    names = [event["event"] for event in events if event["event"]]
    for required in ("message_start", "message_delta", "message_stop"):
        if required not in names:
            raise AssertionError(f"Claude stream missing {required}")
    if names.index("message_start") > names.index("message_stop"):
        raise AssertionError("Claude message_start appears after message_stop")
    deltas = [
        event["data"].get("delta", {}).get("stop_reason")
        for event in events
        if isinstance(event["data"], dict) and event["event"] == "message_delta"
    ]
    if not any(deltas):
        raise AssertionError("Claude stream missing stop_reason")
    return {
        "ok": True,
        "events": {name: names.count(name) for name in sorted(set(names))},
        "stop_reason": next(reason for reason in reversed(deltas) if reason),
    }


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", default=DEFAULT_SERVER)
    parser.add_argument("--model", default="DeepSeek-V4-Pro")
    parser.add_argument("--json", action="store_true", help="print machine-readable summary")
    args = parser.parse_args()

    started_at = time.time()
    results = {"model": args.model, "server": args.server, "checks": {}}
    cfg = _load_config()

    health = _health(args.server)
    results["checks"]["health"] = health
    if health.get("status") != "ok":
        raise AssertionError(f"server unhealthy: {health}")

    results["checks"]["direct_tabbit"] = await verify_direct_tabbit(cfg, args.model)
    results["checks"]["openai_stream"] = verify_openai_stream(args.server, args.model, cfg)
    results["checks"]["claude_stream"] = verify_claude_stream(args.server, args.model, cfg)
    results["duration_sec"] = round(time.time() - started_at, 2)

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        print("=" * 80)
        print("API fidelity live smoke passed")
        print("=" * 80)
        print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except urllib.error.URLError as e:
        print(f"server/network error: {e}", file=sys.stderr)
        raise SystemExit(1)
