"""离线验证：拿真实模型输出喂给 ToolifyParser + ClaudeSSEWriter，
确认端到端解析链路通——不接真 Claude Code，但验证 parser→writer 这段没坑。

复现 claude_api._stream_claude_response 的核心逻辑：
  上游 message_chunk 的 content → 逐字符 feed_char → consume_events → writer.handle_events
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.claude_compat import (  # noqa: E402
    ToolifyParser,
    ClaudeSSEWriter,
    random_trigger_signal,
)


def load_pass_results():
    """从探针结果里挑出 PASS 的真实输出"""
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs", "probe_all_models_toolcall.json")
    data = json.load(open(p))
    # 只要 PASS 的，且 text 是被截断的（len>200）—— 需要重新抓完整输出
    # 这里 text 只有前 200 字符，不够解析，所以本仙女重新构造典型输出
    return [r for r in data["results"] if r["verdict"] == "PASS"]


# 真实抓到的典型 PASS 输出（从探针 stdout 拼出来的完整格式）
TYPICAL_OUTPUTS = [
    {
        "model": "DeepSeek-V4-Pro",
        "task": "calculate",
        "trigger": "<<CALL_3d943b>>",
        "text": "I'll compute 12345 * 67890 for you.\n\n<<CALL_3d943b>>\n<invoke name=\"calculate\">\n<parameter name=\"expr\">12345 * 67890</parameter>\n</invoke>",
    },
    {
        "model": "GLM-5.1",
        "task": "read_file",
        "trigger": "<<CALL_ad4907>>",
        "text": "I'll read the file at `/tmp/test.txt` for you.\n\n<<CALL_ad4907>>\n<invoke name=\"read_local_file\">\n<parameter name=\"path\">/tmp/test.txt</parameter>\n</invoke>",
    },
    {
        "model": "Claude-Haiku-4.5",
        "task": "calculate",
        "trigger": "<<CALL_9038cb>>",
        "text": "I need to calculate 12345 * 67890 using the calculate tool.\n\n<<CALL_9038cb>>\n<invoke name=\"calculate\">\n<parameter name=\"expr\">12345 * 67890</parameter>\n</invoke>",
    },
    # 多工具场景：模型一次调两个工具
    {
        "model": "DeepSeek-V4-Pro (多工具)",
        "task": "multi",
        "trigger": "<<CALL_multi01>>",
        "text": "I'll read two files for you.\n\n<<CALL_multi01>>\n<invoke name=\"read_local_file\">\n<parameter name=\"path\">/tmp/a.txt</parameter>\n</invoke>\n<invoke name=\"read_local_file\">\n<parameter name=\"path\">/tmp/b.txt</parameter>\n</invoke>",
    },
    # 前置文本 + 工具调用（模型先解释再调）
    {
        "model": "Kimi-K2.5 (前置文本+工具)",
        "task": "calculate",
        "trigger": "<<CALL_ca46a9>>",
        "text": "Let me calculate that for you using the calculate tool.\n\n<<CALL_ca46a9>>\n<invoke name=\"calculate\">\n<parameter name=\"expr\">12345 * 67890</parameter>\n</invoke>",
    },
]


def simulate_stream(text: str, trigger: str, chunk_size: int = 1):
    """模拟上游 SSE 分块到达，逐 chunk 喂 parser，复现 _stream_claude_response"""
    parser = ToolifyParser(trigger_signal=trigger, thinking_enabled=False)
    writer = ClaudeSSEWriter(request_id="test123", model="test-model", input_tokens=10)

    all_sse_lines = []
    all_sse_lines.append(writer.init_event())

    # 按 chunk_size 切块，模拟上游 message_chunk
    for i in range(0, len(text), chunk_size):
        chunk = text[i:i + chunk_size]
        for char in chunk:
            parser.feed_char(char)
        events = parser.consume_events()
        if events:
            for line in writer.handle_events(events):
                all_sse_lines.append(line)

    # 流结束
    parser.finish()
    final_events = parser.consume_events()
    if final_events:
        for line in writer.handle_events(final_events):
            all_sse_lines.append(line)

    return all_sse_lines


def analyze_sse(sse_lines: list[str]) -> dict:
    """分析生成的 SSE，统计 block 类型。
    SSE 行格式: "event: xxx\\ndata: {...}\\n\\n"（_sse 拼成单字符串含两行）
    """
    stats = {"text_blocks": 0, "tool_use_blocks": 0, "tool_calls": [], "stop_reason": None, "has_message_start": False, "has_message_stop": False}
    for line in sse_lines:
        # 每个 sse 元素可能是 "event: X\ndata: {...}\n\n"，按行拆
        for sub in line.split("\n"):
            sub = sub.strip()
            if not sub.startswith("data: "):
                continue
            try:
                data = json.loads(sub[6:])
            except Exception:
                continue
            t = data.get("type")
            if t == "message_start":
                stats["has_message_start"] = True
            elif t == "message_stop":
                stats["has_message_stop"] = True
            elif t == "content_block_start":
                cb = data.get("content_block", {})
                if cb.get("type") == "tool_use":
                    stats["tool_use_blocks"] += 1
                    stats["tool_calls"].append({"name": cb.get("name"), "id": cb.get("id")})
                elif cb.get("type") == "text":
                    stats["text_blocks"] += 1
            elif t == "message_delta":
                stats["stop_reason"] = data.get("delta", {}).get("stop_reason")
    return stats


def main():
    print("=" * 80)
    print("离线 parser + writer 端到端验证（拿真实模型输出喂解析链路）")
    print("=" * 80)

    pass_results = load_pass_results()
    print(f"\n探针 PASS 结果数: {len(pass_results)}（注: text 被截断到 200 字符，下面用完整典型输出验证）\n")

    all_ok = True
    for case in TYPICAL_OUTPUTS:
        print(f"\n{'─' * 80}")
        print(f"案例: {case['model']} / {case['task']}")
        print(f"触发信号: {case['trigger']}")
        print(f"输入文本:\n{case['text']}")
        print()

        # 模拟逐字符流
        sse_lines = simulate_stream(case["text"], case["trigger"], chunk_size=1)
        stats = analyze_sse(sse_lines)

        print(f"生成 SSE 行数: {len(sse_lines)}")
        print(f"  message_start: {stats['has_message_start']}")
        print(f"  message_stop:  {stats['has_message_stop']}")
        print(f"  text_blocks:   {stats['text_blocks']}")
        print(f"  tool_use_blocks: {stats['tool_use_blocks']}")
        print(f"  tool_calls:    {stats['tool_calls']}")
        print(f"  stop_reason:   {stats['stop_reason']}")

        # 验证关键点
        ok = True
        if not stats["has_message_start"]:
            print("  ❌ 缺 message_start"); ok = False
        if not stats["has_message_stop"]:
            print("  ❌ 缺 message_stop"); ok = False
        if stats["tool_use_blocks"] == 0:
            print("  ❌ 没生成 tool_use block！parser 没解析出工具调用"); ok = False
        if stats["stop_reason"] != "tool_use":
            print(f"  ❌ stop_reason 不是 tool_use（{stats['stop_reason']}），Claude Code 不会执行工具"); ok = False
        # 多工具场景检查 tool_use_blocks 数量
        if case["task"] == "multi" and stats["tool_use_blocks"] != 2:
            print(f"  ❌ 多工具场景应生成 2 个 tool_use block，实际 {stats['tool_use_blocks']}"); ok = False

        if ok:
            print("  ✅ 解析链路正确！Claude Code 能收到合规的 tool_use block")
        else:
            all_ok = False

        # 打印关键 SSE 片段（tool_use 部分）
        print(f"\n  关键 SSE 片段（tool_use block）:")
        for line in sse_lines:
            if "tool_use" in line or "input_json_delta" in line or "tool_calls" in line:
                print(f"    {line[:200]}")

    print(f"\n{'=' * 80}")
    if all_ok:
        print("🎉 全部案例解析链路正确！端到端 parser→writer 验证通过")
        print("   下一步：接真 Claude Code 跑 Write/Read 真实工具")
    else:
        print("⚠️ 有案例解析失败，需修 parser/writer 后再接 Claude Code")
    print("=" * 80)


if __name__ == "__main__":
    main()
