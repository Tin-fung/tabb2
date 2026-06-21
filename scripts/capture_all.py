"""抓所有 tabbit.ai 请求，看有哪些接口 + agent 模式怎么走"""
from mitmproxy import http

def response(flow: http.HTTPFlow) -> None:
    url = flow.request.pretty_url
    if "tabbit" not in url:
        return
    # 只看 API 请求，排除静态资源
    if any(x in url for x in [".js", ".css", ".png", ".jpg", ".svg", ".woff", ".ico", "matomo", "minidump"]):
        return
    status = flow.response.status_code
    method = flow.request.method
    # 简短路径
    path = url.replace("https://web.tabbit.ai", "").replace("https://www.tabbit.ai", "")
    if len(path) > 80:
        path = path[:80] + "..."
    body = flow.request.get_text()[:200] if flow.request.get_text() else ""
    print(f"{method} {status} {path}")
    if body and method == "POST":
        print(f"  body: {body}")
