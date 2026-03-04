# browser-sidecar (prototype)

Minimal browser automation sidecar for nano-claw.

## Features

- `GET /healthz`
- `POST /v1/browser` with actions:
  - `open`
  - `extract`
  - `screenshot`
- Domain allow/deny enforcement
- Step budget per session
- Headless Chromium via Playwright

## Run

```bash
cd browser/sidecar
npm install
npm run start
```

Default bind: `127.0.0.1:4500`

## Environment variables

- `BROWSER_SIDECAR_BIND` (default `127.0.0.1:4500`)
- `BROWSER_SIDECAR_ALLOW_DOMAINS` (comma-separated allowlist; empty means allow all)
- `BROWSER_SIDECAR_DENY_DOMAINS` (comma-separated denylist)
- `BROWSER_SIDECAR_MAX_STEPS` (default `20`)
- `BROWSER_SIDECAR_TIMEOUT_SEC` (default `30`)

