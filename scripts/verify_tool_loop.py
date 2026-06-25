"""全自动多轮工具回环验证（模拟 Claude Code agent loop）。

模拟 Claude Code 的完整行为：
  1. 发带 tools 的 Claude Messages 请求 → 收 SSE → 解析出 tool_use block
  2. 本地"执行"工具（模拟 Write/Read/Bash），生成 tool_result
  3. 把 assistant(tool_use) + user(tool_result) 拼回 messages → 发第二轮
  4. 模型看到结果给最终答案 → 验证 stop_reason=end_turn

全程直连本地服务 http://localhost:8800/v1/messages，复用真实链路。
不依赖真 Claude Code，但验证了 Claude Code 会走的所有协议点。

用法: .venv/bin/python scripts/verify_tool_loop.py [--model DeepSeek-V4-Pro]
"""
import argparse
import json
import os
import sys
import time
import urllib.request
import urllib.error

SERVER = "http://localhost:8800"
MAX_ROUNDS = 5

# 模拟的工具实现（Claude Code 真实工具的简化版）
def execute_tool(name: str, arguments: dict) -> str:
    """本地执行工具，返回结果字符串（模拟 Claude Code 跑工具）"""
    name = {
        "Write": "write_file",
        "Read": "read_file",
        "LS": "list_dir",
    }.get(name, name)
    if name == "write_file":
        path = arguments.get("file_path", "/tmp/unknown")
        content = arguments.get("content", "")
        # 真写文件，让 read 能读回来
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write(content)
            return f"Successfully wrote {len(content)} characters to {path}"
        except Exception as e:
            return f"Error writing file: {e}"
    elif name == "read_file":
        path = arguments.get("file_path", "/tmp/unknown")
        try:
            with open(path) as f:
                return f.read()
        except FileNotFoundError:
            return f"Error: file {path} not found"
        except Exception as e:
            return f"Error reading file: {e}"
    elif name == "calculate":
        expr = arguments.get("expr", "")
        try:
            # 只允许纯算术，安全起见
            allowed = set("0123456789+-*/(). ")
            if all(c in allowed for c in expr):
                return str(eval(expr))
            return f"Error: unsupported expression"
        except Exception as e:
            return f"Error: {e}"
    elif name == "list_dir":
        path = arguments.get("path", ".")
        try:
            return "\n".join(sorted(os.listdir(path)))
        except Exception as e:
            return f"Error: {e}"
    return f"Unknown tool: {name}"


# 工具定义（Claude Code 风格 schema）
TOOLS = [
    {
        "name": "write_file",
        "description": "Write content to a file on the local filesystem.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "absolute path"},
                "content": {"type": "string", "description": "file content"},
            },
            "required": ["file_path", "content"],
        },
    },
    {
        "name": "read_file",
        "description": "Read a file from the local filesystem.",
        "input_schema": {
            "type": "object",
            "properties": {"file_path": {"type": "string", "description": "absolute path"}},
            "required": ["file_path"],
        },
    },
    {
        "name": "calculate",
        "description": "Evaluate a math expression.",
        "input_schema": {
            "type": "object",
            "properties": {"expr": {"type": "string"}},
            "required": ["expr"],
        },
    },
    {
        "name": "list_dir",
        "description": "List directory contents.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
]


