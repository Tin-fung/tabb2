#!/usr/bin/env python3
"""
v10 —— 端到端验证：建 session + 发消息 + unique-uuid，确认完整链路通

已知:
- x-req-ctx 头破 493
- unique-uuid 头破 492
- /api/v1/chat/completion 是真实 endpoint

待验证:
- 怎么建新 session（试 /chat/new RSC + /api/v1/session 各种变体）
- 建完 session 后用新 session_id 发消息能否成功
"""
import re, json, uuid, hashlib, base64, urllib.parse, asyncio, sys
from pathlib import Path
import httpx

CONFIG_PATH = Path("/app/config.json")
BASE = "https://web.tabbit.ai"


def load_token():
    cfg = json.loads(CONFIG_PATH.read_text())
    parts = cfg["tokens"][0]["value"].split("|")
    return parts[0], parts[1] if len(parts) > 1 else None, parts[2] if len(parts) > 2 else None


JWT, NEXT_AUTH, DEVICE_ID = load_token()
USER_ID = "6c00f622-a88a-4d2f-81d2-a4fd6e890d62"
COOKIES = {"token": JWT, "user_id": USER_ID, "managed": "tab_browser", "NEXT_LOCALE": "zh"}
if NEXT_AUTH:
    COOKIES["next-auth.session-token"] = NEXT_AUTH


def headers(referer="/newtab"):
    x_req_ctx = base64.b64encode(f"1.1.39(10101039)".encode()).decode()
    return {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
        "sec-ch-ua": '"Chromium";v="148", "Tabbit";v="148", "Not/A)Brand";v="99"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"macOS"',
        "x-chrome-id-consistency-request": f"version=1,client_id=e7fa44387b1238ef1f6f,device_id={DEVICE_ID},sync_account_id={USER_ID},signin_mode=all_accounts,signout_mode=show_confirmation",
        "x-req-ctx": x_req_ctx,
        "unique-uuid": str(uuid.uuid4()),
        "origin": BASE,
        "referer": f"{BASE}{referer}",
    }


async def try_create_session(client):
    """试各种方式建 session"""
    print("=== [1] 建会话（试各种方式）===")

    # 方式A: 旧 RSC /chat/new（在新域名上）
    print("\n--- A: GET /chat/new RSC ---")
    router_state = ["", {"children": ["chat", {"children": [["id","new","d"], {"children": ["__PAGE__",{},None,"refetch"]}, None, None]}, None, None]}, None, None]
    h = {**headers("/chat/new"), "rsc": "1", "next-router-state-tree": urllib.parse.quote(json.dumps(router_state))}
    r = await client.get(f"{BASE}/chat/new", params={"_rsc":"auto"}, headers=h, cookies=COOKIES, timeout=20, follow_redirects=True)
    print(f"  status={r.status_code} final_url={r.url} len={len(r.text)}")
    uuids = re.findall(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", r.text)
    if uuids:
        print(f"  找到 UUID: {uuids[:3]}")
        return uuids[0]
    print(f"  前 300 字: {r.text[:300]}")

    # 方式B: 试一批 POST endpoint
    print("\n--- B: POST 各种 session endpoint ---")
    for ep in ["/api/v1/session", "/api/v1/session/new", "/api/v1/session/create",
               "/api/v1/chat/session", "/api/v1/sessions", "/api/v0/session",
               "/api/v1/chat/new", "/api/v1/chat"]:
        for body in [{"title":"New Chat"}, {}, None]:
            try:
                h2 = {**headers("/newtab"), "content-type": "application/json"}
                r = await client.post(f"{BASE}{ep}", json=body, headers=h2, cookies=COOKIES, timeout=15)
                if r.status_code != 404:
                    print(f"  POST {ep} body={body} -> {r.status_code} {r.text[:120]}")
                    if r.status_code in (200, 201):
                        try:
                            d = r.json()
                            sid = d.get("id") or d.get("session_id") or (d.get("data") or {}).get("id") if isinstance(d.get("data"),dict) else None
                            if sid:
                                print(f"    ✅ session_id={sid}")
                                return sid
                        except: pass
            except Exception as e:
                print(f"  POST {ep} -> EXC {str(e)[:40]}")
            if r.status_code == 404:
                break  # 这个 endpoint 不存在，不用试不同 body

    return None


async def send_and_verify(client, sid):
    """用给定 session_id 发消息，验证完整链路"""
    print(f"\n=== [2] 用 session_id={sid} 发消息 ===")
    body = {
        "chat_session_id": sid, "message_id": None, "content": "用一句话说你好",
        "selected_model": "Default", "parallel_group_id": None,
        "task_name": "chat", "agent_mode": False,
        "metadatas": {"html_content": "<p>用一句话说你好</p>"}, "references": [],
        "entity": {"key": "d41d8cd98f00b204e9800998ecf8427e", "extras": {"type": "tab", "url": ""}},
    }
    h = {**headers(f"/session/{sid}"), "accept": "text/event-stream", "content-type": "application/json"}
    try:
        async with client.stream("POST", f"{BASE}/api/v1/chat/completion", json=body, headers=h, cookies=COOKIES, timeout=60) as resp:
            print(f"  status={resp.status_code}")
            full = ""
            async for line in resp.aiter_lines():
                if line:
                    print(f"  | {line[:150]}")
                    if line.startswith("data:"):
                        try:
                            d = json.loads(line[5:].strip())
                            if d.get("content"): full += d["content"]
                        except: pass
            if full:
                print(f"\n  🎉 完整回复: {full}")
                return True
            return False
    except Exception as e:
        print(f"  EXC: {e}")
        return False


async def main():
    async with httpx.AsyncClient(timeout=httpx.Timeout(connect=15, read=60, write=15, pool=15), follow_redirects=False, verify=False) as client:
        sid = await try_create_session(client)
        if sid:
            await send_and_verify(client, sid)
        else:
            print("\n😞 建会话失败。但已知抓包里的 session_id 能用，可能需要先在真实浏览器建一次会话。")
            # 直接用抓包里的 session_id 测发消息（验证 unique-uuid 修复有效）
            print("\n=== 用抓包里的 session_id 验证发消息 ===")
            await send_and_verify(client, "6c5d70df-ff42-43db-8bf5-48b2788693d1")


asyncio.run(main())
