# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Tabbit2API — an unofficial API adapter that translates Tabbit browser's internal API into OpenAI-compatible (`/v1/chat/completions`) and Anthropic Claude-compatible (`/v1/messages`) endpoints. Third-party apps that speak OpenAI or Claude protocol can use Tabbit's models transparently.

## Commands

### Run locally
```bash
pip install -r requirements.txt
cp config.json.example data/config.json
uvicorn tabbit2api:app --host 0.0.0.0 --port 8800
```

### Run with Docker
```bash
docker compose up -d
docker compose logs -f        # first-run password printed to console
```

### Lint / Type check
No linter or type checker is configured in this project.

### Tests
No test suite exists.

## Architecture

### Protocol Translation Flow

```
Client (OpenAI/Claude format)
  → routes/openai_compat.py  or  routes/claude_api.py
    → core/claude_compat.py   (format conversion + tool aliasing)
    → core/token_manager.py   (round-robin token selection)
    → core/tabbit_client.py   (upstream HTTP + SSE streaming)
  ← ToolifyParser → ClaudeSSEWriter / OpenAISSEWriter
Client (SSE stream)
```

### Core Modules (`core/`)

- **config.py** — `ConfigManager` singleton. Loads/saves `data/config.json`, deep-merges with `DEFAULT_CONFIG`. Generates random admin password + JWT secret on first run. Migrates legacy domains and password hashes.
- **auth.py** — JWT creation/verification (HS256, 24h expiry). Backward-compatible with legacy SHA-256+salt passwords.
- **tabbit_client.py** — Async HTTP client to Tabbit upstream. Token format: `jwt_token|next_auth_session_token|device_id`. Handles server time sync for `unique-uuid` validation. Creates chat sessions via RSC protocol, sends messages via SSE.
- **token_manager.py** — Round-robin token pool with circuit breaker (3 consecutive errors → 5-min cooldown). Lazy `TabbitClient` per token.
- **model_registry.py** — Fetches model list from upstream on startup, caches 1h. Stale cache extended 5min on fetch failure. `/v1/models` returns 503 if registry unavailable.
- **log_store.py** — Bounded deque (500 entries) of request logs with query/pagination.
- **claude_compat.py** — The heart of the system (1100+ lines). Converts Anthropic Messages API ↔ Tabbit plain-text protocol. Contains `ToolifyParser` (streaming state machine), `ClaudeSSEWriter`/`OpenAISSEWriter`, content overflow handling, and tool name aliasing.

### Route Modules (`routes/`)

- **admin_api.py** — JWT-protected `/api/admin/*` endpoints: login, token CRUD, settings, diagnostics, model refresh, Google OAuth.
- **claude_api.py** — `POST /v1/messages` (Anthropic compatible). Model resolution via `CLAUDE_MODEL_MAP` + dynamic registry.
- **openai_compat.py** — `POST /v1/chat/completions` (OpenAI compatible). Tool normalization and argument repair.

### Deferred Init Pattern

Route modules create empty routers at import time. `tabbit2api.py` calls `init()` on each to inject shared singletons (`ConfigManager`, `TokenManager`, `LogStore`).

## Critical Constraints

### 20K Character Limit
Tabbit gateway enforces ~20,421 character limit on `content` field (HTTP 492 error). Mitigation:
- `build_content_with_refs()` — splits old history into `references` channel (bypasses limit, verified to 70K+)
- `compress_content()` — hard compression fallback, preserves tool call protocol
- Critical instructions (containing preference keywords like "must", "always") are force-pinned to `content`

### Tool Name Aliasing
Tabbit models have native tools (Write, Read, Edit, Bash). If injected tool names collide, the model uses native channel instead of `<<CALL>>` text protocol. Solution: all tools prefixed with `cc_` (e.g., `Write` → `cc_Write`), restored after parsing via `name_map`.

### Server Time Sync
`unique-uuid` embeds server timestamps at fixed hex positions. Clock drift causes validation failures. `_sync_server_time` reads upstream `Date` header to compute offset.

## Configuration

Config lives at `data/config.json` (bind-mounted in Docker). See `config.json.example` for schema. Key sections:
- `admin` — password_hash, salt, jwt_secret (auto-generated on first run)
- `tabbit` — base_url, client_id
- `tokens` — array of token objects (pipe-separated value format)
- `proxy` — optional api_key for client auth, system_prompt
- `claude` — default_model, system_prompt

## Admin Panel

- URL: `http://localhost:8800/admin`
- First-run password printed to Docker console on startup
- Diagnostics endpoint: `GET /api/admin/diagnose` — comprehensive self-check (config, connectivity, version sync, token health, server time sync)

## Scripts (`scripts/`)

Standalone diagnostic/research tools (require `mitmproxy` for capture scripts):
- `probe_context_limit.py` — binary-searches exact char limit per model
- `probe_bypass.py` / `probe_bypass_verify.py` — tests 20K limit bypass channels
- `probe_system_prompt.py` — compares system prompt injection strategies
- `capture_*.py` / `eval_capture.py` — mitmdump scripts for traffic analysis
