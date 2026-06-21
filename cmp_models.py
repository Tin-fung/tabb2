#!/usr/bin/env python3
"""对比两个 models endpoint + 验证 display_name 当 selectedModel

大BOSS在 VPS 跑:
  docker cp /tmp/cmp_models.py tabbit2api:/app/cmp_models.py  # 或直接 git pull
  docker exec tabbit2api python /app/cmp_models.py
"""
import json, asyncio, sys
from pathlib import Path
import httpx

CONFIG_PATH = Path("/app/config.json")
cfg = json.loads(CONFIG_PATH.read_text())
BASE = cfg.get("tabbit", {}).get("base_url", "https://web.tabbit.ai")
parts = cfg["tokens"][0]["value"].split("|")
JWT = parts[0]
USER_ID = "6c00f622-a88a-4d2f-81d2-a4fd6e890d62"
COOKIES = {"token": JWT, "user_id": USER_ID, "managed": "tab_browser", "NEXT_LOCALE": "zh"}
if len(parts) > 1: COOKIES["next-auth.session-token"] = parts[1]


async def fetch_models(client, url, label):
    print(f"\n=== {label}: {url} ===")
    try:
        r = await client.get(url, cookies=COOKIES, timeout=15)
        print(f"  status={r.status_code}")
        if r.status_code != 200:
            print(f"  body: {r.text[:200]}")
            return {}
        data = r.json()
        # 统一提取模型列表
        models = []
        if "supported_models" in data:
            for provider, ms in data["supported_models"].items():
                for m in ms:
                    models.append({"provider": provider, **m})
        elif "models" in data:
            models = data["models"] if isinstance(data["models"], list) else []
        print(f"  模型数: {len(models)}")
        for m in models[:20]:
            print(f"    name={m.get('name','?'):40} display_name={m.get('display_name','?'):25} provider={m.get('provider','?')}")
        return {m.get("name"): m for m in models if m.get("name")}
    except Exception as e:
        print(f"  ERR: {e}")
        return {}


async def main():
    async with httpx.AsyncClient(timeout=20, verify=False) as client:
        # 两个候选 endpoint
        a = await fetch_models(client, f"{BASE}/api/v0/chat/models", "接口A (本仙女当前用的)")
        b = await fetch_models(client, f"{BASE}/proxy/v1/model_config/models?a=0", "接口B (参考项目用的)")

        print("\n=== 对比差异 ===")
        only_a = set(a) - set(b)
        only_b = set(b) - set(a)
        print(f"  仅A有: {only_a if only_a else '无'}")
        print(f"  仅B有: {only_b if only_b else '无'}")

        # 验证: 用 display_name 当 selectedModel 发消息
        print("\n=== 验证 display_name 当 selectedModel ===")
        import base64, uuid, hashlib
        tabbit_cfg = cfg.get("tabbit", {})
        x_req_ctx = base64.b64encode(f"{tabbit_cfg.get('browser_version','1.1.39')}({tabbit_cfg.get('sparkle_version',10101039)})".encode()).decode()
        # 取 glm 的 display_name 试
        glm = a.get("glm-5") or next((m for m in a.values() if "glm" in m.get("name","").lower()), None)
        if glm:
            display = glm.get("display_name")
            print(f"  glm name={glm.get('name')} display_name={display}")
            # 建会话
            import re, urllib.parse
            router_state = ["",{"children":["chat",{"children":[["id","new","d"],{"children":["__PAGE__",{},None,"refetch"]},None,None]},None,None]},None,None]
            h = {"x-req-ctx": x_req_ctx, "unique-uuid": str(uuid.uuid4()), "rsc":"1",
                 "next-router-state-tree": urllib.parse.quote(json.dumps(router_state)),
                 "x-chrome-id-consistency-request": f"version=1,client_id={tabbit_cfg.get('client_id','')},device_id=test,sync_account_id={USER_ID},signin_mode=all_accounts,signout_mode=show_confirmation"}
            r = await client.get(f"{BASE}/chat/new", params={"_rsc":"auto"}, headers=h, cookies=COOKIES, follow_redirects=True)
            m = re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", r.text)
            if m:
                sid = m.group(0)
                # 用 display_name 发
                for model_val in [display, glm.get("name"), "GLM-5.1"]:
                    body = {"chat_session_id": sid, "message_id": None, "content": "hi",
                            "selected_model": model_val, "parallel_group_id": None, "task_name": "chat",
                            "agent_mode": False, "metadatas": {"html_content": "<p>hi</p>"}, "references": [],
                            "entity": {"key": hashlib.md5(b"").hexdigest(), "extras": {"type":"tab","url":""}}}
                    sh = {"x-req-ctx": x_req_ctx, "unique-uuid": str(uuid.uuid4()), "accept":"text/event-stream","content-type":"application/json",
                          "x-chrome-id-consistency-request": f"version=1,client_id={tabbit_cfg.get('client_id','')},device_id=test,sync_account_id={USER_ID},signin_mode=all_accounts,signout_mode=show_confirmation"}
                    try:
                        async with client.stream("POST", f"{BASE}/api/v1/chat/completion", json=body, headers=sh, cookies=COOKIES, timeout=30) as resp:
                            got=False;err="";txt=""
                            async for line in resp.aiter_lines():
                                if line.startswith("data:"):
                                    try:
                                        d=json.loads(line[5:].strip())
                                        if d.get("code"): err=f"code={d['code']}"
                                        if d.get("content"): txt+=d["content"]
                                    except: pass
                                if "message_chunk" in line: got=True
                            print(f"    selected_model={model_val:20} -> {'✅'+txt[:30] if got else (err or '空')}")
                    except Exception as e:
                        print(f"    selected_model={model_val:20} -> EXC {e}")

asyncio.run(main())
