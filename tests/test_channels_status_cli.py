from types import SimpleNamespace

from typer.testing import CliRunner

from zen_claw.cli.commands import app


def test_channels_status_includes_rbac_columns(monkeypatch) -> None:
    cfg = SimpleNamespace(
        channels=SimpleNamespace(
            whatsapp=SimpleNamespace(enabled=True, bridge_url="ws://localhost:3001", admins=[], users=[]),
            discord=SimpleNamespace(enabled=False, gateway_url="wss://gateway.discord.gg", admins=["d-admin"], users=["d-user1", "d-user2"]),
            telegram=SimpleNamespace(enabled=True, token="1234567890abcdef", admins=["t-admin"], users=[]),
        )
    )
    monkeypatch.setattr("zen_claw.config.loader.load_config", lambda: cfg)

    out = CliRunner().invoke(app, ["channels", "status"])
    assert out.exit_code == 0
    assert "Channel Status" in out.output
    assert "RBAC" in out.output
    assert "Admins" in out.output
    assert "Users" in out.output
    assert "WhatsApp" in out.output
    assert "Discord" in out.output
    assert "Telegram" in out.output
    assert "ws://localhost:3001" in out.output
