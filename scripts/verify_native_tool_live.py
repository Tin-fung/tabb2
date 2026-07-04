"""Live smoke test for Tabbit native tool observability.

This verifies the live Native Tool Plane path against a running local server:

  1. Drain an OpenAI-compatible streaming request that should trigger upstream
     Tabbit native search.
  2. Poll admin logs/status until native tool summary fields appear.
  3. Validate that the native tool executed upstream and was logged by tabb2.

The script intentionally avoids printing proxy/admin credentials.

Usage:
  TABBIT_ADMIN_TOKEN=... .venv/bin/python scripts/verify_native_tool_live.py
  TABBIT_ADMIN_PASSWORD=... .venv/bin/python scripts/verify_native_tool_live.py --json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.config import ConfigManager  # noqa: E402


DEFAULT_SERVER = "http://localhost:8800"
DEFAULT_MODEL = "Default"
DEFAULT_TOOL_NAME = "parallel_web_search"
DEFAULT_PROMPT = (
    "Use your built-in web search to find one current AI or technology news item. "
    "Summarize the result in one short paragraph and mention that you searched."
)


def normalize_server(server: str) -> str:
    return (server or DEFAULT_SERVER).rstrip("/")


def proxy_auth_headers(
    cfg: ConfigManager,
    *,
    api_key_override: str | None = None,
    bearer_override: str | None = None,
) -> dict:
    api_key = api_key_override or cfg.get("proxy", "api_key", default="")
    if api_key:
        return {"Authorization": f"Bearer {api_key}"}

    if cfg.get("tokens", default=[]):
        raise RuntimeError(
            "proxy.api_key is required when verifying endpoints backed by the token pool"
        )

    if bearer_override:
        return {"Authorization": f"Bearer {bearer_override}"}
    raise RuntimeError(
        "no token pool found; pass --proxy-bearer or configure tokens and proxy.api_key"
    )


def admin_auth_headers(token: str) -> dict:
    if not token:
        raise RuntimeError(
            "admin token is required; set TABBIT_ADMIN_TOKEN or pass --admin-password"
        )
    return {"Authorization": f"Bearer {token}"}


def extract_log_items(payload: Any) -> list[dict]:
    if isinstance(payload, dict):
        if isinstance(payload.get("items"), list):
            return payload["items"]
        if isinstance(payload.get("recent_logs"), list):
            return payload["recent_logs"]
    return []


def validate_native_tool_log(log: dict, tool_name: str) -> dict:
    names = log.get("native_tool_names") or []
    statuses = log.get("native_tools_status") or []
    count = int(log.get("native_tools_count") or 0)
    result_chars = int(log.get("native_tools_result_chars") or 0)

    if count <= 0:
        raise AssertionError(f"expected native tool activity for {tool_name}")
    if tool_name not in names:
        raise AssertionError(f"expected {tool_name} in native_tool_names, got {names}")

    indexes = [i for i, name in enumerate(names) if name == tool_name]
    matched_statuses = [
        statuses[i] for i in indexes
        if i < len(statuses)
    ]
    if not matched_statuses:
        matched_statuses = statuses
    if not matched_statuses or any(status != "success" for status in matched_statuses):
        raise AssertionError(f"expected successful native tool status, got {statuses}")
    if result_chars <= 0:
        raise AssertionError("expected non-empty native tool result")

    return {
        "timestamp": log.get("timestamp"),
        "model": log.get("model"),
        "status": log.get("status"),
        "native_tools_count": count,
        "native_tool_names": names,
        "native_tools_status": statuses,
        "native_tools_duration_ms": int(log.get("native_tools_duration_ms") or 0),
        "native_tools_result_chars": result_chars,
    }


def _parse_log_timestamp(timestamp: str | None) -> float | None:
    if not timestamp:
        return None
    try:
        dt = datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc).timestamp()


def find_native_tool_log(
    logs: list[dict],
    tool_name: str,
    *,
    since_epoch: float | None = None,
    model: str | None = None,
) -> dict | None:
    for log in logs:
        if model and log.get("model") != model:
            continue
        if since_epoch is not None:
            log_ts = _parse_log_timestamp(log.get("timestamp"))
            if log_ts is not None and log_ts < since_epoch - 1:
                continue
        try:
            validate_native_tool_log(log, tool_name)
        except AssertionError:
            continue
        return log
    return None


def _request_json(
    method: str,
    url: str,
    *,
    body: dict | None = None,
    headers: dict | None = None,
    timeout: int = 30,
) -> dict:
    data = None
    request_headers = {"Accept": "application/json", **(headers or {})}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=request_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {url} failed: HTTP {e.code} {raw[:300]}") from e
    return json.loads(raw or "{}")


def _post_json_stream(
    url: str,
    body: dict,
    *,
    headers: dict | None = None,
    timeout: int = 180,
) -> list[dict]:
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    events = []
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            buffer = ""
            for chunk in iter(lambda: resp.read(1024), b""):
                buffer += chunk.decode("utf-8", errors="replace")
                while "\n\n" in buffer:
                    raw, buffer = buffer.split("\n\n", 1)
                    data_lines = []
                    for line in raw.splitlines():
                        if line.startswith("data: "):
                            data_lines.append(line[6:])
                    if not data_lines:
                        continue
                    data_str = "\n".join(data_lines)
                    if data_str == "[DONE]":
                        events.append({"data": "[DONE]"})
                        continue
                    try:
                        events.append({"data": json.loads(data_str)})
                    except json.JSONDecodeError:
                        continue
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"POST {url} failed: HTTP {e.code} {raw[:300]}") from e
    return events


def login_admin(server: str, password: str, *, timeout: int = 30) -> str:
    payload = _request_json(
        "POST",
        f"{server}/api/admin/login",
        body={"password": password},
        timeout=timeout,
    )
    token = payload.get("token")
    if not token:
        raise RuntimeError("admin login did not return a token")
    return token


def resolve_admin_token(args: argparse.Namespace, server: str) -> str:
    token = args.admin_token or os.environ.get("TABBIT_ADMIN_TOKEN")
    if token:
        return token
    password = args.admin_password or os.environ.get("TABBIT_ADMIN_PASSWORD")
    if password:
        return login_admin(server, password, timeout=args.http_timeout)
    raise RuntimeError(
        "admin auth required; set TABBIT_ADMIN_TOKEN or TABBIT_ADMIN_PASSWORD"
    )


def fetch_admin_logs(
    server: str,
    headers: dict,
    *,
    page_size: int = 50,
    timeout: int = 30,
) -> list[dict]:
    query = urllib.parse.urlencode({"page": 1, "page_size": page_size})
    payload = _request_json(
        "GET",
        f"{server}/api/admin/logs?{query}",
        headers=headers,
        timeout=timeout,
    )
    return extract_log_items(payload)


def fetch_status_logs(
    server: str,
    headers: dict,
    *,
    timeout: int = 30,
) -> list[dict]:
    payload = _request_json(
        "GET",
        f"{server}/api/admin/status",
        headers=headers,
        timeout=timeout,
    )
    return extract_log_items(payload)


def wait_for_native_tool_log(
    server: str,
    admin_headers: dict,
    tool_name: str,
    *,
    since_epoch: float,
    model: str | None = None,
    timeout: int = 45,
    poll_interval: float = 2.0,
    page_size: int = 50,
    http_timeout: int = 30,
) -> dict:
    deadline = time.time() + timeout
    last_error = None
    while time.time() < deadline:
        try:
            logs = fetch_admin_logs(
                server,
                admin_headers,
                page_size=page_size,
                timeout=http_timeout,
            )
            log = find_native_tool_log(
                logs,
                tool_name,
                since_epoch=since_epoch,
                model=model,
            )
            if log:
                return validate_native_tool_log(log, tool_name)

            status_logs = fetch_status_logs(server, admin_headers, timeout=http_timeout)
            log = find_native_tool_log(
                status_logs,
                tool_name,
                since_epoch=since_epoch,
                model=model,
            )
            if log:
                return validate_native_tool_log(log, tool_name)
        except Exception as e:  # pragma: no cover - live diagnostics only
            last_error = str(e)
        time.sleep(poll_interval)

    suffix = f"; last admin error: {last_error}" if last_error else ""
    raise AssertionError(
        f"native tool log not found for {tool_name} within {timeout}s{suffix}"
    )


def verify_health(server: str, *, timeout: int = 10) -> dict:
    health = _request_json("GET", f"{server}/health", timeout=timeout)
    if health.get("status") != "ok":
        raise AssertionError(f"server unhealthy: {health}")
    return health


def trigger_openai_native_search(
    server: str,
    model: str,
    prompt: str,
    headers: dict,
    *,
    timeout: int = 180,
) -> dict:
    events = _post_json_stream(
        f"{server}/v1/chat/completions",
        {
            "model": model,
            "stream": True,
            "stream_options": {"include_usage": True},
            "messages": [{"role": "user", "content": prompt}],
        },
        headers=headers,
        timeout=timeout,
    )
    chunks = [event["data"] for event in events if isinstance(event.get("data"), dict)]
    if not chunks:
        raise AssertionError("OpenAI stream returned no JSON chunks")
    if not any(event.get("data") == "[DONE]" for event in events):
        raise AssertionError("OpenAI stream missing [DONE]")

    text = ""
    for chunk in chunks:
        for choice in chunk.get("choices", []):
            delta = choice.get("delta") or {}
            text += delta.get("content") or ""
    return {
        "chunks": len(chunks),
        "done": True,
        "text_preview": text[:160],
    }


def run_live_smoke(args: argparse.Namespace) -> dict:
    server = normalize_server(args.server)
    cfg = ConfigManager()
    proxy_headers = proxy_auth_headers(
        cfg,
        api_key_override=args.proxy_api_key or os.environ.get("TABBIT_PROXY_API_KEY"),
        bearer_override=args.proxy_bearer or os.environ.get("TABBIT_PROXY_BEARER"),
    )
    admin_headers = admin_auth_headers(resolve_admin_token(args, server))

    started_at = time.time()
    results = {
        "server": server,
        "model": args.model,
        "tool_name": args.tool_name,
        "checks": {},
    }
    results["checks"]["health"] = verify_health(server, timeout=args.http_timeout)
    results["checks"]["openai_stream"] = trigger_openai_native_search(
        server,
        args.model,
        args.prompt,
        proxy_headers,
        timeout=args.request_timeout,
    )
    results["checks"]["native_tool_log"] = wait_for_native_tool_log(
        server,
        admin_headers,
        args.tool_name,
        since_epoch=started_at,
        model=args.model,
        timeout=args.log_timeout,
        poll_interval=args.poll_interval,
        page_size=args.log_page_size,
        http_timeout=args.http_timeout,
    )
    return results


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify live Tabbit native tool execution is visible in admin logs.",
    )
    parser.add_argument("--server", default=DEFAULT_SERVER)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--tool-name", default=DEFAULT_TOOL_NAME)
    parser.add_argument("--prompt", default=DEFAULT_PROMPT)
    parser.add_argument("--admin-token", default=None)
    parser.add_argument("--admin-password", default=None)
    parser.add_argument("--proxy-api-key", default=None)
    parser.add_argument("--proxy-bearer", default=None)
    parser.add_argument("--http-timeout", type=int, default=30)
    parser.add_argument("--request-timeout", type=int, default=180)
    parser.add_argument("--log-timeout", type=int, default=45)
    parser.add_argument("--poll-interval", type=float, default=2.0)
    parser.add_argument("--log-page-size", type=int, default=50)
    parser.add_argument("--json", action="store_true", help="print machine-readable summary")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    results = run_live_smoke(args)
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        print("Native tool live smoke ok")
        print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (AssertionError, RuntimeError, urllib.error.URLError) as e:
        print(f"native tool live smoke failed: {e}", file=sys.stderr)
        raise SystemExit(1)
