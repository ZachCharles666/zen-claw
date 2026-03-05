import json
import math
import re
import sqlite3
from pathlib import Path
from typing import Any

from loguru import logger

from zen_claw.agent.memory_recall import MemoryRecallStrategy


class SqliteMemoryIndex:
    """SQLite-based index for memory, combining FTS5 keyword search and vector embeddings."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._embedder = None
        self._embedder_loaded = False
        self._init_db()

    def _get_embedder(self) -> Any:
        if not self._embedder_loaded:
            self._embedder_loaded = True
            try:
                from zen_claw.knowledge.embedder import LocalEmbedder

                self._embedder = LocalEmbedder()
                logger.info("SqliteMemoryIndex: Loaded LocalEmbedder for vector search.")
            except ImportError:
                logger.debug(
                    "SqliteMemoryIndex: LocalEmbedder dependencies not found. Falling back to FTS5 only."
                )
        return self._embedder

    def _init_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("""
            CREATE TABLE IF NOT EXISTS memory_embeddings (
                content TEXT PRIMARY KEY,
                embedding TEXT
            )
        """)
        # Create FTS5 virtual table for keyword search
        conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
                content,
                tokenize='unicode61'
            )
        """)
        conn.commit()
        conn.close()

    def sync_and_search(
        self, query: str, candidates: list[str], top_k: int = 8
    ) -> list[tuple[float, str]]:
        """
        Synchronize memory candidates to DB, compute missing embeddings,
        and return top-k hybrid search results.
        """
        if not candidates or not query:
            return []

        conn = sqlite3.connect(str(self.db_path))
        try:
            cur = conn.cursor()

            # 1. Sync FTS5 table atomically — DELETE and INSERT in a single transaction
            #    so a crash between the two never leaves the index empty.
            with conn:
                cur.execute("DELETE FROM memory_fts")
                cur.executemany(
                    "INSERT INTO memory_fts (content) VALUES (?)",
                    [(c,) for c in candidates],
                )

            # 2. Find missing embeddings
            cur.execute("SELECT content FROM memory_embeddings")
            existing_set = {row[0] for row in cur.fetchall()}
            missing = [c for c in candidates if c not in existing_set]

            embedder = self._get_embedder()
            embs_dict: dict[str, list[float]] = {}

            # Compute and insert missing embeddings in its own transaction
            if missing and embedder:
                try:
                    vectors = embedder.embed(missing)
                    records = [(c, json.dumps(v)) for c, v in zip(missing, vectors)]
                    with conn:
                        cur.executemany(
                            "INSERT INTO memory_embeddings (content, embedding) VALUES (?, ?)",
                            records,
                        )
                except Exception as e:
                    logger.warning(f"SqliteMemoryIndex: Failed to embed incoming memories: {e}")

            # 3. Load all embeddings for the current candidate set
            cur.execute("SELECT content, embedding FROM memory_embeddings")
            for c, emb_str in cur.fetchall():
                if c in candidates:
                    try:
                        embs_dict[c] = json.loads(emb_str)
                    except Exception:
                        pass

            # 4. Keyword search via FTS5
            fts_scores: dict[str, float] = {}
            try:
                # We wrap the query in quotes to avoid syntax errors with raw FTS queries
                clean_query = query.replace('"', '""')
                cur.execute(
                    "SELECT content, rank FROM memory_fts WHERE memory_fts MATCH ? ORDER BY rank LIMIT 50",
                    (f'"{clean_query}"*',),
                )
                for c, rank in cur.fetchall():
                    # SQLite fts5 rank is typically negative; more negative = better
                    fts_scores[c] = abs(1.0 / (1.1 + rank))
            except sqlite3.OperationalError:
                pass  # Invalid query syntax fallback
        finally:
            conn.close()

        # 5. Calculate hybrid scores
        query_vec = None
        if embedder:
            try:
                query_vec = embedder.embed([query])[0]
            except Exception:
                pass

        scored: list[tuple[float, str]] = []
        query_tokens = {tok for tok in re.findall(r"\w+", query.lower()) if len(tok) > 1}

        for c in candidates:
            score = 0.0

            # FTS keyword component
            if c in fts_scores:
                score += 1.0 + fts_scores[c]

            # Vector cosine similarity component
            if query_vec and c in embs_dict:
                v = embs_dict[c]
                dot = sum(a * b for a, b in zip(query_vec, v))
                mag1 = math.sqrt(sum(a * a for a in query_vec))
                mag2 = math.sqrt(sum(b * b for b in v))
                if mag1 > 0 and mag2 > 0:
                    sim = dot / (mag1 * mag2)
                    if sim > 0.4:
                        score += sim * 2.0  # Weight vector similarity higher

            # Naive fallback
            elif query.lower() in c.lower():
                score += 0.5
            else:
                # Fallback lexical overlap when vector/FTS cannot return useful signal.
                cand_tokens = {tok for tok in re.findall(r"\w+", c.lower()) if len(tok) > 1}
                if query_tokens and cand_tokens:
                    overlap = len(query_tokens & cand_tokens) / max(1, len(query_tokens))
                    if overlap > 0:
                        score += overlap

            if score > 0:
                scored.append((score, c))

        return sorted(scored, key=lambda x: x[0], reverse=True)[:top_k]

class SqliteVectorRecallStrategy(MemoryRecallStrategy):
    """Memory recall strategy utilizing SQLite FTS5 and Vector Embeddings."""

    def __init__(self, db_path: Path):
        self._index = SqliteMemoryIndex(db_path)

    def score(self, query: str, candidate: str) -> float:
        # Implementing the abstract method, but we prefer bulk_score for performance.
        res = self._index.sync_and_search(query, [candidate], top_k=1)
        if res:
            return res[0][0]
        return 0.0

    def bulk_score(self, query: str, candidates: list[str]) -> list[tuple[float, str]]:
        """Efficiently score a batch of candidates."""
        return self._index.sync_and_search(query, candidates, top_k=50)
