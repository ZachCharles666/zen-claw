from pathlib import Path

from zen_claw.session.manager import SessionManager
from zen_claw.utils.helpers import get_sessions_path


def test_session_manager_prefers_workspace_sessions_dir(tmp_path: Path) -> None:
    manager = SessionManager(tmp_path)
    session = manager.get_or_create("cli:direct")
    session.add_message("user", "hello")
    manager.save(session)

    assert manager.sessions_dir == tmp_path / ".zen-claw" / "sessions"
    assert manager.sessions_dir.exists()
    assert (manager.sessions_dir / "cli_direct.jsonl").exists()


def test_session_manager_falls_back_to_cwd_sessions_dir_when_workspace_unwritable(
    tmp_path: Path, monkeypatch
) -> None:
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    monkeypatch.chdir(repo_dir)

    workspace_sessions = tmp_path / ".zen-claw" / "sessions"
    cwd_sessions = repo_dir / ".zen-claw" / "sessions"
    home_data = tmp_path / "home" / ".zen-claw"

    def _fake_ensure_dir(path: Path) -> Path:
        if path == workspace_sessions:
            raise PermissionError("workspace is not writable")
        path.mkdir(parents=True, exist_ok=True)
        return path

    monkeypatch.setattr("zen_claw.utils.helpers.ensure_dir", _fake_ensure_dir)
    monkeypatch.setattr("zen_claw.utils.helpers.get_data_path", lambda: home_data)

    manager = SessionManager(tmp_path)

    assert manager.sessions_dir == cwd_sessions


def test_get_sessions_path_uses_tempdir_as_last_resort(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    cwd_sessions = repo_dir / ".zen-claw" / "sessions"
    home_sessions = tmp_path / "home" / ".zen-claw" / "sessions"
    temp_sessions = tmp_path / "temp" / "zen-claw" / "sessions"

    def _fake_ensure_dir(path: Path) -> Path:
        if path in {workspace / ".zen-claw" / "sessions", cwd_sessions, home_sessions}:
            raise PermissionError("blocked")
        path.mkdir(parents=True, exist_ok=True)
        return path

    monkeypatch.chdir(repo_dir)
    monkeypatch.setattr("zen_claw.utils.helpers.ensure_dir", _fake_ensure_dir)
    monkeypatch.setattr("zen_claw.utils.helpers.get_data_path", lambda: home_sessions.parent)
    monkeypatch.setattr("zen_claw.utils.helpers.gettempdir", lambda: str(tmp_path / "temp"))

    assert get_sessions_path(workspace) == temp_sessions
