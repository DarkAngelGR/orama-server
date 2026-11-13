"""Fixtures for the Plan 5 end-to-end proof against clean Ubuntu or a Debian VM."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from e2e.target import LocalTarget, Target

REPO_ROOT = Path(__file__).resolve().parents[2]
PROJECTS = ("orama-apps", "orama-core")
INDEXER_CONTAINER = "orama-e2e-indexer"
UPDATER_CONTAINER = "orama-updater"


def _install_ids() -> tuple[int, int] | None:
    uid = os.environ.get("ORAMA_TEST_PUID") or os.environ.get("SUDO_UID")
    gid = os.environ.get("ORAMA_TEST_PGID") or os.environ.get("SUDO_GID")
    if not (uid and gid):
        if os.geteuid() != 0:
            return os.getuid(), os.getgid()
        return None
    if not (uid.isdigit() and gid.isdigit()):
        return None
    if int(uid) == 0 or int(gid) == 0:
        return None
    return int(uid), int(gid)


def _check_node_playwright() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.fail("e2e proof: Node.js is required on the driver host")
    probe = subprocess.run(
        [node, "-e", "require('playwright')"],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if probe.returncode != 0:
        pytest.fail("e2e proof: playwright npm package is not available (set NODE_PATH)")


def _check_local_docker() -> None:
    for cmd in (["docker", "version"], ["docker", "compose", "version"]):
        try:
            res = subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as exc:
            pytest.fail(f"e2e proof: {' '.join(cmd)} failed: {exc}")
        if res.returncode != 0:
            pytest.fail(f"e2e proof: {' '.join(cmd)} exited {res.returncode}")


def _check_sudo_if_non_root() -> None:
    if os.geteuid() == 0:
        return
    probe = subprocess.run(
        ["sudo", "-n", "true"],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if probe.returncode != 0:
        pytest.fail("e2e proof: requires root or passwordless sudo")


def _cleanup_local(target: LocalTarget) -> None:
    target.run(["docker", "rm", "-f", INDEXER_CONTAINER], timeout=60)
    target.run(["docker", "rm", "-f", UPDATER_CONTAINER], timeout=60)
    for project in PROJECTS:
        target.run(
            ["docker", "compose", "-p", project, "down", "-v", "--remove-orphans"],
            timeout=300,
        )
        label = f"label=com.docker.compose.project={project}"
        for kind in ("container", "network", "volume"):
            ls_arg = "-aq" if kind == "container" else "-q"
            listed = target.run(["docker", kind, "ls", ls_arg, "--filter", label], timeout=60)
            ids = listed.stdout.split() if listed.returncode == 0 else []
            if ids:
                rm_args = ["docker", kind, "rm"]
                if kind == "container":
                    rm_args.append("-f")
                target.run([*rm_args, *ids], timeout=300)
    target.run(["rm", "-rf", "/srv/orama-e2e"], timeout=120)


@pytest.fixture(scope="session")
def e2e_target() -> Target:
    mode = os.environ.get("ORAMA_E2E_TARGET", "").strip().lower()
    if not mode:
        if os.environ.get("ORAMA_E2E_REQUIRED") == "1":
            pytest.fail("e2e proof: CI must set ORAMA_E2E_TARGET")
        pytest.skip("e2e proof: set ORAMA_E2E_TARGET=ubuntu|debian (modifies target machine)")
    _check_node_playwright()
    ids = _install_ids()
    if ids is None:
        pytest.fail("e2e proof: set ORAMA_TEST_PUID/ORAMA_TEST_PGID to a non-root user")
    puid, pgid = ids

    if mode == "ubuntu":
        _check_sudo_if_non_root()
        _check_local_docker()
        target = LocalTarget(
            name="ubuntu",
            puid=puid,
            pgid=pgid,
            data_root="/srv/orama-e2e/data",
            config_root="/srv/orama-e2e/cfg",
            repo_dir=str(REPO_ROOT),
        )
        _cleanup_local(target)
        try:
            yield target
        finally:
            try:
                _save_failed_run_logs(target)
            finally:
                _cleanup_local(target)
        return

    pytest.fail(f"e2e proof: unsupported ORAMA_E2E_TARGET={mode!r}")


def _save_failed_run_logs(target: Target) -> None:
    scan_path = os.environ.get("ORAMA_E2E_SCAN_VALUES")
    log_path = os.environ.get("ORAMA_E2E_LOG_DIR")
    if not (scan_path and log_path) or Path(scan_path).exists():
        return
    # Abort artifact collection if setup failed too early to read the secrets.
    try:
        target.collect_stored_secrets()
        target.save_logs(Path(log_path))
        target.write_scan_values(Path(scan_path))
    except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired):
        pass
