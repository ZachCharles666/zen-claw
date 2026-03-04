---
name: summarize
description: Summarize or extract text/transcripts from URLs, podcasts, and local files (great fallback for "transcribe this YouTube/video").
homepage: https://summarize.sh
metadata: {"zen-claw":{"emoji":"receipt","requires":{"bins":["summarize"]},"install":[{"id":"brew","kind":"brew","formula":"steipete/tap/summarize","bins":["summarize"],"label":"Install summarize (brew)"}]}}
---

# Summarize

Fast CLI to summarize URLs, local files, and YouTube links.

## When to use (trigger phrases)

Use this skill immediately when the user asks any of:
- "use summarize.sh"
- "what is this link/video about?"
- "summarize this URL/article"
- "transcribe this YouTube/video" (best-effort transcript extraction; no `yt-dlp` needed)

## Quick start

```bash
summarize "https://example.com" --model google/gemini-3-flash-preview
summarize "/path/to/file.pdf" --model google/gemini-3-flash-preview
summarize "https://youtu.be/dQw4w9WgXcQ" --youtube auto
```

## YouTube: summary vs transcript

Best-effort transcript (URLs only):

```bash
summarize "https://youtu.be/dQw4w9WgXcQ" --youtube auto --extract-only
```

If the user asked for a transcript but it is huge, return a tight summary first, then ask which section or time range to expand.

## Model + keys

Set the API key for your chosen provider:

```bash
export OPENROUTER_API_KEY=...
# or provider-specific key supported by summarize
```

## Operational guidance

- Prefer URL/file input over raw pasted long text.
- If extraction fails, explain failure briefly and offer a retry path.
- Keep outputs concise unless user explicitly asks for a detailed breakdown.

## Safety and quality

- Do not fabricate transcript lines.
- Label best-effort extraction clearly.
- For copyrighted content, provide concise summaries unless user has rights and requests more detail.

## Optional services

- `FIRECRAWL_API_KEY` for blocked sites
- `APIFY_API_TOKEN` for YouTube fallback
