"""SSH tunnel helper: reach Postgres on Altare when not directly routed."""
from __future__ import annotations

import socket
import subprocess
import time


def port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        return False


class SshTunnel:
    """Reach Postgres on Altare when the intranet address is not routed here."""

    def __init__(self, ssh_host: str, remote_port: int) -> None:
        self.ssh_host = ssh_host
        self.remote_port = remote_port
        self.local_port = 15432
        self.process: subprocess.Popen[bytes] | None = None

    def __enter__(self) -> int:
        while port_open(self.local_port):
            self.local_port += 1
        self.process = subprocess.Popen(
            [
                "ssh", "-N", "-o", "BatchMode=yes", "-o", "ExitOnForwardFailure=yes",
                "-L", f"127.0.0.1:{self.local_port}:127.0.0.1:{self.remote_port}",
                self.ssh_host,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                error = self.process.stderr.read().decode("utf-8", errors="replace") if self.process.stderr else ""
                raise SystemExit(f"SSH tunnel to {self.ssh_host} failed: {error.strip()}")
            if port_open(self.local_port):
                return self.local_port
            time.sleep(0.2)
        raise SystemExit(f"SSH tunnel to {self.ssh_host} did not open")

    def __exit__(self, *args: object) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
