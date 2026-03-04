from zen_claw.agent.context_compression import ContextCompressor
from zen_claw.agent.memory_extractor import MemoryExtractor


def test_context_compressor_plan_and_summary_message() -> None:
    compressor = ContextCompressor(trigger_messages=5, keep_recent=2, min_new_messages=2)
    messages = [
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "u2"},
        {"role": "assistant", "content": "a2"},
        {"role": "user", "content": "u3"},
        {"role": "assistant", "content": "a3"},
    ]
    plan = compressor.plan(messages, summarized_upto=0)
    assert plan.should_compress is True
    assert len(plan.prefix_messages) >= 2
    assert len(plan.recent_messages) == 2

    msg = compressor.build_summary_message("important facts")
    assert msg["role"] == "assistant"
    assert "[Rolling Summary]" in msg["content"]


def test_memory_extractor_parse_json_and_fallback() -> None:
    extractor = MemoryExtractor()
    parsed = extractor.parse(
        '{"should_write": true, "memory_type": "long_term", "content": "User prefers concise answers."}'
    )
    assert parsed.should_write is True
    assert parsed.memory_type == "long_term"
    assert "concise" in parsed.content

    wrapped = extractor.parse(
        "Result:\n```json\n{\"should_write\": false, \"memory_type\": \"daily\", \"content\": \"\"}\n```"
    )
    assert wrapped.should_write is False


