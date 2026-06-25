"""批量探测所有上游模型对工具调用协议的遵循度。

对每个模型跑两个任务：
  1. 纯计算（calculate 工具）—— 无安全敏感性，测基础遵循度
  2. 读文件（read_local_file 工具）—— 略敏感，测抗 injection 恐惧

判定：
  ✅ PASS  = 输出触发信号 + <invoke> 完整 XML
  ⚠️ PARTIAL = 输出部分协议痕迹但未完整
  ❌ REJECT = 明确拒绝（提 prompt injection）
  💤 SILENT = 空回复或抢答（无视协议）

用法: .venv/bin/python scripts/probe_all_models_toolcall.py
"""
import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.tabbit_client import TabbitClient  # noqa: E402
from core.claude_compat import map_claude_to_content, random_trigger_signal  # noqa: E402
from core.model_registry import ModelRegistry  # noqa: E402

# 两套测试任务
TASKS = [
    {
        "key": "calculate",
        "tool": {
            "name": "calculate",
            "description": "Compute a math expression and return the result. The model must call this tool for any calculation.",
            "input_schema": {
                "type": "object",
                "properties": {"expr": {"type": "string", "description": "math expression"}},
                "required": ["expr"],
            },
        },
        "user_msg": "What is 12345 * 67890? You MUST use the calculate tool to compute this precisely.",
    },
    {
        "key": "read_file",
        "tool": {
            "name": "read_local_file",
            "description": "Read a local file on the user machine. The model must call this tool - it cannot read files itself.",
            "input_schema": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "absolute file path"}},
                "required": ["path"],
            },
        },
        "user_msg": "Read the file at /tmp/test.txt and tell me its contents. You MUST use the read_local_file tool.",
    },
]


def classify(text: str, trigger_signal: str) -> str:
    """判定模型输出的遵循度"""
    if not text:
        return "SILENT"
    has_ts = trigger_signal in text
    has_invoke = "<invoke" in text.lower()
    has_param = "<parameter" in text.lower()
    if has_ts and has_invoke and has_param:
        return "PASS"
    if has_ts or has_invoke:
        return "PARTIAL"
    lower = text.lower()
    if "injection" in lower or "not part of" in lower or "won't follow" in lower or "fabricat" in lower:
        return "REJECT"
    # 有内容但完全无视协议 → 抢答或闲扯
    return "SILENT"


async def test_model(client: TabbitClient, model: str, task: dict) -> dict:
    """测单个模型单个任务，返回结果"""
    body = {
        "tools": [task["tool"]],
        "messages": [{"role": "user", "content": task["user_msg"]}],
    }
    ts = random_trigger_signal()
    body["_trigger_signal"] = ts
    content, refs, tn = map_claude_to_content(body, ts)
    try:
        sid = await client.create_chat_session()
        full = ""
        async for ev in client.send_message(sid, content, model, references=refs, task_name=tn):
            if ev["event"] == "message_chunk":
                full += ev["data"].get("content", "")
        verdict = classify(full, ts)
        return {
            "model": model,
            "task": task["key"],
            "verdict": verdict,
            "len": len(full),
            "text": full[:200],
        }
    except Exception as e:
        return {
            "model": model,
            "task": task["key"],
            "verdict": "ERROR",
            "len": 0,
            "text": str(e)[:200],
        }


async def main():
    cfg = json.load(open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json")))
    token = cfg["tokens"][0]["value"]
    base_url = cfg["tabbit"]["base_url"]

    # 拉真实模型清单
    reg = ModelRegistry(base_url, verify_ssl=False)
    await reg.refresh_with_retry(retries=2)
    models = [m["selected_model"] for m in reg._models_meta] if reg._models_meta else []
    # Default 混合模型放第一个测
    if "Default" in models:
        models.remove("Default")
        models.insert(0, "Default")
    print(f"待测模型 {len(models)} 个: {models}\n")

    client = TabbitClient(
        token, base_url=base_url,
        browser_version=cfg["tabbit"].get("browser_version"),
        sparkle_version=cfg["tabbit"].get("sparkle_version"),
        default_browser=True,
    )

    results = []
    for i, model in enumerate(models):
        print(f"[{i+1}/{len(models)}] 测试 {model} ...", flush=True)
        for task in TASKS:
            r = await test_model(client, model, task)
            mark = {"PASS": "✅", "PARTIAL": "⚠️", "REJECT": "❌", "SILENT": "💤", "ERROR": "💥"}.get(r["verdict"], "?")
            print(f"  {mark} {r['task']:12s} {r['verdict']:8s} | {r['text'][:80]}", flush=True)
            results.append(r)
            await asyncio.sleep(1)
        await asyncio.sleep(1)

    await client.client.aclose()

    # 汇总表
    print("\n" + "=" * 80)
    print("汇总：各模型工具调用遵循度")
    print("=" * 80)
    print(f"{'模型':<22} {'计算任务':<10} {'读文件':<10} {'综合':<8}")
    print("-" * 80)
    by_model = {}
    for r in results:
        by_model.setdefault(r["model"], {})[r["task"]] = r["verdict"]
    recommendations = []
    for model, tasks in by_model.items():
        calc = tasks.get("calculate", "?")
        rf = tasks.get("read_file", "?")
        # 综合判定
        if calc == "PASS" and rf == "PASS":
            overall = "⭐优秀"
            recommendations.append((model, "优秀"))
        elif calc == "PASS":
            overall = "✅可用"
            recommendations.append((model, "可用(计算OK)"))
        elif "PARTIAL" in (calc, rf):
            overall = "⚠️勉强"
            recommendations.append((model, "勉强"))
        else:
            overall = "❌不可用"
        print(f"{model:<22} {calc:<10} {rf:<10} {overall}")

    print("\n" + "=" * 80)
    print("推荐：可用于 Claude Code 工具调用的模型")
    print("=" * 80)
    if recommendations:
        for m, level in recommendations:
            print(f"  ✅ {m:<22} {level}")
    else:
        print("  （暂无模型通过，需进一步研究注入方式）")

    # 存档
    out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs", "probe_all_models_toolcall.json")
    with open(out, "w") as f:
        json.dump({"timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "results": results}, f, ensure_ascii=False, indent=2)
    print(f"\n详细数据已存: {out}")


if __name__ == "__main__":
    asyncio.run(main())
