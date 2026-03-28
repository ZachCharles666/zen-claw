"""BM25-based semantic tool selector — C2.

Filters a tool definition list down to the *top_k* most relevant tools for a
given query, using a lightweight BM25 implementation that requires no external
dependencies.

Each tool is scored against the query using its ``name`` and ``description``
fields.  Tools are ranked by BM25 score and the top-k are returned.  When fewer
than top_k tools exist, all tools are returned unchanged.
"""

from __future__ import annotations

import math
import re
from typing import Any

# ── Tokeniser ─────────────────────────────────────────────────────────────────

def _tokenise(text: str) -> list[str]:
    """Lower-case, split on non-alphanumeric, strip empty tokens."""
    return [t for t in re.split(r"[^a-z0-9]+", text.lower()) if t]


def _doc_text(tool_def: dict[str, Any]) -> str:
    """Combine name and description into a single string for scoring."""
    name = str(tool_def.get("function", {}).get("name") or tool_def.get("name") or "")
    desc = str(
        tool_def.get("function", {}).get("description") or tool_def.get("description") or ""
    )
    return f"{name} {name} {desc}"  # double name for higher weight


# ── BM25 ──────────────────────────────────────────────────────────────────────

_K1 = 1.5
_B = 0.75


def _bm25_scores(
    corpus_tokens: list[list[str]],
    query_tokens: list[str],
) -> list[float]:
    """Return a BM25 score for each document in *corpus_tokens*."""
    corpus_size = len(corpus_tokens)
    if corpus_size == 0:
        return []

    avg_dl = sum(len(d) for d in corpus_tokens) / corpus_size

    # Build IDF: how many documents contain each query term
    df: dict[str, int] = {}
    for tokens in corpus_tokens:
        seen = set(tokens)
        for qt in query_tokens:
            if qt in seen:
                df[qt] = df.get(qt, 0) + 1

    scores: list[float] = []
    for doc_tokens in corpus_tokens:
        dl = len(doc_tokens)
        tf_map: dict[str, int] = {}
        for t in doc_tokens:
            tf_map[t] = tf_map.get(t, 0) + 1

        score = 0.0
        for qt in query_tokens:
            tf = tf_map.get(qt, 0)
            df_t = df.get(qt, 0)
            if df_t == 0:
                continue
            idf = math.log((corpus_size - df_t + 0.5) / (df_t + 0.5) + 1)
            tf_norm = (tf * (_K1 + 1)) / (tf + _K1 * (1 - _B + _B * dl / avg_dl))
            score += idf * tf_norm
        scores.append(score)

    return scores


# ── Public API ────────────────────────────────────────────────────────────────

def bm25_select(
    tools: list[dict[str, Any]],
    query: str,
    *,
    top_k: int = 10,
) -> list[dict[str, Any]]:
    """Return the *top_k* most query-relevant tools from *tools*.

    If ``len(tools) <= top_k`` the original list is returned unchanged.
    The relative order of returned tools mirrors the original list order
    (not BM25 rank), to preserve deterministic behaviour for the LLM.

    Parameters
    ----------
    tools:
        List of OpenAI-format tool definitions (``{"type": "function", ...}``).
    query:
        The user message or a short query hint describing what the user wants.
    top_k:
        Maximum number of tools to return.
    """
    if len(tools) <= top_k:
        return tools

    query_tokens = _tokenise(query)
    if not query_tokens:
        return tools[:top_k]

    corpus = [_tokenise(_doc_text(t)) for t in tools]
    scores = _bm25_scores(corpus, query_tokens)

    # Pick indices of top-k by score
    ranked = sorted(range(len(tools)), key=lambda i: scores[i], reverse=True)
    top_indices = set(ranked[:top_k])

    # Return in original order
    return [t for i, t in enumerate(tools) if i in top_indices]
