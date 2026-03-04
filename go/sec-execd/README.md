# go-sec-execd (prototype)

Minimal secure execution sidecar for nano-claw.

## Features

- HTTP API: `POST /v1/exec`
- Session API: `POST /v1/sessions/start`, `GET /v1/sessions`, `GET /v1/sessions/{id}`, `POST /v1/sessions/{id}/kill`, `GET /v1/sessions/{id}/read`, `POST /v1/sessions/{id}/write`, `POST /v1/sessions/{id}/signal`, `POST /v1/sessions/{id}/resize`
- Optional PTY mode for sessions (`pty=true` on start request; uses `script -q -c ...` on supported hosts)
- Command timeout with hard cap
- Dangerous command denylist
- Workspace boundary check (`working_dir` must stay inside workspace)
- Approval hook via `X-Approval-Token`
- Optional request-scoped approval via HMAC signature (recommended)
- Output truncation

## Run

```bash
cd go/sec-execd
go run .
```

Default bind: `127.0.0.1:4488`

## Environment variables

- `SEC_EXECD_BIND` (default `127.0.0.1:4488`)
- `SEC_EXECD_WORKSPACE` (default current directory)
- `SEC_EXECD_REQUIRE_APPROVAL` (default `true`)
- `SEC_EXECD_APPROVAL_TOKEN` (required when approval enabled)
- `SEC_EXECD_APPROVAL_SECRET` (if set, token is ignored and HMAC approval is required)
- `SEC_EXECD_DEFAULT_TIMEOUT_SEC` (default `30`)
- `SEC_EXECD_MAX_TIMEOUT_SEC` (default `120`)
- `SEC_EXECD_MAX_OUTPUT_BYTES` (default `10000`)
- `SEC_EXECD_SESSION_RETENTION_SEC` (default `1800`; completed sessions older than this are garbage-collected)

## Request example

```bash
curl -sS -X POST http://127.0.0.1:4488/v1/exec \
  -H "Content-Type: application/json" \
  -H "X-Approval-Token: local-dev-token" \
  -d '{"command":"echo hello","working_dir":".","timeout_seconds":10}'
```

## HMAC approval (recommended)

When `SEC_EXECD_APPROVAL_SECRET` is set, each request must include:
- `X-Trace-Id`
- `X-Approval-Timestamp` (unix seconds)
- `X-Approval-Signature` (hex HMAC-SHA256 over canonical string)

Canonical string:
`trace_id + "\\n" + ts + "\\n" + method + "\\n" + path + "\\n" + sha256(body)`

## Response shape

```json
{
  "ok": true,
  "stdout": "hello\r\n",
  "exit_code": 0,
  "duration_ms": 8
}
```

## Session output read example

```bash
curl -sS "http://127.0.0.1:4488/v1/sessions/s-1/read?cursor=0&max_bytes=2048" \
  -H "X-Approval-Token: local-dev-token"
```

## Session start with PTY example

```bash
curl -sS -X POST http://127.0.0.1:4488/v1/sessions/start \
  -H "Content-Type: application/json" \
  -H "X-Approval-Token: local-dev-token" \
  -d '{"command":"python -i","working_dir":".","timeout_seconds":120,"pty":true}'
```

If PTY is not available on the host, sidecar returns:
- `error_code`: `pty_unsupported`
- HTTP 400

## Session write example

```bash
curl -sS -X POST http://127.0.0.1:4488/v1/sessions/s-1/write \
  -H "Content-Type: application/json" \
  -H "X-Approval-Token: local-dev-token" \
  -d '{"input":"status\n"}'
```

## Session signal example

```bash
curl -sS -X POST http://127.0.0.1:4488/v1/sessions/s-1/signal \
  -H "Content-Type: application/json" \
  -H "X-Approval-Token: local-dev-token" \
  -d '{"signal":"interrupt"}'
```

## Session resize example (PTY sessions only)

```bash
curl -sS -X POST http://127.0.0.1:4488/v1/sessions/s-1/resize \
  -H "Content-Type: application/json" \
  -H "X-Approval-Token: local-dev-token" \
  -d '{"rows":40,"cols":120}'
```

