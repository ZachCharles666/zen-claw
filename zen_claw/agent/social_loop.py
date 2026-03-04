"""Autonomous social participation loop."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from loguru import logger

from zen_claw.agent.memory import MemoryStore
from zen_claw.agent.tools.social_platform import (
    SocialPlatformGetTool,
    SocialPlatformLikeTool,
    SocialPlatformPostTool,
)
from zen_claw.providers.base import LLMProvider

_FILTER_SYSTEM = (
    "Select worthwhile post IDs for response. Reply with JSON array of IDs only, e.g. "
    '["1","2"] or [] if none.'
)
_COMPOSE_SYSTEM = "Write a concise, constructive reply (2-5 sentences)."


@dataclass
class SocialPlatformConfig:
    platform: str
    base_url: str
    submolt: str
    auth_header: str
    max_posts_per_cycle: int = 10
    dry_run: bool = False
    proxy_url: str = "http://127.0.0.1:4499/v1/fetch"
    system_prompt_override: str = ""


@dataclass
class SocialLoopResult:
    cycle_start: datetime
    posts_fetched: int = 0
    posts_filtered: int = 0
    responses_composed: int = 0
    responses_posted: int = 0
    errors: list[str] = field(default_factory=list)
    dry_run: bool = False

    @property
    def duration_sec(self) -> float:
        return (datetime.now(UTC) - self.cycle_start).total_seconds()


class SocialAgentLoop:
    """Specialized loop for social platform engagement."""

    def __init__(
        self,
        config: Any,
        platform_config: SocialPlatformConfig,
        provider: LLMProvider,
        workspace: Path,
        model: str | None = None,
    ):
        self._config = config
        self._pc = platform_config
        self._provider = provider
        self._workspace = Path(workspace)
        self._model = model or str(getattr(config.agents.defaults, "model", "") or provider.get_default_model())
        self._memory = MemoryStore(self._workspace)
        self._get_tool = SocialPlatformGetTool(proxy_url=platform_config.proxy_url)
        self._post_tool = SocialPlatformPostTool(proxy_url=platform_config.proxy_url)
        self._like_tool = SocialPlatformLikeTool(proxy_url=platform_config.proxy_url)

    async def run_once(self) -> SocialLoopResult:
        result = SocialLoopResult(cycle_start=datetime.now(UTC), dry_run=self._pc.dry_run)
        posts = await self._fetch_new_posts()
        result.posts_fetched = len(posts)
        if not posts:
            return result
        interesting = await self._filter_interesting(posts)
        result.posts_filtered = len(interesting)
        for post in interesting:
            post_id = str(post.get("id") or "").strip()
            if not post_id:
                continue
            if await self._already_responded(post_id):
                continue
            try:
                # Like/upvote the post before responding
                await self._maybe_like_post(post_id)
                text = await self._compose_response(post)

                # Auto-sign social posts
                try:
                    from zen_claw.auth.identity import AgentIdentity
                    identity = AgentIdentity(self._workspace / ".agent_keys")
                    identity.get_or_create_keypair()
                    sig = identity.sign(text.encode("utf-8"))
                    pub = identity.public_key_hex()

                    text += f"\n\n---\n*zen-claw Signature*\n**Key:** `{pub}`\n**Sig:** `{sig}`"
                except Exception as exc:
                    logger.warning("social loop auto-sign failed: {}", exc)

                result.responses_composed += 1
                if self._pc.dry_run:
                    await self._record_interaction(post_id, "dry_run_comment", text)
                    continue
                posted = await self._post_response(post_id, text)
                if posted:
                    result.responses_posted += 1
                    await self._record_interaction(post_id, "comment", text)
                else:
                    result.errors.append(f"post_failed:{post_id}")
            except Exception as exc:
                logger.warning("social loop exception: {}", exc)
                result.errors.append(str(exc))
        return result

    async def run_forever(self, interval_sec: int = 3600) -> None:
        while True:
            start = datetime.now(UTC)
            r = await self.run_once()
            logger.info(
                "social loop cycle complete fetched={} filtered={} composed={} posted={} errors={}",
                r.posts_fetched,
                r.posts_filtered,
                r.responses_composed,
                r.responses_posted,
                len(r.errors),
            )
            elapsed = (datetime.now(UTC) - start).total_seconds()
            await asyncio.sleep(max(1, int(interval_sec - elapsed)))

    async def _fetch_new_posts(self) -> list[dict[str, Any]]:
        res = await self._get_tool.execute(
            base_url=self._pc.base_url,
            endpoint="/api/posts",
            auth_header=self._pc.auth_header,
            query_params={"submolt": self._pc.submolt, "limit": str(self._pc.max_posts_per_cycle)},
        )
        if not res.ok:
            return []
        try:
            payload = json.loads(res.content)
        except Exception:
            return []
        posts = payload.get("posts")
        if isinstance(posts, list):
            return [p for p in posts if isinstance(p, dict)]
        return []

    async def _filter_interesting(self, posts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not posts:
            return []
        preview = [
            {
                "id": str(p.get("id") or ""),
                "title": str(p.get("title") or ""),
                "body": str(p.get("body") or p.get("content") or "")[:500],
            }
            for p in posts
        ]
        resp = await self._provider.chat(
            messages=[
                {"role": "system", "content": _FILTER_SYSTEM},
                {"role": "user", "content": json.dumps(preview, ensure_ascii=False)},
            ],
            model=self._model,
            max_tokens=256,
            temperature=0.2,
        )
        raw = str(resp.content or "")
        try:
            selected = json.loads(raw)
            if not isinstance(selected, list):
                return []
            selected_ids = {str(x) for x in selected}
        except Exception:
            m = re.search(r"\[[\s\S]*\]", raw)
            if not m:
                return []
            try:
                selected = json.loads(m.group(0))
                selected_ids = {str(x) for x in selected} if isinstance(selected, list) else set()
            except Exception:
                return []
        id_map = {str(p.get("id") or ""): p for p in posts}
        return [id_map[i] for i in selected_ids if i in id_map]

    async def _compose_response(self, post: dict[str, Any]) -> str:
        system = self._pc.system_prompt_override.strip() or _COMPOSE_SYSTEM
        user = (
            f"Title: {str(post.get('title') or '')}\n"
            f"Body: {str(post.get('body') or post.get('content') or '')}\n"
            f"Author: {str(post.get('author') or post.get('username') or 'anonymous')}"
        )
        resp = await self._provider.chat(
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            model=self._model,
            max_tokens=512,
            temperature=0.7,
        )
        return str(resp.content or "").strip()

    async def _post_response(self, post_id: str, text: str) -> bool:
        res = await self._post_tool.execute(
            base_url=self._pc.base_url,
            endpoint=f"/api/posts/{post_id}/comments",
            payload={"body": text},
            auth_header=self._pc.auth_header,
        )
        return bool(res.ok)

    async def _maybe_like_post(self, post_id: str) -> None:
        """Upvote a post if the platform config has like support enabled. Silently swallows errors."""
        try:
            res = await self._like_tool.execute(
                base_url=self._pc.base_url,
                post_id=post_id,
                auth_header=self._pc.auth_header,
            )
            if not res.ok:
                logger.debug("social loop: like failed for post {} — {}", post_id, res)
        except Exception as exc:
            logger.debug("social loop: like raised for post {} — {}", post_id, exc)

    async def _already_responded(self, post_id: str) -> bool:
        marker = f"[social:{self._pc.platform}:post:{post_id}]"
        if marker in self._memory.read_today():
            return True
        for i in range(1, 8):
            day = (datetime.now().date() - timedelta(days=i)).strftime("%Y-%m-%d")
            p = self._memory.memory_dir / f"{day}.md"
            if not p.exists():
                continue
            try:
                if marker in p.read_text(encoding="utf-8"):
                    return True
            except OSError:
                continue
        return False

    async def _record_interaction(self, post_id: str, action: str, content: str) -> None:
        marker = f"[social:{self._pc.platform}:post:{post_id}]"
        now = datetime.now(UTC).strftime("%H:%M UTC")
        entry = (
            f"\n## Social Interaction - {self._pc.platform}/{self._pc.submolt} @ {now}\n"
            f"- Action: `{action}`\n"
            f"- Post ID: `{post_id}` {marker}\n"
            f"- Response:\n\n> {content[:500]}\n"
        )
        self._memory.append_today(entry)

