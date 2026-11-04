"""Plan 1 on a real Docker host: install twice, deploy the stack, nothing leaks.

The tests run in file order and share module fixtures: ``installed`` runs install.sh once,
``applied`` writes a setup and applies it, ``rerun`` runs both again. See conftest.py
for when this runs and which folders it uses.
"""

from __future__ import annotations

import json
import os
import re
import stat
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import pytest

from .conftest import REPO_ROOT, Host, docker_inspect, run

pytestmark = pytest.mark.integration

INSTALL_TIMEOUT = 1800
APPLY_TIMEOUT = 1200
READY_TIMEOUT = 600  # slow first starts of the apps

APPS = ["plex", "jellyfin", "sonarr", "radarr", "prowlarr", "bazarr", "sabnzbd", "qbittorrent"]
ARR_STATUS = {
    "sonarr": "/api/v3/system/status",
    "radarr": "/api/v3/system/status",
    "prowlarr": "/api/v1/system/status",
}
DATA_SUBFOLDERS = [
    "media", "media/movies", "media/tv",
    "downloads", "downloads/usenet", "downloads/usenet/movies", "downloads/usenet/tv",
    "downloads/torrents", "downloads/torrents/movies", "downloads/torrents/tv",
]
ADDRESS = re.compile(r"Orama is ready\. Open http://\S+:(\d+) in your browser\.")
DOCKER_SOCKETS = {"/var/run/docker.sock", "/run/docker.sock"}
SHARED_NETWORK = "orama-net"


@dataclass
class Outputs:
    """Everything the commands printed, checked for secrets at the end."""

    texts: dict[str, str]

    def add(self, host: Host, name: str, text: str) -> None:
        self.texts[name] = text
        host.save_log(f"{name}.log", text)


@pytest.fixture(scope="module")
def outputs() -> Outputs:
    return Outputs({})


def install(host: Host, outputs: Outputs, name: str) -> tuple[int, str]:
    env = {
        **os.environ,
        "ORAMA_BUILD_LOCAL": "1",
        "ORAMA_SRC": str(REPO_ROOT),
        "ORAMA_DATA_ROOT": str(host.data_root),
        "ORAMA_CONFIG_ROOT": str(host.config_root),
        "ORAMA_PUID": str(host.puid),
        "ORAMA_PGID": str(host.pgid),
    }
    out = run(["bash", str(REPO_ROOT / "install.sh")], env=env, timeout=INSTALL_TIMEOUT)
    outputs.add(host, name, out.stdout)
    return out.returncode, out.stdout


def apply(host: Host, outputs: Outputs, name: str) -> tuple[int, str]:
    out = run(["docker", "exec", "orama-server", "python", "-m", "orama.cli", "apply",
               "--setup", str(host.state_dir / "setup.json")], timeout=APPLY_TIMEOUT)
    outputs.add(host, name, out.stdout)
    return out.returncode, out.stdout


def orama_port(output: str) -> int:
    found = ADDRESS.findall(output)
    assert found, f"no address line in the installer output:\n{output}"
    return int(found[-1])


def http_get(port: int, path: str, headers: dict[str, str] | None = None) -> tuple[int, str]:
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except OSError:
        return 0, ""


def container_ids() -> dict[str, str]:
    out = run(["docker", "ps", "-a", "--no-trunc", "--format", "{{.Names}} {{.ID}}"])
    assert out.returncode == 0, out.stdout
    pairs = (line.split() for line in out.stdout.splitlines() if line.strip())
    return {name: cid for name, cid in pairs if name.startswith("orama-")}


def published_host_ports(info: dict) -> set[int]:
    bindings = info["HostConfig"].get("PortBindings") or {}
    return {int(b["HostPort"]) for spec in bindings.values() for b in spec or [] if b["HostPort"]}


def bind_sources(info: dict) -> list[str]:
    return [m["Source"] for m in info["Mounts"] if m["Type"] == "bind"]


def stored_secrets(host: Host) -> dict[str, str]:
    """Secret names and values, read inside Orama as PUID. Never printed or logged."""
    snippet = (
        "import json, sys\n"
        "from pathlib import Path\n"
        "from orama.secrets import SecretStore\n"
        "store = SecretStore(Path(sys.argv[1]))\n"
        "print(json.dumps({n: store.get(n) for n in store.names()}))\n"
    )
    out = run(["docker", "exec", "orama-server", "python", "-c", snippet,
               str(host.state_dir / "secrets.bin")])
    assert out.returncode == 0, "reading the secret store failed (output withheld)"
    return json.loads(out.stdout)


# ---------------------------------------------------------------- fixtures

