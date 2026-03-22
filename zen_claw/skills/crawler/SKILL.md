# crawler

Use this skill when the user needs repeatable web content collection from a fixed source URL.

## Inputs to collect

- source name
- source url
- target notebook
- whether browser rendering is required
- optional CSS selector
- crawl frequency

## Working style

1. Prefer direct HTTP extraction for simple static pages.
2. Switch to browser-backed extraction when the page requires rendering or a selector-based slice.
3. Preserve the original URL as the document source for downstream RAG and audit trails.
4. Attach stable metadata so later retention and filtering stay usable.
5. Treat scheduled crawling as an operator action and keep audit output available.

## Output shape

- extracted document count
- chunks added
- target notebook
- source url
- extraction mode
