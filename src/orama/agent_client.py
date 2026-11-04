"""Orama's client for the Agent socket: one connection and one JSON line per call."""

from __future__ import annotations

import json
import socket
import uuid
from typing import Any

DEFAULT_SOCKET = "/run/orama/agent.sock"
MAX_RESPONSE = 16 * 1024 * 1024
# Strictly above the Agent's worst case for one apply: ps 60 + pull 3600 + up 720 + ps 60
# = 4440 s (orama_agent.runner.CALL_TIMEOUT / PULL_TIMEOUT / UP_TIMEOUT), plus a 60 s margin.
DEFAULT_TIMEOUT = 4500.0


class AgentError(Exception):
    """The Agent refused or failed a command; the message is safe to show."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class AgentClient:
    def __init__(
        self, socket_path: str = DEFAULT_SOCKET, timeout: float = DEFAULT_TIMEOUT
    ) -> None:
        self.socket_path = socket_path
        self.timeout = timeout

    def ensure_folders(self, paths: list[str]) -> list[str]:
        return self._call("ensure-folders", {"paths": list(paths)})

    def apply(self, compose: dict, env_files: dict[str, dict[str, str]]) -> list[str]:
        return self._call("apply", {"compose": compose, "env_files": env_files})["changed"]

    def status(self) -> dict[str, str]:
        return self._call("status", {})

    def restart(self, app: str) -> None:
        self._call("restart", {"app": app})

    def seed_config(self, app: str, values: dict[str, str]) -> bool:
        """Write fixed credential keys into SABnzbd's or qBittorrent's config file.

        True if the file changed (the app was stopped and started again if it was running).
        """
        return self._call("seed-config", {"app": app, "values": dict(values)})["changed"]

    def _call(self, cmd: str, args: dict[str, Any]) -> Any:
        rid = uuid.uuid4().hex
        request = json.dumps({"id": rid, "cmd": cmd, "args": args}).encode() + b"\n"
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(self.timeout)
                sock.connect(self.socket_path)
                sock.sendall(request)
                line = _read_line(sock)
        except TimeoutError:
            raise AgentError(f"The Agent did not answer {cmd} in time.") from None
        except OSError as e:
            reason = e.strerror or type(e).__name__
            raise AgentError(f"Cannot reach the Agent ({reason}).") from None
        try:
            response = json.loads(line)
        except ValueError:
            raise AgentError("The Agent sent an answer Orama cannot read.") from None
        if not isinstance(response, dict) or response.get("id") != rid:
            raise AgentError("The Agent sent an answer to a different request.")
        if response.get("ok") is not True:
            raise AgentError(str(response.get("error") or "The Agent refused the request."))
        return response.get("result")


def _read_line(sock: socket.socket) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while True:
        chunk = sock.recv(65536)
        if not chunk:
            raise AgentError("The Agent closed the connection without answering.")
        chunks.append(chunk)
        size += len(chunk)
        if b"\n" in chunk:
            return b"".join(chunks).split(b"\n", 1)[0]
        if size > MAX_RESPONSE:
            raise AgentError("The Agent's answer is too large.")
