import json
import socket
import urllib.request

from zen_claw.cli.commands import _gateway_health_payload, _start_gateway_health_server


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _request(url: str) -> tuple[int, str]:
    with urllib.request.urlopen(url, timeout=5) as resp:
        return int(resp.status), resp.read().decode("utf-8")


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
