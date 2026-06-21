"""验证 references / v2+force_execute / html_content 三条绕过通道。

基于前端逆向报告：
- references[].content 是独立通道，前端 SCRIPT 模式塞整页 HTML，后端是否校验未知
- /api/v2/chat/completion 有 force_execute 字段，可能跳过校验
- metadatas.html_content 与 content 同内容但前端不校验
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
logger = logging.getLogger("probe")

# 远超 20421 的测试长度
LONG_LEN = 100000


def make_padding(n: int) -> str:
    return "请总结以下内容：\n" + "测试填充内容。" * (n // 8)


async def get_session(client: TabbitClient) -> str:
    sid = await client.create_chat_session()
    logger.info("  会话 %s", sid[:8])
    return sid


async def send_v1(client: TabbitClient, session_id: str, content: str, model: str = "Default") -> tuple[bool, str]:
    """标准 v1 接口，作为对照"""
    payload = {
        "chat_session_id": session_id, "message_id": None,
        "content": content, "selected_model": model,
        "parallel_group_id": None, "task_name": "chat", "agent_mode": False,
        "metadatas": {"html_content": f"<p>{content}</p>"},
        "references": [], "entity": {"key": hashlib.md5(b"").hexdigest(), "extras": {"type": "tab", "url": ""}},
    }
    headers = {**client._get_headers(f"/session/{session_id}", with_uuid=True),
               "accept": "text/event-stream", "Content-Type": "application/json"}
    try:
        resp = await client.client.post(
            f"{client.base_url}/api/v1/chat/completion",
            json=payload, headers=headers, cookies=client._get_cookies())
        body = resp.text
        with open(f"/tmp/bypass_v1_{len(content)}.txt", "w") as f:
            f.write(body)
        has_492 = "492" in body[:300]
        has_ready = "event: ready" in body or "event: message_start" in body
        return (not has_492), ("492拒" if has_492 else ("✓ready" if has_ready else body[:100]))
    except Exception as e:
        return False, f"err: {e}"


async def send_v2_force(client: TabbitClient, session_id: str, content: str, model: str = "Default") -> tuple[bool, str]:
    """v2 接口 + force_execute=true"""
    payload = {
        "chat_session_id": session_id, "message_id": None,
        "content": content, "selected_model": model,
        "parallel_group_id": None, "task_name": "chat", "agent_mode": False,
        "force_execute": True,  # ← 关键：强制执行
        "client_turn_id": hashlib.md5(content.encode()).hexdigest()[:16],
        "metadatas": {"html_content": f"<p>{content}</p>"},
        "references": [], "entity": {"key": hashlib.md5(b"").hexdigest(), "extras": {"type": "tab", "url": ""}},
    }
    headers = {**client._get_headers(f"/session/{session_id}", with_uuid=True),
               "accept": "text/event-stream", "Content-Type": "application/json"}
    try:
        resp = await client.client.post(
            f"{client.base_url}/api/v2/chat/completion",
            json=payload, headers=headers, cookies=client._get_cookies())
        body = resp.text
        with open(f"/tmp/bypass_v2force_{len(content)}.txt", "w") as f:
            f.write(body)
        has_492 = "492" in body[:300]
        has_ready = "event: ready" in body or "event: message_start" in body
        return (not has_492), ("492拒" if has_492 else ("✓ready" if has_ready else body[:100]))
    except Exception as e:
        return False, f"err: {e}"


async def send_references(client: TabbitClient, session_id: str, content: str, model: str = "Default") -> tuple[bool, str]:
    """SCRIPT 模式 + 超长 references.content（content 主字段保持短）"""
    short_content = "请总结上面引用的内容。"  # content 主字段很短
    long_ref = content  # 超长内容塞 references
    payload = {
        "chat_session_id": session_id, "message_id": None,
        "content": short_content, "selected_model": model,
        "parallel_group_id": None, "task_name": "script", "agent_mode": False,  # SCRIPT 模式
        "metadatas": {"html_content": f"<p>{short_content}</p>"},
        "references": [
            {"type": "dom", "title": "网页内容", "content": long_ref,
             "metadata": {"path": "https://example.com", "file_ids": []}}
        ],
        "entity": {"key": hashlib.md5(b"").hexdigest(), "extras": {"type": "tab", "url": "https://example.com"}},
    }
    headers = {**client._get_headers(f"/session/{session_id}", with_uuid=True),
               "accept": "text/event-stream", "Content-Type": "application/json"}
    try:
        resp = await client.client.post(
            f"{client.base_url}/api/v1/chat/completion",
            json=payload, headers=headers, cookies=client._get_cookies())
        body = resp.text
        with open(f"/tmp/bypass_ref_{len(long_ref)}.txt", "w") as f:
            f.write(body)
        has_492 = "492" in body[:300]
        has_ready = "event: ready" in body or "event: message_start" in body
        return (not has_492), ("492拒" if has_492 else ("✓ready" if has_ready else body[:120]))
    except Exception as e:
        return False, f"err: {e}"


async def send_html_content(client: TabbitClient, session_id: str, content: str, model: str = "Default") -> tuple[bool, str]:
    """短 content + 超长 metadatas.html_content"""
    short_content = "请总结以下内容。"
    long_html = f"<p>{content}</p>"  # 超长塞 html_content
    payload = {
        "chat_session_id": session_id, "message_id": None,
        "content": short_content, "selected_model": model,
        "parallel_group_id": None, "task_name": "chat", "agent_mode": False,
        "metadatas": {"html_content": long_html},  # ← 超长 html
        "references": [], "entity": {"key": hashlib.md5(b"").hexdigest(), "extras": {"type": "tab", "url": ""}},
    }
    headers = {**client._get_headers(f"/session/{session_id}", with_uuid=True),
               "accept": "text/event-stream", "Content-Type": "application/json"}
    try:
        resp = await client.client.post(
            f"{client.base_url}/api/v1/chat/completion",
            json=payload, headers=headers, cookies=client._get_cookies())
        body = resp.text
        with open(f"/tmp/bypass_html_{len(long_html)}.txt", "w") as f:
            f.write(body)
        has_492 = "492" in body[:300]
        has_ready = "event: ready" in body or "event: message_start" in body
        return (not has_492), ("492拒" if has_492 else ("✓ready" if has_ready else body[:120]))
    except Exception as e:
        return False, f"err: {e}"


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="data/config.json")
    ap.add_argument("--model", default="Default", help="测试模型")
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
        default_browser=True)

    long_content = make_padding(LONG_LEN)
    logger.info("测试内容长度: %d 字符（远超 20421 边界）", len(long_content))
    logger.info("模型: %s", args.model)
    print()

    # 对照组：v1 超长 content（预期 492）
    logger.info("【对照组】v1 + 超长 content（预期被 492 拒）")
    sid = await get_session(client)
    ok, note = await send_v1(client, sid, long_content, args.model)
    print(f"  v1+长content: {'✓通过' if ok else '✗拒'} | {note}\n")
    await asyncio.sleep(1)

    # 实验1：v2 + force_execute
    logger.info("【实验1】v2 + force_execute=true + 超长 content")
    sid = await get_session(client)
    ok, note = await send_v2_force(client, sid, long_content, args.model)
    print(f"  v2+force: {'✓通过' if ok else '✗拒'} | {note}\n")
    await asyncio.sleep(1)

    # 实验2：references.content 塞超长
    logger.info("【实验2】SCRIPT模式 + 超长 references.content（content主字段短）")
    sid = await get_session(client)
    ok, note = await send_references(client, sid, long_content, args.model)
    print(f"  references: {'✓通过' if ok else '✗拒'} | {note}\n")
    await asyncio.sleep(1)

    # 实验3：html_content 塞超长
    logger.info("【实验3】短content + 超长 metadatas.html_content")
    sid = await get_session(client)
    ok, note = await send_html_content(client, sid, long_content, args.model)
    print(f"  html_content: {'✓通过' if ok else '✗拒'} | {note}\n")

    print("=" * 60)
    print("结论：任一实验「✓通过」即找到绕过通道，详细响应见 /tmp/bypass_*.txt")
    await client.client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
