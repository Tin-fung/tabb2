"""临时抓包代理：截在 Claude Code 和本地 proxy 之间，dump 完整双向流量。

用法:
  .venv/bin/python scripts/capture_claudecode.py
然后把 Claude Code 的 ANTHROPIC_BASE_URL 改到 http://localhost:8801
真测一轮，本仙女看 dump 文件诊断。

每个请求 dump 到 /tmp/cc_capture_<n>.json，含：
  - request: method/path/headers/body
  - response: status/headers/完整 SSE 原文
"""
import asyncio
import json
import time
import os
import sys
from aiohttp import web, ClientSession

# 强制 stdout 无缓冲，实时看到抓包
sys.stdout.reconfigure(line_buffering=True)

UPSTREAM = "http://localhost:8800"
DUMP_DIR = "/tmp"
counter = 0


async def proxy_handler(request: web.Request) -> web.StreamResponse:
    global counter
    counter += 1
    idx = counter

    # 读请求
    req_body = await request.read()
    req_headers = dict(request.headers)
    path = request.path_qs

    try:
        req_json = json.loads(req_body) if req_body else None
    except Exception:
        req_json = None

    # 转发到上游
    async with ClientSession() as session:
        async with session.request(
            request.method,
            f"{UPSTREAM}{path}",
            headers={k: v for k, v in req_headers.items() if k.lower() not in ("host", "content-length")},
            data=req_body if req_body else None,
        ) as upstream_resp:
            # 读完整响应
            resp_body = await upstream_resp.read()
            resp_headers = dict(upstream_resp.headers)

    # dump
    dump = {
        "idx": idx,
        "timestamp": time.strftime("%H:%M:%S"),
        "request": {
            "method": request.method,
            "path": path,
            "headers": req_headers,
            "body": req_json if req_json else req_body.decode("utf-8", errors="replace")[:2000],
        },
        "response": {
            "status": upstream_resp.status,
            "headers": resp_headers,
            "body_raw": resp_body.decode("utf-8", errors="replace"),
        },
    }
    dump_path = os.path.join(DUMP_DIR, f"cc_capture_{idx:03d}.json")
    with open(dump_path, "w") as f:
        json.dump(dump, f, ensure_ascii=False, indent=2)

    # 关键摘要打到 stdout
    model = req_json.get("model", "?") if req_json else "?"
    stream = req_json.get("stream", False) if req_json else False
    n_tools = len(req_json.get("tools", [])) if req_json else 0
    n_msgs = len(req_json.get("messages", [])) if req_json else 0
    print(f"[{idx:03d}] {time.strftime('%H:%M:%S')} {request.method} {path}")
    print(f"      req: model={model} stream={stream} tools={n_tools} msgs={n_msgs}")
    print(f"      resp: status={upstream_resp.status} len={len(resp_body)}")
    # 解析 SSE 响应，提取关键事件
    if "event-stream" in resp_headers.get("Content-Type", ""):
        events = []
        for line in resp_body.decode("utf-8", errors="replace").split("\n"):
            if line.startswith("event: "):
                events.append(line[7:].strip())
        print(f"      sse events: {events}")
        # 提取 stop_reason 和 content_block 类型
        stop_reason = None
        block_types = []
        for line in resp_body.decode("utf-8", errors="replace").split("\n"):
            if line.startswith("data: "):
                try:
                    d = json.loads(line[6:])
                    if d.get("type") == "message_delta":
                        stop_reason = d.get("delta", {}).get("stop_reason")
                    if d.get("type") == "content_block_start":
                        block_types.append(d.get("content_block", {}).get("type"))
                except Exception:
                    pass
        print(f"      block_types: {block_types}")
        print(f"      stop_reason: {stop_reason}")
    print(f"      dump: {dump_path}")

    # 透传响应给 Claude Code
    out = web.StreamResponse(status=upstream_resp.status, headers={
        k: v for k, v in resp_headers.items() if k.lower() not in ("transfer-encoding", "content-encoding", "content-length")
    })
    await out.prepare(request)
    await out.write(resp_body)
    await out.write_eof()
    return out


async def main():
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", proxy_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 8801)
    await site.start()
    print("=" * 70)
    print("抓包代理启动: http://127.0.0.1:8801")
    print(f"上游: {UPSTREAM}")
    print("请把 Claude Code 的 base_url 指向 http://localhost:8801")
    print("dump 文件: /tmp/cc_capture_*.json")
    print("Ctrl+C 退出")
    print("=" * 70)
    # 保持运行
    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n退出")
