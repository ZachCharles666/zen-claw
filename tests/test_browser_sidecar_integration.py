import base64
import json
import os
import socket
import subprocess
import time
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import pytest

from zen_claw.agent.tools.browser import (
    BrowserClickTool,
    BrowserExtractTool,
    BrowserOpenTool,
    BrowserScreenshotTool,
    BrowserTypeTool,
)


def _find_free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class _PageHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if urlparse(self.path).path == "/slow":
            time.sleep(2.2)
        body = b"""<!doctype html><html><body>
<h1 id='title'>Nano Claw Browser E2E</h1>
<input id='q' value='' />
<button id='btn' onclick="document.getElementById('title').innerText='Clicked'">go</button>
<p>ok</p>
</body></html>"""
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):  # noqa: A003
        return


def _wait_http_ok(url: str, timeout_sec: float = 15.0) -> bool:
    import urllib.error
    import urllib.request

    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.0) as resp:
                if int(resp.status) < 400:
                    return True
        except (urllib.error.URLError, TimeoutError, ValueError):
            time.sleep(0.2)
    return False


def _is_env_browser_launch_blocked(error_message: str) -> bool:
    msg = str(error_message or "").lower()
    return "spawn eperm" in msg or "browsertype.launch" in msg and "eperm" in msg


def _is_env_sidecar_unhealthy(code: str) -> bool:
    return str(code or "") in {"browser_sidecar_unhealthy", "browser_sidecar_unreachable"}


def _start_sidecar(*, allow_domains: str = "127.0.0.1,localhost", timeout_sec: int | None = None) -> tuple[subprocess.Popen[str], str]:
    sidecar_dir = Path("browser/sidecar")
    node = "node.exe" if os.name == "nt" else "node"
    sidecar_port = _find_free_port()
    env = os.environ.copy()
    env["BROWSER_SIDECAR_BIND"] = f"127.0.0.1:{sidecar_port}"
    env["BROWSER_SIDECAR_ALLOW_DOMAINS"] = allow_domains
    if timeout_sec is not None:
        env["BROWSER_SIDECAR_TIMEOUT_SEC"] = str(timeout_sec)
    proc = subprocess.Popen(
        [node, "server.js"],
        cwd=str(sidecar_dir),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    sidecar_url = f"http://127.0.0.1:{sidecar_port}/v1/browser"
    return proc, sidecar_url


@pytest.mark.asyncio
async def test_browser_sidecar_e2e_open_extract_screenshot(tmp_path: Path) -> None:
    sidecar_dir = Path("browser/sidecar")
    if not sidecar_dir.joinpath("server.js").exists():
        pytest.skip("browser sidecar server.js not found")

    node = "node.exe" if os.name == "nt" else "node"
    if not shutil_which(node):
        pytest.skip("node not installed")

    if not sidecar_dir.joinpath("node_modules").exists():
        pytest.skip("browser sidecar deps not installed (run npm install in browser/sidecar)")

    page_port = _find_free_port()
    page_server = ThreadingHTTPServer(("127.0.0.1", page_port), _PageHandler)
    import threading

    t = threading.Thread(target=page_server.serve_forever, daemon=True)
    t.start()

    proc, sidecar_url = _start_sidecar(allow_domains="127.0.0.1,localhost")
    try:
        assert _wait_http_ok(sidecar_url.replace("/v1/browser", "/healthz"), timeout_sec=25.0)
        target_url = f"http://127.0.0.1:{page_port}/"

        open_tool = BrowserOpenTool(
            mode="sidecar",
            sidecar_url=sidecar_url,
            sidecar_healthcheck=True,
            allowed_domains=["127.0.0.1", "localhost"],
        )
        opened = await open_tool.execute(url=target_url)
        if not opened.ok and opened.error and _is_env_sidecar_unhealthy(opened.error.code):
            pytest.skip("browser sidecar health is unstable in current environment")
        if not opened.ok and opened.error and _is_env_browser_launch_blocked(opened.error.message):
            pytest.skip("browser launch blocked by environment permissions")
        assert opened.ok is True
        opened_data = json.loads(opened.content)
        session_id = opened_data["session_id"]
        assert session_id

        extract_tool = BrowserExtractTool(
            mode="sidecar",
            sidecar_url=sidecar_url,
            sidecar_healthcheck=True,
            allowed_domains=["127.0.0.1", "localhost"],
        )
        extracted = await extract_tool.execute(sessionId=session_id, selector="#title")
        assert extracted.ok is True
        extracted_data = json.loads(extracted.content)
        assert "Nano Claw Browser E2E" in extracted_data.get("text", "")

        screenshot_tool = BrowserScreenshotTool(
            mode="sidecar",
            sidecar_url=sidecar_url,
            sidecar_healthcheck=True,
            allowed_domains=["127.0.0.1", "localhost"],
        )
        shot = await screenshot_tool.execute(sessionId=session_id, fullPage=True)
        assert shot.ok is True
        shot_data = json.loads(shot.content)
        image_b64 = str(shot_data.get("image_base64") or "")
        assert len(image_b64) > 100
        _ = base64.b64decode(image_b64.encode("utf-8"))

        type_tool = BrowserTypeTool(
            mode="sidecar",
            sidecar_url=sidecar_url,
            sidecar_healthcheck=True,
            allowed_domains=["127.0.0.1", "localhost"],
        )
        typed = await type_tool.execute(sessionId=session_id, selector="#q", text="zen-claw", clear=True)
        assert typed.ok is True

        click_tool = BrowserClickTool(
            mode="sidecar",
            sidecar_url=sidecar_url,
            sidecar_healthcheck=True,
            allowed_domains=["127.0.0.1", "localhost"],
        )
        clicked = await click_tool.execute(sessionId=session_id, selector="#btn")
        assert clicked.ok is True
        extracted2 = await extract_tool.execute(sessionId=session_id, selector="#title")
        assert extracted2.ok is True
        assert "Clicked" in json.loads(extracted2.content).get("text", "")
    finally:
        page_server.shutdown()
        page_server.server_close()
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)


