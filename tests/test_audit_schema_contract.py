import json

from zen_claw.observability.trace import TraceContext


def test_audit_payload_contract_fields_present() -> None:
    payload = TraceContext.event_text(
        "tool.policy.denied",
        "trace-1",
        policy_code="tool_policy_denied",
        policy_scope="session",
        error_kind="permission",
        retryable=False,
        message="denied by session policy",
    )
    data = json.loads(payload)
    for key in [
        "event",
        "trace_id",
        "policy_code",
        "policy_scope",
        "error_kind",
        "retryable",
        "message",
    ]:
        assert key in data


def test_audit_event_name_convention() -> None:
    event = "tool.policy.denied"
    assert event.count(".") >= 2
    assert event == event.lower()


