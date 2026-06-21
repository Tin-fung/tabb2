#!/usr/bin/env python3
"""v11 —— 诊断 adapter 实际配置 + 实际发出的请求头"""
import json, sys
from pathlib import Path
import httpx

CONFIG_PATH = Path("/app/config.json")

print("=== [1] 容器内实际 config.json 的 tabbit 段 ===")
cfg = json.loads(CONFIG_PATH.read_text())
print(json.dumps(cfg.get("tabbit", {}), ensure_ascii=False, indent=2))

print("\n=== [2] 用 adapter 的 TabbitClient 实际发一次请求，dump 头 ===")
sys.path.insert(0, "/app")
from core.tabbit_client import TabbitClient

token = cfg["tokens"][0]["value"]
tabbit_cfg = cfg.get("tabbit", {})
client = TabbitClient(
    token,
    tabbit_cfg.get("base_url"),
    tabbit_cfg.get("client_id"),
    tabbit_cfg.get("browser_version"),
    tabbit_cfg.get("sparkle_version"),
)

print(f"  client.base_url = {client.base_url}")
print(f"  client.browser_version = {client.browser_version}")
print(f"  client.sparkle_version = {client.sparkle_version}")
print(f"  client.device_id = {client.device_id}")
print(f"\n  _get_headers() 实际返回:")
for k, v in client._get_headers("/session/test").items():
    print(f"    {k}: {v[:80]}")

print(f"\n=== [3] 用 adapter 的 client 建会话 + 发消息 ===")
import asyncio

async def run():
    try:
        sid = await client.create_chat_session()
        print(f"  session_id = {sid}")
    except Exception as e:
        print(f"  建会话失败: {e}")
        return

    # 直接复刻 v10 的发消息逻辑
    import hashlib, uuid
    payload = {
        "chat_session_id": sid, "message_id": None, "content": "hi",
        "selected_model": "Default", "parallel_group_id": None,
        "task_name": "chat", "agent_mode": False,
        "metadatas": {"html_content": "<p>hi</p>"}, "references": [],
        "entity": {"key": hashlib.md5(b"").hexdigest(), "extras": {"type": "tab", "url": ""}},
    }
    headers = {**client._get_headers(f"/session/{sid}"), "accept": "text/event-stream", "content-type": "application/json"}
    print(f"\n  发送 POST {client.base_url}/api/v1/chat/completion")
    print(f"  请求头:")
    for k, v in headers.items():
        print(f"    {k}: {v[:80]}")
    try:
        async with client.client.stream("POST", f"{client.base_url}/api/v1/chat/completion", json=payload, headers=headers, cookies=client._get_cookies(), timeout=60) as resp:
            print(f"\n  响应 status={resp.status_code}")
            async for line in resp.aiter_lines():
                if line:
                    print(f"  | {line[:150]}")
    except Exception as e:
        print(f"  EXC: {e}")

asyncio.run(run())