def call_messages(messages: list, model: str) -> tuple[list[dict], str]:
    """调用 /v1/messages，返回 (assistant_content_blocks, stop_reason)。
    解析 SSE 流，重组出 content blocks。
    """
    body = {
        "model": model,
        "max_tokens": 4096,
        "stream": True,
        "tools": TOOLS,
        "messages": messages,
    }
    req = urllib.request.Request(
        f"{SERVER}/v1/messages",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    # 流式读取
    blocks = {}  # index -> block
    block_order = []
    stop_reason = "end_turn"
    current_idx = None
    current_tool_input = {}

    with urllib.request.urlopen(req, timeout=180) as resp:
        buffer = ""
        for chunk in iter(lambda: resp.read(1024), b""):
            buffer += chunk.decode("utf-8", errors="replace")
            while "\n\n" in buffer:
                event_str, buffer = buffer.split("\n\n", 1)
                event_type = None
                data_str = None
                for line in event_str.split("\n"):
                    if line.startswith("event: "):
                        event_type = line[7:].strip()
                    elif line.startswith("data: "):
                        data_str = line[6:]
                if not data_str:
                    continue
                try:
                    data = json.loads(data_str)
                except Exception:
                    continue
                t = data.get("type")
                if t == "content_block_start":
                    idx = data.get("index")
                    cb = data.get("content_block", {})
                    blocks[idx] = {"type": cb.get("type"), "text": "", "name": cb.get("name", ""), "id": cb.get("id", ""), "input": {}}
                    block_order.append(idx)
                    current_idx = idx
                    current_tool_input = {}
                elif t == "content_block_delta":
                    idx = data.get("index", current_idx)
                    delta = data.get("delta", {})
                    if delta.get("type") == "text_delta":
                        if idx in blocks:
                            blocks[idx]["text"] += delta.get("text", "")
                    elif delta.get("type") == "input_json_delta":
                        # 累积 partial_json
                        if idx in blocks:
                            blocks[idx].setdefault("_raw_input", "")
                            blocks[idx]["_raw_input"] += delta.get("partial_json", "")
                elif t == "content_block_stop":
                    idx = data.get("index", current_idx)
                    if idx in blocks and blocks[idx]["type"] == "tool_use" and "_raw_input" in blocks[idx]:
                        try:
                            blocks[idx]["input"] = json.loads(blocks[idx]["_raw_input"])
                        except Exception:
                            blocks[idx]["input"] = {}
                elif t == "message_delta":
                    stop_reason = data.get("delta", {}).get("stop_reason", stop_reason)

    ordered_blocks = [blocks[i] for i in sorted(blocks.keys()) if i in blocks]
    return ordered_blocks, stop_reason


def run_loop(model: str, user_msg: str, test_name: str) -> dict:
    """跑一个完整的多轮回环"""
    print(f"\n{'=' * 80}")
    print(f"测试: {test_name}")
    print(f"模型: {model}")
    print(f"用户: {user_msg}")
    print(f"{'=' * 80}")

    messages = [{"role": "user", "content": user_msg}]
    rounds = []
    final_text = ""
    success = False
    error = None

    for round_num in range(1, MAX_ROUNDS + 1):
        print(f"\n--- 第 {round_num} 轮 ---")
        try:
            blocks, stop_reason = call_messages(messages, model)
        except Exception as e:
            error = f"round {round_num} 请求失败: {e}"
            print(f"❌ {error}")
            break

        # 分离 tool_use 和 text
        tool_uses = [b for b in blocks if b["type"] == "tool_use"]
        text_blocks = [b for b in blocks if b["type"] == "text"]
        round_text = "".join(b["text"] for b in text_blocks)

        print(f"  stop_reason: {stop_reason}")
        print(f"  text: {round_text[:150]!r}")
        print(f"  tool_use 数量: {len(tool_uses)}")
        for tu in tool_uses:
            print(f"    → {tu['name']}({json.dumps(tu['input'], ensure_ascii=False)[:80]})")

        rounds.append({
            "round": round_num,
            "stop_reason": stop_reason,
            "tool_uses": [{"name": tu["name"], "input": tu["input"]} for tu in tool_uses],
            "text": round_text[:200],
        })

        if stop_reason == "tool_use" and tool_uses:
            # 把 assistant 的 tool_use 加回 messages
            assistant_content = []
            if round_text:
                assistant_content.append({"type": "text", "text": round_text})
            for tu in tool_uses:
                assistant_content.append({"type": "tool_use", "id": tu["id"], "name": tu["name"], "input": tu["input"]})
            messages.append({"role": "assistant", "content": assistant_content})

            # 执行工具 + 回喂 tool_result
            tool_results = []
            for tu in tool_uses:
                result = execute_tool(tu["name"], tu["input"])
                print(f"    执行结果: {result[:80]!r}")
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tu["id"],
                    "content": result,
                })
            messages.append({"role": "user", "content": tool_results})
            continue

        # 没工具调用 = 结束
        final_text = round_text
        if stop_reason == "end_turn":
            success = True
            print(f"\n✅ 模型给出最终答案，回环结束")
        else:
            print(f"\n⚠️ 非预期停止: stop_reason={stop_reason}")
        break
    else:
        error = f"超过 {MAX_ROUNDS} 轮仍未结束"

    print(f"\n最终答案: {final_text[:300]!r}")
    return {
        "test": test_name,
        "model": model,
        "success": success,
        "rounds": len(rounds),
        "final_text": final_text[:300],
        "error": error,
        "round_details": rounds,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="DeepSeek-V4-Pro", help="上游模型名")
    ap.add_argument("--server", default=SERVER)
    args = ap.parse_args()
    server_url = args.server

    # 探活
    try:
        with urllib.request.urlopen(f"{server_url}/health", timeout=5) as r:
            h = json.loads(r.read())
        print(f"服务探活: {h}")
        if not h.get("status") == "ok":
            print("❌ 服务不健康"); sys.exit(1)
    except Exception as e:
        print(f"❌ 服务不可达: {e}"); sys.exit(1)

    # 多个真实场景测试
    test_file = f"/tmp/tool_loop_test_{int(time.time())}.txt"
    tests = [
        {
            "name": "单工具-计算",
            "msg": f"What is 12345 * 67890 + 999? Use the calculate tool. Give me the final number.",
        },
        {
            "name": "写读回环-两轮",
            "msg": f"Write 'Hello from tool loop!' to {test_file}, then read it back and tell me what you wrote. You must use write_file then read_file.",
        },
        {
            "name": "多工具一轮",
            "msg": f"Use the calculate tool to compute 100*100, AND list_dir on /tmp, in the same response. Report both results.",
        },
    ]

    results = []
    for t in tests:
        r = run_loop(args.model, t["msg"], t["name"])
        results.append(r)
        time.sleep(2)

    # 汇总
    print(f"\n{'=' * 80}")
    print("多轮工具回环验证汇总")
    print(f"{'=' * 80}")
    print(f"{'测试':<20} {'轮数':<6} {'结果':<8} {'说明'}")
    print("-" * 80)
    all_ok = True
    for r in results:
        status = "✅成功" if r["success"] else "❌失败"
        note = r["error"] or r["final_text"][:40]
        print(f"{r['test']:<20} {r['rounds']:<6} {status:<8} {note}")
        if not r["success"]:
            all_ok = False

    print(f"\n{'=' * 80}")
    if all_ok:
        print("🎉 全部多轮回环验证通过！工具调用链路完整可用")
        print("   parser→writer→Claude Code 协议→工具执行→tool_result 回喂→下一轮，全通")
        print("   下一步可接真 Claude Code 实测")
    else:
        print("⚠️ 有测试失败，需诊断具体轮次")
    print(f"{'=' * 80}")

    # 存档
    out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs", "verify_tool_loop.json")
    with open(out, "w") as f:
        json.dump({"timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "model": args.model, "results": results}, f, ensure_ascii=False, indent=2)
    print(f"\n详细数据: {out}")


if __name__ == "__main__":
    main()