@pytest.fixture(scope="module")
def installed(clean_host: Host, outputs: Outputs) -> dict:
    status, output = install(clean_host, outputs, "install-1")
    assert status == 0, f"install.sh failed (exit {status}):\n{output}"
    draft = json.loads((clean_host.state_dir / "setup.draft.json").read_text(encoding="utf-8"))
    return {"output": output, "port": orama_port(output), "draft": draft}


@pytest.fixture(scope="module")
def applied(clean_host: Host, installed: dict, outputs: Outputs) -> dict:
    setup = dict(installed["draft"])
    setup.update(media_servers=["jellyfin", "plex"], download_methods=["torrent", "usenet"],
                 vpn=None)
    target = clean_host.state_dir / "setup.json"
    # Saved by Orama itself (validated, owned by PUID), like the setup page will.
    snippet = (
        "import sys\n"
        "from orama.setup_model import DesiredSetup\n"
        "DesiredSetup.model_validate_json(sys.stdin.read()).save(sys.argv[1])\n"
    )
    save = run(["docker", "exec", "-i", "orama-server", "python", "-c", snippet, str(target)],
               input=json.dumps(setup))
    assert save.returncode == 0, f"saving the setup failed:\n{save.stdout}"

    status, output = apply(clean_host, outputs, "apply-1")
    if status != 0:
        diagnose_apps_up(clean_host)
    assert status == 0, f"apply failed (exit {status}):\n{output}"
    wait_until_ready(APPS)
    return {"output": output, "setup": setup}


@pytest.fixture(scope="module")
def rerun(clean_host: Host, installed: dict, applied: dict, outputs: Outputs) -> dict:
    before = container_ids()
    install_status, install_output = install(clean_host, outputs, "install-2")
    apply_status, apply_output = apply(clean_host, outputs, "apply-2")
    return {
        "before": before, "after": container_ids(),
        "install": (install_status, install_output), "apply": (apply_status, apply_output),
    }


def diagnose_apps_up(host: Host) -> None:
    """The Agent hides compose's stderr; rerun its ``up`` here so the CI logs show why."""
    cfg = host.config_root
    out = run(["docker", "compose", "--project-directory", str(cfg),
               "--env-file", str(cfg / "app-env" / "compose.env"), "-p", "orama-apps",
               "-f", str(cfg / "compose.yaml"), "up", "-d", "--wait"], timeout=APPLY_TIMEOUT)
    host.save_log("apply-1-compose-up.log", out.stdout)


def wait_until_ready(apps: list[str]) -> None:
    """Every app container running, and healthy where its image has a health check."""
    deadline = time.monotonic() + READY_TIMEOUT
    while True:
        waiting = {}
        for app in apps:
            state = docker_inspect(f"orama-{app}")["State"]
            health = (state.get("Health") or {}).get("Status")
            if state["Status"] != "running" or health not in (None, "healthy"):
                waiting[app] = f"{state['Status']}/{health or 'no health check'}"
        if not waiting:
            return
        if time.monotonic() > deadline:
            pytest.fail(f"apps not ready after {READY_TIMEOUT}s: {waiting}")
        time.sleep(5)


def poll_http(port: int, path: str, headers: dict[str, str] | None = None,
              timeout: float = READY_TIMEOUT) -> int:
    """Status of GET path once it is not a connection error / 5xx, or the last status."""
    deadline = time.monotonic() + timeout
    while True:
        status, _ = http_get(port, path, headers)
        if 0 < status < 500 or time.monotonic() > deadline:
            return status
        time.sleep(5)


# ---------------------------------------------------------------- install

def test_installer_starts_orama(installed: dict):
    for name in ("orama-server", "orama-server-agent"):
        assert docker_inspect(name)["State"]["Status"] == "running", name
    assert http_get(installed["port"], "/healthz") == (200, "ok")


def test_orama_has_no_docker_socket_and_only_its_state_folder(clean_host: Host, installed):
    info = docker_inspect("orama-server")
    targets = {m["Destination"] for m in info["Mounts"]}
    sources = bind_sources(info)
    assert not DOCKER_SOCKETS & (targets | set(sources))
    under_config = [s for s in sources if Path(s).is_relative_to(clean_host.config_root)]
    assert under_config == [str(clean_host.state_dir)]
    assert published_host_ports(info) == {installed["port"]}


def test_agent_has_no_network(installed):
    info = docker_inspect("orama-server-agent")
    assert info["HostConfig"]["NetworkMode"] == "none"
    assert published_host_ports(info) == set()
    assert not info["NetworkSettings"].get("Ports")


def test_draft_avoids_the_taken_port(installed, blocked_port: int):
    ports = installed["draft"]["ports"]
    assert ports["sabnzbd"] != blocked_port
    assert len(set(ports.values())) == len(ports)


# ---------------------------------------------------------------- apply

