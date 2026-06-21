"""决定性验证：模型是否真的读到了 references/html_content 里的超长内容。

在 8万字符深处埋独特暗号，问模型暗号是什么。
答对 = 真突破，模型读到了超长内容。
答错/不知道 = 假通过，上游收了但没喂模型。
"""
import argparse
import asyncio
import json
import os
import sys
import logging
import hashlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.tabbit_client import TabbitClient  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("verify")

# 独特暗号（随机性高，模型不可能猜到）
CANARY = "ZQX-WUBBLE-7391-NEBULA"


def make_long_content_with_canary(total_len: int) -> str:
    """构造超长内容，在中间偏后位置埋暗号。"""
    # 暗号埋在 60% 位置（深处，不可能靠开头蒙到）
    canary_pos = int(total_len * 0.6)
    prefix = "这是一段关于市场营销的历史叙述。" * (canary_pos // 18)
    # 埋暗号，前后加明显标记
    canary_block = f"\n\n【内部编号：{CANARY}】\n这是一段被标记的特殊内容，编号为 {CANARY}。\n\n"
    suffix = "营销文化跨越了地域和时代的界限。" * ((total_len - canary_pos - 200) // 18)
    return prefix + canary_block + suffix


async def collect_full_response(client: TabbitClient, session_id: str, content: str,
                                 references: list, html_content: str, model: str,
                                 use_channel: str) -> str:
    """发请求并收集完整文本响应"""
    short_content = content  # 短的提问
    payload = {
        "chat_session_id": session_id, "message_id": None,
        "content": short_content, "selected_model": model,
        "parallel_group_id": None, "task_name": "script" if use_channel == "references" else "chat",
        "agent_mode": False,
        "metadatas": {"html_content": html_content},
        "references": references,
        "entity": {"key": hashlib.md5(b"").hexdigest(), "extras": {"type": "tab", "url": "https://example.com"}},
    }
    headers = {**client._get_headers(f"/session/{session_id}", with_uuid=True),
               "accept": "text/event-stream", "Content-Type": "application/json"}

    full_text = ""
    async with client.client.stream("POST", f"{client.base_url}/api/v1/chat/completion",
                                     json=payload, headers=headers, cookies=client._get_cookies()) as resp:
        async for line in resp.aiter_lines():
            if line.startswith("data:"):
                try:
                    data = json.loads(line[5:].strip())
                    # 收集 message_chunk 的 content
                    if "content" in data:
                        full_text += data["content"]
                except Exception:
                    pass
    return full_text


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="data/config.json")
    ap.add_argument("--model", default="Default")
    args = ap.parse_args()

    with open(args.config) as f:
        cfg = json.load(f)
    tokens = cfg.get("tokens", [])
    first = tokens[0]
    token = first["value"] if isinstance(first, dict) else first
    client = TabbitClient(
        token, base_url=cfg.get("tabbit", {}).get("base_url", "https://web.tabbit.ai"),
        browser_version=cfg.get("tabbit", {}).get("browser_version"),
        sparkle_version=cfg.get("tabbit", {}).get("sparkle_version"),
        default_browser=True)

    long_content = make_long_content_with_canary(80000)
    logger.info("超长内容: %d 字符，暗号 %s 埋在 60%% 位置", len(long_content), CANARY)
    logger.info("提问：内容中的内部编号是什么？\n")

    # 验证通道A：references
    logger.info("【验证A】references 通道")
    sid = await client.create_chat_session()
    logger.info("  会话 %s", sid[:8])
    question = "上面引用的内容中，【内部编号】是什么？只回答编号本身。"
    refs = [{"type": "dom", "title": "网页内容", "content": long_content,
             "metadata": {"path": "https://example.com", "file_ids": []}}]
    text_a = await collect_full_response(client, sid, question, refs, f"<p>{question}</p>", args.model, "references")
    print(f"  模型回答: {text_a[:300]}")
    hit_a = CANARY in text_a
    print(f"  暗号命中: {'✓ 是！模型读到了！' if hit_a else '✗ 未命中'}\n")
    with open("/tmp/verify_refs.txt", "w") as f:
        f.write(text_a)
    await asyncio.sleep(2)

    # 验证通道B：html_content
    logger.info("【验证B】html_content 通道")
    sid = await client.create_chat_session()
    logger.info("  会话 %s", sid[:8])
    question = "以下内容中，【内部编号】是什么？只回答编号本身。"
    html = f"<p>{question}</p><div>{long_content}</div>"
    text_b = await collect_full_response(client, sid, question, [], html, args.model, "html")
    print(f"  模型回答: {text_b[:300]}")
    hit_b = CANARY in text_b
    print(f"  暗号命中: {'✓ 是！模型读到了！' if hit_b else '✗ 未命中'}\n")
    with open("/tmp/verify_html.txt", "w") as f:
        f.write(text_b)

    print("=" * 60)
    print("决定性结论：")
    print(f"  references 通道: {'✅ 真突破！模型读到超长内容' if hit_a else '❌ 假通过，模型没读到'}")
    print(f"  html_content 通道: {'✅ 真突破！模型读到超长内容' if hit_b else '❌ 假通过，模型没读到'}")
    if hit_a or hit_b:
        print("\n🎉 2万字符限制被突破！长上下文模型可用！")
    await client.client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
