"""Trace context helpers for request correlation."""

import json
import uuid
from typing import Any


class TraceContext:
    """Helpers to create/propagate trace identifiers across components."""

    TRACE_ID_KEY = "trace_id"
    PARENT_TRACE_ID_KEY = "parent_trace_id"

    @staticmethod
    def new_trace_id() -> str:
        """Create a short trace ID suitable for logs and message metadata."""
        return uuid.uuid4().hex[:16]

    @classmethod
    def ensure_trace_id(cls, metadata: dict[str, Any] | None) -> tuple[str, dict[str, Any]]:
        """
        Ensure metadata has a trace_id and return normalized metadata.

        Returns:
            (trace_id, normalized_metadata)
        """
        normalized = dict(metadata or {})
        trace_id = str(normalized.get(cls.TRACE_ID_KEY) or "").strip()
        if not trace_id:
            trace_id = cls.new_trace_id()
            normalized[cls.TRACE_ID_KEY] = trace_id
        return trace_id, normalized

    @classmethod
    def get_trace_id(cls, metadata: dict[str, Any] | None) -> str | None:
        """Read trace_id from metadata if present."""
        if not metadata:
            return None
        trace_id = str(metadata.get(cls.TRACE_ID_KEY) or "").strip()
        return trace_id or None

    @classmethod
    def child_metadata(
        cls,
        parent_trace_id: str | None,
        metadata: dict[str, Any] | None = None,
        keep_trace_id: bool = True,
    ) -> dict[str, Any]:
        """Build child metadata preserving parent linkage."""
        child = dict(metadata or {})
        if keep_trace_id and parent_trace_id:
            child[cls.TRACE_ID_KEY] = parent_trace_id
        elif not keep_trace_id:
            child[cls.TRACE_ID_KEY] = cls.new_trace_id()

        if parent_trace_id:
            child[cls.PARENT_TRACE_ID_KEY] = parent_trace_id
        return child

    @staticmethod
    def fields_text(**fields: Any) -> str:
        """Render stable key-value JSON fragment for structured logs."""
        return json.dumps(fields, ensure_ascii=False, sort_keys=True, default=str)

    @classmethod
    def event_text(
        cls,
        event: str,
        trace_id: str | None,
        *,
        error_kind: str | None = None,
        retryable: bool | None = None,
        **fields: Any,
    ) -> str:
        """
        Render a normalized observability payload.

        Required schema keys:
        - event
        - trace_id
        - error_kind
        - retryable
        """
        payload = {
            "event": event,
            "trace_id": trace_id,
            "error_kind": error_kind,
            "retryable": retryable,
            **fields,
        }
        return cls.fields_text(**payload)
