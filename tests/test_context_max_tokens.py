from pathlib import Path

from zen_claw.agent.context import ContextBuilder


def _disable_skills(ctx: ContextBuilder) -> None:
    ctx.skills.get_always_skills = lambda: []
    ctx.skills.build_skills_summary = lambda: ""
    ctx.skills.load_skills_for_context = lambda names: ""


def test_build_messages_truncates_old_history_when_over_budget(tmp_path: Path) -> None:
    ctx = ContextBuilder(tmp_path, max_tokens=200)
    _disable_skills(ctx)

    history = []
    for i in range(8):
        history.append({"role": "assistant", "content": f"old-{i}-" + ("x" * 220)})

    out = ctx.build_messages(history=history, current_message="latest question")
    assert out[0]["role"] == "system"
    assert out[-1]["role"] == "user"
    assert out[-1]["content"] == "latest question"
    assert len(out) < len(history) + 2
    assert "dropped_history_messages=" in str(out[0]["content"])


def test_build_messages_trims_system_prompt_when_only_two_messages(tmp_path: Path) -> None:
    ctx = ContextBuilder(tmp_path, max_tokens=120)
    _disable_skills(ctx)
    ctx.build_system_prompt = lambda skill_names=None, memory_query=None: "y" * 5000

    out = ctx.build_messages(history=[], current_message="hi")
    assert len(out) == 2
    assert out[-1]["content"] == "hi"
    assert "Context truncated due to max_tokens" in str(out[0]["content"])
