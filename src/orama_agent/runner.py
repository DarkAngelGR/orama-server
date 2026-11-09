"""Runs ``docker compose`` for the apps project, with a fixed argv and a minimal env.

No shell is used and no string from a request reaches argv except an app name that is
an exact key of the catalogue. Every call pins the project directory, project name,
compose file and an Agent-owned empty ``--env-file``, so no ``.env`` file and no
``COMPOSE_*`` variable can change what compose does.

The one other command is Recyclarr's one-shot ``docker run``, whose argv is fixed from the
Agent's own roots and the catalogue: nothing from a request reaches it at all. Its
container has a fixed name and is removed (``docker rm -f``) before every run and after a
failed one, so a CLI killed by the timeout never leaves Recyclarr running with the keys.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from orama.catalog import CATALOG
from orama.layout import app_config_dir, app_env_dir
from orama_agent.recyclarr import CONTAINER_CONFIG
from orama_agent.validate import PROJECT_NAME, SHARED_NETWORK, Refused, Roots

COMPOSE_FILE = "compose.yaml"
CORE_PROJECT_NAME = "orama-core"
CORE_COMPOSE_FILE = "core.compose.yaml"
AGENT_ENV_FILE = "compose.env"  # empty, lives in app-env (root 0700)
# Worst case for one apply: ps (60) + pull (3600) + up (720) + ps (60) = 4440 s, which
# leaves a 60 s margin under the client's default of 4500 s
# (orama.agent_client.DEFAULT_TIMEOUT).
PULL_TIMEOUT = 3600  # a first install downloads every app image
UP_TIMEOUT = 720  # up, restart, stop and start
CALL_TIMEOUT = 60  # ps
RECYCLARR_TIMEOUT = 900  # one sync of Sonarr and Radarr, including its guide download
RECYCLARR_CONTAINER = "orama-recyclarr"
STDERR_TAIL_LINES = 40
LOG_HINT = "To see why, run: sudo docker logs orama-server-agent"
_WHAT = {
    "pull": "Downloading the apps",
    "up": "Starting the apps",
    "ps": "Checking the apps",
    "restart": "Restarting the app",
    "stop": "Stopping the app",
    "start": "Starting the app",
    "recyclarr": "Updating the quality profiles",
    "self-update": "Updating Orama",
}
_DEFAULT_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


class RunnerError(Exception):
    """docker compose failed. The message is plain and safe to show and log.

    ``detail`` is the tail of compose's stderr. It may hold secrets (compose can echo
    container settings): it is never sent to the client, and is logged only redacted.
    """

    def __init__(self, message: str, detail: str = "") -> None:
        super().__init__(message)
        self.detail = detail


def _tail(text: str | bytes | None) -> str:
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    return "\n".join((text or "").splitlines()[-STDERR_TAIL_LINES:])


def project_file(roots: Roots) -> Path:
    return roots.config / COMPOSE_FILE


def core_project_file(roots: Roots) -> Path:
    return roots.config / CORE_COMPOSE_FILE


def agent_env_file(roots: Roots) -> Path:
    return Path(app_env_dir(PurePosixPath(roots.config))) / AGENT_ENV_FILE


def _child_env() -> dict[str, str]:
    env = {"PATH": os.environ.get("PATH") or _DEFAULT_PATH, "HOME": os.environ.get("HOME", "/root")}
    if os.environ.get("DOCKER_HOST"):
        env["DOCKER_HOST"] = os.environ["DOCKER_HOST"]
    return env


def parse_ps(output: str) -> list[dict[str, Any]]:
    """Parse ``docker compose ps --format json``: a JSON array or one object per line."""
    text = output.strip()
    if not text:
        return []
    if text.startswith("["):
        items = json.loads(text)
    else:
        items = [json.loads(line) for line in text.splitlines() if line.strip()]
    return [item for item in items if isinstance(item, dict)]


def _checked_app(app: str) -> str:
    if not isinstance(app, str) or app not in CATALOG:
        raise Refused("That is not an app Orama knows about.")
    return app


class ComposeRunner:
    def __init__(
        self, roots: Roots, run: Callable[..., subprocess.CompletedProcess] = subprocess.run
    ) -> None:
        self._roots = roots
        self._run = run

    def _prefix(self, file: str, project: str = PROJECT_NAME) -> list[str]:
        return [
            "docker", "compose",
            "--project-directory", str(self._roots.config),
            "--env-file", str(agent_env_file(self._roots)),
            "-p", project,
            "-f", file,
        ]

    def _compose(self, file: str, args: list[str], timeout: int) -> str:
        return self._exec(self._prefix(file) + args, _WHAT[args[0]], timeout)

    def _exec(self, argv: list[str], what: str, timeout: int) -> str:
        try:
            proc = self._run(
                argv, env=_child_env(), capture_output=True, text=True, timeout=timeout,
                check=False, shell=False,
            )
        except subprocess.TimeoutExpired as e:
            raise RunnerError(f"{what} did not finish in time. {LOG_HINT}", _tail(e.stderr)) from None
        except OSError as e:
            raise RunnerError(
                f"{what} failed: Docker Compose could not be started. {LOG_HINT}",
                f"{type(e).__name__}: {e.strerror or ''}",
            ) from None
        if proc.returncode != 0:
            raise RunnerError(
                f"{what} failed. {LOG_HINT}",
                f"exit code {proc.returncode}\n{_tail(proc.stderr)}",
            )
        return proc.stdout

    def _containers(self, file: str) -> list[dict[str, Any]]:
        out = self._compose(file, ["ps", "-a", "--format", "json"], CALL_TIMEOUT)
        try:
            return parse_ps(out)
        except ValueError:
            raise RunnerError(
                f"Checking the apps failed: Docker Compose printed something Orama cannot "
                f"read. {LOG_HINT}"
            ) from None

    def up(self, project_file: str) -> list[str]:
        """Bring the project up; return services whose container is new or recreated."""
        before = {c.get("Service"): c.get("ID") for c in self._containers(project_file)}
        self._compose(project_file, ["pull", "--quiet"], PULL_TIMEOUT)
        self._compose(
            project_file, ["up", "-d", "--remove-orphans", "--wait"], UP_TIMEOUT
        )
        after = {c.get("Service"): c.get("ID") for c in self._containers(project_file)}
        return sorted(
            s for s, cid in after.items() if isinstance(s, str) and before.get(s) != cid
        )

    def ps(self) -> dict[str, str]:
        return {
            c["Service"]: str(c.get("State", ""))
            for c in self._containers(str(project_file(self._roots)))
            if isinstance(c.get("Service"), str)
        }

    def restart(self, app: str) -> None:
        self._one_app("restart", app)

    def stop(self, app: str) -> None:
        self._one_app("stop", app)

    def start(self, app: str) -> None:
        self._one_app("start", app)

    def _one_app(self, action: str, app: str) -> None:
        self._compose(str(project_file(self._roots)), [action, _checked_app(app)], UP_TIMEOUT)

    def self_update(self) -> None:
        """Pull the core images and recreate the core project from core.compose.yaml."""
        file = str(core_project_file(self._roots))
        prefix = self._prefix(file, CORE_PROJECT_NAME)
        what = _WHAT["self-update"]
        self._exec(prefix + ["pull", "--quiet"], what, PULL_TIMEOUT)
        self._exec(prefix + ["up", "-d", "--remove-orphans", "--wait"], what, UP_TIMEOUT)

    def recyclarr_sync(self) -> None:
        """Run Recyclarr once on the config the Agent wrote, as PUID:PGID on orama-net."""
        folder = app_config_dir(PurePosixPath(self._roots.config), "recyclarr")
        argv = [
            "docker", "run", "--rm",
            "--name", RECYCLARR_CONTAINER,
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--network", SHARED_NETWORK,
            "--user", f"{self._roots.puid}:{self._roots.pgid}",
            "-v", f"{folder}:/config",
            CATALOG["recyclarr"].image,
            "sync", "--config", CONTAINER_CONFIG,
        ]
        self._remove_recyclarr()
        try:
            self._exec(argv, _WHAT["recyclarr"], RECYCLARR_TIMEOUT)
        except RunnerError:
            try:  # a timed-out CLI leaves the container running: stop it
                self._remove_recyclarr()
            except RunnerError:
                pass  # the run's own error is the one to report
            raise

    def _remove_recyclarr(self) -> None:
        """``docker rm -f`` Recyclarr's container; none being there is fine."""
        argv = ["docker", "rm", "-f", RECYCLARR_CONTAINER]
        what = _WHAT["recyclarr"]
        try:
            proc = self._run(
                argv, env=_child_env(), capture_output=True, text=True, timeout=CALL_TIMEOUT,
                check=False, shell=False,
            )
        except subprocess.TimeoutExpired as e:
            raise RunnerError(f"{what} did not finish in time. {LOG_HINT}", _tail(e.stderr)) from None
        except OSError as e:
            raise RunnerError(
                f"{what} failed: Docker could not be started. {LOG_HINT}",
                f"{type(e).__name__}: {e.strerror or ''}",
            ) from None
        if proc.returncode != 0 and "no such container" not in (proc.stderr or "").lower():
            raise RunnerError(
                f"{what} failed. {LOG_HINT}", f"exit code {proc.returncode}\n{_tail(proc.stderr)}"
            )


