"""抓 Tabbit 客户端 agent/工具调用请求的完整 payload。

用法：
  mitmdump -s scripts/capture_agent.py
  # 然后在真机 Tabbit 客户端触发一个 agent 任务（如"搜索今天科技新闻"）

重点抓 /api/v1/chat/completion + /session/{id} 的 request body，对比
agent_mode / task_name / 是否带工具定义字段（非 content 正文里的）。
print 到 stdout（mitm 默认显示），同时落 logs/capture_agent.log。
"""
import json
import os
import sys
from mitmproxy import http

LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "capture_agent.log")


def _log(msg: str) -> None:
    # stdout 强制 flush，确保 mitm 能实时显示
    print(msg, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except Exception as e:
        print(f"[log write err {e}]", flush=True)


def request(flow: http.HTTPFlow) -> None:
    url = flow.request.pretty_url
    if "tabbit" not in url:
        return
    # 真机用 /api/v1/chat/completion 或 /session/{id} 两个接口发消息
    if "/api/v1/chat/completion" not in url and "/session/" not in url:
        return
    # 只看 POST（session GET 是页面加载）
    if flow.request.method != "POST":
        return
    method = flow.request.method
    body_text = flow.request.get_text() or ""
    _log(f"\n{'='*70}")
    _log(f"REQUEST {method} {url}")
    # 抓关键头
    for h in ("x-req-ctx", "unique-uuid", "content-type"):
        v = flow.request.headers.get(h, "")
        if v:
            _log(f"  {h}: {v[:80]}")
    if body_text:
        try:
            d = json.loads(body_text)
            _log("  body (parsed):")
            # 重点字段
            for key in ("selected_model", "task_name", "agent_mode",
                        "parallel_group_id", "references", "entity"):
                if key in d:
                    val = d[key]
                    if isinstance(val, (list, dict)):
                        val = json.dumps(val, ensure_ascii=False)[:300]
                    _log(f"    {key}: {val}")
            # content 长度 + 是否含工具协议痕迹
            content = d.get("content", "")
            _log(f"    content_len: {len(content)}")
            _log(f"    content_has_invoke: {'<invoke' in content}")
            _log(f"    content_has_trigger: {'<<CALL_' in content}")
            # 完整 body 存档（脱敏后）
            safe = json.dumps(d, ensure_ascii=False, indent=2)
            # 截断超长 content
            if len(safe) > 4000:
                safe = safe[:4000] + "\n... [truncated]"
            _log(f"  full body:\n{safe}")
            # 列出所有顶层 key，找未预期字段
            _log(f"  top-level keys: {list(d.keys())}")
        except Exception as e:
            _log(f"  body (raw, parse failed {e}): {body_text[:500]}")


def response(flow: http.HTTPFlow) -> None:
    url = flow.request.pretty_url
    if "tabbit" not in url:
        return
    if "/api/v1/chat/completion" not in url and "/chat/send" not in url:
        return
    status = flow.response.status_code
    ctype = flow.response.headers.get("content-type", "")
    _log(f"RESPONSE {status} {ctype[:50]}")
    # SSE 流：只抓前几行看事件类型
    if "event-stream" in ctype:
        text = flow.response.get_text() or ""
        lines = text.splitlines()[:30]
        _log("  sse first lines:")
        for ln in lines:
            _log(f"    {ln[:120]}")
