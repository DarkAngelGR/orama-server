"""Plans 1 and 2a on a real Docker host: install twice, deploy and wire the stack, nothing leaks.

The tests run in file order and share module fixtures: ``installed`` runs install.sh once,
``applied`` writes a setup and applies it, ``rerun`` runs both again. See conftest.py
for when this runs and which folders it uses.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import select
import stat
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import pytest

from orama.wiring.plex import WAITING_FOR_SIGN_IN
from orama_agent import recyclarr

from . import media_contracts, request_contracts
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
    "downloads", "downloads/usenet", "downloads/usenet/incomplete",
    "downloads/usenet/movies", "downloads/usenet/tv",
    "downloads/torrents", "downloads/torrents/movies", "downloads/torrents/tv",
]
# With a one-time setup code (until a password exists): .../setup?code=<code>.
ADDRESS = re.compile(
    r"Orama is ready\. Open http://\S+:(\d+)(?:/setup\?code=[A-Za-z0-9_-]{16,64})? "
    r"in your browser\."
)
SETUP_CODE = re.compile(r"(/setup\?code=)[A-Za-z0-9_-]+")
DOCKER_SOCKETS = {"/var/run/docker.sock", "/run/docker.sock"}
SHARED_NETWORK = "orama-net"
NEWZNAB = "orama-test-newznab"
PROXY_STAND_IN = "orama-test-proxy"
FAKE_SERVER_HOST = "fake-nntp.invalid"
SYNC_TIMEOUT = 120
WIRED_APPS = ["prowlarr", "sonarr", "radarr", "sabnzbd", "qbittorrent"]


@dataclass
class Outputs:
    """Everything the commands printed, checked for secrets at the end."""

    texts: dict[str, str]

    def add(self, host: Host, name: str, text: str) -> None:
        self.texts[name] = text
        # The saved logs are CI artifacts: never keep the one-time setup code in them.
        host.save_log(f"{name}.log", SETUP_CODE.sub(r"\1<hidden>", text))


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


def http_json(url: str, headers: dict[str, str] | None = None):
    request = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read())


def failure_lines(output: str) -> list[str]:
    return [line for line in output.splitlines() if line.startswith("✗")]


def skip_lines(output: str) -> list[str]:
    return [line for line in output.splitlines() if line.startswith("…")]


# CI has no Plex account: the only step skipped on every apply.
PLEX_WAITING = ["… Plex: skipped, " + WAITING_FOR_SIGN_IN]


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
def newznab(clean_host: Host, installed: dict):
    """A fake Newznab indexer on the shared network, for Prowlarr to add."""
    started = run(["docker", "run", "-d", "--name", NEWZNAB, "--network", SHARED_NETWORK,
                   "--network-alias", "fake-newznab",
                   "-v", f"{Path(__file__).with_name('fake_newznab.py')}:/fake.py:ro",
                   "python:3.12-slim", "python", "/fake.py"], timeout=600)
    assert started.returncode == 0, f"fake Newznab did not start:\n{started.stdout}"
    yield
    clean_host.save_log("container-newznab.log", run(["docker", "logs", NEWZNAB]).stdout)
    run(["docker", "rm", "-f", NEWZNAB])


@pytest.fixture(scope="module")
def applied(
    clean_host: Host, installed: dict, newznab, outputs: Outputs,
) -> dict:
    setup = dict(installed["draft"])
    setup.update(
        media_servers=["jellyfin", "plex"], download_methods=["torrent", "usenet"], vpn=None,
        usenet_servers=[{"slug": "fake", "host": FAKE_SERVER_HOST}],
        indexers=[{"slug": "fake", "definition": "newznab", "name": "Fake Newznab",
                   "fields": {"baseUrl": "http://fake-newznab:8000"},
                   "secret_fields": ["apiKey"]}],
    )
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

    # Secrets as the owner would enter them; values built here, never printed.
    for name in ("usenet:fake:password", "indexer:fake:apiKey"):
        saved = run(["docker", "exec", "-i", "orama-server", "python", "-m", "orama.cli",
                     "set-secret", name, "--setup", str(target)], input=secrets.token_hex(16))
        assert saved.returncode == 0, f"set-secret {name} failed:\n{saved.stdout}"
        outputs.add(clean_host, "set-secret-" + name.replace(":", "-"), saved.stdout)

    status, output = apply(clean_host, outputs, "apply-1")
    assert status == 1, f"apply should report only the fake Usenet server (exit {status}):\n{output}"
    wait_until_ready(APPS)
    return {"output": output, "setup": setup}


@pytest.fixture(scope="module")
def rerun(clean_host: Host, installed: dict, applied: dict, outputs: Outputs) -> dict:
    first_apply = applied["output"]
    before = container_ids()
    install_status, install_output = install(clean_host, outputs, "install-2")
    apply_status, apply_output = apply(clean_host, outputs, "apply-2")
    return {
        "first_apply": first_apply, "before": before, "after": container_ids(),
        "install": (install_status, install_output), "apply": (apply_status, apply_output),
    }


def app_warnings(host: Host, apps: list[str], lines: int = 30) -> str:
    """The apps' last warning and error log lines, every stored secret scrubbed out.

    Shown only when an assertion fails, so a CI log says why an app refused something.
    """
    values = [v for v in stored_secrets(host).values() if len(v) >= 8]
    parts = []
    for app in apps:
        log = run(["docker", "logs", "--tail", "2000", f"orama-{app}"]).stdout.splitlines()
        found = [l for l in log if re.search(r"\b(warn|error|fatal)\b", l, re.IGNORECASE)]
        text = "\n".join(found[-lines:])
        for v in values:
            text = text.replace(v, "***")
        parts.append(f"--- {app} warnings and errors:\n{text}")
    return "\n".join(parts)


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

def test_every_app_runs(clean_host: Host, applied: dict):
    lines = applied["output"].splitlines()
    assert "✓ Folders ready" in lines
    for app in APPS:
        assert f"✓ {app} started" in lines
    # The fake Usenet server cannot log in; that is the one visible, non-blocking failure.
    failures = failure_lines(applied["output"])
    assert len(failures) == 1 and FAKE_SERVER_HOST in failures[0], (
        f"{failures}\n{app_warnings(clean_host, WIRED_APPS)}")
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


# ---------------------------------------------------------------- download wiring

def app_get(applied: dict, secrets_: dict[str, str], app: str, path: str):
    port = applied["setup"]["ports"][app]
    return http_json(f"http://127.0.0.1:{port}{path}", {"X-Api-Key": secrets_[f"apikey:{app}"]})


def test_plan2b_bazarr_configuration(clean_host: Host, applied: dict):
    keys = stored_secrets(clean_host)
    settings = app_get(applied, keys, "bazarr", "/api/system/settings")
    for app in ("sonarr", "radarr"):
        assert settings["general"][f"use_{app}"] is True
        assert settings[app]["ip"] == app
        if settings[app]["apikey"] != keys[f"apikey:{app}"]:
            pytest.fail("wrong link key (values withheld)")
    for switch in ("ignore_ass_subs", "ignore_pgs_subs", "ignore_vobsub_subs"):
        assert settings["general"][switch] is True, switch
    profiles = app_get(applied, keys, "bazarr", "/api/system/languages/profiles")
    mine = [p for p in profiles if p["name"] == "Orama"]
    assert len(mine) == 1
    assert [i["language"] for i in mine[0]["items"]] == ["el", "en"]
    for kind in ("serie", "movie"):
        assert settings["general"][f"{kind}_default_enabled"] is True
        assert str(settings["general"][f"{kind}_default_profile"]) == str(mine[0]["profileId"])


@pytest.mark.parametrize("app", ["sonarr", "radarr"])
def test_plan2b_quality_scores(clean_host: Host, applied: dict, app):
    keys = stored_secrets(clean_host)
    profiles = app_get(applied, keys, app, "/api/v3/qualityprofile")
    mine = [p for p in profiles if p["name"] == "Orama"]
    if len(mine) != 1:
        logs = clean_host.config_root / "config/recyclarr/logs"
        messages = []
        for path in logs.rglob("*"):
            if path.is_file():
                text = path.read_text(errors="replace")
                for value in keys.values():
                    if len(value) >= 8:
                        text = text.replace(value, "***")
                messages.append(text[-20000:])
        pytest.fail("Orama quality profile missing; Recyclarr logs:\n" + "\n".join(messages))
    scores = {i["name"]: i["score"] for i in mine[0]["formatItems"]}
    wanted = {name: -10000 for name in (
        "DV (Disk)", "DV Boost", "DV (w/o HDR fallback)", "DTS", "DTS-HD MA",
        "DTS X", "DTS-HD HRA", "DTS-ES", "TrueHD", "TrueHD ATMOS", "PCM", "FLAC")}
    wanted["HDR"] = 1500
    assert {name: scores.get(name) for name in wanted} == wanted, scores


def test_plan2b_jellyfin_and_notifications(clean_host: Host, applied: dict):
    keys = stored_secrets(clean_host)
    port = applied["setup"]["ports"]["jellyfin"]
    base = f"http://127.0.0.1:{port}"
    assert http_json(base + "/System/Info/Public")["StartupWizardCompleted"] is True
    token = keys["apikey:jellyfin"]
    headers = {"Authorization": f'MediaBrowser Client="Orama", Device="Orama", '
               f'DeviceId="orama", Version="1", Token="{token}"'}
    folders = http_json(base + "/Library/VirtualFolders", headers)
    assert {(f["Name"], tuple(f["Locations"])) for f in folders} == {
        ("Movies", ("/data/media/movies",)), ("TV Shows", ("/data/media/tv",))}
    for app in ("sonarr", "radarr"):
        notifications = app_get(applied, keys, app, "/api/v3/notification")
        mine = [n for n in notifications if n["name"] == "Jellyfin (Orama)"]
        assert len(mine) == 1
        fields = {f["name"]: f.get("value") for f in mine[0]["fields"]}
        assert (fields["host"], fields["updateLibrary"], fields["notify"]) == (
            "jellyfin", True, False), app
        assert all(mine[0][flag] is True for flag in ("onDownload", "onUpgrade", "onRename")), app
    assert "Plex is waiting for sign-in" in applied["output"]


@pytest.mark.parametrize("app", ["sonarr", "radarr"])
def test_pinned_request_read_contracts(clean_host, applied, app):
    request_contracts.read_contracts(app, applied["setup"]["ports"][app],
                                     stored_secrets(clean_host)[f"apikey:{app}"])


@pytest.mark.parametrize("app", ["sonarr", "radarr"])
def test_pinned_title_request_contracts(clean_host, applied, app, capsys):
    keys = stored_secrets(clean_host)
    ports = applied["setup"]["ports"]
    observation = request_contracts.title_contracts(
        app, ports[app], keys[f"apikey:{app}"], clean_host, ports["bazarr"], keys["apikey:bazarr"])
    with capsys.disabled():
        print(f"Request contract fixture {app}: {json.dumps(observation, sort_keys=True)}")


def test_pinned_authenticated_request_webhooks(clean_host, applied, tmp_path):
    request_contracts.authenticated_webhooks(applied["setup"]["ports"],
                                             stored_secrets(clean_host), tmp_path)


def test_pinned_notification_contracts(clean_host, applied, capsys):
    for app in ("sonarr", "radarr"):
        schemas = app_get(applied, stored_secrets(clean_host), app, "/api/v3/notification/schema")
        # Templates leave "name" empty; the type is in implementationName/implementation.
        def label(s):
            return " ".join(str(s.get(k) or "") for k in
                            ("implementationName", "implementation", "name")).lower()
        media = [s for s in schemas if any(n in label(s) for n in ("plex", "emby", "jellyfin"))]
        assert any("plex" in label(s) for s in media), sorted(label(s) for s in schemas)
        assert any(n in label(s) for s in media for n in ("emby", "jellyfin"))
        with capsys.disabled():
            for schema in media:
                fields = sorted(f["name"] for f in schema["fields"])
                print(f"Contract {app} {schema['implementation']}: {fields}")


def test_pinned_bazarr_contract(applied, capsys):
    with capsys.disabled():
        media_contracts.bazarr(applied["setup"]["ports"]["bazarr"])


def test_pinned_jellyfin_contract(applied, capsys):
    # Runs on its own throwaway container; the installed Jellyfin stays untouched.
    with capsys.disabled():
        media_contracts.jellyfin()


def test_pinned_recyclarr_contract(clean_host, applied, tmp_path, capsys):
    with capsys.disabled():
        media_contracts.recyclarr(clean_host, tmp_path, stored_secrets(clean_host)["apikey:radarr"])


def test_pinned_plex_contract(applied, capsys):
    with capsys.disabled():
        media_contracts.plex(applied["setup"]["ports"]["plex"])


@pytest.mark.parametrize("app,root,category_field,category", [
    ("sonarr", "/data/media/tv", "tvCategory", "tv"),
    ("radarr", "/data/media/movies", "movieCategory", "movies"),
])
def test_arr_apps_are_wired_to_the_download_clients(
    clean_host: Host, applied: dict, app: str, root: str, category_field: str, category: str,
):
    secrets_ = stored_secrets(clean_host)
    folders = {r["path"].rstrip("/") for r in app_get(applied, secrets_, app, "/api/v3/rootfolder")}
    assert root in folders
    clients = {c["name"]: c for c in app_get(applied, secrets_, app, "/api/v3/downloadclient")}
    assert {"SABnzbd (Orama)", "qBittorrent (Orama)"} <= set(clients)
    for name, client in clients.items():
        fields = {f["name"]: f.get("value") for f in client["fields"]}
        assert fields.get(category_field) == category, name
    config = app_get(applied, secrets_, app, "/api/v3/config/mediamanagement")
    assert config["copyUsingHardlinks"] is True


def test_prowlarr_has_its_apps_and_the_indexer(clean_host: Host, applied: dict):
    secrets_ = stored_secrets(clean_host)
    apps = {a["name"] for a in app_get(applied, secrets_, "prowlarr", "/api/v1/applications")}
    assert {"Sonarr (Orama)", "Radarr (Orama)"} <= apps
    indexers = {i["name"] for i in app_get(applied, secrets_, "prowlarr", "/api/v1/indexer")}
    assert "Fake Newznab" in indexers


@pytest.mark.parametrize("app", ["sonarr", "radarr"])
def test_indexer_reaches_sonarr_and_radarr(clean_host: Host, applied: dict, app: str):
    secrets_ = stored_secrets(clean_host)
    deadline = time.monotonic() + SYNC_TIMEOUT
    while True:
        names = [i["name"] for i in app_get(applied, secrets_, app, "/api/v3/indexer")]
        if any("Fake Newznab" in n for n in names) or time.monotonic() > deadline:
            break
        time.sleep(5)
    assert any("Fake Newznab" in n for n in names), (
        f"{app} indexers: {names}\n{app_warnings(clean_host, ['prowlarr', app])}")


def test_sabnzbd_is_wired(clean_host: Host, applied: dict):
    secrets_ = stored_secrets(clean_host)
    port = applied["setup"]["ports"]["sabnzbd"]
    base = f"http://127.0.0.1:{port}/api?output=json&apikey={secrets_['apikey:sabnzbd']}&mode=get_config"
    config = http_json(base)["config"]
    assert config["misc"]["complete_dir"].rstrip("/") == "/data/downloads/usenet"
    folders = {c["name"]: str(c.get("dir") or "").rstrip("/") for c in config["categories"]}
    assert folders.get("tv") == "tv" and folders.get("movies") == "movies", folders
    assert "fake" in {s["name"] for s in config["servers"]}


def test_qbittorrent_is_wired(clean_host: Host, applied: dict):
    secrets_ = stored_secrets(clean_host)
    port = applied["setup"]["ports"]["qbittorrent"]
    form = urllib.parse.urlencode(
        {"username": "orama", "password": secrets_["password:qbittorrent"]}).encode()
    with urllib.request.urlopen(
        f"http://127.0.0.1:{port}/api/v2/auth/login", data=form, timeout=20
    ) as response:
        assert response.status in (200, 204)
        cookie = response.headers["Set-Cookie"].split(";")[0]
    categories = http_json(f"http://127.0.0.1:{port}/api/v2/torrents/categories",
                           {"Cookie": cookie})
    assert categories["tv"]["savePath"].rstrip("/") == "/data/downloads/torrents/tv"
    assert categories["movies"]["savePath"].rstrip("/") == "/data/downloads/torrents/movies"


PROXY_WIRING = """
import json, sys
from pathlib import Path
from orama.secrets import GLUETUN_PROXY_PASSWORD_NAME, SecretStore
from orama.setup_model import DesiredSetup
from orama.wiring.engine import run_steps
from orama.wiring.http import Http
from orama.wiring.prowlarr import prowlarr_steps


