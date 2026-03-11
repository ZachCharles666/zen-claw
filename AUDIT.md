# zen-claw Capability Audit

**Date**: 2026-03-11
**Version**: 0.1.3.post5
**License**: MIT

## Overview

zen-claw is a light, safe personal AI assistant framework written in Python 3.11+ with Go and Node.js sidecar components. It provides operational visibility, multi-turn conversational AI, security hardening, knowledge management, and distributed node support.

---

## 1. Gateway & Dashboard

### Dashboard Server (`zen_claw/dashboard/server.py`)
- **Framework**: FastAPI (79.5 KB)
- Snapshot-based dashboard rendering with OpenAPI/Swagger docs
- Audit logging in JSONL format
- Cron job management, webhook routing, knowledge management integration
- Node task queue monitoring, rate limiting statistics, approval chain integrity checks

### Streaming

| Protocol | Endpoint | Format |
|----------|----------|--------|
| WebSocket | `/chat/ws/{session_id}` | `{"type":"token","content":"..."}` / `{"type":"done"}` / `{"type":"error"}` |
| SSE | `/api/v1/agent/invoke/stream` | `data: {"token":"..."}\n\n` ending with `data: [DONE]\n\n` |

### REST API (`/api/v1/`)

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| GET | `/health` | None | Health check |
| GET | `/info` | None | Version, model, capabilities |
| POST | `/agent/invoke` | `X-API-Key` | Synchronous agent invocation |
| POST | `/agent/invoke/stream` | `X-API-Key` | SSE streaming invocation |
| POST | `/chat/upload` | — | File upload for chat/RAG |

### Webhook Endpoints (`zen_claw/dashboard/webhooks.py`)

- WeChat verification and message handling (signature-based, encryption support)
- WeCom verification and message handling
- DingTalk webhook integration
- Slack event handler (challenge verification)
- Generic workflow trigger (`/webhook/trigger/{agent_id}`) with trace ID, payload validation, and audit logging

### Chat UI (`zen_claw/dashboard/static/chat.html`)
- Markdown rendering with syntax highlighting (marked.js, highlight.js)
- Real-time WebSocket streaming, voice recording (MediaRecorder API, webm)
- File upload with progress, typing indicator, command autocomplete
- Dark theme with glassmorphism design

---

## 2. Tunnel & Gateway Security

### Gateway Security (`zen_claw/tunnel/gateway.py`)
- **Class**: `TunnelGatewaySecurity`
- Path constraints: only `/webhook/*`, POST only
- Payload size limits: 2 MB default
- HMAC-SHA256 signature verification with key rotation
- Clock drift tolerance: 5-minute window
- Nonce replay detection: 1-hour TTL, 10,000 capacity, auto-eviction at 90%
- Circuit breaker DoS protection: IP-based blacklisting (100 hits threshold)

**Required Headers**: `x-claw-signature`, `x-claw-timestamp`, `x-claw-nonce`, `x-claw-key-id`
**Signature Payload**: `"timestamp.nonce." + binary_body`

### Tunnel Manager (`zen_claw/tunnel/manager.py`)
- Manages cloudflared subprocess lifecycle (named tunnel or quick tunnel mode)
- Graceful shutdown: SIGTERM with 5s timeout, then SIGKILL
- Restart metrics tracking, TryCloudflare URL parsing

---

## 3. Go Sidecar Modules

### sec-execd (`go/sec-execd/main.go`, 61 KB)
Minimal secure execution sidecar on `127.0.0.1:4488`.

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/v1/exec` | POST | Execute command, return result |
| `/v1/sessions/start` | POST | Start long-running session |
| `/v1/sessions` | GET | List sessions |
| `/v1/sessions/{id}` | GET | Session status |
| `/v1/sessions/{id}/kill` | POST | Terminate session |
| `/v1/sessions/{id}/read` | GET | Read output (cursor/max_bytes) |
| `/v1/sessions/{id}/write` | POST | Send input |
| `/v1/sessions/{id}/signal` | POST | Send signal |
| `/v1/sessions/{id}/resize` | POST | Resize PTY |

**Security**: workspace boundary enforcement, dangerous command denylist, output truncation (10 KB default), approval token or HMAC-based approval, session GC (1800s default).

**Config Env Vars**: `SEC_EXECD_WORKSPACE`, `SEC_EXECD_REQUIRE_APPROVAL`, `SEC_EXECD_APPROVAL_TOKEN`, `SEC_EXECD_APPROVAL_SECRET`, `SEC_EXECD_DEFAULT_TIMEOUT_SEC` (30), `SEC_EXECD_MAX_TIMEOUT_SEC` (120), `SEC_EXECD_MAX_OUTPUT_BYTES` (10000), `SEC_EXECD_SESSION_RETENTION_SEC` (1800).

### net-proxy (`go/net-proxy/main.go`)
Minimal outbound network proxy on `127.0.0.1:4499`.

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/v1/fetch` | POST | Fetch URL content |
| `/v1/search` | POST | Brave Search API proxy |
| `/healthz` | GET | Health check |

