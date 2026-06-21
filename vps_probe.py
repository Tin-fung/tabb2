#!/usr/bin/env python3
"""
v7 —— 原样复刻抓包请求，定位 493 真正触发点

策略:
1. 用抓包里的完整真实头 + body 原样打 /api/v1/chat/completion
2. 如果通了 → 逐个删头，定位哪个是 493 关键
3. 如果不通 → 是 IP/账号问题，不是请求构造问题

注意: 用容器内 config.json 的真实 token 替换抓包里的占位 token
"""
import json, asyncio, sys
from pathlib import Path
import httpx

CONFIG_PATH = Path("/app/config.json")


def load_token_parts():
    cfg = json.loads(CONFIG_PATH.read_text())
    tokens = cfg.get("tokens", [])
    if not tokens:
        sys.exit("❌ config.json 里没有 token")
    parts = tokens[0]["value"].split("|")
    return parts[0], parts[1] if len(parts) > 1 else None, parts[2] if len(parts) > 2 else None


JWT, NEXT_AUTH, DEVICE_ID = load_token_parts()
# 抓包里的 device_id 是真实浏览器的，本仙女用配置里的（token 绑定的）
# 但 x-chrome-id-consistency-request 里也用配置的 device_id 保持一致


async def main():
    base = "https://web.tabbit.ai"
    # 先建一个新 session（用真实浏览器方式：直接 POST /api/v1/session 或用抓包里的 session）
    # 抓包里 session_id 是 6c5d70df-...，但那个会话可能已过期，本仙女新建一个

    async with httpx.AsyncClient(timeout=httpx.Timeout(connect=15, read=60, write=15, pool=15), follow_redirects=False, verify=False) as client:
        # 先试 /api/v0/user 看账号状态（之前返回 405）
        print("=== [0] 账号状态 ===")
        r = await client.get(f"{base}/api/v0/user", headers={
            "x-req-ctx": "MS4xLjM5KDEwMTAxMDM5KQ==",
            "x-chrome-id-consistency-request": f"version=1,client_id=e7fa44387b1238ef1f6f,device_id={DEVICE_ID},sync_account_id=6c00f622-a88a-4d2f-81d2-a4fd6e890d62,signin_mode=all_accounts,signout_mode=show_confirmation",
        }, cookies={"token": JWT, "user_id": "6c00f622-a88a-4d2f-81d2-a4fd6e890d62"})
        print(f"  /api/v0/user -> {r.status_code} {r.text[:200]}")

        # 建 session —— 试几种方式
        print("\n=== [1] 建会话 ===")
        sid = None
        # 方式A: POST /api/v1/session
        for body in [{"title": "New Chat"}, {}, {"name": "New Chat"}, {"type": "chat"}]:
            r = await client.post(f"{base}/api/v1/session", json=body, headers={
                "content-type": "application/json",
                "x-req-ctx": "MS4xLjM5KDEwMTAxMDM5KQ==",
                "x-chrome-id-consistency-request": f"version=1,client_id=e7fa44387b1238ef1f6f,device_id={DEVICE_ID},sync_account_id=6c00f622-a88a-4d2f-81d2-a4fd6e890d62,signin_mode=all_accounts,signout_mode=show_confirmation",
            }, cookies={"token": JWT, "user_id": "6c00f622-a88a-4d2f-81d2-a4fd6e890d62"})
            print(f"  POST /api/v1/session body={body} -> {r.status_code} {r.text[:150]}")
            if r.status_code in (200, 201):
                try:
                    d = r.json()
                    sid = d.get("id") or d.get("session_id") or (d.get("data") or {}).get("id")
                    if sid:
                        print(f"    ✅ 拿到 session_id={sid}")
                        break
                except: pass

        if not sid:
            # 方式B: 用抓包里的 session_id 直接试（可能已过期）
            sid = "6c5d70df-ff42-43db-8bf5-48b2788693d1"
            print(f"  用抓包里的 session_id={sid} 直接试")

        # [2] 原样复刻抓包请求 —— 完整头
        print(f"\n=== [2] 原样复刻抓包请求（完整头）===")
        headers_full = {
            "accept": "text/event-stream",
            "content-type": "application/json",
            "origin": base,
            "referer": f"{base}/session/{sid}",
            "sec-ch-ua": '"Chromium";v="148", "Tabbit";v="148", "Not/A)Brand";v="99"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"macOS"',
            "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
            "x-chrome-id-consistency-request": f"version=1,client_id=e7fa44387b1238ef1f6f,device_id={DEVICE_ID},sync_account_id=6c00f622-a88a-4d2f-81d2-a4fd6e890d62,signin_mode=all_accounts,signout_mode=show_confirmation",
            "x-req-ctx": "MS4xLjM5KDEwMTAxMDM5KQ==",
        }
        body_full = {
            "chat_session_id": sid, "message_id": None, "content": "hi",
            "selected_model": "Default", "parallel_group_id": None,
            "task_name": "chat", "agent_mode": False,
            "metadatas": {"html_content": "<p>hi</p>"}, "references": [],
            "entity": {"key": "d41d8cd98f00b204e9800998ecf8427e", "extras": {"type": "tab", "url": ""}},
        }
        cookies_full = {"token": JWT, "user_id": "6c00f622-a88a-4d2f-81d2-a4fd6e890d62", "managed": "tab_browser", "NEXT_LOCALE": "zh"}
        if NEXT_AUTH:
            cookies_full["next-auth.session-token"] = NEXT_AUTH
        res = await try_send(client, "完整头", f"{base}/api/v1/chat/completion", headers_full, body_full, cookies_full)

        if "493" in str(res):
            # [3] 逐个删头定位
            print(f"\n=== [3] 逐个删头定位 493 触发头 ===")
            for h in ["x-req-ctx", "x-chrome-id-consistency-request", "origin", "referer", "sec-ch-ua", "sec-ch-ua-platform", "user-agent"]:
                h2 = {k:v for k,v in headers_full.items() if k != h}
                await try_send(client, f"删{h}", f"{base}/api/v1/chat/completion", h2, body_full, cookies_full)

            # [4] 试加抓包里那些签名头（虽然本仙女不知道怎么算，但先试固定值看会不会变）
            print(f"\n=== [4] 试加抓包里的签名头（固定值）===")
            h3 = {**headers_full,
                  "x-nonce": "8bea33fe7363a0d0b47f5927beb4e1c3b9260ab8ef20de51cf4b151fa41dc0eb",
                  "x-signature": "2f49be32-28db-4b1b-9e65-ea39ec7fd349",
                  "x-timestamp": "1782022153089",
                  "trace-id": "b488806c-9ad9-4075-8f49-cab5b8d3c777",
                  "unique-uuid": "6262f08a-8133-9576-4c8e-d0c1e09799e0"}
            await try_send(client, "加签名头(固定值)", f"{base}/api/v1/chat/completion", h3, body_full, cookies_full)


async def try_send(client, label, url, headers, body, cookies):
    try:
        async with client.stream("POST", url, json=body, headers=headers, cookies=cookies, timeout=30) as resp:
            got = False; err = ""; txt = ""
            async for line in resp.aiter_lines():
                if line.startswith("data:"):
                    try:
                        d = json.loads(line[5:].strip())
                        if d.get("code") == 493: err = "493"
                        elif d.get("code"): err = f"code={d.get('code')}:{d.get('message','')[:50]}"
                        if d.get("content"): txt += d["content"]
                    except: pass
                if "message_chunk" in line or '"content"' in line: got = True
            result = f"✅成功:{txt[:40]!r}" if got else (err or f"status={resp.status_code}空")
            mark = "🎉" if got else "  "
            print(f"  {mark} {label:25} -> {result}")
            return result
    except Exception as e:
        print(f"     {label:25} -> EXC {str(e)[:50]}")
        return f"EXC:{e}"


asyncio.run(main())
