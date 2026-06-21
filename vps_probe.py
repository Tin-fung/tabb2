#!/usr/bin/env python3
"""
VPS 端版本号探测脚本 —— 在部署环境直接跑，找能通过上游校验的 browser_version

用法（在 VPS 上、容器内执行，网络环境最真实）:
  docker exec tabbit2api python /app/vps_probe.py

会自动:
1. 从容器内 config.json 读 token
2. 试多个候选版本号（含从官方升级 API 动态拿的）
3. 对每个版本号打真实 /chat/send，报告结果
4. 找到能通的版本号直接告诉你
"""
import re, json, uuid, hashlib, base64, urllib.parse, asyncio, sys
from pathlib import Path
import httpx

BASE_URL = "https://web.tabbitbrowser.com"
CLIENT_ID = "e7fa44387b1238ef1f6f"
CONFIG_PATH = Path("/app/config.json")


def load_token():
    cfg = json.loads(CONFIG_PATH.read_text())
    tokens = cfg.get("tokens", [])
    if not tokens:
        sys.exit("❌ config.json 里没有 token")
    t = tokens[0]
    print(f"[token] name={t.get('name')} status={t.get('status')}")
    return t["value"]


def parse_token(token_str):
    parts = token_str.split("|")
    jwt = parts[0]
    next_auth = parts[1] if len(parts) > 1 else None
    device_id = parts[2] if len(parts) > 2 else str(uuid.uuid4())
    try:
        payload = json.loads(base64.urlsafe_b64decode(jwt.split(".")[1] + "=="))
        user_id = payload.get("id", payload.get("sub", str(uuid.uuid4())))
    except Exception:
        user_id = str(uuid.uuid4())
    return jwt, next_auth, device_id, user_id


def make_headers(v, platform="Windows"):
    ua_os = {"Windows": "Windows NT 10.0; Win64; x64",
             "macOS": "Macintosh; Intel Mac OS X 10_15_7"}[platform]
    return {
        "User-Agent": f"Mozilla/5.0 ({ua_os}) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{v}.0.0.0 Safari/537.36",
        "sec-ch-ua": f'"Not:A-Brand";v="99", "Tabbit";v="{v}", "Chromium";v="{v}"',
        "sec-ch-ua-platform": f'"{platform}"',
        "x-chrome-id-consistency-request": (
            f"version=1,client_id={CLIENT_ID},device_id={DEVICE_ID},"
            f"sync_account_id={USER_ID},signin_mode=all_accounts,signout_mode=show_confirmation"
        ),
        "referer": f"{BASE_URL}/newtab",
    }


def make_cookies():
    c = {"token": JWT, "user_id": USER_ID, "managed": "tab_browser", "NEXT_LOCALE": "zh"}
    if NEXT_AUTH:
        c["next-auth.session-token"] = NEXT_AUTH
    return c


async def fetch_latest_versions(client):
    """从官方升级 API 拿最新版本号"""
    try:
        r = await client.get(f"{BASE_URL}/api/v0/upgrade/latest", params={"from": "website"}, timeout=15)
        data = r.json()
        vers = {}
        for item in data.get("versions", []):
            pf = item.get("platform")
            sv = item.get("short_version_string")
            if pf and sv and pf not in vers:
                vers[pf] = sv
        print(f"[官方升级API] 最新版本: {vers}")
        return vers
    except Exception as e:
        print(f"[官方升级API] 拉取失败: {e}")
        return {}


async def create_session(client, v, platform):
    router_state = ["", {"children": ["chat", {"children": [["id","new","d"], {"children": ["__PAGE__",{},None,"refetch"]}, None, None]}, None, None]}, None, None]
    h = {**make_headers(v, platform), "rsc": "1",
         "next-router-state-tree": urllib.parse.quote(json.dumps(router_state))}
    r = await client.get(f"{BASE_URL}/chat/new", params={"_rsc":"auto"}, headers=h, cookies=make_cookies())
    # 兼容多种 uuid 格式
    m = re.search(r"/chat/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", r.text)
    if not m:
        m = re.search(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", r.text)
    return m.group(1) if m else None


async def try_send(client, v, platform):
    sid = await create_session(client, v, platform)
    if not sid:
        return "NO_SESSION"
    content = "hi"
    payload = {
        "chat_session_id": sid, "content": content, "selected_model": "最佳",
        "agent_mode": False, "metadatas": {"html_content": f"<p>{content}</p>"},
        "entity": {"key": hashlib.md5(b"").hexdigest(), "extras": {"type":"tab","url":""}},
    }
    sh = {**make_headers(v, platform), "Accept":"text/event-stream", "Content-Type":"application/json"}
    try:
        async with client.stream("POST", f"{BASE_URL}/chat/send", json=payload, headers=sh, cookies=make_cookies()) as resp:
            got_chunk = False
            err = ""
            async for line in resp.aiter_lines():
                if line.startswith("data:"):
                    try:
                        d = json.loads(line[5:].strip())
                        if d.get("code") == 493 or "版本" in d.get("message",""):
                            err = "493版本校验失败"
                        elif d.get("code"):
                            err = f"err{d.get('code')}:{d.get('message','')[:50]}"
                    except: pass
                if "message_chunk" in line:
                    got_chunk = True
            return "✅成功有内容" if got_chunk else (err or "空响应")
    except Exception as e:
        return f"EXC:{str(e)[:50]}"


async def main():
    global JWT, NEXT_AUTH, DEVICE_ID, USER_ID
    token_str = load_token()
    JWT, NEXT_AUTH, DEVICE_ID, USER_ID = parse_token(token_str)

    async with httpx.AsyncClient(timeout=httpx.Timeout(connect=15, read=60, write=15, pool=15), follow_redirects=False, verify=False) as client:
        latest = await fetch_latest_versions(client)

        # 候选版本号：官方API给的 + 几个猜测
        candidates = []
        if latest.get("windows"):
            candidates.append((latest["windows"], "Windows"))
        if latest.get("mac"):
            candidates.append((latest["mac"], "macOS"))
        for v in ["1.1.39", "1.1", "1.0", "0.33.13", "145"]:
            for p in ["Windows", "macOS"]:
                if (v, p) not in candidates:
                    candidates.append((v, p))

        print(f"\n=== 开始试 {len(candidates)} 个组合 ===")
        winner = None
        for v, p in candidates:
            res = await try_send(client, v, p)
            mark = "🎉" if "成功" in res else "  "
            print(f"  {mark} version={v:10} platform={p:8} -> {res}")
            if "成功" in res and not winner:
                winner = (v, p)

        print("\n" + "="*50)
        if winner:
            print(f"🎉 找到可用组合！browser_version={winner[0]}  platform={winner[1]}")
            print(f"   进 admin 面板 → 设置 → 浏览器版本号填: {winner[0]}")
        else:
            print("😞 所有组合都失败了。可能根因不是版本号，需要抓真实浏览器请求头对比。")
            print("   把本脚本输出贴给本仙女，换思路。")


if __name__ == "__main__":
    asyncio.run(main())
