"""Runs ``docker compose`` for the apps project, with a fixed argv and a minimal env.

No shell is used and no string from a request reaches argv except an app name that is
an exact key of the catalogue. Every call pins the project directory, project name,
compose file and an Agent-owned empty ``--env-file``, so no ``.env`` file and no
``COMPOSE_*`` variable can change what compose does.

The one other command is Recyclarr's one-shot ``docker run``, whose argv is fixed from the
Agent's own roots and the catalogue: nothing from a request reaches it at all. Its
container has a fixed name and is removed (``docker rm -f``) before every run and after a
failed one, so a CLI killed by the timeout never leaves Recyclarr running with the keys.

Updating Orama itself is two steps, neither of which takes anything from a request:

- ``self_update_pull`` downloads the two core images named in the installer's own
  ``core.compose.yaml``. Nothing is stopped or started, so a failed or offline download
  leaves every container as it was.
- ``self_update`` recreates the core project from what is already downloaded. The Agent
  cannot do that itself (compose would stop the Agent in the middle of its own command),
  so it starts a short-lived helper container from the Agent image, with a fixed argv. The
  helper needs the Docker socket and reads the config root, and nothing else: it has no
  network (the Docker daemon is what downloads images, never the helper), no capabilities,
  no way to gain privileges, a read-only root file system, the config root mounted
  read-only, and process and memory limits. It is kept after it exits so that
  ``docker logs orama-updater`` says what the last update did; the next update removes it.
  When every core container already runs the image its name points at, no helper is started.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
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
# Orama's own two images are small. Worst case for the download step: pull (1200) + two
# image checks (60 each) = 1320 s; for the restart step: two image checks, rm and run
# (60 each) = 240 s. The client's limits for both sit above these
# (orama.agent_client.SELF_UPDATE_PULL_TIMEOUT / SELF_UPDATE_TIMEOUT).
CORE_PULL_TIMEOUT = 1200
UP_TIMEOUT = 720  # up, restart, stop and start
CALL_TIMEOUT = 60  # ps
RECYCLARR_TIMEOUT = 900  # one sync of Sonarr and Radarr, including its guide download
RECYCLARR_CONTAINER = "orama-recyclarr"
UPDATER_CONTAINER = "orama-updater"
UPDATER_WAIT_SECONDS = "300"  # then the helper gives up instead of waiting for ever
UPDATER_TMPFS = "/tmp:rw,noexec,nosuid,nodev,size=16m"
UPDATER_PIDS = "256"
UPDATER_MEMORY = "512m"
DOCKER_SOCKET = "/var/run/docker.sock"
# The core project's services and their fixed container names (orama.compose).
CORE_CONTAINERS = {"orama": "orama-server", "orama-agent": "orama-server-agent"}
PULL_FAILED = ("Orama could not download the update, so nothing was changed. "
               "Check the internet connection and try again.")
CORE_FILE_BAD = ("Orama's own settings file (core.compose.yaml) is not as the installer wrote "
                 "it. Run the installer again.")
# An image name as Docker spells it: lower-case repository, optional tag, optional digest.
# It can never start with "-" (an option) or hold a space, so it is safe as one argv item.
_IMAGE_REF = re.compile(
    r"[a-z0-9][a-z0-9._/-]{0,199}(?::[0-9]{1,5}/[a-z0-9][a-z0-9._/-]{0,199})?"
    r"(?::[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})?(?:@sha256:[0-9a-f]{64})?"
)
_IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}")
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
    "self-update-pull": "Downloading the Orama update",
    "self-update": "Updating Orama",
}
_DEFAULT_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
_TRANSIENT_PULL_MARKERS = (
    "toomanyrequests",
    "rate limit",
    "429 too many requests",
    "tls handshake timeout",
    "connection reset by peer",
    "temporary failure in name resolution",
)


def _is_transient_pull_error(detail: str) -> bool:
    lowered = (detail or "").lower()
    return any(marker in lowered for marker in _TRANSIENT_PULL_MARKERS)


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


def _docker_socket_gid() -> int:
    """The group of the Docker socket as the Agent sees it (the host's numeric id)."""
    return os.stat(DOCKER_SOCKET).st_gid


def _checked_app(app: str) -> str:
    if not isinstance(app, str) or app not in CATALOG:
        raise Refused("That is not an app Orama knows about.")
    return app


class ComposeRunner:
    def __init__(
        self, roots: Roots, run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        socket_gid: Callable[[], int] = _docker_socket_gid,
    ) -> None:
        self._roots = roots
        self._run = run
        self._socket_gid = socket_gid

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

    def _pull(self, file: str, project: str = PROJECT_NAME, what: str = _WHAT["pull"],
              budget: int = PULL_TIMEOUT) -> None:
        """Pull all images for ``file``; fall back to sequential pulls on burst rate limits.

        Everything, retries included, ends within ``budget`` seconds.
        """
        prefix = self._prefix(file, project)
        deadline = time.monotonic() + budget
        try:
            self._exec(prefix + ["pull", "--quiet"], what, budget)
            return
        except RunnerError as err:
            if not _is_transient_pull_error(err.detail):
                raise
            try:
                doc = yaml.safe_load(Path(file).read_text())
                services = sorted(
                    s for s in (doc.get("services") or {}) if isinstance(s, str)
                )
            except (OSError, yaml.YAMLError, AttributeError, TypeError):
                raise err from None
            if not services:
                raise
        for service in services:
            for attempt in range(3):
                remaining = int(deadline - time.monotonic())
                if remaining <= 0:
                    raise RunnerError(
                        f"{what} did not finish in time. {LOG_HINT}",
                        "Pull timeout exceeded",
                    )
                timeout = min(remaining, 600)
                try:
                    self._exec(prefix + ["pull", "--quiet", service], what, timeout)
                    break
                except RunnerError as service_err:
                    if attempt == 2 or not _is_transient_pull_error(service_err.detail):
                        raise
                    time.sleep(min(2.0, 0.5 * (attempt + 1)))

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
        self._pull(project_file)
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

    def _core_images(self, file: str) -> dict[str, str]:
        """``{service: image}`` for the two core services, from the installer's own file.

        The file is root-owned in the root-owned config root; Orama cannot write it. Even
        so, each image must look like an image name before it is ever put in an argv.
        """
        try:
            doc = yaml.safe_load(Path(file).read_text())
            services = doc["services"]
            images = {name: services[name]["image"] for name in CORE_CONTAINERS}
        except (OSError, yaml.YAMLError, AttributeError, KeyError, TypeError):
            raise RunnerError(CORE_FILE_BAD) from None
        for image in images.values():
            if not isinstance(image, str) or not _IMAGE_REF.fullmatch(image):
                raise RunnerError(CORE_FILE_BAD)
        return images

    def _core_is_behind(self, images: dict[str, str]) -> bool:
        """True unless every core container already runs the image its name points at.

        Compares the image each container was created from with what the image name means
        now (after a download that is the new version). Any doubt counts as "behind", so
        the worst a wrong answer does is one unneeded ``up``, which changes nothing.
        """
        what = _WHAT["self-update"]
        try:
            running = self._exec(
                ["docker", "container", "inspect", "--format", "{{.Image}}",
                 *CORE_CONTAINERS.values()], what, CALL_TIMEOUT).split()
            wanted = self._exec(
                ["docker", "image", "inspect", "--format", "{{.Id}}",
                 *(images[name] for name in CORE_CONTAINERS)], what, CALL_TIMEOUT).split()
        except RunnerError:
            return True
        known = len(running) == len(CORE_CONTAINERS) and all(
            _IMAGE_ID.fullmatch(i) for i in running)
        return not (known and running == wanted)

    def _remove_updater(self) -> None:
        """``docker rm -f`` any previous helper container; none being there is fine."""
        argv = ["docker", "rm", "-f", UPDATER_CONTAINER]
        what = _WHAT["self-update"]
        try:
            self._exec(argv, what, CALL_TIMEOUT)
        except RunnerError:
            pass

    def self_update_pull(self) -> bool:
        """Download the core images. True if a core container is now behind its image.

        Nothing is stopped, started or recreated here, so a failed download (offline, the
        registry refusing) leaves every running container untouched.
        """
        file = str(core_project_file(self._roots))
        images = self._core_images(file)
        try:
            self._pull(file, CORE_PROJECT_NAME, _WHAT["self-update-pull"], CORE_PULL_TIMEOUT)
        except RunnerError as e:
            raise RunnerError(f"{PULL_FAILED} {LOG_HINT}", e.detail) from None
        return self._core_is_behind(images)

    def self_update(self) -> bool:
        """Recreate the core project from the downloaded images, through a helper container.

        False, and nothing is started, when every core container is already up to date.
        The helper outlives this call (and this Agent, which it replaces); see the module
        docstring for why each of its options is there.
        """
        file = str(core_project_file(self._roots))
        what = _WHAT["self-update"]
        images = self._core_images(file)
        if not self._core_is_behind(images):
            return False
        try:
            socket_gid = int(self._socket_gid())
        except (OSError, ValueError):
            raise RunnerError(
                f"{what} failed: Docker's socket was not found. {LOG_HINT}") from None
        self._remove_updater()
        config = str(self._roots.config)
        argv = [
            "docker", "run", "-d",
            "--name", UPDATER_CONTAINER,
            "--network", "none",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--read-only",
            "--tmpfs", UPDATER_TMPFS,
            "--pids-limit", UPDATER_PIDS,
            "--memory", UPDATER_MEMORY,
            # Root with no capabilities reaches the socket as its owner or through its group.
            "--group-add", str(socket_gid),
            # The docker CLI keeps its (empty) settings in the tmpfs, not the read-only root.
            "-e", "HOME=/tmp",
            "-e", "DOCKER_CONFIG=/tmp/.docker",
            "-v", f"{DOCKER_SOCKET}:{DOCKER_SOCKET}",
            "-v", f"{config}:{config}:ro",
            images["orama-agent"],
            "docker", "compose",
            "--project-directory", config,
            "--env-file", str(agent_env_file(self._roots)),
            "-p", CORE_PROJECT_NAME,
            "-f", file,
            "up", "-d", "--pull", "never", "--remove-orphans",
            "--wait", "--wait-timeout", UPDATER_WAIT_SECONDS,
        ]
        self._exec(argv, what, CALL_TIMEOUT)
        return True

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
        self.self_update_pull_calls = 0
        self.fail_next_self_update_pull = False
        self.on_self_update_pull: Callable[[], None] | None = None
        self.self_update_calls = 0
        self.fail_next_self_update = False
        self.core_is_behind = True  # what both update steps report

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

    def self_update_pull(self) -> bool:
        self.self_update_pull_calls += 1
        if self.on_self_update_pull is not None:
            self.on_self_update_pull()
        if self.fail_next_self_update_pull:
            self.fail_next_self_update_pull = False
            raise RunnerError(f"{PULL_FAILED} {LOG_HINT}", "exit code 1")
        return self.core_is_behind

    def self_update(self) -> bool:
        self.self_update_calls += 1
        if self.fail_next_self_update:
            self.fail_next_self_update = False
            raise RunnerError(f"{_WHAT['self-update']} failed. {LOG_HINT}", "exit code 1")
        return self.core_is_behind

    def recyclarr_sync(self) -> None:
        self.recyclarr_calls += 1
        if self.on_recyclarr is not None:
            self.on_recyclarr()
        if self.fail_next_recyclarr:
            self.fail_next_recyclarr = False
            raise RunnerError(f"{_WHAT['recyclarr']} failed. {LOG_HINT}", "exit code 1")
