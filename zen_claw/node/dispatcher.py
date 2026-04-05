"""Gateway-side dispatcher for node tasks."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Awaitable, Callable

from loguru import logger

from zen_claw.agent.tools.connector_sidecar import ConnectorSidecarClient
from zen_claw.bus.events import OutboundMessage
from zen_claw.node.service import NodeService
from zen_claw.security_context import issue_gateway_envelope


class NodeTaskDispatcher:
    """Consume node tasks and execute via gateway+agent runtime."""

    def __init__(
        self,
        *,
        node_service: NodeService,
        on_agent_prompt: Callable[..., Awaitable[str]],
        publish_outbound: Callable[[OutboundMessage], Awaitable[None]],
        poll_interval_sec: float = 2.0,
        enabled: bool = True,
        channel_circuit_enabled: bool = True,
        channel_circuit_window: int = 20,
        channel_circuit_min_samples: int = 5,
        channel_circuit_fail_rate_threshold: float = 0.5,
        channel_circuit_open_sec: float = 60.0,
        chaos_enabled: bool = False,
        chaos_fail_every: int = 0,
        chaos_channels: list[str] | None = None,
        now_fn: Callable[[], float] | None = None,
        connector_proxy_url: str = "",
        connector_approval_mode: str = "token",
        connector_approval_token: str = "",
        trusted_local_channels: list[str] | None = None,
    ):
        self.node_service = node_service
        self.on_agent_prompt = on_agent_prompt
        self.publish_outbound = publish_outbound
        self.poll_interval_sec = max(0.2, float(poll_interval_sec))
        self.enabled = enabled
        self.channel_circuit_enabled = bool(channel_circuit_enabled)
        self.channel_circuit_window = max(1, int(channel_circuit_window))
        self.channel_circuit_min_samples = max(1, int(channel_circuit_min_samples))
        self.channel_circuit_fail_rate_threshold = min(
            1.0, max(0.0, float(channel_circuit_fail_rate_threshold))
        )
        self.channel_circuit_open_sec = max(1.0, float(channel_circuit_open_sec))
        self.chaos_enabled = bool(chaos_enabled)
        self.chaos_fail_every = max(0, int(chaos_fail_every))
        self.chaos_channels = {
            str(c).strip().lower() for c in (chaos_channels or []) if str(c).strip()
        }
        self._now = now_fn or time.monotonic
        self._channel_circuit: dict[str, dict[str, Any]] = {}
        self._chaos_attempts_by_channel: dict[str, int] = {}
        self._running = False
        self._task: asyncio.Task | None = None
        self._connector_sidecar = ConnectorSidecarClient(
            proxy_url=connector_proxy_url,
            approval_mode=connector_approval_mode,
            approval_token=connector_approval_token,
        )

    async def start(self) -> None:
        if not self.enabled:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop(), name="node-task-dispatcher")
        logger.info("Node task dispatcher started")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run_loop(self) -> None:
        while self._running:
            try:
                did_work = await self.run_once()
                if not did_work:
                    await asyncio.sleep(self.poll_interval_sec)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Node task dispatcher error: {e}")
                await asyncio.sleep(self.poll_interval_sec)

    async def run_once(self) -> bool:
        self.node_service.expire_pending_approvals()
        task = self.node_service.claim_next_gateway_task(worker_id="gateway")
        if not task:
            return False
        await self._execute_task(task)
        return True

    async def _execute_task(self, task: dict[str, Any]) -> None:
        task_id = str(task.get("task_id") or "")
        trace_id = str(task.get("trace_id") or "")
        task_type = str(task.get("task_type") or "").strip().lower()
        node_id = str(task.get("node_id") or "")
        payload = task.get("payload")
        if not isinstance(payload, dict):
            payload = {}

        try:
            if task_type == "agent.prompt":
                prompt = str(
                    payload.get("prompt") or payload.get("message") or payload.get("content") or ""
                ).strip()
                if not prompt:
                    raise ValueError("prompt/message/content is required")
                session_key = str(payload.get("session_key") or f"node:{node_id}").strip()
                channel = str(payload.get("channel") or "cli").strip() or "cli"
                chat_id = str(payload.get("chat_id") or node_id or "node").strip()
                agent_id = str(payload.get("agent_id") or "default").strip().lower() or "default"
                media_raw = payload.get("media")
                media = [str(x) for x in media_raw] if isinstance(media_raw, list) else None
                response = await self.on_agent_prompt(
                    prompt,
                    session_key=session_key,
                    channel=channel,
                    chat_id=chat_id,
                    media=media,
                    agent_id=agent_id,
                    trace_id=trace_id,
                )
                if bool(payload.get("deliver")):
                    reply_channel = str(payload.get("reply_channel") or channel).strip()
                    reply_chat = str(payload.get("reply_chat_id") or chat_id).strip()
                    if reply_channel and reply_chat:
                        result = await self._dispatch_outbound_via_sidecar(
                            task_id=task_id,
                            node_id=node_id,
                            trace_id=trace_id,
                            payload={
                                "channel": reply_channel,
                                "chat_id": reply_chat,
                                "content": response or "",
                                "tenant_id": payload.get("tenant_id"),
                                "workspace_id": payload.get("workspace_id"),
                                "agent_profile": payload.get("agent_profile"),
                                "role": payload.get("role"),
                            },
                        )
                        if result is not None and not result.get("ok", False):
                            raise RuntimeError(str(result.get("error") or "reply send failed"))
                self.node_service.complete_task_system(
                    task_id=task_id,
                    ok=True,
                    result={"response": response or ""},
                    error="",
                )
                return

            if task_type == "message.send":
                channel = str(payload.get("channel") or "").strip()
                chat_id = str(payload.get("chat_id") or "").strip()
                text = str(payload.get("content") or payload.get("text") or "").strip()
                if not channel or not chat_id or not text:
                    raise ValueError("message.send requires channel, chat_id, content/text")
                result = await self._dispatch_message_send(
                    task_id=task_id,
                    node_id=node_id,
                    trace_id=trace_id,
                    payload=payload,
                )
                if result is not None and not result.get("ok", False):
                    raise RuntimeError(str(result.get("error") or "message.send failed"))
                self.node_service.complete_task_system(
                    task_id=task_id,
                    ok=True,
                    result={"sent": True},
                    error="",
                )
                return

            raise ValueError(f"unsupported gateway task type: {task_type}")
        except Exception as e:
            self.node_service.complete_task_system(
                task_id=task_id,
                ok=False,
                result={},
                error=str(e),
            )

    async def _dispatch_message_send(
        self,
        *,
        task_id: str,
        node_id: str,
        trace_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        return await self._dispatch_outbound_via_sidecar(
            task_id=task_id,
            node_id=node_id,
            trace_id=trace_id,
            payload=payload,
        )

    async def _dispatch_outbound_via_sidecar(
        self,
        *,
        task_id: str,
        node_id: str,
        trace_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        channel = str(payload.get("channel") or "").strip()
        chat_id = str(payload.get("chat_id") or "").strip()
        text = str(payload.get("content") or payload.get("text") or "").strip()
        metadata = {
            "source": "node_dispatcher",
            "node_id": node_id,
            "task_id": task_id,
            "trace_id": trace_id,
        }
        if not self._connector_sidecar.proxy_url:
            return {
                "ok": False,
                "error": "message.send requires connector sidecar configuration",
                "error_code": "message_send_sidecar_not_configured",
            }
        security_context = issue_gateway_envelope(
            trace_id=trace_id,
            channel="system",
            sender_id=f"node:{node_id}",
            chat_id=chat_id,
            tenant_id=str(payload.get("tenant_id") or "default").strip().lower(),
            workspace_id=str(payload.get("workspace_id") or f"node:{node_id}").strip(),
            agent_profile=str(payload.get("agent_profile") or "node_dispatcher").strip().lower(),
            role=str(payload.get("role") or "operator").strip().lower(),
            trust_level="trusted_local",
            origin_surface="node_dispatcher",
            policy_snapshot={
                "source": "node_dispatcher",
                "task_type": "message.send",
            },
            subject_type="node_dispatcher",
            trust_tier="trusted_local",
            capability_grants=["connector.message.send"],
        )
        request_payload = self._connector_sidecar.build_request_payload(
            target_url=f"channel://{channel}/{chat_id}",
            method="POST",
            headers={"Content-Type": "application/json"},
            body=json.dumps(
                {
                    "channel": channel,
                    "chat_id": chat_id,
                    "content": text,
                    "metadata": metadata,
                },
                ensure_ascii=False,
            ),
            connector_name=str(channel).strip().lower(),
            action="connector.message.send",
            target_resource=f"channel.{str(channel).strip().lower()}.message",
            security_context=security_context,
        )
        result = await self._connector_sidecar.execute_request(
            request_payload=request_payload,
            trace_id=trace_id,
        )
        return {
            "ok": result.ok,
            "error": result.error.message if result.error else "",
            "error_code": result.error.code if result.error else "",
            "sidecar_target": str(result.meta.get("sidecar_target") or ""),
        }

    def _channel_state(self, channel: str) -> dict[str, Any]:
        key = str(channel or "").strip().lower()
        row = self._channel_circuit.get(key)
        if row is None:
            row = {"history": [], "open_until": 0.0}
            self._channel_circuit[key] = row
        return row

    def _is_channel_open(self, channel: str) -> bool:
        if not self.channel_circuit_enabled:
            return False
        row = self._channel_state(channel)
        return float(row.get("open_until") or 0.0) > self._now()

    def _record_channel_result(self, channel: str, ok: bool) -> None:
        if not self.channel_circuit_enabled:
            return
        row = self._channel_state(channel)
        history = row.get("history")
        if not isinstance(history, list):
            history = []
        history.append(1 if ok else 0)
        if len(history) > self.channel_circuit_window:
            history = history[-self.channel_circuit_window :]
        row["history"] = history

        if len(history) < self.channel_circuit_min_samples:
            return
        failures = len([x for x in history if x == 0])
        fail_rate = float(failures) / float(len(history))
        if fail_rate >= self.channel_circuit_fail_rate_threshold:
            row["open_until"] = self._now() + self.channel_circuit_open_sec
            logger.warning(
                "Node dispatcher channel circuit opened: "
                + f"channel={channel}, fail_rate={fail_rate:.2f}, "
                + f"samples={len(history)}, open_sec={self.channel_circuit_open_sec}"
            )

    async def _send_with_channel_circuit(self, msg: OutboundMessage) -> None:
        channel = str(msg.channel or "").strip().lower()
        if self._should_inject_chaos(channel):
            raise RuntimeError(f"chaos_injected_timeout: channel={channel}")
        if self._is_channel_open(channel):
            raise RuntimeError(f"channel circuit open: {channel}")
        try:
            await self.publish_outbound(msg)
            self._record_channel_result(channel, ok=True)
        except Exception:
            self._record_channel_result(channel, ok=False)
            raise

    def _should_inject_chaos(self, channel: str) -> bool:
        if not self.chaos_enabled or self.chaos_fail_every <= 0:
            return False
        if (
            self.chaos_channels
            and "*" not in self.chaos_channels
            and channel not in self.chaos_channels
        ):
            return False
        count = int(self._chaos_attempts_by_channel.get(channel, 0)) + 1
        self._chaos_attempts_by_channel[channel] = count
        return (count % self.chaos_fail_every) == 0
