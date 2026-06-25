"""探针矩阵：找「让模型用 <<CALL>> 协议而非 Tabbit 原生工具」的方法。

对照实验维度：
  1. task_name: chat / script / GEN / WEB_GEN（看有没有哪个关闭原生工具）
  2. agent_mode: false / true
  3. 工具协议注入位置：content 开头 / 末尾 / references
  4. 模型：DeepSeek-V4-Pro / GLM-5.1 / Claude-Sonnet-4.6
  5. 用户消息强度：普通 / 强制约束

判定：模型输出 <<CALL_xxx>> + <invoke> = 成功（用你的协议）
      模型输出 message_tool_calls 事件 = 失败（走原生）
      模型纯文本回答 = 失败（不调工具）

用法: .venv/bin/python scripts/probe_suppress_native.py
"""
import asyncio
import json
import os
import sys
import time
import hashlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.tabbit_client import TabbitClient  # noqa: E402
from core.claude_compat import random_trigger_signal, build_tool_prompt  # noqa: E402

# 简单工具集（避免 54 工具的噪音，只给 2 个不撞原生名的）
SIMPLE_TOOLS = [
    {
        "name": "calc_add",
        "description": "Add two numbers and return the sum.",
        "input_schema": {
            "type": "object",
            "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
            "required": ["a", "b"],
        },
    },
    {
        "name": "my_search",
        "description": "Search my local knowledge base for information.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
]

USER_MSG = "What is 12345 + 67890? Use the calc_add tool. Also search my local knowledge base for 'tabbit' using my_search. You MUST use these tools via the trigger signal format."


def build_content_variant(variant: str, trigger_signal: str) -> str:
    """不同注入位置变体"""
    tool_prompt = build_tool_prompt(SIMPLE_TOOLS, trigger_signal)
    user = f"[User]: {USER_MSG}"
    hint = "[Assistant]:"

    if variant == "start":
        # 工具协议在最前
        return f"[System]: {tool_prompt}\n\n{user}\n\n{hint}"
    elif variant == "end":
        # 工具协议在末尾（紧贴 Assistant 提示）
        return f"{user}\n\n[System]: {tool_prompt}\n\n{hint}"
    elif variant == "around":
        # 用户消息被工具协议夹击
        return f"[System]: {tool_prompt}\n\n{user}\n\n[System]: Remember: to call a tool you MUST output {trigger_signal} then <invoke> XML.\n\n{hint}"
    return f"[System]: {tool_prompt}\n\n{user}\n\n{hint}"


async def test_one(client: TabbitClient, model: str, task_name: str, agent_mode: bool, variant: str) -> dict:
    """测一组参数"""
    trigger_signal = random_trigger_signal()
    content = build_content_variant(variant, trigger_signal)

    # 构造 payload，允许自定义 task_name / agent_mode
    payload = {
        "chat_session_id": None,  # 先建会话再填
        "message_id": None,
        "content": content,
        "selected_model": model,
        "parallel_group_id": None,
        "task_name": task_name,
        "agent_mode": agent_mode,
        "metadatas": {"html_content": f"<p>{content[:200]}</p>"},
        "references": [],
        "entity": {"key": hashlib.md5(b"").hexdigest(), "extras": {"type": "tab", "url": ""}},
    }

    # 建会话
    try:
        session_id = await client.create_chat_session()
    except Exception as e:
        return {"ok": False, "error": f"session: {e}"}
    payload["chat_session_id"] = session_id

    headers = {
        **client._get_headers(f"/session/{session_id}", with_uuid=True),
        "accept": "text/event-stream",
        "Content-Type": "application/json",
    }

    full_text = ""
    native_tool_calls = []
    has_trigger = False
    has_invoke = False
    error_code = None

    try:
        async with client.client.stream(
            "POST", f"{client.base_url}/api/v1/chat/completion",
            json=payload, headers=headers, cookies=client._get_cookies(),
        ) as resp:
            if resp.status_code != 200:
                body = await resp.aread()
                return {"ok": False, "error": f"http {resp.status_code}: {body.decode()[:100]}"}
            current_event = None
            async for line in resp.aiter_lines():
                if line.startswith("event:"):
                    current_event = line[len("event:"):].strip()
                elif line.startswith("data:") and current_event:
                    data_str = line[len("data:"):].strip()
                    try:
                        data = json.loads(data_str)
                    except Exception:
                        continue
                    if current_event == "message_chunk":
                        full_text += data.get("content", "")
                    elif current_event == "message_tool_calls":
                        # 上游原生工具调用！
                        for tc in data.get("tool_calls", []):
                            fn = tc.get("function", {})
                            native_tool_calls.append(fn.get("name", "?"))
                    elif current_event == "error":
                        error_code = data.get("code")
    except Exception as e:
        return {"ok": False, "error": f"stream: {e}"}

    has_trigger = trigger_signal in full_text
    has_invoke = "<invoke" in full_text.lower()

    # 判定
    if has_trigger and has_invoke:
        verdict = "✅用你的协议"
    elif native_tool_calls:
        verdict = f"❌走原生({','.join(native_tool_calls)})"
    elif full_text.strip():
        verdict = "⚠️纯文本不调工具"
    else:
        verdict = "❌空回复"

    return {
        "ok": True,
        "verdict": verdict,
        "text_len": len(full_text),
        "text_head": full_text[:120].replace("\n", " "),
        "native_tools": native_tool_calls,
        "has_trigger": has_trigger,
        "error_code": error_code,
    }


async def main():
    cfg_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json")
    with open(cfg_path) as f:
        cfg = json.load(f)
    token = cfg["tokens"][0]["value"]
    client = TabbitClient(
        token, base_url=cfg["tabbit"]["base_url"],
        browser_version=cfg["tabbit"].get("browser_version"),
        sparkle_version=cfg["tabbit"].get("sparkle_version"), default_browser=True,
    )

    # 实验矩阵（精简，聚焦最有希望的维度）
    experiments = [
        # (model, task_name, agent_mode, variant, 描述)
        ("DeepSeek-V4-Pro", "chat", False, "start", "基线：chat+agent_false+协议在前"),
        ("DeepSeek-V4-Pro", "script", False, "start", "task_name=script"),
        ("DeepSeek-V4-Pro", "chat", True, "start", "agent_mode=true"),
        ("DeepSeek-V4-Pro", "chat", False, "end", "协议在末尾"),
        ("DeepSeek-V4-Pro", "chat", False, "around", "协议夹击+强化提示"),
        ("GLM-5.1", "chat", False, "start", "GLM-5.1 基线"),
        ("GLM-5.1", "chat", False, "around", "GLM-5.1 强化"),
        ("Claude-Sonnet-4.6", "chat", False, "start", "Sonnet 基线"),
        ("Claude-Sonnet-4.6", "chat", False, "around", "Sonnet 强化"),
    ]

    print("=" * 100)
    print("探针矩阵：寻找压制 Tabbit 原生工具、激活 <<CALL>> 协议的方法")
    print("=" * 100)
    print(f"{'#':<3} {'模型':<20} {'task':<8} {'agent':<6} {'变体':<8} {'判定':<28} {'文本':<6} {'原生工具'}")
    print("-" * 100)

    results = []
    for i, (model, task, am, variant, desc) in enumerate(experiments, 1):
        r = await test_one(client, model, task, am, variant)
        r["desc"] = desc
        r["params"] = {"model": model, "task_name": task, "agent_mode": am, "variant": variant}
        results.append(r)
        if r.get("ok"):
            print(f"{i:<3} {model:<20} {task:<8} {str(am):<6} {variant:<8} {r['verdict']:<28} {r['text_len']:<6} {r['native_tools']}")
        else:
            print(f"{i:<3} {model:<20} {task:<8} {str(am):<6} {variant:<8} ❌错误: {r.get('error','')[:40]}")
        await asyncio.sleep(2)

    print("\n" + "=" * 100)
    print("结论")
    print("=" * 100)
    used_protocol = [r for r in results if r.get("ok") and r.get("has_trigger") and r.get("has_invoke")]
    if used_protocol:
        print(f"🎉 找到 {len(used_protocol)} 组让模型用 <<CALL>> 协议的参数！")
        for r in used_protocol:
            print(f"   {r['params']} → {r['verdict']}")
        print("   → 按这组参数调整注入策略，<<CALL>> 路线可活！")
    else:
        native = [r for r in results if r.get("ok") and r.get("native_tools")]
        text_only = [r for r in results if r.get("ok") and not r.get("native_tools") and not r.get("has_trigger") and r.get("text_len",0)>0]
        print(f"❌ 所有 {len(experiments)} 组实验都没有让模型用 <<CALL>> 协议")
        print(f"   走原生工具: {len(native)} 组")
        print(f"   纯文本不调: {len(text_only)} 组")
        print()
        print("结论：<<CALL>> 注入路线在当前 Tabbit 上游下是死路。")
        print("      模型有原生工具集，无视 content 里的外部工具协议。")
        print("      建议转方案 A（透传 message_tool_calls）或 C（纯对话定位）。")

    await client.client.aclose()

    # 存档
    out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs", "probe_suppress_native.json")
    with open(out, "w") as f:
        json.dump({"timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "results": results}, f, ensure_ascii=False, indent=2)
    print(f"\n详细数据: {out}")


if __name__ == "__main__":
    asyncio.run(main())
