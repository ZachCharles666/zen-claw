"""Context builder for assembling agent prompts."""

import base64
import mimetypes
import platform
from pathlib import Path
from typing import Any

from loguru import logger

from zen_claw.agent.memory import MemoryStore
from zen_claw.agent.memory_recall import KeywordRecallStrategy, NoopRecallStrategy
from zen_claw.agent.skills import SkillsLoader
from zen_claw.agent.tools.result import ToolResult


class ContextBuilder:
    """
    Builds the context (system prompt + messages) for the agent.
    
    Assembles bootstrap files, memory, skills, and conversation history
    into a coherent prompt for the LLM.
    """
    BOOTSTRAP_FILES = ["AGENTS.md", "SOUL.md", "USER.md", "TOOLS.md", "IDENTITY.md", ".agentrules", "ARCHITECTURE.md"]
    def __init__(self, workspace: Path, memory_recall_mode: str = "sqlite", max_tokens: int = 8192):
        self.workspace = workspace
        self.memory_recall_mode = memory_recall_mode
        self.max_tokens = max(512, int(max_tokens))

        if memory_recall_mode == "none":
            recall_strategy = NoopRecallStrategy()
        elif memory_recall_mode == "sqlite":
            try:
                from zen_claw.agent.memory_sqlite import SqliteVectorRecallStrategy
                recall_strategy = SqliteVectorRecallStrategy(workspace / "memory" / "memory.db")
            except Exception as exc:
                logger.debug("sqlite memory recall unavailable, fallback to keyword: {}", exc)
                recall_strategy = KeywordRecallStrategy()
        elif memory_recall_mode == "rag":
            try:
                from zen_claw.agent.memory_recall import RagRecallStrategy
                from zen_claw.config.loader import get_data_dir
                recall_strategy = RagRecallStrategy(data_dir=get_data_dir(), notebook_id="default")
            except Exception:
                recall_strategy = KeywordRecallStrategy()
        else:
            recall_strategy = KeywordRecallStrategy()

        self.memory = MemoryStore(workspace, recall_strategy=recall_strategy)
        self.skills = SkillsLoader(workspace)
        self.max_media_items = 4
        self.max_media_bytes = 5 * 1024 * 1024
        self._allowed_media_roots = [
            workspace.resolve(),
            (Path.home() / ".zen-claw" / "media").resolve(),
        ]

    def build_system_prompt(self, skill_names: list[str] | None = None, memory_query: str | None = None) -> str:
        """
        Build the system prompt from bootstrap files, memory, and skills.
        
        Args:
            skill_names: Optional list of skills to include.
        
        Returns:
            Complete system prompt.
        """
        parts = []

        # Core identity
        parts.append(self._get_identity())

        # Bootstrap files
        bootstrap = self._load_bootstrap_files()
        if bootstrap:
            parts.append(bootstrap)

        # Memory context
        memory = ""
        if self.memory_recall_mode == "recent":
            memory = self.memory.get_recent_memory_context(days=3, max_items=8, max_chars=1200)
        elif memory_query and self.memory_recall_mode != "none":
            memory = self.memory.get_relevant_memory_context(memory_query)
        if not memory:
            memory = self.memory.get_memory_context()
        if memory:
            parts.append(f"# Memory\n\n{memory}")

        tool_learning = self.memory.get_tool_learning_context(query=memory_query)
        if tool_learning:
            parts.append(f"# Tool Corrections\n\n{tool_learning}")

        # Skills - progressive loading
        # 1. Always-loaded skills: include full content
        always_skills = self.skills.get_always_skills()
        if always_skills:
            always_content = self.skills.load_skills_for_context(always_skills)
            if always_content:
                parts.append(f"# Active Skills\n\n{always_content}")

        # 1b. Explicitly requested skills: include full content (if enabled + requirements met).
        if skill_names:
            requested: list[str] = []
            seen: set[str] = set(always_skills)
            for name in skill_names:
                n = (name or "").strip()
                if not n or n in seen:
                    continue
                meta = self.skills._get_skill_meta(n)
                if not self.skills._check_requirements(meta):
                    continue
                if not self.skills.is_skill_enabled(n):
                    continue
                seen.add(n)
                requested.append(n)
            if requested:
                requested_content = self.skills.load_skills_for_context(requested)
                if requested_content:
                    parts.append(f"# Requested Skills\n\n{requested_content}")

        # 2. Available skills: only show summary (agent uses read_file to load)
        skills_summary = self.skills.build_skills_summary()
        if skills_summary:
            parts.append(f"""# Skills

The following skills extend your capabilities. To use a skill, read its SKILL.md file using the read_file tool.
Skills with available="false" need dependencies installed first - you can try installing them with apt/brew.

{skills_summary}""")

        return "\n\n---\n\n".join(parts)

    def _get_identity(self) -> str:
        """Get the core identity section."""
        from datetime import datetime
        now = datetime.now().strftime("%Y-%m-%d %H:%M (%A)")
        workspace_path = str(self.workspace.expanduser().resolve())
        system = platform.system()
        runtime = f"{'macOS' if system == 'Darwin' else system} {platform.machine()}, Python {platform.python_version()}"

        return f"""# zen-claw 🐈

You are zen-claw, a helpful AI assistant. You have access to tools that allow you to:
- Read, write, and edit files
- Execute shell commands
- Search the web and fetch web pages
- Send messages to users on chat channels
- Spawn subagents for complex background tasks

## Current Time
{now}

## Runtime
{runtime}

## Workspace
Your workspace is at: {workspace_path}
- Memory files: {workspace_path}/memory/MEMORY.md
- Daily notes: {workspace_path}/memory/YYYY-MM-DD.md
- Custom skills: {workspace_path}/skills/{{skill-name}}/SKILL.md

IMPORTANT: When responding to direct questions or conversations, reply directly with your text response.
Only use the 'message' tool when you need to send a message to a specific chat channel (like WhatsApp).
For normal conversation, just respond with text - do not call the message tool.

Always be helpful, accurate, and concise. When using tools, explain what you're doing.
When remembering something, write to {workspace_path}/memory/MEMORY.md"""

    def _load_bootstrap_files(self) -> str:
        """Load all bootstrap files from workspace."""
        parts = []

        for filename in self.BOOTSTRAP_FILES:
            file_path = self.workspace / filename
            if file_path.exists():
                content = file_path.read_text(encoding="utf-8")
                parts.append(f"## {filename}\n\n{content}")

        return "\n\n".join(parts) if parts else ""

    def build_messages(
        self,
        history: list[dict[str, Any]],
        current_message: str,
        skill_names: list[str] | None = None,
        media: list[str] | None = None,
        channel: str | None = None,
        chat_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        Build the complete message list for an LLM call.

        Args:
            history: Previous conversation messages.
            current_message: The new user message.
            skill_names: Optional skills to include.
            media: Optional list of local file paths for images/media.
            channel: Current channel (telegram, feishu, etc.).
            chat_id: Current chat/user ID.

        Returns:
            List of messages including system prompt.
        """
        messages = []

        # System prompt
        system_prompt = self.build_system_prompt(skill_names, memory_query=current_message)
        if channel and chat_id:
            system_prompt += f"\n\n## Current Session\nChannel: {channel}\nChat ID: {chat_id}"
        messages.append({"role": "system", "content": system_prompt})

        # History
        messages.extend(history)

        # Current message (with optional image attachments)
        user_content = self._build_user_content(current_message, media)
        messages.append({"role": "user", "content": user_content})

        return self._apply_max_tokens(messages)

    def _apply_max_tokens(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Hard-truncate context by dropping oldest history under max_tokens budget."""
        if self._estimate_tokens(messages) <= self.max_tokens:
            return messages

        # Keep system prompt and latest user message whenever possible.
        if len(messages) <= 2:
            if len(messages) == 2 and isinstance(messages[0].get("content"), str):
                system = dict(messages[0])
                latest = messages[1]
                target_chars = max(256, self.max_tokens * 4 - self._estimate_tokens([latest]) * 4 - 64)
                text = str(system.get("content") or "")
                if len(text) > target_chars:
                    system["content"] = text[:target_chars] + "\n\n[Context truncated due to max_tokens]"
                    return [system, latest]
            return messages

        system = messages[0]
        latest = messages[-1]
        history = list(messages[1:-1])
        dropped = 0

        while history and self._estimate_tokens([system, *history, latest]) > self.max_tokens:
            history.pop(0)
            dropped += 1

        pruned = [system, *history, latest]
        if dropped > 0 and isinstance(system.get("content"), str):
            system = dict(system)
            system["content"] = (
                str(system["content"])
                + f"\n\n[Context truncated due to max_tokens: dropped_history_messages={dropped}]"
            )
            pruned = [system, *history, latest]
        if self._estimate_tokens(pruned) <= self.max_tokens:
            return pruned

        # Final guardrail: trim system prompt text itself if still too large.
        if isinstance(system.get("content"), str):
            sys_text = str(system["content"])
            target_chars = max(256, self.max_tokens * 4 - self._estimate_tokens([latest]) * 4 - 64)
            if len(sys_text) > target_chars:
                system = dict(system)
                system["content"] = sys_text[:target_chars] + "\n\n[Context truncated due to max_tokens]"
                pruned = [system, latest]
        return pruned

    def _estimate_tokens(self, messages: list[dict[str, Any]]) -> int:
        total = 0
        for msg in messages:
            total += self._estimate_content_tokens(msg.get("content"))
            total += 4  # Role and structure overhead.
        return total

    def _estimate_content_tokens(self, content: Any) -> int:
        if isinstance(content, str):
            return max(1, len(content) // 4)
        if isinstance(content, list):
            total = 0
            for item in content:
                if isinstance(item, dict):
                    if item.get("type") == "text":
                        total += max(1, len(str(item.get("text", ""))) // 4)
                    elif item.get("type") == "image_url":
                        total += 85
                    else:
                        total += max(1, len(str(item)) // 4)
                else:
                    total += max(1, len(str(item)) // 4)
            return total
        return max(1, len(str(content)) // 4)

    def _build_user_content(self, text: str, media: list[str] | None) -> str | list[dict[str, Any]]:
        """Build user message content with optional base64-encoded images."""
        if not media:
            return text

        images = []
        extras = []
        refs = []
        for path in media[: self.max_media_items]:
            p = Path(path)
            mime, _ = mimetypes.guess_type(path)
            if not p.is_file():
                if self._is_media_reference(path):
                    refs.append(path)
                continue
            if not self._is_allowed_media_path(p) or not mime:
                continue
            try:
                size = p.stat().st_size
                if size > self.max_media_bytes:
                    continue
            except OSError:
                continue
            if mime.startswith("image/"):
                b64 = base64.b64encode(p.read_bytes()).decode()
                images.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})
                continue

            # Keep audio/video as metadata blocks to preserve user intent without
            # forcing provider-specific binary payloads.
            if mime.startswith("audio/") or mime.startswith("video/"):
                extras.append(f"- {p.name} ({mime}, {size} bytes)")

        if not images and not extras and not refs:
            return text
        out = list(images)
        if refs:
            out.append(
                {
                    "type": "text",
                    "text": "Attached media references:\n" + "\n".join(f"- {r}" for r in refs),
                }
            )
        if extras:
            out.append(
                {
                    "type": "text",
                    "text": "Attached media files:\n" + "\n".join(extras),
                }
            )
        out.append({"type": "text", "text": text})
        return out

    def _is_allowed_media_path(self, path: Path) -> bool:
        """Only allow media files from configured safe roots."""
        try:
            resolved = path.resolve()
            return any(resolved.is_relative_to(root) for root in self._allowed_media_roots)
        except Exception:
            return False

    def _is_media_reference(self, value: str) -> bool:
        """Allow URI-like media references from channels that cannot provide local files yet."""
        s = (value or "").strip()
        if len(s) > 1024 or "://" not in s:
            return False
        scheme = s.split("://", 1)[0].lower()
        if scheme == "media":
            rest = s.split("://", 1)[1]
            parts = [p for p in rest.split("/") if p]
            if len(parts) < 3:
                return False
            return parts[0].lower() in {"feishu", "whatsapp", "telegram", "discord"}
        return scheme in {"feishu", "whatsapp", "telegram", "discord"}

    def add_tool_result(
        self,
        messages: list[dict[str, Any]],
        tool_call_id: str,
        tool_name: str,
        result: str | ToolResult
    ) -> list[dict[str, Any]]:
        """
        Add a tool result to the message list.
        
        Args:
            messages: Current message list.
            tool_call_id: ID of the tool call.
            tool_name: Name of the tool.
            result: Tool execution result.
        
        Returns:
            Updated message list.
        """
        content = result.to_tool_message_content() if isinstance(result, ToolResult) else result
        messages.append({
            "role": "tool",
            "tool_call_id": tool_call_id,
            "name": tool_name,
            "content": content
        })
        return messages

    def add_assistant_message(
        self,
        messages: list[dict[str, Any]],
        content: str | None,
        tool_calls: list[dict[str, Any]] | None = None
    ) -> list[dict[str, Any]]:
        """
        Add an assistant message to the message list.
        
        Args:
            messages: Current message list.
            content: Message content.
            tool_calls: Optional tool calls.
        
        Returns:
            Updated message list.
        """
        msg: dict[str, Any] = {"role": "assistant", "content": content or ""}

        if tool_calls:
            msg["tool_calls"] = tool_calls

        messages.append(msg)
        return messages