class Memory:  # stands in for the secret store, so the real one is not touched
    def __init__(self):
        self.values = {}

    def get(self, name):
        return self.values.get(name)

    def set(self, name, value):
        self.values[name] = value

    def names(self):
        return list(self.values)


real, wanted = sys.argv[1], sys.argv[2]
request = json.load(sys.stdin)
store = Memory()
store.set("apikey:prowlarr", SecretStore(Path(real)).get("apikey:prowlarr"))
store.set(GLUETUN_PROXY_PASSWORD_NAME, request["password"])
setup = DesiredSetup.model_validate(request["setup"])
steps = [s for s in prowlarr_steps(setup, store, Http()) if s.name == wanted]
print(json.dumps([[r.name, r.status, r.message] for r in run_steps(steps)]))
"""


def prowlarr_api(applied: dict, secrets_: dict[str, str], method: str, path: str,
                 body=None, params: str = ""):
    """Prowlarr's own API from the host; returns (status, parsed body or None)."""
    port = applied["setup"]["ports"]["prowlarr"]
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/v1{path}{params}", data=data, method=method,
        headers={"X-Api-Key": secrets_["apikey:prowlarr"], "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read()
            return response.status, (json.loads(raw) if raw.strip() else None)
    except urllib.error.HTTPError as e:
        # Prowlarr's validation answer (field names and reasons, never our values).
        return e.code, e.read().decode(errors="replace")[:1500]


def run_proxy_wiring(setup: dict, step: str, password: str, clean_host: Host) -> list:
    """The real Prowlarr proxy steps, run inside Orama against the real Prowlarr."""
    done = run(["docker", "exec", "-i", "orama-server", "python", "-c", PROXY_WIRING,
                str(clean_host.state_dir / "secrets.bin"), step],
               input=json.dumps({"setup": setup, "password": password}), timeout=300)
    assert done.returncode == 0, f"{step} did not run (output withheld)"
    return json.loads(done.stdout)


def test_prowlarr_vpn_proxy_and_masked_secrets(clean_host: Host, applied: dict):
    """The Http proxy shapes and the proxy-off path against the pinned Prowlarr (no VPN needed)."""
    from orama.catalog import GLUETUN_PROXY_USER

    secrets_ = stored_secrets(clean_host)

    def api(method, path, body=None, params=""):
        return prowlarr_api(applied, secrets_, method, path, body, params)

    def listed(kind, name):
        status, items = api("GET", f"/{kind}")
        assert status == 200
        return next((i for i in items if i["name"] == name), None)

    status, schema = api("GET", "/indexerProxy/schema")
    assert status == 200
    http_proxy = next((s for s in schema if s.get("implementation") == "Http"), None)
    assert http_proxy is not None, [s.get("implementation") for s in schema]
    assert {"host", "port", "username", "password"} <= {f["name"] for f in http_proxy["fields"]}

    # Prowlarr fetches a page through an HTTP proxy when it is saved, so something must
    # answer on gluetun:8888; the VPN itself is not part of CI.
    stand_in = run(["docker", "run", "-d", "--name", PROXY_STAND_IN, "--network", SHARED_NETWORK,
                    "--network-alias", "gluetun",
                    "-v", f"{Path(__file__).with_name('fake_proxy.py')}:/proxy.py:ro",
                    "python:3.12-slim", "python", "/proxy.py"], timeout=600)
    assert stand_in.returncode == 0, f"stand-in proxy did not start:\n{stand_in.stdout}"
    deadline = time.monotonic() + 60
    while run(["docker", "exec", "orama-server", "python", "-c",
               "import socket; socket.create_connection(('gluetun', 8888), 3)"]).returncode != 0:
        assert time.monotonic() < deadline, "the stand-in proxy never answered"
        time.sleep(1)
    on = {**applied["setup"], "vpn": {"route_prowlarr": True}}
    first_password, second_password = secrets.token_hex(12), secrets.token_hex(12)
    try:
        # Create, then update (the password changed), through the real wiring code.
        assert run_proxy_wiring(on, "prowlarr:proxy", first_password, clean_host) == [
            ["prowlarr:proxy", "changed", "added the VPN proxy"]]
        proxy = listed("indexerProxy", "Gluetun (Orama)")
        assert proxy is not None
        fields = {f["name"]: f.get("value") for f in proxy["fields"]}
        assert (fields["host"], fields["port"], fields["username"]) == (
            "gluetun", 8888, GLUETUN_PROXY_USER)
        masked = first_password not in json.dumps(proxy)  # Prowlarr masks it when read back
        assert masked, "Prowlarr showed the proxy password (value withheld)"
        vpn_tags = [t["id"] for t in api("GET", "/tag")[1] if str(t["label"]).lower() == "vpn"]
        assert len(vpn_tags) == 1 and vpn_tags[0] in proxy["tags"]
        assert run_proxy_wiring(on, "prowlarr:proxy", second_password, clean_host) == [
            ["prowlarr:proxy", "changed", "restored the VPN proxy"]]
        assert len([p for p in api("GET", "/indexerProxy")[1] if p["name"] == "Gluetun (Orama)"]) == 1

        # Route the indexer through the proxy, then switch the proxy off again: Orama sends
        # the indexer back with its secret masked and forceSave; Prowlarr must keep the stored one.
        indexer = listed("indexer", "Fake Newznab")
        tagged = {**indexer, "tags": sorted({*indexer["tags"], vpn_tags[0]})}
        assert api("PUT", f"/indexer/{indexer['id']}", tagged, "?forceSave=true")[0] in (200, 202)
        off = run_proxy_wiring(applied["setup"], "prowlarr:proxy-off", second_password, clean_host)
        assert off[0][:2] == ["prowlarr:proxy-off", "changed"], off
        assert "removed the VPN proxy" in off[0][2]
        assert listed("indexerProxy", "Gluetun (Orama)") is None
        after = listed("indexer", "Fake Newznab")
        assert vpn_tags[0] not in after["tags"]
        assert api("POST", "/indexer/test", after)[0] in (200, 204)
        # What Prowlarr sent to the indexer is the real key, not the mask.
        seen = run(["docker", "exec", NEWZNAB, "python", "-c",
                    ("import urllib.request; print(urllib.request.urlopen("
                     "'http://127.0.0.1:8000/?t=lastkey').read().decode())")])
        kept = seen.stdout.strip() == secrets_["indexer:fake:apiKey"]  # never shown on failure
        assert kept, "Prowlarr did not keep the indexer's stored key (values withheld)"
    finally:  # leave Prowlarr as the wiring left it, whatever happened above
        clean_host.save_log("container-proxy.log", run(["docker", "logs", PROXY_STAND_IN]).stdout)
        run(["docker", "rm", "-f", PROXY_STAND_IN])
        proxy = listed("indexerProxy", "Gluetun (Orama)")
        if proxy is not None:
            api("DELETE", f"/indexerProxy/{proxy['id']}")
        for tag in api("GET", "/tag")[1] or []:
            if str(tag["label"]).lower() == "vpn":
                api("DELETE", f"/tag/{tag['id']}")


# ---------------------------------------------------------------- run again (RF3)

def test_second_install_and_apply_change_nothing(clean_host: Host, installed: dict, rerun: dict):
    status, output = rerun["install"]
    assert status == 0, f"second install.sh failed (exit {status}):\n{output}"
    assert orama_port(output) == installed["port"]
    status, output = rerun["apply"]
    assert status == 1, f"second apply: unexpected exit {status}:\n{output}"
    # Deploy and wiring both report nothing: the only ✓ is the always-printed folders line.
    assert [l for l in output.splitlines() if l.startswith("✓")] == ["✓ Folders ready"], (
        f"{output}\n{app_warnings(clean_host, WIRED_APPS)}")
    failures = failure_lines(output)
    assert len(failures) == 1 and FAKE_SERVER_HOST in failures[0], failures
    assert failure_lines(output) == failure_lines(rerun["first_apply"])
    assert skip_lines(output) == PLEX_WAITING
    assert rerun["after"] == rerun["before"]
    # Recyclarr runs only as a one-shot: no container and no config with keys left behind.
    assert run(["docker", "inspect", "orama-recyclarr"]).returncode != 0
    folder = clean_host.config_root / "config/recyclarr"
    assert not (folder / "recyclarr.yml").exists()
    assert not (folder / "settings.yml").exists()
    # Recyclarr fetched the guide data at Orama's pinned commits, not at their latest.
    for kind, name, commit in (
        ("trash-guides", "orama-trash-guides", recyclarr.TRASH_GUIDES_COMMIT),
        ("config-templates", "orama-config-templates", recyclarr.CONFIG_TEMPLATES_COMMIT),
    ):
        fetched = (folder / "resources" / kind / "git" / name / ".git/FETCH_HEAD").read_text()
        assert fetched.split()[0] == commit, (kind, fetched)
    assert http_get(installed["port"], "/healthz") == (200, "ok")


def test_quality_and_device_changes_reuse_profile(clean_host: Host, applied: dict,
                                                rerun: dict, outputs: Outputs):
    keys = stored_secrets(clean_host)
    def profiles(app):
        return [p for p in app_get(applied, keys, app, "/api/v3/qualityprofile")
                if p["name"] == "Orama"]
    ids = {app: profiles(app)[0]["id"] for app in ("sonarr", "radarr")}
    path = clean_host.state_dir / "setup.json"
    original = path.read_text()
    changed = json.loads(original)
    changed.update(device="samsung_s95f", quality="1080p")
    try:
        path.write_text(json.dumps(changed))
        status, output = apply(clean_host, outputs, "apply-device-quality-change")
        assert status == 1
        assert len(failure_lines(output)) == 1 and FAKE_SERVER_HOST in failure_lines(output)[0]
        for app, profile_id in ids.items():
            mine = profiles(app)
            assert len(mine) == 1 and mine[0]["id"] == profile_id
            scores = {i["name"]: i["score"] for i in mine[0]["formatItems"]}
            assert scores["TrueHD"] != -10000 and scores["TrueHD ATMOS"] != -10000
            assert scores["DTS"] == -10000 and scores["HDR10+ Boost"] == 1500
        status, output = apply(clean_host, outputs, "apply-device-quality-again")
        assert status == 1
        assert [l for l in output.splitlines() if l.startswith("✓")] == ["✓ Folders ready"]
        assert skip_lines(output) == PLEX_WAITING
    finally:
        path.write_text(original)
        status, output = apply(clean_host, outputs, "apply-restore-default-profile")
        assert status == 1
    for app, profile_id in ids.items():
        assert profiles(app)[0]["id"] == profile_id
        scores = {i["name"]: i["score"] for i in profiles(app)[0]["formatItems"]}
        assert scores["TrueHD"] == scores["TrueHD ATMOS"] == -10000


def test_jellyfin_interrupted_apply_recovers(clean_host: Host, rerun: dict, outputs: Outputs):
    before_key = stored_secrets(clean_host)["apikey:jellyfin"]
    script = Path(__file__).with_name("jellyfin_recovery.py").read_text()
    proc = subprocess.Popen(
        ["docker", "exec", "-i", "orama-server", "python", "-c", script,
         str(clean_host.state_dir / "setup.json")],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert select.select([proc.stdout], [], [], 120)[0], "recovery handshake timed out"
        assert proc.stdout.readline().strip() == "READY", "recovery setup failed"
        assert run(["docker", "kill", "orama-jellyfin"]).returncode == 0
        output, _ = proc.communicate("continue\n", timeout=60)
        assert proc.returncode == 0, "recovery probe failed (output withheld)"
        results = json.loads(output)
        assert results["jellyfin:libraries"] == "failed"
        assert results["jellyfin:startup"] == results["jellyfin:api-key"] == "unchanged"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()
        assert run(["docker", "start", "orama-jellyfin"]).returncode == 0
    status, output = apply(clean_host, outputs, "apply-recovery")
    assert status == 1
    changes = [line for line in output.splitlines() if line.startswith("✓")]
    assert changes == ["✓ Folders ready", "✓ Jellyfin: added the TV Shows library"]
    if stored_secrets(clean_host)["apikey:jellyfin"] != before_key:
        pytest.fail("API key changed (values withheld)")
    status, output = apply(clean_host, outputs, "apply-after-recovery")
    assert status == 1
    assert [line for line in output.splitlines() if line.startswith("✓")] == ["✓ Folders ready"]


def test_no_secret_in_logs_or_outputs(clean_host: Host, rerun: dict, outputs: Outputs):
    # Short entries are flags and markers (e.g. "1"), which any text contains; real secrets are longer.
    values = [v for v in stored_secrets(clean_host).values() if len(v) >= 8]
    assert values, "the secret store is empty"
    texts = dict(outputs.texts)
    for name in ("orama-server", "orama-server-agent", "orama-jellyfin", "orama-bazarr",
                 "orama-sonarr", "orama-radarr"):
        texts[f"docker logs {name}"] = run(["docker", "logs", name]).stdout
    logs = clean_host.config_root / "config/recyclarr/logs"
    for path in logs.rglob("*"):
        if path.is_file():
            texts[f"Recyclarr/{path.relative_to(logs)}"] = path.read_text(errors="replace")
    # Booleans only: an assertion must never show a secret value.
    leaks = sorted(where for where, text in texts.items() if any(v in text for v in values))
    assert leaks == [], "secret values found in the output of these"
