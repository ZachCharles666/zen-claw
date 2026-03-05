import logging
import shutil
import signal
import subprocess
import sys
import threading
import time
from typing import Dict, Optional

logger = logging.getLogger("zen_claw.tunnel")


class TunnelManager:
    """
    Manages the lifecycle of the cloudflared tunneling subprocess.
    """

    def __init__(self, port: int = 8000, tunnel_token: Optional[str] = None):
        self.port = port
        self.tunnel_token = tunnel_token
        self.process: Optional[subprocess.Popen] = None
        self._running = False
        self._monitor_thread: Optional[threading.Thread] = None
        self.metrics = {"restarts": 0, "start_time": None, "last_error": None}

    def _get_cloudflared_path(self) -> str:
        """Finds the cloudflared executable in the system PATH."""
        path = shutil.which("cloudflared")
        if not path:
            raise FileNotFoundError(
                "cloudflared executable not found in PATH. Please install it first."
            )
        return path

    def _build_command(self) -> list:
        """Builds the cloudflared command."""
        cmd = [self._get_cloudflared_path(), "tunnel"]

        # If token is provided, run named tunnel, otherwise run quick tunnel
        if self.tunnel_token:
            cmd.extend(["--no-autoupdate", "run", "--token", self.tunnel_token])
        else:
            # Quick tunnel
            cmd.extend(["--url", f"http://localhost:{self.port}"])

        return cmd

    def start(self):
        """Starts the cloudflared subprocess."""
        if self.process and self.process.poll() is None:
            logger.warning("Tunnel is already running.")
            return

        cmd = self._build_command()
        logger.info(f"Starting cloudflared tunnel: {' '.join(cmd)}")

        try:
            # We explicitly do not capture stdout/stderr here yet, mostly leaving it to the console or capturing it for diagnostic parsing later.
            # In a production setup, we should redirect to a specific log file or ingest it.
            self.process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1
            )
            self._running = True
            self.metrics["start_time"] = time.time()

            # Start the monitor thread to read logs and watch process health
            self._monitor_thread = threading.Thread(target=self._monitor_process, daemon=True)
            self._monitor_thread.start()

            logger.info(f"Tunnel successfully started with PID: {self.process.pid}")

        except Exception as e:
            self.metrics["last_error"] = str(e)
            logger.error(f"Failed to start tunnel: {e}")
            raise

    def _monitor_process(self):
        """Monitors the cloudflared stdout and lifecycle."""
        if not self.process or not self.process.stdout:
            return

        # Read the subprocess output line by line
        for line in iter(self.process.stdout.readline, ""):
            line = line.strip()
            if not line:
                continue

            # Naive parsing of the TryCloudflare URL for quick tunnels
            if "trycloudflare.com" in line:
                logger.info(f"[Tunnel Output] {line}")
            else:
                logger.debug(f"[Tunnel Output] {line}")

        # If we reach here, the process has terminated
        self.process.wait()
        exit_code = self.process.returncode
        self._running = False

        msg = f"Tunnel process exited with code {exit_code}"
        if exit_code != 0 and exit_code != -signal.SIGTERM and exit_code != -signal.SIGINT:
            logger.error(msg)
            self.metrics["last_error"] = msg
            self._handle_crash()
        else:
            logger.info(msg)

    def _handle_crash(self):
        """Handles unexpected process exit with exponential backoff restarting (SLO)."""
        # Implement backoff logic here if needed
        pass

    def stop(self):
        """Stops the cloudflared subprocess."""
        if self.process and self.process.poll() is None:
            logger.info(f"Stopping tunnel PID: {self.process.pid}")
            self._running = False

            if sys.platform == "win32":
                # Windows doesn't easily support SIGTERM on subprocesses without sending it to the whole group,
                # but terminate() usually acts as a hard kill or close.
                self.process.terminate()
            else:
                self.process.send_signal(signal.SIGTERM)

            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                logger.warning("Tunnel did not stop gracefully, killing it.")
                self.process.kill()
                self.process.wait()

            logger.info("Tunnel stopped.")

    def is_running(self) -> bool:
        """Returns True if the tunnel is currently active."""
        return self._running and self.process is not None and self.process.poll() is None

    def get_status(self) -> Dict:
        """Returns diagnostic metrics and status."""
        return {
            "running": self.is_running(),
            "pid": self.process.pid if self.is_running() else None,
            "restarts": self.metrics["restarts"],
            "uptime_seconds": time.time() - self.metrics["start_time"]
            if self.metrics["start_time"]
            else 0,
            "last_error": self.metrics["last_error"],
        }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    logger.info("Starting Tunnel Manager dev-test...")
    # This is a basic test runner
    tm = TunnelManager(port=8000)
    try:
        tm.start()
        time.sleep(10)  # Let it run for a bit
    except KeyboardInterrupt:
        logger.info("Interrupted.")
    finally:
        tm.stop()
