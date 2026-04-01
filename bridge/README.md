# WhatsApp Bridge Runbook

This bridge is an optional Node-side component used by the WhatsApp channel path. It is not part of the Python mainline Alpha baseline, and it should be operated as a locked, reproducible sidecar.

## Scope

- Purpose: provide the external WhatsApp transport bridge used by the repository's WhatsApp channel integration
- Runtime: Node.js `>=20`
- Supply-chain rule: always install from the committed lockfile with `npm ci`
- Maturity boundary: bridge support remains below the Python mainline because it depends on an external Node runtime and still carries a separate operational surface from Python-only channels

## Prerequisites

- Node.js 20 or later
- npm available on `PATH`
- A working copy that includes:
  - [`package.json`](/E:/nano-claw-public/bridge/package.json)
  - [`package-lock.json`](/E:/nano-claw-public/bridge/package-lock.json)

## Controlled Install

Run from [bridge/](/E:/nano-claw-public/bridge):

```powershell
npm ci
npm run build
```

Do not use `npm install` as the normal release or acceptance path. The CLI bridge bootstrap now expects the lockfile to exist and uses `npm ci`.

## Start

```powershell
npm start
```

This starts the compiled bridge from `dist/index.js`.

## Minimum Smoke Test

Use this as the minimum operator acceptance checklist after install or upgrade:

1. Run `npm ci`.
2. Run `npm run build`.
3. Confirm `dist/index.js` exists.
4. Run `npm start`.
5. Confirm the process starts without dependency resolution or TypeScript build errors.
6. For channel login flows, confirm the QR-based startup path can be reached from `zen-claw channels login`.

For the full real-device acceptance checklist and record template, use [docs/whatsapp-bridge-smoke-checklist.md](/E:/nano-claw-public/docs/whatsapp-bridge-smoke-checklist.md).

## Runtime Status Signals

The bridge now emits a small set of operator-facing status values that the Python channel can observe:

- `starting`
- `qr_required`
- `connected`
- `disconnected`
- `reconnecting`
- `audio_download_failed`

These are intended for diagnosis and smoke verification, not as a full monitoring protocol.

## Upgrade Rules

- Update direct dependencies intentionally, not through floating ranges.
- Regenerate [`package-lock.json`](/E:/nano-claw-public/bridge/package-lock.json) in the repository when changing dependencies.
- Re-run the smoke test above after any dependency or runtime change.

## Known Gaps

- Voice transcription now depends on the bridge being able to persist incoming audio locally and on `GROQ_API_KEY` being available to the Python channel runtime.
- The bridge depends on an external Node runtime and therefore carries a separate operational surface from the Python mainline.
- Independent real-device smoke and integration acceptance is still required after dependency or runtime upgrades.
- The new status signals are intentionally minimal; if future operator needs grow, this should evolve into a more explicit bridge health contract.
