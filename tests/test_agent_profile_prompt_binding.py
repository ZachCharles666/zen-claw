from pathlib import Path

from zen_claw.agent.context import ContextBuilder
from zen_claw.agent.pool import resolve_agent_profile
from zen_claw.config.loader import convert_keys
from zen_claw.config.schema import Config


def test_context_builder_includes_profile_prompt_override_and_file(tmp_path: Path) -> None:
    prompt_file = tmp_path / "prompts" / "finance.md"
    prompt_file.parent.mkdir(parents=True, exist_ok=True)
    prompt_file.write_text("File prompt content", encoding="utf-8")

    ctx = ContextBuilder(
        tmp_path,
        memory_recall_mode="none",
        system_prompt_override="Inline prompt content",
        system_prompt_file="prompts/finance.md",
    )
    prompt = ctx.build_system_prompt(memory_query="anything")
    assert "# Agent Profile" in prompt
    assert "Inline prompt content" in prompt
    assert "File prompt content" in prompt


def test_resolve_agent_profile_exposes_prompt_binding_fields(tmp_path: Path) -> None:
    cfg = Config.model_validate(
        convert_keys(
            {
                "agents": {
                    "profiles": {
                        "finance_writer": {
                            "systemPrompt": "You are a finance agent.",
                            "systemPromptFile": "prompts/finance.md",
                            "workspace": str(tmp_path / "finance-ws"),
                        }
                    }
                }
            }
        )
    )

    profile = resolve_agent_profile(cfg, "finance_writer")
    assert profile.system_prompt == "You are a finance agent."
    assert profile.system_prompt_file == "prompts/finance.md"
