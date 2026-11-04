"""Integration fixtures: a real Docker host, the real installer, the real apps.

Runs only as root on a machine with Docker, and only when ORAMA_INTEGRATION=1 is set:
it installs Orama into the fixed test folders below (wiping any earlier test run there)
and starts real containers. CI runs it with:
  sudo -E env "PATH=$PATH" ORAMA_INTEGRATION=1 .venv/bin/python -m pytest -m integration

The install user comes from ORAMA_TEST_PUID / ORAMA_TEST_PGID, else from SUDO_UID /
SUDO_GID; it must not be root. Logs of every container and every command output are
written to ORAMA_IT_LOG_DIR (default: integration-logs/ in the checkout) before cleanup.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
# Spaces and non-ASCII on purpose (RF1); ":" is refused by the installer (Task 9 ruling).
DATA_PARENT = Path("/srv/orama test")
DATA_ROOT = DATA_PARENT / "Ταινίες"
CONFIG_ROOT = Path("/srv/orama-cfg")
BLOCKED_PORT = 8080  # sabnzbd's default; held by another program during install (RF2)
PROJECTS = ("orama-apps", "orama-core")


@dataclass
class Host:
    puid: int
    pgid: int
    data_root: Path = DATA_ROOT
    config_root: Path = CONFIG_ROOT
    log_dir: Path = field(default_factory=lambda: Path(
        os.environ.get("ORAMA_IT_LOG_DIR") or REPO_ROOT / "integration-logs"))

    @property
    def state_dir(self) -> Path:
        return self.config_root / "orama"

    def save_log(self, name: str, text: str) -> None:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        (self.log_dir / name).write_text(text, encoding="utf-8")


def run(argv: list[str], *, timeout: float = 120, **kw) -> subprocess.CompletedProcess:
    """Run a command; stdout and stderr together in ``.stdout``, never raises on exit code."""
    return subprocess.run(
        argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        timeout=timeout, check=False, **kw,
    )


def docker_inspect(name: str) -> dict:
    out = run(["docker", "inspect", name])
    assert out.returncode == 0, f"docker inspect {name} failed:\n{out.stdout}"
    return json.loads(out.stdout)[0]


def _install_ids() -> tuple[int, int] | None:
    uid = os.environ.get("ORAMA_TEST_PUID") or os.environ.get("SUDO_UID")
    gid = os.environ.get("ORAMA_TEST_PGID") or os.environ.get("SUDO_GID")
    if not (uid and gid and uid.isdigit() and gid.isdigit()):
        return None
    if int(uid) == 0 or int(gid) == 0:
        return None
    return int(uid), int(gid)


def _docker_works() -> bool:
    try:
        return (
            run(["docker", "version"], timeout=30).returncode == 0
            and run(["docker", "compose", "version"], timeout=30).returncode == 0
        )
    except (OSError, subprocess.TimeoutExpired):
        return False


def _remove_everything() -> None:
    """Remove the test projects' containers, networks and volumes, then the test folders."""
    run(["docker", "rm", "-f", "orama-test-newznab"])  # the fake indexer, not in a project
    for project in PROJECTS:
        run(["docker", "compose", "-p", project, "down", "-v", "--remove-orphans"],
            timeout=300)
        label = f"label=com.docker.compose.project={project}"
        for kind in ("container", "network", "volume"):
            listed = run(["docker", kind, "ls", "-aq" if kind == "container" else "-q",
                          "--filter", label])
            ids = listed.stdout.split() if listed.returncode == 0 else []
            if ids:
                run(["docker", kind, "rm", *(["-f"] if kind == "container" else []), *ids],
                    timeout=300)
    for path in (CONFIG_ROOT, DATA_PARENT):
        shutil.rmtree(path, ignore_errors=True)


def _collect_logs(host: Host) -> None:
    for project in PROJECTS:
        listed = run(["docker", "ps", "-a", "--format", "{{.Names}}",
                      "--filter", f"label=com.docker.compose.project={project}"])
        for name in listed.stdout.split():
            host.save_log(f"container-{name}.log", run(["docker", "logs", name]).stdout)
    host.save_log("docker-ps.txt", run(["docker", "ps", "-a"]).stdout)


def require_host() -> tuple[int, int]:
    """The install ids, once this host may and can run the integration test.

    Without the ORAMA_INTEGRATION=1 opt-in the test is skipped. With it, a missing
    prerequisite fails the run, so CI can never pass by skipping it.
    """
    if os.environ.get("ORAMA_INTEGRATION") != "1":
        pytest.skip("integration test: set ORAMA_INTEGRATION=1 (it changes this machine)")
    if os.geteuid() != 0:
        pytest.fail("integration test: needs root (run it with sudo -E)")
    if not _docker_works():
        pytest.fail("integration test: Docker and docker compose are not available")
    ids = _install_ids()
    if ids is None:
        pytest.fail("integration test: set ORAMA_TEST_PUID/ORAMA_TEST_PGID to a non-root user")
    return ids


@pytest.fixture(scope="session")
def host() -> Host:
    ids = require_host()
    _remove_everything()  # a run that was killed earlier
    return Host(puid=ids[0], pgid=ids[1])


@pytest.fixture(scope="session")
def blocked_port(host: Host):
    """Another program listening on BLOCKED_PORT for the whole test (RF2)."""
    with tempfile.TemporaryDirectory() as empty:
        proc = subprocess.Popen(
            [sys.executable, "-m", "http.server", str(BLOCKED_PORT), "--bind", "0.0.0.0"],
            cwd=empty, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            deadline = time.monotonic() + 30
            while True:
                try:
                    socket.create_connection(("127.0.0.1", BLOCKED_PORT), timeout=1).close()
                    break
                except OSError:
                    if proc.poll() is not None or time.monotonic() > deadline:
                        pytest.fail(f"could not start a listener on port {BLOCKED_PORT}")
                    time.sleep(0.2)
            yield BLOCKED_PORT
        finally:
            proc.terminate()
            proc.wait(timeout=10)


@pytest.fixture(scope="session")
def clean_host(host: Host, blocked_port: int):
    """The host for the whole test; logs are collected, then everything is removed."""
    try:
        yield host
    finally:
        _collect_logs(host)
        _remove_everything()
