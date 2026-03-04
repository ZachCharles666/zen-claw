# Security Policy

This project includes runtime controls for tool execution, channel access, and sidecar isolation.

## Supported Version

Current active development branch is `main`.

## Reporting a Vulnerability

Report security issues privately through GitHub Security Advisories for this repository.
If private reporting is unavailable, open a minimal issue without sensitive exploit details and request a private contact channel.

Please include:

- affected commit or branch
- environment (OS, Python version, deployment mode)
- exact reproduction steps
- expected behavior and observed behavior
- impact assessment

## Security Controls in nano-claw

- Tool policy engine with layered allow/deny scopes:
- `agent`
- `subagent`
- `channel` overlays

- Default deny-sensitive behavior:
- `exec` and `spawn` are deny-by-default unless policy allows.

- Subagent hard guardrail:
- high-risk tools remain denied to subagents unless explicitly configured.

- Skill permission gate:
- supports `off`, `warn`, `enforce`.
- in production hardening mode, loaded skills are enforced.

- Channel RBAC:
- per-channel `admins` and `users` lists.
- global `allowFrom` and `denyFrom` are also supported.

- Outbound rate limiting:
- token bucket per channel/chat.
- modes: `delay` or `drop`.
- optional drop notice with cooldown.

- Sidecar hardening:
- exec sidecar: `go/sec-execd`
- network sidecar: `go/net-proxy`
- optional local supervisor with restart backoff and circuit-breaker.

- Path and workspace protections:
- optional `restrictToWorkspace` for file and command operations.

## Hardening Recommendations

Use these settings for production-like deployments:

- `tools.policy.productionHardening = true`
- configure `tools.network.*` as canonical config
- keep sidecar local fallback disabled
- keep `allowSubagentSensitiveTools = false`
- enable channel RBAC (`admins`/`users`)
- enable rate limiting per channel
- run the release gate before deployment

## Security Validation

Recommended pre-release checks:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\selftest_local.ps1 -FailOnHighRiskConfig -FailOnLegacyConfig -FailOnInvalidSkillManifest
powershell -ExecutionPolicy Bypass -File .\scripts\release_gate.ps1
```

## Scope Notes

- This project is intended for self-hosted operation.
- External channel services and model providers are outside this repository's trust boundary.
- Protect API keys in `~/.nano-claw/config.json` and never commit secrets.
