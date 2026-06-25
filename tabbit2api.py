#!/usr/bin/env python3
import time
import logging
from pathlib import Path
from collections import defaultdict
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles

from core.config import ConfigManager
from core.token_manager import TokenManager
from core.log_store import LogStore
from core.model_registry import init_registry, get_registry
from routes import openai_compat, admin_api, claude_api

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("tabbit2openai")

# ── 初始化核心组件 ──
cfg = ConfigManager()
token_manager = TokenManager(cfg)
log_store = LogStore(max_entries=cfg.get("logging", "max_entries", default=500))
# 动态模型注册表（从上游拉取真实模型清单）
# 取第一个可用 token 用于认证拉取（/proxy 接口需要 JWT cookie）
_tokens = cfg.get("tokens", default=[]) or []
_first_token = next((t["value"] for t in _tokens if t.get("enabled", True)), None)
model_registry = init_registry(
    cfg.get("tabbit", "base_url", default="https://web.tabbit.ai"),
    verify_ssl=cfg.get("tabbit", "verify_ssl", default=False),
    token_str=_first_token,
)

# ── 初始化路由模块 ──
openai_compat.init(token_manager, cfg, log_store)
admin_api.init(cfg, token_manager, log_store)
claude_api.init(token_manager, cfg, log_store)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(
        "Tabbit2API started — tokens: %d, port: %d",
        len(cfg.get("tokens", default=[])),
        cfg.get("server", "port", default=8800),
    )

    # 启动时检查 API Key 设置
    api_key = cfg.get("proxy", "api_key")
    if not api_key:
        logger.warning("=" * 60)
        logger.warning("⚠️  proxy.api_key 未设置，API 端点无需认证！")
        logger.warning("⚠️  任何请求都能调用 API，存在安全风险")
        logger.warning("⚠️  请在管理面板 Settings 中设置 API Key")
        logger.warning("=" * 60)

    # 启动时拉取动态模型清单（带重试，失败也不阻塞启动）。
    # 注意：不再用静态 MODEL_MAP 兜底 /v1/models——旧清单会误导第三方平台。
    # 拉取失败时返回明确错误，让用户在管理 UI 手动刷新。
    await model_registry.refresh_with_retry()
    yield
    await token_manager.close_all()


app = FastAPI(lifespan=lifespan)

# ── CORS 中间件 ──
# 管理面板和 API 端点都在同源，不需要开放跨域。
# 如需跨域（如前端部署在不同域名），在 config.json 中设置 cors_origins 列表。
_cors_origins = cfg.get("cors_origins", default=None)
if _cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

# ── 安全中间件 ──

# 请求体大小限制（10MB）
MAX_BODY_SIZE = 10 * 1024 * 1024

# 速率限制配置
RATE_LIMIT_LOGIN = 5        # 登录：5 次尝试
RATE_LIMIT_LOGIN_WINDOW = 900  # 15 分钟窗口
RATE_LIMIT_API = 60         # API：60 次请求
RATE_LIMIT_API_WINDOW = 60  # 1 分钟窗口

# 速率限制存储（内存，生产环境建议用 Redis）
login_attempts: dict[str, list[float]] = defaultdict(list)
api_requests: dict[str, list[float]] = defaultdict(list)


@app.middleware("http")
async def security_middleware(request: Request, call_next):
    """安全中间件：请求体大小限制 + 速率限制"""
    # 优先从反向代理头取真实 IP（nginx: proxy_set_header X-Real-IP $remote_addr）
    client_ip = (
        request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        or request.headers.get("x-real-ip", "").strip()
        or (request.client.host if request.client else "unknown")
    )
    now = time.time()

    # 1. 请求体大小限制
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > MAX_BODY_SIZE:
        return JSONResponse(
            status_code=413,
            content={"detail": "请求体过大，最大允许 10MB"}
        )

    # 2. 登录接口速率限制
    if request.url.path == "/api/admin/login" and request.method == "POST":
        login_attempts[client_ip] = [
            t for t in login_attempts[client_ip]
            if now - t < RATE_LIMIT_LOGIN_WINDOW
        ]
        if len(login_attempts[client_ip]) >= RATE_LIMIT_LOGIN:
            return JSONResponse(
                status_code=429,
                content={"detail": "登录尝试过多，请15分钟后重试"}
            )
        login_attempts[client_ip].append(now)

    # 3. API 接口速率限制
    elif request.url.path.startswith("/v1/"):
        api_requests[client_ip] = [
            t for t in api_requests[client_ip]
            if now - t < RATE_LIMIT_API_WINDOW
        ]
        if len(api_requests[client_ip]) >= RATE_LIMIT_API:
            return JSONResponse(
                status_code=429,
                content={"detail": "请求过于频繁，请稍后重试"}
            )
        api_requests[client_ip].append(now)

    return await call_next(request)


# ── 挂载路由 ──
app.include_router(claude_api.router)  # Claude Messages API（/v1/messages）
app.include_router(openai_compat.router)  # OpenAI 兼容（/v1/chat/completions）
app.include_router(admin_api.router)

# ── 静态文件 & 管理面板入口 ──
static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.get("/admin")
async def admin_page():
    return FileResponse(str(static_dir / "index.html"))


@app.get("/health")
async def health():
    """轻量健康检查（无需鉴权，供 Docker healthcheck / 监控用）

    只检查服务是否存活 + 核心组件是否初始化。
    深度诊断走 /api/admin/diagnose（需鉴权）。
    """
    from core.model_registry import get_registry
    registry = get_registry()
    return {
        "status": "ok",
        "tokens": len(cfg.get("tokens", default=[])),
        "model_registry_ready": bool(registry and registry.ready),
    }


if __name__ == "__main__":
    import urllib3

    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    uvicorn.run(
        app,
        host=cfg.get("server", "host", default="0.0.0.0"),
        port=cfg.get("server", "port", default=8800),
    )
