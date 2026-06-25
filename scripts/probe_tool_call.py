"""探针：用项目真实逻辑构造 content（含工具 prompt），直接打上游 Tabbit，
dump 原始 SSE，定位「工具调用为何不通」的根因——是上游不吐，还是 parser 吃了。

用法: .venv/bin/python scripts/probe_tool_call.py
"""
import asyncio
import json
import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.tabbit_client import TabbitClient  # noqa: E402
from core.claude_compat import map_claude_to_content, random_trigger_signal  # noqa: E402


def build_tool_request():
    """构造一个真实的 Claude Code 风格工具请求"""
    return {
        "model": "claude-sonnet-4-5-20250929",
        "max_tokens": 1024,
        "tools": [
            {
                "name": "get_weather",
                "description": "Get the current weather for a given city.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "city": {"type": "string", "description": "City name"},
                        "unit": {"type": "string", "enum": ["celsius", "fahrenheit"]},
                    },
                    "required": ["city"],
                },
            }
        ],
        "messages": [
            {"role": "user", "content": "What is the weather in Beijing? You MUST use the get_weather tool to find out."}
        ],
    }


async def main():
    # 读 config 拿 token
    cfg_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json")
    with open(cfg_path) as f:
        cfg = json.load(f)
    tokens = cfg.get("tokens", [])
    if not tokens:
        print("❌ 无 token"); sys.exit(1)
    token = tokens[0]["value"]

    client = TabbitClient(
        token,
        base_url=cfg.get("tabbit", {}).get("base_url", "https://web.tabbit.ai"),
        browser_version=cfg.get("tabbit", {}).get("browser_version"),
        sparkle_version=cfg.get("tabbit", {}).get("sparkle_version"),
        default_browser=True,
    )

    # 用项目真实逻辑构造 content
    body = build_tool_request()
    trigger_signal = random_trigger_signal()
    body["_trigger_signal"] = trigger_signal
    content, references, task_name = map_claude_to_content(body, trigger_signal)

    print("=" * 70)
    print("【1】注入到上游的 content（前 2500 字符）")
    print("=" * 70)
    print(content[:2500])
    print(f"\n... (content 总长 {len(content)} 字符, references={len(references)} 段)")
    print(f"trigger_signal = {trigger_signal}")

    # 打印 content 里关键的工具指令片段
    print("\n" + "=" * 70)
    print("【2】content 中工具协议关键片段")
    print("=" * 70)
    if "trigger_signal" in content or trigger_signal in content:
        idx = content.find(trigger_signal)
        if idx >= 0:
            print(f"✓ trigger_signal 出现在 content 位置 {idx}")
            print(f"  上下文: ...{content[max(0,idx-50):idx+len(trigger_signal)+50]}...")
        else:
            print(f"✗ trigger_signal 未出现在 content（模板占位符问题！）")
    if "<invoke" in content:
        print("✓ <invoke> 示例在 content 中")
    if "get_weather" in content:
        print("✓ 工具名 get_weather 在 content 中")

    # 建会话
    print("\n" + "=" * 70)
    print("【3】建会话 + 打上游，dump 原始 SSE")
    print("=" * 70)
    session_id = await client.create_chat_session()
    print(f"session_id = {session_id}")

    # 用 Default 模型（大BOSS测试用的）
    raw_events = []
    print("\n--- 原始 SSE 事件流 ---")
    async for event in client.send_message(session_id, content, "Default", references=references, task_name=task_name):
        raw_events.append(event)
        et = event["event"]
        ed = event["data"]
        # 截断显示
        ed_str = json.dumps(ed, ensure_ascii=False)
        if len(ed_str) > 300:
            ed_str = ed_str[:300] + "..."
        print(f"event: {et}  data: {ed_str}")

    print(f"\n--- 共收到 {len(raw_events)} 个事件 ---")

    # 分析
    print("\n" + "=" * 70)
    print("【4】根因分析")
    print("=" * 70)
    chunks = [e for e in raw_events if e["event"] == "message_chunk"]
    full_text = "".join(e["data"].get("content", "") for e in chunks)
    print(f"message_chunk 数量: {len(chunks)}")
    print(f"拼接全文长度: {len(full_text)}")
    print(f"全文内容: {full_text[:1000]!r}")

    if not full_text:
        print("\n🚨 上游返回空内容！根因在上游/认证层，不是 parser。")
        print("   可能：content 被网关拒（492/493）但 error 事件没正确抛，或模型拒答。")
    elif trigger_signal in full_text or "<invoke" in full_text.lower():
        print("\n✅ 上游模型输出了工具调用信号！根因在 parser 或 SSE writer。")
        print(f"   触发信号位置: {full_text.find(trigger_signal) if trigger_signal in full_text else 'N/A'}")
        print(f"   <invoke> 位置: {full_text.lower().find('<invoke')}")
    else:
        print("\n⚠️ 上游有内容但没输出工具调用信号——模型没遵循工具协议。")
        print("   根因在 prompt 注入不够强，或 Default 混合模型工具遵循度差。")
        print("   建议：换 Claude-Opus-4.8 或 Claude-Sonnet-4.6 实测。")

    await client.client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