@pytest.mark.asyncio
async def test_browser_sidecar_e2e_domain_denied() -> None:
    sidecar_dir = Path("browser/sidecar")
    if not sidecar_dir.joinpath("server.js").exists():
        pytest.skip("browser sidecar server.js not found")
    if not sidecar_dir.joinpath("node_modules").exists():
        pytest.skip("browser sidecar deps not installed (run npm install in browser/sidecar)")

    proc, sidecar_url = _start_sidecar(allow_domains="127.0.0.1,localhost")
    try:
        assert _wait_http_ok(sidecar_url.replace("/v1/browser", "/healthz"), timeout_sec=25.0)
        open_tool = BrowserOpenTool(
            mode="sidecar",
            sidecar_url=sidecar_url,
            sidecar_healthcheck=True,
            allowed_domains=["example.com"],
            blocked_domains=["example.com"],
        )
        denied = await open_tool.execute(url="http://example.com/")
        if not denied.ok and denied.error and _is_env_sidecar_unhealthy(denied.error.code):
            pytest.skip("browser sidecar health is unstable in current environment")
        assert denied.ok is False
        assert denied.error is not None
        assert denied.error.code == "domain_denied"
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)


@pytest.mark.asyncio
async def test_browser_sidecar_e2e_open_timeout() -> None:
    sidecar_dir = Path("browser/sidecar")
    if not sidecar_dir.joinpath("server.js").exists():
        pytest.skip("browser sidecar server.js not found")
    if not sidecar_dir.joinpath("node_modules").exists():
        pytest.skip("browser sidecar deps not installed (run npm install in browser/sidecar)")

    page_port = _find_free_port()
    page_server = ThreadingHTTPServer(("127.0.0.1", page_port), _PageHandler)
    import threading

    t = threading.Thread(target=page_server.serve_forever, daemon=True)
    t.start()
    proc, sidecar_url = _start_sidecar(allow_domains="127.0.0.1,localhost", timeout_sec=1)
    try:
        assert _wait_http_ok(sidecar_url.replace("/v1/browser", "/healthz"), timeout_sec=25.0)
        open_tool = BrowserOpenTool(
            mode="sidecar",
            sidecar_url=sidecar_url,
            sidecar_healthcheck=True,
            allowed_domains=["127.0.0.1", "localhost"],
            timeout_sec=8,
        )
        timeout_result = await open_tool.execute(url=f"http://127.0.0.1:{page_port}/slow")
        if not timeout_result.ok and timeout_result.error and _is_env_sidecar_unhealthy(timeout_result.error.code):
            pytest.skip("browser sidecar health is unstable in current environment")
        if not timeout_result.ok and timeout_result.error and _is_env_browser_launch_blocked(timeout_result.error.message):
            pytest.skip("browser launch blocked by environment permissions")
        assert timeout_result.ok is False
        assert timeout_result.error is not None
        assert timeout_result.error.code == "browser_action_failed"
        assert "Timeout" in timeout_result.error.message
    finally:
        page_server.shutdown()
        page_server.server_close()
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)


def shutil_which(name: str) -> str | None:
    import shutil

    return shutil.which(name)
