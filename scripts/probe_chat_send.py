"""探测 /chat/send 接口（agent 模式）的 content 长度限制。

对比 /api/v1/chat/completion 的 20421 边界，看 agent 接口是否绕过网关限制。
"""
import argparse
import asyncio
import json
import os
import sys
import logging

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.tabbit_client import TabbitClient  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("probe")

MAX_CONTENT_LEN = 18450  # 旧接口安全阈值
SAFE_FACTOR = 0.9


def make_padding(target_len: int) -> str:
    return "1+1=" + "x" * max(0, target_len - 4)


async def try_chat_send(client: TabbitClient, session_id: str, content: str) -> tuple[bool, str]:
    """打 /chat/send，返回 (是否通过, 备注)"""
    payload = {
        "chat_session_id": session_id,
        "message_id": None,
        "content": content,
        "selected_model": "Default",
        "parallel_group_id": None,
        "task_name": "chat",
        "agent_mode": True,  # ← 关键：agent 模式
        "metadatas": {"html_content": f"<p>{content}</p>"},
        "references": [],
        "entity": {"key": "d41d8cd98f00b204e9800998ecf8427e", "extras": {"type": "tab", "url": ""}},
    }
    headers = {
        **client._get_headers(f"/session/{session_id}", with_uuid=True),
        "accept": "text/event-stream",
        "Content-Type": "application/json",
    }
    try:
        resp = await client.client.post(
            f"{client.base_url}/chat/send",
            json=payload, headers=headers, cookies=client._get_cookies(),
        )
        body = resp.text
        # 把完整响应存文件供分析（每次覆盖，文件名带长度）
        with open(f"/tmp/chatsend_{len(content)}.txt", "w") as f:
            f.write(body)
        if resp.status_code == 200:
            # 判断：ready=正常开始, error=有问题但 HTTP 放行
            has_ready = "event: ready" in body or "event: message_start" in body
            has_error = "event: error" in body
            has_492 = '"code":492' in body or "492" in body[:200]
            if has_492:
                return False, f"492 in body"
            if has_ready and not has_error:
                return True, f"✓正常生成 (ready)"
            if has_ready and has_error:
                return True, f"⚠ready+error并存"
            if has_error:
                # 提取 error message
                import re
                m = re.search(r'"message"\s*:\s*"([^"]{0,60})', body)
                msg = m.group(1) if m else "?"
                return True, f"⚠HTTP放行但event:error | msg={msg}"
            return True, f"200 ok, body_head={body[:80]!r}"
        else:
            return False, f"http {resp.status_code}, body={body[:120]!r}"
    except Exception as e:
        return False, f"exception: {e}"


async def probe(client: TabbitClient, max_len: int):
    logger.info("▶ 探测 /chat/send (agent_mode) 边界，上限 %d", max_len)
    session_id = await client.create_chat_session()
    logger.info("  会话 %s", session_id[:8])

    # 先试几个关键点：18450(旧阈值) / 50000 / 100000 / 200000
    test_points = [18450, 50000, 100000, 200000, max_len]
    test_points = [t for t in test_points if t <= max_len]
    # 去重保序
    seen = set()
    test_points = [t for t in test_points if not (t in seen or seen.add(t))]

    results = []
    for tp in test_points:
        content = make_padding(tp)
        ok, note = await try_chat_send(client, session_id, content)
        mark = "✓通过" if ok else "✗被拒"
        logger.info("  %s %d chars → %s | %s", mark, tp, mark, note[:60])
        results.append((tp, ok, note))
        if not ok and "492" in note:
            logger.info("  撞 492，停止往上试")
            break
        await asyncio.sleep(1)

    print("\n" + "=" * 60)
    print("/chat/send (agent 模式) 探测结果")
    print("=" * 60)
    print(f"{'长度':<10} {'结果':<8} 备注")
    print("-" * 60)
    for tp, ok, note in results:
        print(f"{tp:<10} {'✓' if ok else '✗':<8} {note[:50]}")
    print("=" * 60)

    # 找最大通过长度
    passed = [tp for tp, ok, _ in results if ok]
    if passed:
        max_pass = max(passed)
        logger.info("最大通过长度: %d (safe=%d)", max_pass, int(max_pass * SAFE_FACTOR))
    else:
        logger.info("全部被拒，/chat/send 也有严格限制")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="data/config.json")
    ap.add_argument("--max", type=int, default=200000)
    args = ap.parse_args()

    with open(args.config) as f:
        cfg = json.load(f)
    tokens = cfg.get("tokens", [])
    if not tokens:
        logger.error("无 token"); sys.exit(1)
    first = tokens[0]
    token = first["value"] if isinstance(first, dict) else first

    client = TabbitClient(
        token,
        base_url=cfg.get("tabbit", {}).get("base_url", "https://web.tabbit.ai"),
        browser_version=cfg.get("tabbit", {}).get("browser_version"),
        sparkle_version=cfg.get("tabbit", {}).get("sparkle_version"),
        default_browser=True,
    )
    await probe(client, args.max)
    await client.client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
