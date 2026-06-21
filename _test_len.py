#!/usr/bin/env python3
"""快速测试 content 长度阈值，定位 492 触发的字符数"""
import json, asyncio, sys
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
import base64, uuid
x_req_ctx = base64.b64encode(b"1.1.39(10101039)").decode()
COOKIES = {"token": JWT, "user_id": USER_ID, "managed": "tab_browser", "NEXT_LOCALE": "zh"}
if NEXT_AUTH: COOKIES["next-auth.session-token"] = NEXT_AUTH


async def make_session(client):
    import urllib.parse, re
    router_state = ["",{"children":["chat",{"children":[["id","new","d"],{"children":["__PAGE__",{},None,"refetch"]},None,None]},None,None]},None,None]
    h = {"User-Agent":"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
         "x-chrome-id-consistency-request": f"version=1,client_id=e7fa44387b1238ef1f6f,device_id={DEVICE_ID},sync_account_id={USER_ID},signin_mode=all_accounts,signout_mode=show_confirmation",
         "x-req-ctx": x_req_ctx, "unique-uuid": str(uuid.uuid4()),
         "rsc":"1","next-router-state-tree": urllib.parse.quote(json.dumps(router_state)),
         "referer": f"{BASE}/chat/new"}
    r = await client.get(f"{BASE}/chat/new", params={"_rsc":"auto"}, headers=h, cookies=COOKIES)
    m = re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", r.text)
    return m.group(0) if m else None


async def try_len(client, length):
    sid = await make_session(client)
    if not sid: return "NO_SESSION"
    # 构造指定长度的 content（重复字符）
    content = "请回复ok。" + ("a" * length)
    payload = {"chat_session_id": sid, "message_id": None, "content": content,
               "selected_model": "Default", "parallel_group_id": None,
               "task_name": "chat", "agent_mode": False,
               "metadatas": {"html_content": f"<p>{content[:100]}</p>"}, "references": [],
               "entity": {"key": "d41d8cd98f00b204e9800998ecf8427e", "extras": {"type": "tab", "url": ""}}}
    h = {"User-Agent":"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
         "x-chrome-id-consistency-request": f"version=1,client_id=e7fa44387b1238ef1f6f,device_id={DEVICE_ID},sync_account_id={USER_ID},signin_mode=all_accounts,signout_mode=show_confirmation",
         "x-req-ctx": x_req_ctx, "unique-uuid": str(uuid.uuid4()),
         "accept":"text/event-stream","content-type":"application/json",
         "referer": f"{BASE}/session/{sid}"}
    try:
        async with client.stream("POST", f"{BASE}/api/v1/chat/completion", json=payload, headers=h, cookies=COOKIES, timeout=60) as resp:
            got = False; err = ""
            async for line in resp.aiter_lines():
                if line.startswith("data:"):
                    try:
                        d = json.loads(line[5:].strip())
                        if d.get("code") == 492: err = "492"
                        elif d.get("code") == 493: err = "493"
                        elif d.get("code"): err = f"code={d.get('code')}"
                        if d.get("content"): got = True
                    except: pass
            return "✅成功" if got else (err or "空")
    except Exception as e:
        return f"EXC:{str(e)[:30]}"


async def main():
    async with httpx.AsyncClient(timeout=httpx.Timeout(connect=15, read=60, write=15, pool=15), follow_redirects=True, verify=False) as client:
        # 二分法找阈值
        for length in [1000, 10000, 50000, 100000, 130000, 140000, 145000, 149000, 149221]:
            r = await try_len(client, length)
            print(f"  content_len={length:7d} -> {r}")


asyncio.run(main())
