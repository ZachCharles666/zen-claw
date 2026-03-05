from pathlib import Path

from zen_claw.agent.context import ContextBuilder


def _disable_skills(ctx: ContextBuilder) -> None:
    ctx.skills.get_always_skills = lambda: []
    ctx.skills.build_skills_summary = lambda: ""
    ctx.skills.load_skills_for_context = lambda names: ""


def test_build_user_content_without_media_returns_plain_text(tmp_path: Path) -> None:
    ctx = ContextBuilder(tmp_path)
    out = ctx._build_user_content("hello", media=None)
    assert out == "hello"


def test_build_user_content_with_image_media_builds_multimodal_payload(tmp_path: Path) -> None:
    ctx = ContextBuilder(tmp_path)
    img = tmp_path / "img.png"
    img.write_bytes(b"not-real-png-but-extension-is-enough")

    out = ctx._build_user_content("describe this", media=[str(img)])
    assert isinstance(out, list)
    assert out[-1] == {"type": "text", "text": "describe this"}
    assert out[0]["type"] == "image_url"
    assert str(out[0]["image_url"]["url"]).startswith("data:image/png;base64,")


def test_build_user_content_ignores_non_image_files(tmp_path: Path) -> None:
    ctx = ContextBuilder(tmp_path)
    txt = tmp_path / "note.txt"
    txt.write_text("abc", encoding="utf-8")

    out = ctx._build_user_content("hello", media=[str(txt)])
    assert out == "hello"


def test_build_user_content_limits_media_items(tmp_path: Path) -> None:
    ctx = ContextBuilder(tmp_path)
    paths = []
    for i in range(6):
        img = tmp_path / f"img{i}.png"
        img.write_bytes(b"img")
        paths.append(str(img))

    out = ctx._build_user_content("many images", media=paths)
    assert isinstance(out, list)
    # max 4 images + trailing text block
    assert len(out) == 5


def test_build_user_content_skips_oversized_image(tmp_path: Path) -> None:
    ctx = ContextBuilder(tmp_path)
    ctx.max_media_bytes = 8
    img = tmp_path / "large.png"
    img.write_bytes(b"0123456789abcdef")

    out = ctx._build_user_content("describe", media=[str(img)])
    assert out == "describe"


def test_build_user_content_adds_audio_video_metadata_block(tmp_path: Path) -> None:
    ctx = ContextBuilder(tmp_path)
    audio = tmp_path / "clip.mp3"
    video = tmp_path / "clip.mp4"
    audio.write_bytes(b"audio")
    video.write_bytes(b"video")

    out = ctx._build_user_content("summarize attachments", media=[str(audio), str(video)])
    assert isinstance(out, list)
    assert any(
        part.get("type") == "text" and "Attached media files:" in str(part.get("text"))
        for part in out
    )
    assert out[-1] == {"type": "text", "text": "summarize attachments"}


def test_build_user_content_rejects_media_outside_workspace(tmp_path: Path) -> None:
    ctx = ContextBuilder(tmp_path)
    outside = tmp_path.parent / "outside.png"
    outside.write_bytes(b"img")
    try:
        out = ctx._build_user_content("hello", media=[str(outside)])
        assert out == "hello"
    finally:
        outside.unlink(missing_ok=True)


def test_build_messages_with_media_and_session_metadata(tmp_path: Path) -> None:
    ctx = ContextBuilder(tmp_path)
    _disable_skills(ctx)
    img = tmp_path / "img.png"
    img.write_bytes(b"img")

    messages = ctx.build_messages(
        history=[{"role": "assistant", "content": "prev"}],
        current_message="analyze",
        media=[str(img)],
        channel="telegram",
        chat_id="u123",
    )
    assert len(messages) == 3
    assert "Channel: telegram" in str(messages[0]["content"])
    assert "Chat ID: u123" in str(messages[0]["content"])
    assert messages[1]["content"] == "prev"
    assert isinstance(messages[2]["content"], list)


def test_build_user_content_allows_home_media_dir(tmp_path: Path) -> None:
    ctx = ContextBuilder(tmp_path)
    expected_home_media = (Path.home() / ".zen-claw" / "media").resolve()
    assert expected_home_media in ctx._allowed_media_roots

    # Simulate a channel media root without requiring write access to real HOME.
    fake_root = tmp_path / "channel_media"
    fake_root.mkdir(parents=True, exist_ok=True)
    ctx._allowed_media_roots.append(fake_root.resolve())
    img = fake_root / "chan_test.png"
    img.write_bytes(b"img")
    out = ctx._build_user_content("from channel", media=[str(img)])
    assert isinstance(out, list)
    assert out[-1] == {"type": "text", "text": "from channel"}


def test_build_user_content_includes_external_media_refs(tmp_path: Path) -> None:
    ctx = ContextBuilder(tmp_path)
    out = ctx._build_user_content(
        "handle attachment",
        media=["feishu://image/img_key_123"],
    )
    assert isinstance(out, list)
    assert any(
        p.get("type") == "text" and "Attached media references" in str(p.get("text")) for p in out
    )


def test_build_user_content_rejects_unknown_media_ref_scheme(tmp_path: Path) -> None:
    ctx = ContextBuilder(tmp_path)
    out = ctx._build_user_content("handle attachment", media=["https://example.com/file.mp4"])
    assert out == "handle attachment"


def test_build_user_content_accepts_whitelisted_media_ref_schemes(tmp_path: Path) -> None:
    ctx = ContextBuilder(tmp_path)
    refs = [
        "media://feishu/image/img_0",
        "feishu://image/img_1",
        "whatsapp://audio/aud_1",
        "telegram://file/doc_1",
        "discord://video/vid_1",
    ]
    out = ctx._build_user_content("handle attachments", media=refs)
    assert isinstance(out, list)
    ref_block = [
        p
        for p in out
        if p.get("type") == "text" and "Attached media references" in str(p.get("text"))
    ]
    assert len(ref_block) == 1
    text = str(ref_block[0]["text"])
    for r in refs[:4]:
        assert r in text


def test_build_user_content_mixed_media_refs_respects_item_cap_and_filters_invalid(
    tmp_path: Path,
) -> None:
    ctx = ContextBuilder(tmp_path)
    img1 = tmp_path / "img1.png"
    img2 = tmp_path / "img2.png"
    audio = tmp_path / "clip.mp3"
    img1.write_bytes(b"img1")
    img2.write_bytes(b"img2")
    audio.write_bytes(b"audio")

    out = ctx._build_user_content(
        "handle mixed media",
        media=[
            str(img1),
            "feishu://image/img_1",
            "https://example.com/blocked.mp4",
            str(audio),
            "whatsapp://audio/aud_2",
            str(img2),
        ],
    )
    assert isinstance(out, list)
    # Only first 4 entries are considered by max_media_items default.
    assert any(p.get("type") == "image_url" for p in out)
    assert any(
        "Attached media references" in str(p.get("text"))
        and "feishu://image/img_1" in str(p.get("text"))
        for p in out
    )
    assert any(
        "Attached media files" in str(p.get("text")) and "clip.mp3" in str(p.get("text"))
        for p in out
    )
    serialized = str(out)
    assert "https://example.com/blocked.mp4" not in serialized
    assert "whatsapp://audio/aud_2" not in serialized
    assert "img2.png" not in serialized