**Security**: domain allow/deny policies, HTTP/HTTPS only, redirect limit (5), response size cap (200 KB), audit logging with `X-Trace-Id`.

**Config Env Vars**: `NET_PROXY_ALLOW_DOMAINS`, `NET_PROXY_DENY_DOMAINS`, `NET_PROXY_MAX_BODY_BYTES` (200000), `NET_PROXY_TIMEOUT_SEC` (20), `NET_PROXY_MAX_REDIRECTS` (5).

---

## 4. Browser Sidecar

### Server (`browser/sidecar/server.js`)
- **Runtime**: Node.js 18+ with Playwright (chromium)
- **Bind**: `127.0.0.1:4500`
- **API**: `POST /v1/browser` (single JSON endpoint)
- **Auth**: `X-Approval-Token` header (optional)
- **Actions**: open, click, scroll, type, screenshot, evaluate JS, extract text
- **Policy**: per-request `allowed_domains` / `blocked_domains`, `max_steps` (20)
- **Sessions**: UUID-based, configurable timeout (30s default)

---

## 5. Infrastructure & Deployment

### Docker (`Dockerfile`)
- **Stage 1** (Go builder): `golang:1.22-bookworm` — builds `sec-execd` and `net-proxy` (CGO_ENABLED=0, stripped, linux/amd64)
- **Stage 2** (Runtime): `ghcr.io/astral-sh/uv:python3.12-bookworm-slim` — Python 3.12, Node.js 20
- **Port**: 18790
- **Entrypoint**: `zen-claw status`

### CI (`/.github/workflows/ci.yml`)
- Windows-latest, Python 3.11, sharded across 4 runners
- Test suites: core (4 shards), memory recall (dedicated), channel matrix (webchat, slack, signal, matrix)
- LOC gate tracking
- Triggers: push to main, PRs to main

### Nightly Integration (`/.github/workflows/nightly-integration.yml`)
- Daily UTC 00:00 + manual dispatch
- Scripts: `selftest_local.ps1`, `perf_baseline.ps1`, `perf_report.ps1`

---

## 6. Core Dependencies

| Package | Version | Purpose |
|---------|---------|---------|
| typer | >=0.9.0 | CLI framework |
| litellm | >=1.81.10,<2.0.0 | LLM provider abstraction |
| pydantic | >=2.0.0 | Data validation |
| websockets | >=12.0 | WebSocket server/client |
| httpx[socks] | >=0.25.0 | Async HTTP with SOCKS proxy |
| loguru | >=0.7.0 | Structured logging |
| croniter | >=2.0.0 | Cron expression parsing |
| python-telegram-bot[socks] | >=21.0 | Telegram integration |
| lark-oapi | >=1.0.0 | Lark/DingTalk SDK |
| cryptography | >=42.0.0 | Encryption |
| tenacity | >=8.2.0 | Retry logic |

**Optional**: `rag` (chromadb, sentence-transformers, pymupdf, bm25), `tts` (edge-tts), `multitenant` (PyJWT, bcrypt), `dev` (pytest, ruff).

---

## 7. Test Coverage

| Test File | Focus |
|-----------|-------|
| `test_tunnel_gateway.py` | HMAC validation, circuit breaker, nonce replay |
| `test_sandbox_gateway.py` | Path traversal prevention, env sanitization |
| `test_dashboard_snapshot.py` | Cron, rate limiting, node state, approval chains, knowledge, observability |
| `test_dashboard_cli.py` | CLI args, host binding restrictions |
| `test_dashboard_cron_audit.py` | Cron audit trail, JSONL persistence, trace IDs |

---

## 8. Architectural Patterns

1. **Dual Streaming**: WebSocket (bidirectional) + SSE (unidirectional)
2. **Defense in Depth**: HMAC signatures, nonce replay detection, DoS circuit breaker, workspace sandboxing, domain policies
3. **Sidecar Architecture**: Go binaries for exec/network isolation, Node.js for browser automation
4. **Observability**: JSONL audit logs, event aggregation (compression, routing, workflow), metrics collection
5. **Process Management**: Cloudflared tunnel lifecycle, sidecar supervisor autodiscovery via `bin/` directory
