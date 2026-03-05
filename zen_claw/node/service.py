"""Minimal node protocol service for mobile node PoC."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import time
import uuid
from pathlib import Path
from typing import Any

_TASK_PAYLOAD_MAX_BYTES: int = 100 * 1024
_TASK_RESULT_MAX_BYTES: int = 100 * 1024


def _now_ms() -> int:
    return int(time.time() * 1000)


class NodeService:
    """File-backed node registry and task queue."""

    def __init__(
        self,
        store_path: Path,
        *,
        audit_secret: str | None = None,
        approval_events_log_path: Path | None = None,
        immutable_events_dir: Path | None = None,
    ):
        self.store_path = store_path
        secret = str(audit_secret or os.environ.get("zen_claw_NODE_AUDIT_SECRET", "")).strip()
        self.audit_secret = secret
        self.approval_events_log_path = approval_events_log_path or store_path.with_name(
            store_path.stem + "_approval_events.jsonl"
        )
        imm_dir_env = str(os.environ.get("zen_claw_NODE_AUDIT_IMMUTABLE_DIR", "")).strip()
        self.immutable_events_dir = immutable_events_dir or (
            Path(imm_dir_env) if imm_dir_env else None
        )
        self.alert_log_path = store_path.with_name(store_path.stem + "_audit_alerts.jsonl")
        self._remote_s3_bucket = str(
            os.environ.get("zen_claw_NODE_AUDIT_REMOTE_S3_BUCKET", "")
        ).strip()
        self._remote_s3_prefix = str(
            os.environ.get("zen_claw_NODE_AUDIT_REMOTE_S3_PREFIX", "zen-claw/audit")
        ).strip("/")
        self._remote_s3_region = str(
            os.environ.get("zen_claw_NODE_AUDIT_REMOTE_S3_REGION", "")
        ).strip()
        self._remote_s3_endpoint = str(
            os.environ.get("zen_claw_NODE_AUDIT_REMOTE_S3_ENDPOINT", "")
        ).strip()
        self._remote_s3_access_key = str(
            os.environ.get("zen_claw_NODE_AUDIT_REMOTE_S3_ACCESS_KEY", "")
        ).strip()
        self._remote_s3_secret_key = str(
            os.environ.get("zen_claw_NODE_AUDIT_REMOTE_S3_SECRET_KEY", "")
        ).strip()
        self._remote_s3_session_token = str(
            os.environ.get("zen_claw_NODE_AUDIT_REMOTE_S3_SESSION_TOKEN", "")
        ).strip()
        self._remote_retry_max = max(
            1, int(os.environ.get("zen_claw_NODE_AUDIT_REMOTE_RETRY_MAX", "3") or 3)
        )
        self._remote_retry_backoff_ms = max(
            0, int(os.environ.get("zen_claw_NODE_AUDIT_REMOTE_RETRY_BACKOFF_MS", "300") or 300)
        )
        self._default_token_ttl_sec = max(
            0, int(os.environ.get("zen_claw_NODE_TOKEN_TTL_SEC", "86400") or 86400)
        )
        self._idempotency_window_sec = max(
            0, int(os.environ.get("zen_claw_NODE_IDEMPOTENCY_WINDOW_SEC", "86400") or 86400)
        )
        self._remote_s3_client: Any | None = None
        self._remote_client_error_reported = False
        self._cache: dict[str, Any] | None = None

    def _load(self) -> dict[str, Any]:
        if self._cache is not None:
            return self._cache
        if self.store_path.exists():
            try:
                raw = json.loads(self.store_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    raw.setdefault("nodes", {})
                    raw.setdefault("tasks", [])
                    raw.setdefault("approval_events", [])
                    self._cache = raw
                    return raw
            except (OSError, ValueError, json.JSONDecodeError):
                pass
        self._cache = {"version": 1, "nodes": {}, "tasks": [], "approval_events": []}
        return self._cache

    def _save(self) -> None:
        data = self._load()
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        self.store_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    def register_node(self, *, name: str, platform: str, capabilities: list[str]) -> dict[str, Any]:
        data = self._load()
        node_id = str(uuid.uuid4())[:12]
        token = secrets.token_urlsafe(18)
        now = _now_ms()
        ttl_sec = int(self._default_token_ttl_sec)
        expires_at_ms = (now + ttl_sec * 1000) if ttl_sec > 0 else None
        data["nodes"][node_id] = {
            "node_id": node_id,
            "name": name,
            "platform": platform,
            "capabilities": sorted({c.strip().lower() for c in capabilities if c.strip()}),
            "policy": {
                "allowed_task_types": ["*"],
                "allow_gateway_tasks": True,
                "max_running_tasks": 1,
                "require_approval_task_types": [],
                "approval_timeout_sec": 0,
                "approval_required_count": 1,
            },
            "token": token,
            "token_issued_at_ms": now,
            "token_expires_at_ms": expires_at_ms,
            "token_ttl_sec": ttl_sec,
            "token_revoked": False,
            "status": "active",
            "last_seen_ms": now,
            "created_at_ms": now,
            "updated_at_ms": now,
        }
        self._save()
        return {"node_id": node_id, "token": token, "token_expires_at_ms": expires_at_ms}

    def _auth(self, node_id: str, token: str) -> bool:
        node = self._load()["nodes"].get(node_id)
        if not isinstance(node, dict):
            return False
        if str(node.get("token") or "") != token:
            return False
        if bool(node.get("token_revoked", False)):
            return False
        exp = node.get("token_expires_at_ms")
        if isinstance(exp, int) and exp > 0 and exp <= _now_ms():
            return False
        return True

    def rotate_token(self, *, node_id: str, ttl_sec: int | None = None) -> dict[str, Any] | None:
        data = self._load()
        node = data.get("nodes", {}).get(node_id)
        if not isinstance(node, dict):
            return None
        now = _now_ms()
        ttl = int(self._default_token_ttl_sec if ttl_sec is None else max(0, int(ttl_sec)))
        token = secrets.token_urlsafe(18)
        expires_at_ms = (now + ttl * 1000) if ttl > 0 else None
        node["token"] = token
        node["token_issued_at_ms"] = now
        node["token_expires_at_ms"] = expires_at_ms
        node["token_ttl_sec"] = ttl
        node["token_revoked"] = False
        node["updated_at_ms"] = now
        self._save()
        return {"node_id": node_id, "token": token, "token_expires_at_ms": expires_at_ms}

    def revoke_token(self, *, node_id: str) -> bool:
        data = self._load()
        node = data.get("nodes", {}).get(node_id)
        if not isinstance(node, dict):
            return False
        node["token_revoked"] = True
        node["updated_at_ms"] = _now_ms()
        self._save()
        return True

    def get_token_status(self, *, node_id: str) -> dict[str, Any] | None:
        node = self._load().get("nodes", {}).get(node_id)
        if not isinstance(node, dict):
            return None
        return {
            "node_id": node_id,
            "token_issued_at_ms": node.get("token_issued_at_ms"),
            "token_expires_at_ms": node.get("token_expires_at_ms"),
            "token_ttl_sec": node.get("token_ttl_sec"),
            "token_revoked": bool(node.get("token_revoked", False)),
            "token": str(node.get("token") or ""),
        }

    def scan_token_rotation(
        self,
        *,
        within_sec: int = 3600,
        rotate: bool = False,
        ttl_sec: int | None = None,
    ) -> dict[str, Any]:
        """
        Scan node tokens and optionally rotate revoked/expired/expiring ones.

        within_sec:
        - 0: only expired/revoked
        - >0: include tokens expiring within this window
        """
        window_sec = max(0, int(within_sec))
        now_ms = _now_ms()
        data = self._load()
        nodes = data.get("nodes", {})
        if not isinstance(nodes, dict):
            return {"ok": True, "checked": 0, "candidates": [], "rotated": []}

        candidates: list[dict[str, Any]] = []
        rotated_rows: list[dict[str, Any]] = []
        for node_id, row in nodes.items():
            if not isinstance(row, dict):
                continue
            revoked = bool(row.get("token_revoked", False))
            exp = row.get("token_expires_at_ms")
            expired = isinstance(exp, int) and exp > 0 and exp <= now_ms
            expiring_soon = (
                isinstance(exp, int)
                and exp > now_ms
                and window_sec > 0
                and (exp - now_ms) <= (window_sec * 1000)
            )
            no_expiry = exp is None or (isinstance(exp, int) and exp <= 0)
            reason = ""
            if revoked:
                reason = "revoked"
            elif expired:
                reason = "expired"
            elif expiring_soon:
                reason = "expiring_soon"
            elif no_expiry and window_sec > 0:
                reason = "no_expiry"
            if not reason:
                continue
            candidates.append(
                {
                    "node_id": str(node_id),
                    "reason": reason,
                    "token_expires_at_ms": exp,
                    "token_revoked": revoked,
                }
            )

        if rotate and candidates:
            for c in candidates:
                updated = self.rotate_token(
                    node_id=str(c["node_id"]),
                    ttl_sec=ttl_sec,
                )
                if updated:
                    rotated_rows.append(updated)

        return {
            "ok": True,
            "checked": len([k for k, v in nodes.items() if isinstance(v, dict)]),
            "candidates": candidates,
            "rotated": rotated_rows,
        }

    def heartbeat(self, *, node_id: str, token: str) -> bool:
        if not self._auth(node_id, token):
            return False
        data = self._load()
        node = data["nodes"][node_id]
        node["last_seen_ms"] = _now_ms()
        node["updated_at_ms"] = _now_ms()
        node["status"] = "active"
        self._save()
        return True

    def list_nodes(self) -> list[dict[str, Any]]:
        nodes = self._load().get("nodes", {})
        if not isinstance(nodes, dict):
            return []
        return sorted(
            (dict(v) for v in nodes.values() if isinstance(v, dict)),
            key=lambda x: x.get("created_at_ms", 0),
        )

    def _required_capability_for_task(self, task_type: str) -> str:
        t = str(task_type or "").strip().lower()
        if t.startswith("message."):
            return "notify"
        if t.startswith("browser."):
            return "browser"
        if t.startswith("capture."):
            return "camera"
        return ""

    def _node_has_capability(self, node_id: str, capability: str) -> bool:
        node = self._load().get("nodes", {}).get(node_id)
        if not isinstance(node, dict):
            return False
        wanted = str(capability or "").strip().lower()
        if not wanted:
            return True
        caps = node.get("capabilities", [])
        if not isinstance(caps, list):
            return False
        return wanted in {str(c).strip().lower() for c in caps}

    def _get_node_policy(self, node_id: str) -> dict[str, Any]:
        node = self._load().get("nodes", {}).get(node_id)
        if not isinstance(node, dict):
            return {
                "allowed_task_types": ["*"],
                "allow_gateway_tasks": True,
                "max_running_tasks": 1,
                "require_approval_task_types": [],
                "approval_timeout_sec": 0,
                "approval_required_count": 1,
            }
        policy = node.get("policy")
        if not isinstance(policy, dict):
            policy = {}
        allowed = policy.get("allowed_task_types")
        if not isinstance(allowed, list):
            allowed = ["*"]
        allow_gateway = bool(policy.get("allow_gateway_tasks", True))
        max_running = int(policy.get("max_running_tasks", 1) or 1)
        if max_running < 1:
            max_running = 1
        require_approval = policy.get("require_approval_task_types")
        if not isinstance(require_approval, list):
            require_approval = []
        approval_timeout_sec = int(policy.get("approval_timeout_sec", 0) or 0)
        if approval_timeout_sec < 0:
            approval_timeout_sec = 0
        approval_required_count = int(policy.get("approval_required_count", 1) or 1)
        if approval_required_count < 1:
            approval_required_count = 1
        return {
            "allowed_task_types": [str(x).strip().lower() for x in allowed if str(x).strip()],
            "allow_gateway_tasks": allow_gateway,
            "max_running_tasks": max_running,
            "require_approval_task_types": [
                str(x).strip().lower() for x in require_approval if str(x).strip()
            ],
            "approval_timeout_sec": approval_timeout_sec,
            "approval_required_count": approval_required_count,
        }

    def get_policy(self, *, node_id: str) -> dict[str, Any] | None:
        if node_id not in self._load().get("nodes", {}):
            return None
        return self._get_node_policy(node_id)

    def update_policy(
        self,
        *,
        node_id: str,
        allowed_task_types: list[str] | None = None,
        allow_gateway_tasks: bool | None = None,
        max_running_tasks: int | None = None,
        require_approval_task_types: list[str] | None = None,
        approval_timeout_sec: int | None = None,
        approval_required_count: int | None = None,
    ) -> dict[str, Any] | None:
        data = self._load()
        node = data.get("nodes", {}).get(node_id)
        if not isinstance(node, dict):
            return None
        current = self._get_node_policy(node_id)
        if allowed_task_types is not None:
            cleaned = [str(x).strip().lower() for x in allowed_task_types if str(x).strip()]
            current["allowed_task_types"] = cleaned or ["*"]
        if allow_gateway_tasks is not None:
            current["allow_gateway_tasks"] = bool(allow_gateway_tasks)
        if max_running_tasks is not None:
            current["max_running_tasks"] = max(1, int(max_running_tasks))
        if require_approval_task_types is not None:
            cleaned = [
                str(x).strip().lower() for x in require_approval_task_types if str(x).strip()
            ]
            current["require_approval_task_types"] = cleaned
        if approval_timeout_sec is not None:
            current["approval_timeout_sec"] = max(0, int(approval_timeout_sec))
        if approval_required_count is not None:
            current["approval_required_count"] = max(1, int(approval_required_count))
        node["policy"] = current
        node["updated_at_ms"] = _now_ms()
        self._save()
        return dict(current)

    def _append_approval_event(
        self,
        *,
        task_id: str,
        node_id: str,
        action: str,
        actor: str,
        note: str = "",
        trace_id: str = "",
    ) -> None:
        data = self._load()
        events = data.get("approval_events")
        if not isinstance(events, list):
            events = []
            data["approval_events"] = events
        prev_hash = ""
        if events and isinstance(events[-1], dict):
            prev_hash = str(events[-1].get("hash") or "")
        base_event = {
            "event_id": str(uuid.uuid4())[:12],
            "task_id": str(task_id or ""),
            "node_id": str(node_id or ""),
            "action": str(action or "").strip().lower(),
            "actor": str(actor or "").strip(),
            "note": str(note or ""),
            "at_ms": _now_ms(),
            "prev_hash": prev_hash,
        }
        canonical = json.dumps(
            base_event, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        )
        event_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        if self.audit_secret:
            signature = hmac.new(
                self.audit_secret.encode("utf-8"),
                canonical.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
            sig_alg = "hmac-sha256"
        else:
            signature = event_hash
            sig_alg = "sha256"
        event = dict(base_event)
        event["hash"] = event_hash
        event["signature"] = signature
        event["signature_alg"] = sig_alg
        event["trace_id"] = str(trace_id or "")

        events.append(event)
        if len(events) > 2000:
            events = events[-2000:]
            data["approval_events"] = events
        try:
            self.approval_events_log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.approval_events_log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
        except OSError:
            pass
        self._write_immutable_event(event)

    def _write_immutable_event(self, event: dict[str, Any]) -> bool:
        """
        Best-effort immutable audit sink write.

        Writes each event to its own file named by event hash under immutable_events_dir.
        Existing files are never overwritten.
        """
        local_written = False
        remote_written = False
        ev_hash = str(event.get("hash") or "").strip()
        if not ev_hash:
            return False

        if self.immutable_events_dir is not None:
            local_written = self._write_local_immutable_event(event)
        if self._remote_s3_bucket:
            remote_written = self._write_remote_immutable_event(event)
        return bool(local_written or remote_written)

    def _write_local_immutable_event(self, event: dict[str, Any]) -> bool:
        """Write immutable event to local sink directory."""
        if self.immutable_events_dir is None:
            return False
        ev_hash = str(event.get("hash") or "").strip()
        try:
            self.immutable_events_dir.mkdir(parents=True, exist_ok=True)
            path = self.immutable_events_dir / f"{ev_hash}.json"
            if path.exists():
                return False
            payload = dict(event)
            payload["immutable_written_at_ms"] = _now_ms()
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            return True
        except OSError:
            return False

    def _append_audit_alert(self, *, code: str, message: str, event_hash: str = "") -> None:
        """Best-effort alert log for remote immutable sink failures."""
        row = {
            "at_ms": _now_ms(),
            "code": str(code or "unknown"),
            "message": str(message or ""),
            "event_hash": str(event_hash or ""),
        }
        try:
            self.alert_log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.alert_log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def _get_remote_s3_client(self) -> Any | None:
        """Lazy-init boto3 S3 client for immutable remote sink."""
        if self._remote_s3_client is not None:
            return self._remote_s3_client
        try:
            import boto3  # type: ignore
        except Exception as e:
            if not self._remote_client_error_reported:
                self._append_audit_alert(
                    code="remote_client_unavailable",
                    message=f"boto3 unavailable: {e}",
                )
                self._remote_client_error_reported = True
            return None
        kwargs: dict[str, Any] = {}
        if self._remote_s3_region:
            kwargs["region_name"] = self._remote_s3_region
        if self._remote_s3_endpoint:
            kwargs["endpoint_url"] = self._remote_s3_endpoint
        if self._remote_s3_access_key:
            kwargs["aws_access_key_id"] = self._remote_s3_access_key
        if self._remote_s3_secret_key:
            kwargs["aws_secret_access_key"] = self._remote_s3_secret_key
        if self._remote_s3_session_token:
            kwargs["aws_session_token"] = self._remote_s3_session_token
        try:
            self._remote_s3_client = boto3.client("s3", **kwargs)
            return self._remote_s3_client
        except Exception as e:
            self._append_audit_alert(code="remote_client_init_failed", message=str(e))
            return None

    @staticmethod
    def _s3_not_found(exc: Exception) -> bool:
        response = getattr(exc, "response", None)
        if isinstance(response, dict):
            err = response.get("Error")
            if isinstance(err, dict):
                code = str(err.get("Code") or "")
                if code in {"404", "NoSuchKey", "NotFound"}:
                    return True
        return False

    def _write_remote_immutable_event(self, event: dict[str, Any]) -> bool:
        """Write immutable event to S3/OSS compatible object storage with retries."""
        bucket = self._remote_s3_bucket
        ev_hash = str(event.get("hash") or "").strip()
        if not bucket or not ev_hash:
            return False
        client = self._get_remote_s3_client()
        if client is None:
            return False
        key = (
            f"{self._remote_s3_prefix}/{ev_hash}.json"
            if self._remote_s3_prefix
            else f"{ev_hash}.json"
        )
        payload = dict(event)
        payload["immutable_remote_written_at_ms"] = _now_ms()
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        last_error = ""
        for idx in range(self._remote_retry_max):
            try:
                try:
                    client.head_object(Bucket=bucket, Key=key)
                    return False
                except Exception as e:
                    if not self._s3_not_found(e):
                        raise
                client.put_object(
                    Bucket=bucket,
                    Key=key,
                    Body=body,
                    ContentType="application/json; charset=utf-8",
                )
                return True
            except Exception as e:
                last_error = str(e)
                if idx + 1 < self._remote_retry_max and self._remote_retry_backoff_ms > 0:
                    sleep_sec = float(self._remote_retry_backoff_ms * (idx + 1)) / 1000.0
                    time.sleep(sleep_sec)
        self._append_audit_alert(
            code="remote_immutable_write_failed",
            message=f"bucket={bucket} key={key} retries={self._remote_retry_max} error={last_error}",
            event_hash=ev_hash,
        )
        return False

    @staticmethod
    def _task_type_allowed(task_type: str, patterns: list[str]) -> bool:
        t = str(task_type or "").strip().lower()
        if not t:
            return False
        rules = [str(x).strip().lower() for x in patterns if str(x).strip()]
        if not rules:
            return False
        if "*" in rules:
            return True
        for rule in rules:
            if rule.endswith(".*"):
                prefix = rule[:-2]
                if t.startswith(prefix + "."):
                    return True
            elif t == rule:
                return True
        return False

    def _active_task_count(self, node_id: str) -> int:
        rows = self._load().get("tasks", [])
        count = 0
        for row in rows:
            if not isinstance(row, dict):
                continue
            if row.get("node_id") != node_id:
                continue
            if row.get("status") in {"leased", "running"}:
                count += 1
        return count

    def _collect_static_violations(self, task_type: str, payload: dict[str, Any]) -> list[str]:
        """
        Static checks for node automation payloads.

        Goals:
        - block obvious infinite-loop prompts
        - block policy/privilege override attempts
        - block reserved internal delivery channel usage
        """
        violations: list[str] = []
        t = str(task_type or "").strip().lower()

        forbidden_override_keys = {
            "allow_subagent_sensitive_tools",
            "kill_switch_enabled",
            "kill_switch_reason",
            "production_hardening",
            "tool_policy",
            "policy_override",
        }
        for key in payload.keys():
            token = str(key or "").strip().lower()
            if token in forbidden_override_keys:
                violations.append(f"forbidden_payload_key:{token}")

        loop_markers = [
            "while true",
            "for(;;)",
            "for (;;)",
            "loop forever",
            "repeat forever",
            "infinite loop",
            "无限循环",
            "死循环",
        ]
        if t == "agent.prompt":
            prompt = (
                str(payload.get("prompt") or payload.get("message") or payload.get("content") or "")
                .strip()
                .lower()
            )
            if prompt:
                for marker in loop_markers:
                    if marker in prompt:
                        violations.append(f"loop_risk:{marker}")
                        break

        if t == "message.send":
            channel = str(payload.get("channel") or "").strip().lower()
            if channel in {"system", "_system"}:
                violations.append("reserved_channel:system")

        return violations

    def add_task(
        self,
        *,
        node_id: str,
        task_type: str,
        payload: dict[str, Any],
        idempotency_key: str = "",
        required_capability: str = "",
    ) -> dict[str, Any] | None:
        try:
            payload_bytes = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        except (TypeError, ValueError):
            payload_bytes = 0
        if payload_bytes > _TASK_PAYLOAD_MAX_BYTES:
            return {
                "ok": False,
                "error": f"payload too large: {payload_bytes} bytes (max {_TASK_PAYLOAD_MAX_BYTES})",
                "error_code": "node_payload_too_large",
                "node_id": node_id,
                "task_type": str(task_type or "").strip().lower(),
                "payload_bytes": payload_bytes,
                "max_bytes": _TASK_PAYLOAD_MAX_BYTES,
            }

        data = self._load()
        if node_id not in data.get("nodes", {}):
            return None
        normalized_type = str(task_type or "").strip().lower()
        policy = self._get_node_policy(node_id)
        if not self._task_type_allowed(normalized_type, policy.get("allowed_task_types", ["*"])):
            return {
                "ok": False,
                "error": "task_type_denied",
                "error_code": "node_policy_denied",
                "node_id": node_id,
                "task_type": normalized_type,
            }
        idem = str(idempotency_key or "").strip()
        req_cap = str(
            required_capability or ""
        ).strip().lower() or self._required_capability_for_task(normalized_type)
        if req_cap and not self._node_has_capability(node_id, req_cap):
            return {
                "ok": False,
                "error": "capability_denied",
                "error_code": "node_capability_denied",
                "required_capability": req_cap,
                "node_id": node_id,
                "task_type": normalized_type,
            }
        violations = self._collect_static_violations(normalized_type, payload)
        if violations:
            return {
                "ok": False,
                "error": "dsl_static_check_failed",
                "error_code": "node_dsl_static_denied",
                "violations": violations,
                "node_id": node_id,
                "task_type": normalized_type,
            }
        if idem:
            now_ms = _now_ms()
            for row in data.get("tasks", []):
                if not isinstance(row, dict):
                    continue
                if row.get("node_id") == node_id and str(row.get("idempotency_key") or "") == idem:
                    created_ms = int(row.get("created_at_ms") or 0)
                    if self._idempotency_window_sec > 0:
                        max_age_ms = int(self._idempotency_window_sec * 1000)
                        if created_ms > 0 and (now_ms - created_ms) > max_age_ms:
                            continue
                    existing_type = str(row.get("task_type") or "").strip().lower()
                    existing_payload = row.get("payload")
                    existing_cap = str(row.get("required_capability") or "").strip().lower()
                    if (
                        existing_type != normalized_type
                        or existing_payload != payload
                        or existing_cap != req_cap
                    ):
                        return {
                            "ok": False,
                            "error": "idempotency_replay_conflict",
                            "error_code": "node_replay_conflict",
                            "idempotency_key": idem,
                            "existing_task_id": str(row.get("task_id") or ""),
                            "node_id": node_id,
                            "task_type": normalized_type,
                        }
                    out = dict(row)
                    out["deduplicated"] = True
                    return out
        now = _now_ms()
        require_approval_patterns = policy.get("require_approval_task_types", [])
        requires_approval = self._task_type_allowed(normalized_type, require_approval_patterns)
        approval_timeout_sec = int(policy.get("approval_timeout_sec", 0) or 0)
        approval_required_count = max(1, int(policy.get("approval_required_count", 1) or 1))
        expires_at_ms = (
            _now_ms() + (approval_timeout_sec * 1000)
            if requires_approval and approval_timeout_sec > 0
            else None
        )
        task = {
            "task_id": str(uuid.uuid4())[:12],
            "trace_id": str(uuid.uuid4())[:12],
            "node_id": node_id,
            "task_type": normalized_type,
            "payload": payload,
            "idempotency_key": idem,
            "required_capability": req_cap,
            "status": "pending_approval" if requires_approval else "pending",
            "approval": {
                "required": bool(requires_approval),
                "approved_by": "",
                "approved_at_ms": None,
                "rejected_by": "",
                "rejected_at_ms": None,
                "note": "",
                "expires_at_ms": expires_at_ms,
                "required_count": approval_required_count,
                "approvals": [],
            },
            "created_at_ms": now,
            "updated_at_ms": now,
            "leased_by": None,
            "result": None,
            "error": None,
        }
        data["tasks"].append(task)
        if requires_approval:
            self._append_approval_event(
                task_id=task["task_id"],
                node_id=node_id,
                action="submitted",
                actor="system",
                note="task requires approval",
                trace_id=str(task.get("trace_id") or ""),
            )
        self._save()
        return dict(task)

    def pull_task(self, *, node_id: str, token: str) -> dict[str, Any] | None:
        if not self._auth(node_id, token):
            return None
        policy = self._get_node_policy(node_id)
        if self._active_task_count(node_id) >= int(policy.get("max_running_tasks", 1)):
            return None
        data = self._load()
        for task in data.get("tasks", []):
            if (
                isinstance(task, dict)
                and task.get("node_id") == node_id
                and task.get("status") == "pending"
            ):
                task["status"] = "leased"
                task["leased_by"] = node_id
                task["updated_at_ms"] = _now_ms()
                self._save()
                return dict(task)
        return None

    def ack_task(self, *, node_id: str, token: str, task_id: str) -> bool:
        if not self._auth(node_id, token):
            return False
        data = self._load()
        for task in data.get("tasks", []):
            if not isinstance(task, dict):
                continue
            if task.get("task_id") != task_id or task.get("node_id") != node_id:
                continue
            if task.get("status") not in {"leased", "pending"}:
                return False
            task["status"] = "running"
            task["leased_by"] = node_id
            task["updated_at_ms"] = _now_ms()
            self._save()
            return True
        return False

    def complete_task(
        self,
        *,
        node_id: str,
        token: str,
        task_id: str,
        ok: bool,
        result: dict[str, Any] | None,
        error: str | None,
    ) -> bool:
        if not self._auth(node_id, token):
            return False
        if result is not None:
            try:
                result_bytes = len(json.dumps(result, ensure_ascii=False).encode("utf-8"))
            except (TypeError, ValueError):
                result_bytes = 0
            if result_bytes > _TASK_RESULT_MAX_BYTES:
                logging.getLogger(__name__).warning(
                    "node_result_too_large: node=%s task=%s bytes=%d max=%d; result truncated",
                    node_id,
                    task_id,
                    result_bytes,
                    _TASK_RESULT_MAX_BYTES,
                )
                result = {
                    "ok": False,
                    "error_code": "node_result_too_large",
                    "error": (
                        f"result payload truncated: {result_bytes} bytes exceeded "
                        f"{_TASK_RESULT_MAX_BYTES} byte limit"
                    ),
                }

        data = self._load()
        for task in data.get("tasks", []):
            if not isinstance(task, dict):
                continue
            if task.get("task_id") != task_id or task.get("node_id") != node_id:
                continue
            task["status"] = "done" if ok else "error"
            task["result"] = result or {}
            task["error"] = error or ""
            task["updated_at_ms"] = _now_ms()
            self._save()
            return True
        return False

    def list_tasks(self, *, node_id: str | None = None) -> list[dict[str, Any]]:
        tasks = self._load().get("tasks", [])
        out: list[dict[str, Any]] = []
        for t in tasks:
            if not isinstance(t, dict):
                continue
            if node_id and t.get("node_id") != node_id:
                continue
            out.append(dict(t))
        return sorted(out, key=lambda x: x.get("created_at_ms", 0))

    def approve_task(self, *, task_id: str, approved_by: str, note: str = "") -> bool:
        data = self._load()
        for task in data.get("tasks", []):
            if not isinstance(task, dict):
                continue
            if task.get("task_id") != task_id:
                continue
            if task.get("status") != "pending_approval":
                return False
            approval = task.get("approval")
            if not isinstance(approval, dict):
                approval = {}
            actor = str(approved_by or "").strip()
            if not actor:
                return False
            approval["required"] = bool(approval.get("required", False))
            approval["required_count"] = max(1, int(approval.get("required_count", 1) or 1))
            approvals = approval.get("approvals")
            if not isinstance(approvals, list):
                approvals = []
            approver_set = {str(x).strip() for x in approvals if str(x).strip()}
            if actor not in approver_set:
                approvals.append(actor)
                approver_set.add(actor)
            approval["approvals"] = approvals
            approval["approved_by"] = actor
            approval["approved_at_ms"] = _now_ms()
            approval["note"] = str(note or "")
            task["approval"] = approval
            reached = len(approver_set) >= int(approval.get("required_count", 1))
            task["status"] = "pending" if reached else "pending_approval"
            task["updated_at_ms"] = _now_ms()
            self._append_approval_event(
                task_id=str(task.get("task_id") or ""),
                node_id=str(task.get("node_id") or ""),
                action="approved" if reached else "approved_step",
                actor=str(approved_by or ""),
                note=note,
                trace_id=str(task.get("trace_id") or ""),
            )
            self._save()
            return True
        return False

    def reject_task(self, *, task_id: str, rejected_by: str, reason: str = "") -> bool:
        data = self._load()
        for task in data.get("tasks", []):
            if not isinstance(task, dict):
                continue
            if task.get("task_id") != task_id:
                continue
            if task.get("status") not in {"pending_approval", "pending"}:
                return False
            approval = task.get("approval")
            if not isinstance(approval, dict):
                approval = {}
            approval["required"] = bool(approval.get("required", False))
            approval["rejected_by"] = str(rejected_by or "").strip()
            approval["rejected_at_ms"] = _now_ms()
            approval["note"] = str(reason or "")
            task["approval"] = approval
            task["status"] = "rejected"
            task["error"] = str(reason or "rejected by approver")
            task["updated_at_ms"] = _now_ms()
            self._append_approval_event(
                task_id=str(task.get("task_id") or ""),
                node_id=str(task.get("node_id") or ""),
                action="rejected",
                actor=str(rejected_by or ""),
                note=reason,
                trace_id=str(task.get("trace_id") or ""),
            )
            self._save()
            return True
        return False

    def expire_pending_approvals(self, *, now_ms: int | None = None) -> int:
        now = int(now_ms if now_ms is not None else _now_ms())
        data = self._load()
        expired = 0
        for task in data.get("tasks", []):
            if not isinstance(task, dict):
                continue
            if task.get("status") != "pending_approval":
                continue
            approval = task.get("approval")
            if not isinstance(approval, dict):
                continue
            expires_at_ms = approval.get("expires_at_ms")
            if not isinstance(expires_at_ms, int) or expires_at_ms <= 0:
                continue
            if expires_at_ms > now:
                continue
            task["status"] = "rejected"
            task["error"] = "approval timeout"
            approval["rejected_by"] = "system-timeout"
            approval["rejected_at_ms"] = now
            approval["note"] = "approval timeout"
            task["approval"] = approval
            task["updated_at_ms"] = now
            self._append_approval_event(
                task_id=str(task.get("task_id") or ""),
                node_id=str(task.get("node_id") or ""),
                action="expired",
                actor="system-timeout",
                note="approval timeout",
                trace_id=str(task.get("trace_id") or ""),
            )
            expired += 1
        if expired > 0:
            self._save()
        return expired

    def list_approval_events(
        self, *, node_id: str | None = None, task_id: str | None = None
    ) -> list[dict[str, Any]]:
        rows = self._load().get("approval_events", [])
        out: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            if node_id and row.get("node_id") != node_id:
                continue
            if task_id and row.get("task_id") != task_id:
                continue
            out.append(dict(row))
        return sorted(out, key=lambda x: x.get("at_ms", 0))

    def verify_approval_events(self) -> dict[str, Any]:
        rows = self.list_approval_events()
        prev_hash = ""
        checked = 0
        for idx, row in enumerate(rows):
            if str(row.get("prev_hash") or "") != prev_hash:
                return {
                    "ok": False,
                    "checked": checked,
                    "error_index": idx,
                    "error": "approval event hash chain broken",
                }
            base_event = {
                "event_id": str(row.get("event_id") or ""),
                "task_id": str(row.get("task_id") or ""),
                "node_id": str(row.get("node_id") or ""),
                "action": str(row.get("action") or ""),
                "actor": str(row.get("actor") or ""),
                "note": str(row.get("note") or ""),
                "at_ms": int(row.get("at_ms") or 0),
                "prev_hash": str(row.get("prev_hash") or ""),
            }
            canonical = json.dumps(
                base_event, sort_keys=True, ensure_ascii=False, separators=(",", ":")
            )
            expected_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
            if str(row.get("hash") or "") != expected_hash:
                return {
                    "ok": False,
                    "checked": checked,
                    "error_index": idx,
                    "error": "approval event hash mismatch",
                }
            if self.audit_secret:
                expected_sig = hmac.new(
                    self.audit_secret.encode("utf-8"),
                    canonical.encode("utf-8"),
                    hashlib.sha256,
                ).hexdigest()
                if str(row.get("signature") or "") != expected_sig:
                    return {
                        "ok": False,
                        "checked": checked,
                        "error_index": idx,
                        "error": "approval event signature mismatch",
                    }
            prev_hash = expected_hash
            checked += 1
        return {"ok": True, "checked": checked, "error_index": None, "error": ""}

    def sync_approval_events_to_immutable(self) -> dict[str, Any]:
        """Backfill all in-store approval events into immutable sink directory."""
        if self.immutable_events_dir is None and not self._remote_s3_bucket:
            return {
                "ok": False,
                "synced": 0,
                "skipped": 0,
                "error": "immutable sink not configured",
            }
        synced = 0
        skipped = 0
        for row in self.list_approval_events():
            if self._write_immutable_event(row):
                synced += 1
            else:
                skipped += 1
        return {"ok": True, "synced": synced, "skipped": skipped, "error": ""}

    def claim_next_gateway_task(self, *, worker_id: str = "gateway") -> dict[str, Any] | None:
        """
        Claim next pending task meant for gateway-side execution.

        Current supported gateway task types:
        - agent.prompt
        - message.send
        """
        data = self._load()
        for task in data.get("tasks", []):
            if not isinstance(task, dict):
                continue
            if task.get("status") != "pending":
                continue
            task_type = str(task.get("task_type") or "").strip().lower()
            if task_type not in {"agent.prompt", "message.send"}:
                continue
            node_id = str(task.get("node_id") or "")
            policy = self._get_node_policy(node_id)
            if not bool(policy.get("allow_gateway_tasks", True)):
                continue
            if self._active_task_count(node_id) >= int(policy.get("max_running_tasks", 1)):
                continue
            task["status"] = "running"
            task["leased_by"] = worker_id
            task["updated_at_ms"] = _now_ms()
            self._save()
            return dict(task)
        return None

    def complete_task_system(
        self,
        *,
        task_id: str,
        ok: bool,
        result: dict[str, Any] | None,
        error: str | None,
    ) -> bool:
        """Complete task without node token (for trusted local gateway worker)."""
        data = self._load()
        for task in data.get("tasks", []):
            if not isinstance(task, dict):
                continue
            if task.get("task_id") != task_id:
                continue
            task["status"] = "done" if ok else "error"
            task["result"] = result or {}
            task["error"] = error or ""
            task["updated_at_ms"] = _now_ms()
            self._save()
            return True
        return False
