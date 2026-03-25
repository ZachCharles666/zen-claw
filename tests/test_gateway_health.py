import json
import socket
import urllib.error
import urllib.request

from zen_claw.cli.commands import _gateway_health_payload, _start_gateway_health_server
from zen_claw.dashboard.server import generate_api_key, store_api_key


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _request(url: str) -> tuple[int, str]:
    with urllib.request.urlopen(url, timeout=5) as resp:
        return int(resp.status), resp.read().decode("utf-8")


def _post(url: str, payload: dict[str, object], *, api_key: str | None = None) -> tuple[int, str]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if api_key:
        req.add_header("X-API-Key", api_key)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return int(resp.status), resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read().decode("utf-8")


def test_gateway_health_payload_shape() -> None:
    payload = _gateway_health_payload()
    assert payload["status"] == "ok"
    assert payload["service"] == "zen-claw"
    assert payload["surface"] == "gateway"


def test_gateway_health_server_exposes_api_health() -> None:
    port = _free_port()
    server = _start_gateway_health_server(port=port)
    try:
        status, body = _request(f"http://127.0.0.1:{port}/api/v1/health")
        assert status == 200
        payload = json.loads(body)
        assert payload["status"] == "ok"
        assert payload["surface"] == "gateway"
    finally:
        server.shutdown()
        server.server_close()


def test_gateway_health_server_exposes_healthz() -> None:
    port = _free_port()
    server = _start_gateway_health_server(port=port)
    try:
        status, body = _request(f"http://127.0.0.1:{port}/healthz")
        assert status == 200
        assert body == "ok"
    finally:
        server.shutdown()
        server.server_close()


def test_gateway_invoke_requires_api_key(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("zen_claw.config.loader.get_data_dir", lambda: tmp_path)
    port = _free_port()
    server = _start_gateway_health_server(port=port)
    try:
        status, body = _post(f"http://127.0.0.1:{port}/api/v1/agent/invoke", {"message": "hello"})
        assert status == 401
        payload = json.loads(body)
        assert payload["error"] == "invalid_api_key"
    finally:
        server.shutdown()
        server.server_close()


def test_gateway_invoke_returns_response_with_key(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("zen_claw.config.loader.get_data_dir", lambda: tmp_path)
    raw, _ = generate_api_key()
    store_api_key(raw)
    port = _free_port()
    server = _start_gateway_health_server(port=port)
    try:
        status, body = _post(
            f"http://127.0.0.1:{port}/api/v1/agent/invoke",
            {"message": "invoke-ok"},
            api_key=raw,
        )
        assert status == 200
        payload = json.loads(body)
        assert payload["response"]
        assert payload["session_id"]
    finally:
        server.shutdown()
        server.server_close()
