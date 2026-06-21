#!/usr/bin/env python3
"""
精确探测上游 content 长度阈值（二分法）

用真实风格的 system prompt 填充（比纯 "a" 更接近 Claude Code 实际请求），
二分法快速锁定最大可用长度。

用法: docker exec tabbit2api python /app/_test_len.py
"""
import json, asyncio, sys, re, base64, uuid, urllib.parse
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
X_REQ_CTX = base64.b64encode(b"1.1.39(10101039)").decode()
COOKIES = {"token": JWT, "user_id": USER_ID, "managed": "tab_browser", "NEXT_LOCALE": "zh"}
if NEXT_AUTH:
    COOKIES["next-auth.session-token"] = NEXT_AUTH

# 真实风格 system prompt 片段（模拟 Claude Code 的行为规范文本）
PROMPT_SAMPLE = """You are Claude Code, Anthropic's official CLI for Claude. You are an interactive CLI tool that helps users with software engineering tasks. Use the instructions below and the tools available to you to assist the user. When you need to use a tool, you must strictly follow the format. Always be thorough and systematic. If you encounter unexpected behavior, investigate the root cause before proposing fixes. Prefer minimal changes. Read the full context before editing. Verify your changes when possible. """


def make_content(length: int) -> str:
    """构造指定长度的真实风格 content"""
    # 系统提示 + 用户消息，用真实文本循环填充到目标长度
    prefix = "[System]: " + PROMPT_SAMPLE + "\n\n[User]: "
    if length <= len(prefix) + 20:
        return prefix + "hi"[:max(1, length - len(prefix))]
    # 循环填充真实文本到目标长度
    filler = (PROMPT_SAMPLE + " ") * (length // len(PROMPT_SAMPLE) + 1)
    return (prefix + filler)[:length]


def headers(referer):
    return {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
        "x-chrome-id-consistency-request": f"version=1,client_id=e7fa44387b1238ef1f6f,device_id={DEVICE_ID},sync_account_id={USER_ID},signin_mode=all_accounts,signout_mode=show_confirmation",
        "x-req-ctx": X_REQ_CTX,
        "unique-uuid": str(uuid.uuid4()),
        "accept": "text/event-stream",
        "content-type": "application/json",
        "referer": referer,
    }


async def make_session(client):
    router_state = ["",{"children":["chat",{"children":[["id","new","d"],{"children":["__PAGE__",{},None,"refetch"]},None,None]},None,None]},None,None]
    h = {**headers(f"{BASE}/chat/new"), "rsc": "1",
         "next-router-state-tree": urllib.parse.quote(json.dumps(router_state))}
    r = await client.get(f"{BASE}/chat/new", params={"_rsc": "auto"}, headers=h, cookies=COOKIES)
    m = re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", r.text)
    return m.group(0) if m else None


async def try_len(client, length) -> bool:
    """返回 True=成功，False=492/失败"""
    sid = await make_session(client)
    if not sid:
        return False
    content = make_content(length)
    payload = {
        "chat_session_id": sid, "message_id": None, "content": content,
        "selected_model": "Default", "parallel_group_id": None,
        "task_name": "chat", "agent_mode": False,
        "metadatas": {"html_content": "<p>hi</p>"}, "references": [],
        "entity": {"key": "d41d8cd98f00b204e9800998ecf8427e", "extras": {"type": "tab", "url": ""}},
    }
    h = headers(f"{BASE}/session/{sid}")
    try:
        async with client.stream("POST", f"{BASE}/api/v1/chat/completion", json=payload, headers=h, cookies=COOKIES, timeout=60) as resp:
            async for line in resp.aiter_lines():
                if line.startswith("data:"):
                    try:
                        d = json.loads(line[5:].strip())
                        if d.get("code") in (492, 493):
                            return False
                        if d.get("content"):
                            return True
                    except:
                        pass
            return False
    except Exception:
        return False


async def main():
    async with httpx.AsyncClient(timeout=httpx.Timeout(connect=15, read=60, write=15, pool=15), follow_redirects=True, verify=False) as client:
        # 已知: 10000 ✅, 50000 ❌，二分找最大
        lo, hi = 10000, 50000
        print(f"=== 二分探测精确阈值（已知 {lo}✅ {hi}❌）===")
        best = lo
        while lo + 1000 < hi:
            mid = (lo + hi) // 2
            ok = await try_len(client, mid)
            mark = "✅" if ok else "❌"
            print(f"  {mark} content_len={mid}")
            if ok:
                best = mid
                lo = mid
            else:
                hi = mid
        # 再精细测 best 附近（步长 500）
        print(f"\n=== 精细测（步长 500，从 {best} 往上）===")
        for length in range(best, best + 5000, 500):
            ok = await try_len(client, length)
            mark = "✅" if ok else "❌"
            print(f"  {mark} content_len={length}")
            if ok:
                best = length
            else:
                break
        print(f"\n🎯 最大可用 content_len = {best}")
        print(f"   建议设阈值 = {int(best * 0.9)}（留 10% 余量）")


asyncio.run(main())
