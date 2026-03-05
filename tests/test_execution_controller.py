from zen_claw.agent.execution import ExecutionController
from zen_claw.agent.tools.result import ToolErrorKind, ToolResult


def test_execution_controller_reflection_budget() -> None:
    ctrl = ExecutionController(max_reflections=1, enable_planning=True)
    assert ctrl.should_plan() is True
    assert ctrl.can_reflect(0) is True
    assert ctrl.can_reflect(1) is False


def test_execution_controller_collects_error_hints() -> None:
    ctrl = ExecutionController(max_reflections=1, enable_planning=False)
    results = [
        ToolResult.success("ok"),
        ToolResult.failure(ToolErrorKind.PARAMETER, "missing query"),
        ToolResult.failure(ToolErrorKind.RETRYABLE, "timeout"),
    ]
    hints = ctrl.collect_error_hints(results)
    assert len(hints) == 2
    assert hints[0].kind == ToolErrorKind.PARAMETER.value
    assert hints[1].retryable is True


def test_execution_controller_reflection_prompt_contains_error_details() -> None:
    ctrl = ExecutionController()
    hints = ctrl.collect_error_hints(
        [ToolResult.failure(ToolErrorKind.PERMISSION, "blocked by policy")]
    )
    prompt = ctrl.build_reflection_prompt(hints)
    assert "kind=permission" in prompt
    assert "blocked by policy" in prompt
