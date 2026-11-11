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
# Updating Orama itself. Download: pull 1200 + two image checks of 60 = 1320 s
# (orama_agent.runner.CORE_PULL_TIMEOUT / CALL_TIMEOUT). Restart: four calls of 60 = 240 s;
# Orama holds apply.lock while it waits for this one, so it is kept short.
SELF_UPDATE_PULL_TIMEOUT = 1500.0
SELF_UPDATE_TIMEOUT = 300.0


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

    def ensure_folders(self, paths: list[str]) -> dict[str, list[str]]:
        """``{"created": [...], "not_writable": [...]}``: the folders the Agent made, and
        the existing ones the apps (PUID:PGID) cannot write to (left unchanged)."""
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

    def recyclarr_sync(self, apps: dict[str, Any], force: bool = False) -> bool:
        """Sync Sonarr's/Radarr's quality profiles: ``{app: {"templates", "scores"}}``.

        True if Recyclarr ran; False if this exact choice already synced (unless ``force``).
        """
        args: dict[str, Any] = {"apps": apps}
        if force:
            args["force"] = True
        return self._call("recyclarr-sync", args)["changed"]

    def data_space(self) -> dict[str, int]:
        """Return ``{"total_bytes": int, "free_bytes": int}`` for the configured data root."""
        return self._call("data-space", {})

    def self_update_pull(self) -> bool:
        """Download the newest Orama and Agent images; nothing is restarted.

        True if a restart (:meth:`self_update`) would change anything. Can take minutes.
        """
        result = self._call("self-update-pull", {}, self._limit(SELF_UPDATE_PULL_TIMEOUT))
        return bool(result["newer"])

    def self_update(self) -> bool:
        """Restart Orama and the Agent on the images already downloaded.

        True if the restart was started (this Orama process is then replaced within
        seconds); False if both already run the newest downloaded version.
        """
        return bool(self._call("self-update", {}, self._limit(SELF_UPDATE_TIMEOUT))["started"])

    def _limit(self, seconds: float) -> float:
        """A step's own time limit, never above the one this client was made with."""
        return min(self.timeout, seconds)

    def _call(self, cmd: str, args: dict[str, Any], timeout: float | None = None) -> Any:
        rid = uuid.uuid4().hex
        request = json.dumps({"id": rid, "cmd": cmd, "args": args}).encode() + b"\n"
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(self.timeout if timeout is None else timeout)
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
