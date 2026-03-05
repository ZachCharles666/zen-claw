from typer.testing import CliRunner

from zen_claw.cli.commands import app


def test_dashboard_command_passes_args_to_server(monkeypatch) -> None:
    captured = {}

    class _DummyConfig:
        pass

    def _fake_load_config():
        return _DummyConfig()

    def _fake_run_dashboard_server(config, *, host: str, port: int, refresh_sec: int):
        captured["config"] = config
        captured["host"] = host
        captured["port"] = port
        captured["refresh_sec"] = refresh_sec

    monkeypatch.setattr("zen_claw.config.loader.load_config", _fake_load_config)
    monkeypatch.setattr(
        "zen_claw.dashboard.server.run_dashboard_server", _fake_run_dashboard_server
    )

    out = CliRunner().invoke(
        app,
        ["dashboard", "--host", "127.0.0.1", "--port", "18888", "--refresh-sec", "9"],
    )
    assert out.exit_code == 0
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 18888
    assert captured["refresh_sec"] == 9


def test_dashboard_command_refuses_remote_bind_without_allow_flag(monkeypatch) -> None:
    called = {"run": False}

    def _fake_load_config():
        return object()

    def _fake_run_dashboard_server(config, *, host: str, port: int, refresh_sec: int):
        called["run"] = True

    monkeypatch.setattr("zen_claw.config.loader.load_config", _fake_load_config)
    monkeypatch.setattr(
        "zen_claw.dashboard.server.run_dashboard_server", _fake_run_dashboard_server
    )

    out = CliRunner().invoke(app, ["dashboard", "--host", "0.0.0.0"])
    assert out.exit_code == 1
    assert "non-localhost dashboard bind is blocked" in out.output
    assert called["run"] is False