def test_every_app_runs(applied: dict):
    lines = applied["output"].splitlines()
    assert "✓ Folders ready" in lines
    for app in APPS:
        assert f"✓ {app} started" in lines
    for app in ("gluetun", "recyclarr"):
        assert run(["docker", "inspect", f"orama-{app}"]).returncode != 0, app


def test_orama_reaches_apps_on_the_shared_network(applied: dict):
    assert SHARED_NETWORK in docker_inspect("orama-server")["NetworkSettings"]["Networks"]
    for app in APPS:
        networks = docker_inspect(f"orama-{app}")["NetworkSettings"]["Networks"]
        assert SHARED_NETWORK in networks, app
    # By service name and container port, as the wiring will. Sonarr may still be starting.
    deadline = time.monotonic() + READY_TIMEOUT
    while True:
        reach = run(["docker", "exec", "orama-server", "python", "-c",
                     "import socket; socket.create_connection(('sonarr', 8989), 5)"])
        if reach.returncode == 0 or time.monotonic() > deadline:
            break
        time.sleep(5)
    assert reach.returncode == 0, f"Orama cannot reach sonarr:8989:\n{reach.stdout}"


def test_sabnzbd_publishes_its_assigned_port(applied: dict, blocked_port: int):
    port = applied["setup"]["ports"]["sabnzbd"]
    assert port != blocked_port
    assert published_host_ports(docker_inspect("orama-sabnzbd")) == {port}


def test_no_app_mounts_env_files_or_docker(clean_host: Host, applied):
    env_dir = clean_host.config_root / "app-env"
    for app in APPS:
        info = docker_inspect(f"orama-{app}")
        sources = bind_sources(info)
        assert not [s for s in sources if Path(s).is_relative_to(env_dir)], app
        assert not DOCKER_SOCKETS & set(sources), app


def test_data_folders_belong_to_the_install_user(clean_host: Host, applied):
    for sub in DATA_SUBFOLDERS:
        st = os.stat(clean_host.data_root / sub)
        assert (st.st_uid, st.st_gid) == (clean_host.puid, clean_host.pgid), sub
        assert stat.S_IMODE(st.st_mode) == 0o2775, (sub, oct(st.st_mode))


def test_hardlink_across_containers(clean_host: Host, applied):
    user = f"{clean_host.puid}:{clean_host.pgid}"
    made = run(["docker", "exec", "-u", user, "orama-sonarr", "sh", "-c",
                "touch /data/downloads/usenet/tv/x && ln /data/downloads/usenet/tv/x /data/media/tv/x"])
    assert made.returncode == 0, made.stdout
    seen = run(["docker", "exec", "orama-radarr", "stat", "-c", "%i %h",
                "/data/downloads/usenet/tv/x", "/data/media/tv/x"])
    assert seen.returncode == 0, seen.stdout
    first, second = seen.stdout.splitlines()
    assert first == second
    assert first.split()[1] == "2"


def test_arr_apps_accept_their_stored_api_key(clean_host: Host, applied: dict):
    secrets = stored_secrets(clean_host)
    ports = applied["setup"]["ports"]
    for app, path in ARR_STATUS.items():
        key = secrets.get(f"apikey:{app}")
        assert key, f"no stored API key for {app}"
        status = poll_http(ports[app], path, {"X-Api-Key": key})
        assert status == 200, f"{app} answered {status} with the stored key"
        assert http_get(ports[app], path)[0] == 401, f"{app} answered without a key"


def test_media_servers_answer(applied: dict):
    ports = applied["setup"]["ports"]
    assert 0 < poll_http(ports["plex"], "/identity") < 500
    assert 0 < poll_http(ports["jellyfin"], "/health") < 500


# ---------------------------------------------------------------- run again (RF3)

def test_second_install_and_apply_change_nothing(installed: dict, rerun: dict):
    status, output = rerun["install"]
    assert status == 0, f"second install.sh failed (exit {status}):\n{output}"
    assert orama_port(output) == installed["port"]
    status, output = rerun["apply"]
    assert status == 0, f"second apply failed (exit {status}):\n{output}"
    assert "✓ Nothing to change" in output.splitlines()
    assert rerun["after"] == rerun["before"]
    assert http_get(installed["port"], "/healthz") == (200, "ok")


def test_no_secret_in_logs_or_outputs(clean_host: Host, rerun: dict, outputs: Outputs):
    values = [v for v in stored_secrets(clean_host).values() if v]
    assert values, "the secret store is empty"
    texts = dict(outputs.texts)
    for name in ("orama-server", "orama-server-agent"):
        texts[f"docker logs {name}"] = run(["docker", "logs", name]).stdout
    # Booleans only: an assertion must never show a secret value.
    leaks = sorted(where for where, text in texts.items() if any(v in text for v in values))
    assert leaks == [], "secret values found in the output of these"
