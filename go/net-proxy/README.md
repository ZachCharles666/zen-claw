# go-net-proxy (prototype)

Minimal outbound network proxy for nano-claw web tools.

## Features

- `POST /v1/fetch` fetches URL content
- `POST /v1/search` calls Brave Search API through policy-controlled proxy
- HTTP/HTTPS only
- Domain allow/deny policy
- Redirect limit and redirect policy enforcement
- Response size cap (`max_bytes`)
- Structured audit logs with `X-Trace-Id`

## Run

```bash
cd go/net-proxy
go run .
```

Default bind: `127.0.0.1:4499`

## Environment variables

- `NET_PROXY_BIND` (default `127.0.0.1:4499`)
- `NET_PROXY_ALLOW_DOMAINS` (comma-separated allowlist; empty means allow all)
- `NET_PROXY_DENY_DOMAINS` (comma-separated denylist)
- `NET_PROXY_MAX_BODY_BYTES` (default `200000`)
- `NET_PROXY_TIMEOUT_SEC` (default `20`)
- `NET_PROXY_MAX_REDIRECTS` (default `5`)
- `NET_PROXY_SEARCH_BASE_URL` (default `https://api.search.brave.com/res/v1/web/search`)

