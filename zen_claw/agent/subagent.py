"""Subagent manager for background task execution."""

import asyncio
import json
import os
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from zen_claw.config.schema import (
        ExecToolConfig,
        ToolPolicyConfig,
        WebFetchConfig,
        WebSearchConfig,
    )

from loguru import logger

from zen_claw.agent.tools.filesystem import ListDirTool, ReadFileTool, WriteFileTool
from zen_claw.agent.tools.policy import ToolPolicyEngine
from zen_claw.agent.tools.registry import ToolRegistry
from zen_claw.agent.tools.result import ToolResult
from zen_claw.agent.tools.shell import ExecTool
from zen_claw.agent.tools.web import WebFetchTool, WebSearchTool
from zen_claw.bus.events import InboundMessage
from zen_claw.bus.queue import MessageBus
from zen_claw.observability.trace import TraceContext
from zen_claw.providers.base import LLMProvider

SUBAGENT_HARD_DENY_TOOLS: set[str] = {
    "spawn",
    "message",
    "cron",
    "sessions_spawn",
    "sessions_list",
    "sessions_kill",
    "sessions_read",
    "sessions_write",
    "sessions_signal",
    "sessions_resize",
}


class SubagentManager:
    """
    Manages background subagent execution.

    Subagents are lightweight agent instances that run in the background
    to handle specific tasks. They share the same LLM provider but have
    isolated context and a focused system prompt.
    """

    def __init__(
        self,
        provider: LLMProvider,
        workspace: Path,
        bus: MessageBus,
        model: str | None = None,
        brave_api_key: str | None = None,
        web_search_config: "WebSearchConfig | None" = None,
        web_fetch_config: "WebFetchConfig | None" = None,
        exec_config: "ExecToolConfig | None" = None,
        tool_policy_config: "ToolPolicyConfig | None" = None,
        restrict_to_workspace: bool = False,
    ):
        from zen_claw.config.schema import (
            ExecToolConfig,
            ToolPolicyConfig,
            WebFetchConfig,
            WebSearchConfig,
        )

        self.provider = provider
        self.workspace = workspace
        self.bus = bus
        self.model = model or provider.get_default_model()
        self.brave_api_key = brave_api_key
        self.web_search_config = web_search_config or WebSearchConfig()
        self.web_fetch_config = web_fetch_config or WebFetchConfig()
        self.exec_config = exec_config or ExecToolConfig()
        self.tool_policy_config = tool_policy_config or ToolPolicyConfig()
        self.restrict_to_workspace = restrict_to_workspace
        from zen_claw.agent.skills import SkillsLoader

        self.skills = SkillsLoader(workspace)
        self._running_tasks: dict[str, asyncio.Task[None]] = {}

    async def spawn(
        self,
        task: str,
        label: str | None = None,
        origin_channel: str = "cli",
        origin_chat_id: str = "direct",
        parent_trace_id: str | None = None,
        skill_pins: dict[str, str] | None = None,
    ) -> str:
        """
        Spawn a subagent to execute a task in the background.

        Args:
            task: The task description for the subagent.
            label: Optional human-readable label for the task.
            origin_channel: The channel to announce results to.
            origin_chat_id: The chat ID to announce results to.
            skill_pins: Optional skill version pins.

        Returns:
            Status message indicating the subagent was started.
        """
        task_id = str(uuid.uuid4())[:8]
        display_label = label or task[:30] + ("..." if len(task) > 30 else "")

        origin = {
            "channel": origin_channel,
            "chat_id": origin_chat_id,
        }
        trace_id = parent_trace_id or TraceContext.new_trace_id()

        # Create background task
        bg_task = asyncio.create_task(
            self._run_subagent(
                task_id, task, display_label, origin, trace_id, skill_pins=skill_pins
            )
        )
        self._running_tasks[task_id] = bg_task

        # Cleanup when done
        bg_task.add_done_callback(lambda _: self._running_tasks.pop(task_id, None))

        logger.info(
            f"Spawned subagent [{task_id}]: {display_label} "
            + TraceContext.event_text("subagent.spawn", trace_id, task_id=task_id)
        )
        return f"Subagent [{display_label}] started (id: {task_id}). I'll notify you when it completes."

    async def _run_subagent(
        self,
        task_id: str,
        task: str,
        label: str,
        origin: dict[str, str],
        trace_id: str,
        skill_pins: dict[str, str] | None = None,
    ) -> None:
        """Execute the subagent task and announce the result."""
        logger.info(
            f"Subagent [{task_id}] starting task: {label} "
            + TraceContext.event_text("subagent.start", trace_id, task_id=task_id)
        )

        try:
            # Build subagent tools (no message tool, no spawn tool)
            tools = ToolRegistry(
                policy=ToolPolicyEngine(
                    default_deny_tools=set(self.tool_policy_config.default_deny_tools)
                )
            )
            tools.set_kill_switch(
                self.tool_policy_config.kill_switch_enabled,
                reason=self.tool_policy_config.kill_switch_reason,
            )
            allowed_dir = self.workspace if self.restrict_to_workspace else None
            tools.register(ReadFileTool(allowed_dir=allowed_dir))
            tools.register(WriteFileTool(allowed_dir=allowed_dir))
            tools.register(ListDirTool(allowed_dir=allowed_dir))
            tools.register(
                ExecTool(
                    working_dir=str(self.workspace),
                    timeout=self.exec_config.timeout,
                    restrict_to_workspace=self.restrict_to_workspace,
                    mode=self.exec_config.mode,
                    sidecar_url=self.exec_config.sidecar_url,
                    sidecar_approval_token=self.exec_config.sidecar_approval_token.get_secret_value(),
                    sidecar_fallback_to_local=self.exec_config.sidecar_fallback_to_local,
                    sidecar_healthcheck=self.exec_config.sidecar_healthcheck,
                )
            )
            tools.register(
                WebSearchTool(
                    api_key=self.brave_api_key,
                    max_results=self.web_search_config.max_results,
                    mode=self.web_search_config.mode,
                    proxy_url=self.web_search_config.proxy_url,
                    proxy_healthcheck=self.web_search_config.proxy_healthcheck,
                    proxy_fallback_to_local=self.web_search_config.proxy_fallback_to_local,
                )
            )
            tools.register(
                WebFetchTool(
                    mode=self.web_fetch_config.mode,
                    proxy_url=self.web_fetch_config.proxy_url,
                    proxy_healthcheck=self.web_fetch_config.proxy_healthcheck,
                    proxy_fallback_to_local=self.web_fetch_config.proxy_fallback_to_local,
                )
            )
            self._apply_subagent_policy(tools)

            # Build messages with subagent-specific prompt
            system_prompt = self._build_subagent_prompt(task, skill_pins=skill_pins)
            messages: list[dict[str, Any]] = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": task},
            ]

            # Run agent loop (limited iterations)
            max_iterations = 15
            iteration = 0
            final_result: str | None = None

            while iteration < max_iterations:
                iteration += 1

                response = await self.provider.chat(
                    messages=messages,
                    tools=tools.get_definitions(),
                    model=self.model,
                )

                if response.has_tool_calls:
                    # Add assistant message with tool calls
                    tool_call_dicts = [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.name,
                                "arguments": json.dumps(tc.arguments),
                            },
                        }
                        for tc in response.tool_calls
                    ]
                    messages.append(
                        {
                            "role": "assistant",
                            "content": response.content or "",
                            "tool_calls": tool_call_dicts,
                        }
                    )

                    # Execute tools
                    for tool_call in response.tool_calls:
                        args_str = json.dumps(tool_call.arguments)
                        logger.debug(
                            f"Subagent [{task_id}] executing: {tool_call.name} with arguments: {args_str} "
                            + TraceContext.event_text(
                                "subagent.tool.call",
                                trace_id,
                                task_id=task_id,
                                tool=tool_call.name,
                            )
                        )
                        result = await tools.execute(
                            tool_call.name,
                            tool_call.arguments,
                            trace_id=trace_id,
                        )
                        tool_content = (
                            result.to_tool_message_content()
                            if isinstance(result, ToolResult)
                            else str(result)
                        )
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": tool_call.id,
                                "name": tool_call.name,
                                "content": tool_content,
                            }
                        )
                else:
                    final_result = response.content
                    break

            if final_result is None:
                final_result = "Task completed but no final response was generated."

            logger.info(
                f"Subagent [{task_id}] completed successfully "
                + TraceContext.event_text("subagent.done", trace_id, task_id=task_id)
            )
            await self._announce_result(task_id, label, task, final_result, origin, "ok", trace_id)

        except Exception as e:
            error_msg = f"Error: {str(e)}"
            logger.error(
                f"Subagent [{task_id}] failed: {e} "
                + TraceContext.event_text(
                    "subagent.error",
                    trace_id,
                    task_id=task_id,
                    error_kind="runtime",
                    retryable=False,
                )
            )
            await self._announce_result(task_id, label, task, error_msg, origin, "error", trace_id)

    async def _announce_result(
        self,
        task_id: str,
        label: str,
        task: str,
        result: str,
        origin: dict[str, str],
        status: str,
        trace_id: str,
    ) -> None:
        """Announce the subagent result to the main agent via the message bus."""
        status_text = "completed successfully" if status == "ok" else "failed"

        announce_content = f"""[Subagent '{label}' {status_text}]

Task: {task}

Result:
{result}

Summarize this naturally for the user. Keep it brief (1-2 sentences). Do not mention technical details like "subagent" or task IDs."""

        # Inject as system message to trigger main agent
        msg = InboundMessage(
            channel="system",
            sender_id="subagent",
            chat_id=f"{origin['channel']}:{origin['chat_id']}",
            content=announce_content,
            metadata=TraceContext.child_metadata(trace_id),
        )

        await self.bus.publish_inbound(msg)
        logger.debug(
            f"Subagent [{task_id}] announced result to {origin['channel']}:{origin['chat_id']} "
            + TraceContext.event_text("subagent.announce", trace_id, task_id=task_id)
        )

    def _build_subagent_prompt(self, task: str, skill_pins: dict[str, str] | None = None) -> str:
        """Build a focused system prompt for the subagent."""
        skills_info = ""
        if skill_pins:
            skills_info = "\n\n## Available Skills (Pinned Versions)\n"
            for logical, physical in skill_pins.items():
                skills_info += f"- {logical}: accessible at skills/{physical}/\n"

        return f"""# Subagent

You are a subagent spawned by the main agent to complete a specific task.

## Your Task
{task}

## Rules
1. Stay focused - complete only the assigned task, nothing else
2. Your final response will be reported back to the main agent
3. Do not initiate conversations or take on side tasks
4. Be concise but informative in your findings{skills_info}

## What You Can Do
- Read and write files in the workspace
- Execute shell commands
- Search the web and fetch web pages
- Complete the task thoroughly

## What You Cannot Do
- Send messages directly to users (no message tool available)
- Spawn other subagents
- Access the main agent's conversation history

## Workspace
Your workspace is at: {self.workspace}

When you have completed the task, provide a clear summary of your findings or actions."""

    def get_running_count(self) -> int:
        """Return the number of currently running subagents."""
        return len(self._running_tasks)

    def _apply_subagent_policy(self, tools: ToolRegistry) -> None:
        """Apply subagent policy scopes to a tool registry."""
        tools.set_policy_scope(
            "subagent",
            allow=self.tool_policy_config.subagent.allow,
            deny=self.tool_policy_config.subagent.deny,
        )
        allow_sensitive = (
            self.tool_policy_config.allow_subagent_sensitive_tools
            and self._allow_sensitive_override_enabled()
        )
        if not allow_sensitive:
            # Hard guardrail: these tools stay denied even if allowlisted by config.
            tools.set_policy_scope(
                "subagent_hard_deny",
                deny=SUBAGENT_HARD_DENY_TOOLS,
            )
        elif self.tool_policy_config.allow_subagent_sensitive_tools:
            logger.warning(
                "Subagent hard guardrail disabled by explicit override "
                + TraceContext.event_text(
                    "subagent.guardrail.disabled",
                    None,
                    policy_scope="subagent_hard_deny",
                    policy_code="allow_subagent_sensitive_tools_override",
                    error_kind="permission",
                    retryable=False,
                )
            )

    def _allow_sensitive_override_enabled(self) -> bool:
        """
        Require explicit env confirmation before allowing sensitive subagent tools.

        This prevents accidental config-only disabling of guardrails.
        """
        if not self.tool_policy_config.allow_subagent_sensitive_tools:
            return False
        token = os.getenv("zen-claw_ALLOW_SUBAGENT_SENSITIVE_TOOLS", "").strip().lower()
        return token in {"1", "true", "yes", "on"}
