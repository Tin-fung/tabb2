"""评估报告真伪 —— 抓包器 v2，关键接口存 body。"""
from mitmproxy import http
import json
import re

INTEREST = ["/chat/completion", "/chat/send", "/model_config/models",
            "/api/v1/prompts", "/api/v1/config", "/member/usage",
            "/api/commerce/quota", "/proxy/mcp", "/skill"]

def _dump(flow, tag):
    url = flow.request.pretty_url
    print(f"\n{'='*78}")
    print(f"[{tag}] {flow.request.method} {flow.response.status_code} {url[:140]}")
    # 请求 body
    if flow.request.method == "POST":
        rb = flow.request.get_text() or ""
        print(f"  REQ body ({len(rb)}b): {rb[:600]}")
    # 响应 body
    resp = flow.response
    ct = resp.headers.get("content-type", "")
    body = resp.get_text() or ""
    if "event-stream" in ct:
        events = re.findall(r"^event:\s*(\S+)", body, re.M)
        print(f"  RESP SSE events: {events}")
        # 抓前几个 data
        datas = re.findall(r"^data:\s*(.+)$", body, re.M)
        for d in datas[:5]:
            print(f"    data: {d[:160]}")
        if "492" in body:
            print(f"  ⚠️ 含 492")
    else:
        print(f"  RESP ({len(body)}b, ct={ct[:40]}): {body[:800]}")

def response(flow: http.HTTPFlow) -> None:
    url = flow.request.pretty_url
    if "tabbit" not in url.lower():
        return
    if any(x in url for x in [".js",".css",".png",".jpg",".svg",".woff",".ico","webp",
                                 "matomo","minidump",".pak",".dat",".bin","font","rumt"]):
        return
    for kw in INTEREST:
        if kw in url:
            _dump(flow, kw)
            break
