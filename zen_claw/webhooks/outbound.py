"""Outbound webhook dispatcher — B3."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from zen_claw.agent.tools.capability_policy import (
    CapabilityPolicyEvaluator,
    connector_capability_request,
)
from zen_claw.agent.tools.connector_sidecar import ConnectorSidecarClient
from zen_claw.agent.tools.result import ToolErrorKind, ToolResult
from zen_claw.security_context import issue_gateway_envelope

# ── Audit record ──────────────────────────────────────────────────────────────

@dataclass
class WebhookDispatchRecord:
    """Audit log entry for one webhook dispatch attempt."""

    at_ms: int
    url: str
    agent_id: str
    session_id: str
    intent: str
    status_code: int        # 0 = network error
    attempts: int
    success: bool
    error: str = ""
    trace_id: str = ""
    error_code: str = ""
    sidecar_target: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "at_ms": self.at_ms,
            "url": self.url,
            "agent_id": self.agent_id,
            "session_id": self.session_id,
            "intent": self.intent,
            "status_code": self.status_code,
            "attempts": self.attempts,
            "success": self.success,
            "error": self.error,
            "trace_id": self.trace_id,
            "error_code": self.error_code,
            "sidecar_target": self.sidecar_target,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "WebhookDispatchRecord":
        return cls(
            at_ms=int(d.get("at_ms", 0)),
            url=str(d.get("url", "")),
            agent_id=str(d.get("agent_id", "")),
            session_id=str(d.get("session_id", "")),
            intent=str(d.get("intent", "")),
            status_code=int(d.get("status_code", 0)),
            attempts=int(d.get("attempts", 0)),
            success=bool(d.get("success", False)),
            error=str(d.get("error", "")),
            trace_id=str(d.get("trace_id", "")),
            error_code=str(d.get("error_code", "")),
            sidecar_target=str(d.get("sidecar_target", "")),
        )


# ── Dispatcher ────────────────────────────────────────────────────────────────

class WebhookDispatcher:
    """Dispatch outbound webhooks with HMAC-SHA256 signing and retry logic.

    Parameters
    ----------
    data_dir:
        Directory where ``webhook_dispatch.log.jsonl`` is written.
    secret:
        Shared secret used for HMAC-SHA256 signing.  When empty, the
        ``X-Webhook-Signature`` header is omitted.
    max_retries:
        Number of additional attempts after the first (default 2 → 3 total).
    timeout_sec:
        Per-request HTTP timeout in seconds.
    """

    LOG_FILENAME = "webhook_dispatch.log.jsonl"
    SIGNATURE_HEADER = "X-Webhook-Signature"

    def __init__(
        self,
        data_dir: Path,
        *,
        secret: str = "",
        max_retries: int = 2,
        timeout_sec: float = 10.0,
        connector_proxy_url: str = "",
        connector_approval_mode: str = "token",
        connector_approval_token: str = "",
        security_policy: Any | None = None,
    ) -> None:
        self._log_path = Path(data_dir) / self.LOG_FILENAME
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        self._secret = secret
        self._max_retries = max_retries
        self._timeout_sec = timeout_sec
        self._connector_sidecar = ConnectorSidecarClient(
            proxy_url=connector_proxy_url,
            approval_mode=connector_approval_mode,
            approval_token=connector_approval_token,
            timeout_sec=timeout_sec,
        )
        self._security_policy = security_policy

    # ── public API ────────────────────────────────────────────────────────────

    def dispatch(
        self,
        url: str,
        *,
        agent_id: str,
        session_id: str = "",
        intent: str = "",
        result: str = "",
        extra: dict[str, Any] | None = None,
        trace_id: str = "",
        tenant_id: str = "",
        workspace_id: str = "",
        agent_profile: str = "",
    ) -> WebhookDispatchRecord:
        """POST webhook payload to *url*; retry up to ``max_retries`` times.

        Returns the audit record (whether successful or not).
        """
        payload: dict[str, Any] = {
            "agent_id": agent_id,
            "session_id": session_id,
            "intent": intent,
            "result": result,
            "at_ms": int(time.time() * 1000),
        }
        if extra:
            payload.update(extra)

        if not self._connector_sidecar.proxy_url:
            record = WebhookDispatchRecord(
                at_ms=int(time.time() * 1000),
                url=url,
                agent_id=agent_id,
                session_id=session_id,
                intent=intent,
                status_code=0,
                attempts=1,
                success=False,
                error="webhook dispatch requires connector sidecar configuration",
                trace_id=trace_id,
                error_code="webhook_sidecar_not_configured",
            )
            self._append_log(record)
            return record

        return self._dispatch_via_sidecar(
            url=url,
            payload=payload,
            agent_id=agent_id,
            session_id=session_id,
            intent=intent,
            trace_id=trace_id,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            agent_profile=agent_profile,
        )

    def _dispatch_via_sidecar(
        self,
        *,
        url: str,
        payload: dict[str, Any],
        agent_id: str,
        session_id: str,
        intent: str,
        trace_id: str,
        tenant_id: str,
        workspace_id: str,
        agent_profile: str,
    ) -> WebhookDispatchRecord:
        identity = issue_gateway_envelope(
            trace_id=trace_id,
            channel="system",
            sender_id=f"agent:{agent_id}",
            chat_id=session_id,
            tenant_id=str(tenant_id or "").strip().lower() or "default",
            workspace_id=str(workspace_id or "").strip(),
            agent_profile=str(agent_profile or "").strip().lower() or "default",
            role="system",
            trust_level="trusted_local",
            origin_surface="webhook_dispatcher",
            policy_snapshot={"intent": intent, "source": "webhook_dispatcher"},
            subject_type="webhook",
            trust_tier="trusted_local",
            capability_grants=["connector.webhook.trigger"],
        )
        if self._security_policy is not None:
            evaluator = CapabilityPolicyEvaluator(self._security_policy, identity)
            decision = evaluator.evaluate(
                connector_capability_request(
                    capability="connector.webhook.trigger",
                    connector_name="webhook",
                    target_resource="webhook.dispatch",
                    tenant_id=str(tenant_id or "").strip().lower(),
                    workspace_id=str(workspace_id or "").strip(),
                )
            )
            if not decision.allowed:
                record = WebhookDispatchRecord(
                    at_ms=int(time.time() * 1000),
                    url=url,
                    agent_id=agent_id,
                    session_id=session_id,
                    intent=intent,
                    status_code=0,
                    attempts=1,
                    success=False,
                    error=decision.reason,
                    trace_id=trace_id,
                    error_code=decision.error_code,
                    sidecar_target=self._connector_sidecar.proxy_url,
                )
                self._append_log(record)
                return record
        request_payload = self._connector_sidecar.build_request_payload(
            target_url=url,
            method="POST",
            headers=self._build_headers(json.dumps(payload, ensure_ascii=False).encode()),
            body=json.dumps(payload, ensure_ascii=False),
            connector_name="webhook",
            action="connector.webhook.trigger",
            target_resource="webhook.dispatch",
            security_context=identity,
        )
        header_result = self._connector_sidecar.build_proxy_headers(
            request_payload=request_payload,
            trace_id=trace_id,
        )
        if isinstance(header_result, ToolResult):
            result = header_result
            record = WebhookDispatchRecord(
                at_ms=int(time.time() * 1000),
                url=url,
                agent_id=agent_id,
                session_id=session_id,
                intent=intent,
                status_code=0,
                attempts=1,
                success=False,
                error=result.error.message if result.error else "connector_sidecar_failed",
                trace_id=trace_id,
                error_code=result.error.code if result.error else "connector_sidecar_failed",
                sidecar_target=self._connector_sidecar.proxy_url,
            )
            self._append_log(record)
            return record
        headers, body_bytes = header_result
        try:
            response = httpx.post(
                self._connector_sidecar.proxy_url,
                headers=headers,
                content=body_bytes if self._connector_sidecar.approval_mode == "hmac" else None,
                json=None if self._connector_sidecar.approval_mode == "hmac" else request_payload,
                timeout=self._timeout_sec,
            )
            data = response.json()
            result = self._connector_sidecar._parse_response(  # noqa: SLF001
                status_code=response.status_code,
                data=data if isinstance(data, dict) else {},
            )
        except httpx.TimeoutException as exc:
            result = ToolResult.failure(
                ToolErrorKind.RETRYABLE,
                str(exc),
                code="connector_sidecar_timeout",
                sidecar_target=self._connector_sidecar.proxy_url,
            )
        except (httpx.RequestError, ValueError) as exc:
            result = ToolResult.failure(
                ToolErrorKind.RETRYABLE,
                str(exc),
                code="connector_sidecar_unavailable",
                sidecar_target=self._connector_sidecar.proxy_url,
            )

        record = WebhookDispatchRecord(
            at_ms=int(time.time() * 1000),
            url=url,
            agent_id=agent_id,
            session_id=session_id,
            intent=intent,
            status_code=int(result.meta.get("http_status") or 0),
            attempts=1,
            success=result.ok,
            error=result.error.message if result.error else "",
            trace_id=trace_id,
            error_code=result.error.code if result.error else "",
            sidecar_target=str(result.meta.get("sidecar_target") or self._connector_sidecar.proxy_url),
        )
        self._append_log(record)
        return record

    def read_log(self, *, limit: int = 500) -> list[WebhookDispatchRecord]:
        """Return recent dispatch audit records (most recent first)."""
        if not self._log_path.exists():
            return []
        records: list[WebhookDispatchRecord] = []
        try:
            lines = self._log_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        for line in reversed(lines[-limit:]):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(WebhookDispatchRecord.from_dict(json.loads(line)))
            except Exception:  # noqa: BLE001
                continue
        return records[:limit]

    # ── helpers ───────────────────────────────────────────────────────────────

    def _build_headers(self, body: bytes) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._secret:
            sig = hmac.new(
                self._secret.encode(),
                body,
                hashlib.sha256,
            ).hexdigest()
            headers[self.SIGNATURE_HEADER] = f"sha256={sig}"
        return headers

    def _append_log(self, record: WebhookDispatchRecord) -> None:
        try:
            with self._log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record.to_dict(), ensure_ascii=False) + "\n")
        except OSError:
            pass

    # ── HMAC verification helper (for consumers) ─────────────────────────────

    @staticmethod
    def verify_signature(body: bytes, header_value: str, secret: str) -> bool:
        """Return True if *header_value* matches the HMAC-SHA256 of *body*.

        Intended for use by inbound webhook receivers to validate outgoing
        requests from this dispatcher.
        """
        if not header_value.startswith("sha256="):
            return False
        expected = "sha256=" + hmac.new(
            secret.encode(), body, hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(expected, header_value)
