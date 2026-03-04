"""Configuration loading utilities."""

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from zen_claw.config.schema import Config


def get_config_path() -> Path:
    """Get the default configuration file path."""
    return Path.home() / ".zen-claw" / "config.json"


def get_data_dir() -> Path:
    """Get the zen-claw data directory."""
    from zen_claw.utils.helpers import get_data_path
    return get_data_path()


def load_config(config_path: Path | None = None) -> Config:
    """
    Load configuration from file or create default.
    
    Args:
        config_path: Optional path to config file. Uses default if not provided.
    
    Returns:
        Loaded configuration object.
    """
    path = config_path or get_config_path()

    if path.exists():
        try:
            with open(path) as f:
                data = json.load(f)
            data = _migrate_config(data)
            return Config.model_validate(convert_keys(data))
        except (json.JSONDecodeError, ValueError) as e:
            print(f"Warning: Failed to load config from {path}: {e}")
            print("Using default configuration.")

    return Config()


def save_config(config: Config, config_path: Path | None = None) -> None:
    """
    Save configuration to file.
    
    Args:
        config: Configuration to save.
        config_path: Optional path to save to. Uses default if not provided.
    """
    path = config_path or get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    # Convert to camelCase format; mode='json' serialises SecretStr as plain strings
    data = config.model_dump(mode='json')
    data = convert_to_camel(data)

    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def _migrate_config(data: dict) -> dict:
    """Migrate old config formats to current."""
    # Move tools.exec.restrictToWorkspace �?tools.restrictToWorkspace
    tools = data.get("tools", {})
    exec_cfg = tools.get("exec", {})
    if "restrictToWorkspace" in exec_cfg and "restrictToWorkspace" not in tools:
        tools["restrictToWorkspace"] = exec_cfg.pop("restrictToWorkspace")

    _warn_on_network_legacy_conflicts(tools)
    _warn_on_legacy_tool_fields(tools)

    # Populate tools.network from legacy fields when network is absent.
    # New configs should prefer tools.network, but old fields remain compatible.
    if "network" not in tools:
        network: dict[str, Any] = {}
        if isinstance(exec_cfg, dict) and exec_cfg:
            network["exec"] = deepcopy(exec_cfg)

        web_cfg = tools.get("web", {})
        if isinstance(web_cfg, dict):
            search_cfg = web_cfg.get("search")
            fetch_cfg = web_cfg.get("fetch")
            if isinstance(search_cfg, dict) and search_cfg:
                network["search"] = deepcopy(search_cfg)
            if isinstance(fetch_cfg, dict) and fetch_cfg:
                network["fetch"] = deepcopy(fetch_cfg)

        if network:
            tools["network"] = network
    return data


def _warn_on_legacy_tool_fields(tools: dict[str, Any]) -> None:
    """Warn when legacy tool fields are used without tools.network."""
    if "network" in tools:
        return

    legacy_used = _detect_legacy_tool_fields(tools)

    if legacy_used:
        print(
            "Warning [DEPRECATION]: legacy tool config fields detected ("
            + ", ".join(legacy_used)
            + "). Prefer tools.network.* as canonical config. "
            + "Legacy fields remain compatible for now."
        )


def _detect_legacy_tool_fields(tools: dict[str, Any]) -> list[str]:
    """Return list of legacy tool config paths present in raw tools config."""
    legacy_used: list[str] = []
    if isinstance(tools.get("exec"), dict):
        legacy_used.append("tools.exec")
    web = tools.get("web")
    if isinstance(web, dict):
        if isinstance(web.get("search"), dict):
            legacy_used.append("tools.web.search")
        if isinstance(web.get("fetch"), dict):
            legacy_used.append("tools.web.fetch")
    return legacy_used


def _warn_on_network_legacy_conflicts(tools: dict[str, Any]) -> None:
    """Warn when both tools.network and legacy tool fields exist with conflicting values."""
    network = tools.get("network")
    if not isinstance(network, dict):
        return

    conflicts: list[str] = []

    legacy_exec = tools.get("exec")
    network_exec = network.get("exec")
    if isinstance(legacy_exec, dict) and isinstance(network_exec, dict):
        if legacy_exec != network_exec:
            conflicts.append("tools.exec vs tools.network.exec")

    web = tools.get("web")
    if isinstance(web, dict):
        legacy_search = web.get("search")
        network_search = network.get("search")
        if isinstance(legacy_search, dict) and isinstance(network_search, dict):
            if legacy_search != network_search:
                conflicts.append("tools.web.search vs tools.network.search")

        legacy_fetch = web.get("fetch")
        network_fetch = network.get("fetch")
        if isinstance(legacy_fetch, dict) and isinstance(network_fetch, dict):
            if legacy_fetch != network_fetch:
                conflicts.append("tools.web.fetch vs tools.network.fetch")

    if conflicts:
        print(
            "Warning: Detected conflicting tool config values; using tools.network as precedence: "
            + ", ".join(conflicts)
        )


def convert_keys(data: Any) -> Any:
    """Convert camelCase keys to snake_case for Pydantic."""
    if isinstance(data, dict):
        return {camel_to_snake(k): convert_keys(v) for k, v in data.items()}
    if isinstance(data, list):
        return [convert_keys(item) for item in data]
    return data


def convert_to_camel(data: Any) -> Any:
    """Convert snake_case keys to camelCase."""
    if isinstance(data, dict):
        return {snake_to_camel(k): convert_to_camel(v) for k, v in data.items()}
    if isinstance(data, list):
        return [convert_to_camel(item) for item in data]
    return data


def camel_to_snake(name: str) -> str:
    """Convert camelCase to snake_case."""
    result = []
    for i, char in enumerate(name):
        if char.isupper() and i > 0:
            result.append("_")
        result.append(char.lower())
    return "".join(result)


def snake_to_camel(name: str) -> str:
    """Convert snake_case to camelCase."""
    components = name.split("_")
    return components[0] + "".join(x.title() for x in components[1:])


