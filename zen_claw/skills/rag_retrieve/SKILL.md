# rag_retrieve

Use this skill when the answer should be grounded in local knowledge or uploaded reference material.

## Retrieval workflow

1. Restate the query in the narrowest form possible.
2. Prefer local notebooks, uploaded files, and workspace material before generic reasoning.
3. Quote or summarize only what the retrieved material supports.
4. Separate retrieved facts from model inference.
5. If retrieval quality is weak, say that directly and ask for better source material.

## Inputs to collect

- query
- notebook or corpus hint
- top_k
- metadata filters

## Output shape

- retrieved evidence bullets
- source identifiers or file hints
- final grounded answer
- explicit uncertainty note when evidence is thin
