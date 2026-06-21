#!/usr/bin/env python3
"""
VPS 端深度探测 v2 —— 493 根因定位

策略:
1. dump 完整 493 error 数据 + 响应头，看上游透露什么
2. 试改 x-chrome-id-consistency-request 的 version= 字段
3. 试探账户/额度相关接口，看是不是账号维度被拒
4. 对比 create_chat_session(成功) vs send_message(493) 的差异

用法: docker exec tabbit2api python /app/vps_probe.py
"""
import re, json, uuid, hashlib, base64, urllib.parse, asyncio, sys
from pathlib import Path
import httpx

BASE_URL = "https://web.tabbitbrowser.com"
CLIENT_ID = "e7fa44387b1238ef1f6f"
CONFIG_PATH = Path("/app/config.json")

# 全局
JWT = NEXT_AUTH = DEVICE_ID = USER_ID = None


def load_token():
    cfg = json.loads(CONFIG_PATH.read_text())
    tokens = cfg.get("tokens", [])
    if not tokens:
        sys.exit("❌ config.json 里没有 token")
    t = tokens[0]
    print(f"[token] name={t.get('name')} status={t.get('status')} error_count={t.get('error_count')}")
    return t["value"]


def parse_token(token_str):
    global JWT, NEXT_AUTH, DEVICE_ID, USER_ID
    parts = token_str.split("|")
    JWT = parts[0]
    NEXT_AUTH = parts[1] if len(parts) > 1 else None
    DEVICE_ID = parts[2] if len(parts) > 2 else str(uuid.uuid4())
    try:
        payload = json.loads(base64.urlsafe_b64decode(JWT.split(".")[1] + "=="))
        USER_ID = payload.get("id", payload.get("sub", str(uuid.uuid4())))
        print(f"[JWT] user_id={USER_ID}  exp={payload.get('exp')}  scope={payload.get('scope')}  azp={payload.get('azp')}")
    except Exception as e:
        USER_ID = str(uuid.uuid4())
        print(f"[JWT] 解码失败: {e}")
    print(f"[token] device_id={DEVICE_ID}  next_auth={'有' if NEXT_AUTH else '无'}")


def base_headers(referer="/newtab"):
    return {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
        "sec-ch-ua": '"Not:A-Brand";v="99", "Tabbit";v="145", "Chromium";v="145"',
        "sec-ch-ua-platform": '"Windows"',
        "x-chrome-id-consistency-request": (
            f"version=1,client_id={CLIENT_ID},device_id={DEVICE_ID},"
            f"sync_account_id={USER_ID},signin_mode=all_accounts,signout_mode=show_confirmation"
        ),
        "referer": f"{BASE_URL}{referer}",
    }


def cookies():
    c = {"token": JWT, "user_id": USER_ID, "managed": "tab_browser", "NEXT_LOCALE": "zh"}
    if NEXT_AUTH:
        c["next-auth.session-token"] = NEXT_AUTH
    return c


