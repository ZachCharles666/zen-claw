from zen_claw.config.loader import _detect_legacy_tool_fields, _migrate_config


def test_migrate_populates_network_from_legacy_fields() -> None:
    data = {
        "tools": {
            "exec": {"mode": "sidecar", "sidecarUrl": "http://127.0.0.1:4488/v1/exec"},
            "web": {
                "search": {"mode": "proxy", "proxyUrl": "http://127.0.0.1:4499/v1/search"},
                "fetch": {"mode": "proxy", "proxyUrl": "http://127.0.0.1:4499/v1/fetch"},
            },
        }
    }
    out = _migrate_config(data)
    network = out["tools"]["network"]
    assert network["exec"]["mode"] == "sidecar"
    assert network["search"]["mode"] == "proxy"
    assert network["fetch"]["mode"] == "proxy"


def test_migrate_keeps_existing_network_without_overwrite() -> None:
    data = {
        "tools": {
            "exec": {"mode": "local"},
            "network": {"exec": {"mode": "sidecar"}},
        }
    }
    out = _migrate_config(data)
    assert out["tools"]["network"]["exec"]["mode"] == "sidecar"


def test_migrate_warns_on_network_legacy_conflicts(capsys) -> None:
    data = {
        "tools": {
            "exec": {"mode": "local"},
            "network": {"exec": {"mode": "sidecar"}},
        }
    }
    _ = _migrate_config(data)
    captured = capsys.readouterr()
    assert "using tools.network as precedence" in captured.out


def test_migrate_warns_when_legacy_fields_are_used(capsys) -> None:
    data = {
        "tools": {
            "exec": {"mode": "local"},
            "web": {"search": {"mode": "local"}},
        }
    }
    _ = _migrate_config(data)
    captured = capsys.readouterr()
    assert "Warning [DEPRECATION]" in captured.out


def test_migrate_does_not_warn_legacy_when_network_exists(capsys) -> None:
    data = {
        "tools": {
            "exec": {"mode": "local"},
            "web": {"search": {"mode": "local"}},
            "network": {
                "exec": {"mode": "sidecar"},
                "search": {"mode": "proxy"},
            },
        }
    }
    _ = _migrate_config(data)
    captured = capsys.readouterr()
    assert "legacy tool config fields detected" not in captured.out


def test_detect_legacy_tool_fields_returns_all_present_paths() -> None:
    tools = {
        "exec": {"mode": "local"},
        "web": {"search": {}, "fetch": {}},
    }
    out = _detect_legacy_tool_fields(tools)
    assert out == ["tools.exec", "tools.web.search", "tools.web.fetch"]