class FakeRunner:
    """Test double: same methods, records calls, no Docker.

    ``stop`` and ``start`` flip the service's state in ``ps`` between exited and running.

    ``up`` returns every service the first time and ``[]`` while the compose file is
    unchanged since the last successful ``up``. ``fail_next_up`` makes the next ``up``
    raise once, without recording success.

    ``recyclarr_sync`` counts calls and runs ``on_recyclarr`` (if set) while the config
    file exists; ``fail_next_recyclarr`` makes the next one fail once.
    """

    def __init__(self) -> None:
        self.up_calls = 0
        self.up_files: list[str] = []
        self.restarts: list[str] = []
        self.stops: list[str] = []
        self.starts: list[str] = []
        self._stopped: set[str] = set()
        self.fail_next_up = False
        self._last: str | None = None
        self._services: list[str] = []
        self.recyclarr_calls = 0
        self.on_recyclarr: Callable[[], None] | None = None
        self.fail_next_recyclarr = False
        self.self_update_calls = 0
        self.fail_next_self_update = False

    def up(self, project_file: str) -> list[str]:
        self.up_calls += 1
        self.up_files.append(project_file)
        if self.fail_next_up:
            self.fail_next_up = False
            raise RunnerError(f"Starting the apps failed. {LOG_HINT}", "exit code 1")
        content = Path(project_file).read_text()
        if content == self._last:
            return []
        self._last = content
        self._services = sorted(yaml.safe_load(content)["services"])
        return list(self._services)

    def ps(self) -> dict[str, str]:
        return {s: "exited" if s in self._stopped else "running" for s in self._services}

    def restart(self, app: str) -> None:
        self.restarts.append(_checked_app(app))

    def stop(self, app: str) -> None:
        self.stops.append(_checked_app(app))
        self._stopped.add(app)

    def start(self, app: str) -> None:
        self.starts.append(_checked_app(app))
        self._stopped.discard(app)

    def self_update(self) -> None:
        self.self_update_calls += 1
        if self.fail_next_self_update:
            self.fail_next_self_update = False
            raise RunnerError(f"{_WHAT['self-update']} failed. {LOG_HINT}", "exit code 1")

    def recyclarr_sync(self) -> None:
        self.recyclarr_calls += 1
        if self.on_recyclarr is not None:
            self.on_recyclarr()
        if self.fail_next_recyclarr:
            self.fail_next_recyclarr = False
            raise RunnerError(f"{_WHAT['recyclarr']} failed. {LOG_HINT}", "exit code 1")