async def dump_493_detail(client):
    """建会话后打 send_message，把 493 的完整 error 数据 dump 出来"""
    print("\n" + "="*60)
    print("[1] dump 493 完整 error 数据 + 响应头")
    print("="*60)
    router_state = ["", {"children": ["chat", {"children": [["id","new","d"], {"children": ["__PAGE__",{},None,"refetch"]}, None, None]}, None, None]}, None, None]
    h = {**base_headers("/chat/new"), "rsc": "1", "next-router-state-tree": urllib.parse.quote(json.dumps(router_state))}
    r = await client.get(f"{BASE_URL}/chat/new", params={"_rsc":"auto"}, headers=h, cookies=cookies())
    m = re.search(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", r.text)
    if not m:
        print(f"  NO_SESSION  status={r.status_code}")
        return None
    sid = m.group(1)
    print(f"  session_id={sid}  (create 成功)")

    content = "hi"
    payload = {
        "chat_session_id": sid, "content": content, "selected_model": "最佳",
        "agent_mode": False, "metadatas": {"html_content": f"<p>{content}</p>"},
        "entity": {"key": hashlib.md5(b"").hexdigest(), "extras": {"type":"tab","url":""}},
    }
    sh = {**base_headers(f"/chat/{sid}"), "Accept":"text/event-stream", "Content-Type":"application/json"}
    async with client.stream("POST", f"{BASE_URL}/chat/send", json=payload, headers=sh, cookies=cookies()) as resp:
        print(f"\n  send_message 响应:")
        print(f"    status={resp.status_code}")
        print(f"    响应头:")
        for k, v in resp.headers.items():
            print(f"      {k}: {v}")
        print(f"    SSE 流内容:")
        async for line in resp.aiter_lines():
            if line:
                print(f"      | {line}")
    return sid


async def try_consistency_versions(client):
    """试改 x-chrome-id-consistency-request 里的 version= 字段"""
    print("\n" + "="*60)
    print("[2] 试 x-chrome-id-consistency-request 的 version= 字段")
    print("="*60)
    # 建一个会话复用
    router_state = ["", {"children": ["chat", {"children": [["id","new","d"], {"children": ["__PAGE__",{},None,"refetch"]}, None, None]}, None, None]}, None, None]
    h = {**base_headers("/chat/new"), "rsc": "1", "next-router-state-tree": urllib.parse.quote(json.dumps(router_state))}
    r = await client.get(f"{BASE_URL}/chat/new", params={"_rsc":"auto"}, headers=h, cookies=cookies())
    m = re.search(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", r.text)
    if not m:
        print("  NO_SESSION，跳过")
        return
    sid = m.group(1)

    # 试不同的 version= 值
    for ver in ["1.1.39", "1.1", "0.33.13", "2", "1.0.0", "10101039"]:
        headers = base_headers(f"/chat/{sid}")
        headers["x-chrome-id-consistency-request"] = (
            f"version={ver},client_id={CLIENT_ID},device_id={DEVICE_ID},"
            f"sync_account_id={USER_ID},signin_mode=all_accounts,signout_mode=show_confirmation"
        )
        headers["Accept"] = "text/event-stream"
        headers["Content-Type"] = "application/json"
        content = "hi"
        payload = {"chat_session_id": sid, "content": content, "selected_model": "最佳",
                   "agent_mode": False, "metadatas": {"html_content": f"<p>{content}</p>"},
                   "entity": {"key": hashlib.md5(b"").hexdigest(), "extras": {"type":"tab","url":""}}}
        try:
            async with client.stream("POST", f"{BASE_URL}/chat/send", json=payload, headers=headers, cookies=cookies()) as resp:
                got = False
                err = ""
                async for line in resp.aiter_lines():
                    if line.startswith("data:"):
                        try:
                            d = json.loads(line[5:].strip())
                            if d.get("code") == 493:
                                err = "493"
                            elif d.get("code"):
                                err = f"code={d.get('code')}"
                        except: pass
                    if "message_chunk" in line:
                        got = True
                mark = "🎉" if got else "  "
                print(f"  {mark} consistency version={ver:10} -> {'✅成功' if got else (err or '空')}")
                if got:
                    return ver
        except Exception as e:
            print(f"     version={ver} EXC: {str(e)[:50]}")
    return None


async def probe_account(client):
    """探账户状态/额度，看是不是账号维度被拒"""
    print("\n" + "="*60)
    print("[3] 探账户/额度相关接口")
    print("="*60)
    # 试一批可能的账户信息接口
    paths = [
        "/api/v0/user/info", "/api/v0/user", "/api/v0/user/profile",
        "/api/v0/user/me", "/api/v0/account", "/api/v0/me",
        "/api/v0/user/quota", "/api/v0/user/usage", "/api/v0/quota",
        "/api/v0/user/permission", "/api/v0/user/ai",
        "/api/auth/session", "/api/v0/models", "/api/v0/chat/models",
        "/api/v0/feature-flags", "/api/v0/abtest",
    ]
    for p in paths:
        try:
            r = await client.get(f"{BASE_URL}{p}", headers=base_headers("/newtab"), cookies=cookies(), timeout=15)
            body = r.text[:300] if r.status_code != 404 else ""
            if r.status_code != 404:
                print(f"  {p:35} -> {r.status_code}  {body}")
        except Exception as e:
            print(f"  {p:35} -> EXC {str(e)[:40]}")


async def main():
    token_str = load_token()
    parse_token(token_str)

    async with httpx.AsyncClient(timeout=httpx.Timeout(connect=15, read=60, write=15, pool=15), follow_redirects=False, verify=False) as client:
        await dump_493_detail(client)
        await try_consistency_versions(client)
        await probe_account(client)

        print("\n" + "="*60)
        print("诊断完成。把以上完整输出贴给本仙女。")
        print("="*60)


if __name__ == "__main__":
    asyncio.run(main())
