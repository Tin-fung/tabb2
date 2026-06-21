#!/usr/bin/env python3
"""
v9 —— 签名头消融实验，搞清楚 492 校验规则

已知: 加 x-nonce/x-signature/x-timestamp/trace-id/unique-uuid 能过 492
目标: 搞清楚每个头的作用，能否随机生成，签名怎么算

关键问题:
- x-signature 是动态算的还是随机值就行？
- x-timestamp 必须是当前时间吗？
- x-nonce 必须是特定值吗？
- trace-id/unique-uuid 随机行不行？
"""
import json, uuid, asyncio, sys, time, hashlib
from pathlib import Path
import httpx

CONFIG_PATH = Path("/app/config.json")
BASE = "https://web.tabbit.ai"
SID = "6c5d70df-ff42-43db-8bf5-48b2788693d1"  # 抓包里的 session_id


def load_token():
    cfg = json.loads(CONFIG_PATH.read_text())
    parts = cfg["tokens"][0]["value"].split("|")
    return parts[0], parts[1] if len(parts) > 1 else None


JWT, NEXT_AUTH = load_token()
COOKIES = {"token": JWT, "user_id": "6c00f622-a88a-4d2f-81d2-a4fd6e890d62",
           "managed": "tab_browser", "NEXT_LOCALE": "zh"}
if NEXT_AUTH:
    COOKIES["next-auth.session-token"] = NEXT_AUTH

BASE_HEADERS = {
    "accept": "text/event-stream",
    "content-type": "application/json",
    "origin": BASE,
    "referer": f"{BASE}/session/{SID}",
    "sec-ch-ua": '"Chromium";v="148", "Tabbit";v="148", "Not/A)Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
    "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
    "x-chrome-id-consistency-request": "version=1,client_id=e7fa44387b1238ef1f6f,device_id=6231c2b4-4a85-4151-8902-a052f345ef02,sync_account_id=6c00f622-a88a-4d2f-81d2-a4fd6e890d62,signin_mode=all_accounts,signout_mode=show_confirmation",
    "x-req-ctx": "MS4xLjM5KDEwMTAxMDM5KQ==",
}

BODY = {
    "chat_session_id": SID, "message_id": None, "content": "hi",
    "selected_model": "Default", "parallel_group_id": None,
    "task_name": "chat", "agent_mode": False,
    "metadatas": {"html_content": "<p>hi</p>"}, "references": [],
    "entity": {"key": "d41d8cd98f00b204e9800998ecf8427e", "extras": {"type": "tab", "url": ""}},
}

# 抓包里的真实签名值
REAL = {
    "x-nonce": "8bea33fe7363a0d0b47f5927beb4e1c3b9260ab8ef20de51cf4b151fa41dc0eb",
    "x-signature": "2f49be32-28db-4b1b-9e65-ea39ec7fd349",
    "x-timestamp": "1782022153089",
    "trace-id": "b488806c-9ad9-4075-8f49-cab5b8d3c777",
    "unique-uuid": "6262f08a-8133-9576-4c8e-d0c1e09799e0",
}


async def try_send(client, label, extra_headers):
    h = {**BASE_HEADERS, **extra_headers}
    try:
        async with client.stream("POST", f"{BASE}/api/v1/chat/completion", json=BODY, headers=h, cookies=COOKIES, timeout=30) as resp:
            got = False; err = ""; txt = ""
            async for line in resp.aiter_lines():
                if line.startswith("data:"):
                    try:
                        d = json.loads(line[5:].strip())
                        if d.get("code") == 493: err = "493版本"
                        elif d.get("code") == 492: err = "492签名"
                        elif d.get("code"): err = f"code={d.get('code')}"
                        if d.get("content"): txt += d["content"]
                    except: pass
                if "message_chunk" in line: got = True
            mark = "🎉" if got else "  "
            result = f"✅{txt[:35]!r}" if got else (err or "空")
            print(f"  {mark} {label:40} -> {result}")
            return got
    except Exception as e:
        print(f"     {label:40} -> EXC {str(e)[:40]}")
        return False


async def main():
    async with httpx.AsyncClient(timeout=httpx.Timeout(connect=15, read=60, write=15, pool=15), follow_redirects=False, verify=False) as client:
        print("=== [1] 逐个删签名头，看哪些是必须的 ===")
        sig_keys = ["x-nonce", "x-signature", "x-timestamp", "trace-id", "unique-uuid"]
        for k in sig_keys:
            extra = {kk: vv for kk, vv in REAL.items() if kk != k}
            await try_send(client, f"删{k}", extra)

        print("\n=== [2] 逐个改签名头为随机值，看哪些能随机 ===")
        # 随机生成
        rand_nonce = hashlib.sha256(uuid.uuid4().bytes).hexdigest()
        rand_sig = str(uuid.uuid4())
        rand_ts = str(int(time.time() * 1000))
        rand_trace = str(uuid.uuid4())
        rand_uuid = str(uuid.uuid4())
        rand_map = {"x-nonce": rand_nonce, "x-signature": rand_sig, "x-timestamp": rand_ts,
                    "trace-id": rand_trace, "unique-uuid": rand_uuid}
        for k in sig_keys:
            extra = {**REAL, k: rand_map[k]}
            await try_send(client, f"改{k}=随机", extra)

        print("\n=== [3] 全部用随机值（终极测试）===")
        await try_send(client, "全随机签名头", rand_map)

        print("\n=== [4] 只留最小签名头组合 ===")
        # 试只留 trace-id + unique-uuid（可能是纯追踪，不参与校验）
        await try_send(client, "只trace+unique", {"trace-id": rand_trace, "unique-uuid": rand_uuid})
        # 试只留 x-nonce + x-timestamp + x-signature
        await try_send(client, "只nonce+ts+sig", {"x-nonce": rand_nonce, "x-timestamp": rand_ts, "x-signature": rand_sig})

        print("\n=== [5] x-timestamp 时间偏移测试（看是否校验时效）===")
        for offset_label, ts in [("当前", str(int(time.time()*1000))),
                                  ("+1小时", str(int(time.time()*1000)+3600000)),
                                  ("-1小时", str(int(time.time()*1000)-3600000)),
                                  ("抓包原值", REAL["x-timestamp"])]:
            extra = {**REAL, "x-timestamp": ts}
            await try_send(client, f"ts={offset_label}", extra)


asyncio.run(main())
