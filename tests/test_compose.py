import os
import shutil
import subprocess
from pathlib import Path
from pathlib import PurePosixPath as P

import pytest
import yaml
from conftest import make_setup

from orama.catalog import (
    CATALOG,
    CORE,
    GLUETUN_PORT_FORWARD_UP_COMMAND,
    GLUETUN_PROXY_USER,
    QBITTORRENT_WEBUI_USER,
)
from orama.compose import (
    QBITTORRENT_WEBUI_PORT,
    SHARED_NETWORK,
    build_apps_compose,
    build_core_compose,
    dump_yaml,
)
from orama.layout import app_env_dir, app_env_file
from orama.secrets import SecretStore, ensure_app_keys, env_files_for
from orama.setup_model import enabled_apps

AWKWARD = '/volume1/Ταινίες & TV #1 "x"'
PORTS = {a: s.default_host_port for a, s in CATALOG.items() if s.default_host_port}


def full(**kw):
    values = {
        "media_servers": {"plex", "jellyfin"},
        "download_methods": {"usenet", "torrent"},
        "vpn": {"provider": "protonvpn", "port_forwarding": True},
        "render_gid": 105,
        "nvidia": True,
        "ports": PORTS,
    }
    values.update(kw)
    return make_setup(**values)


def core():
    return build_core_compose(P("/srv/orama"), P("/srv/data"), 1000, 1000, 998, 8765)


def test_compose_roundtrips_through_yaml_with_awkward_path():
    s = make_setup(data_root=AWKWARD, ports=PORTS)
    doc = yaml.safe_load(dump_yaml(build_apps_compose(s)))
    vols = doc["services"]["sonarr"]["volumes"]
    assert {"type": "bind", "source": AWKWARD, "target": "/data"} in vols


def test_top_level_and_service_basics():
    doc = build_apps_compose(full())
    assert doc["name"] == "orama-apps"
    for app, svc in doc["services"].items():
        assert svc["image"] == CATALOG[app].image
        assert svc["container_name"] == f"orama-{app}"
        assert svc["restart"] == "unless-stopped"
        assert all(v["type"] == "bind" for v in svc["volumes"])


def test_every_data_app_sees_same_data_mount():
    s = full()
    doc = build_apps_compose(s)
    for app in enabled_apps(s):
        vols = doc["services"][app]["volumes"]
        has = {"type": "bind", "source": "/srv/data", "target": "/data"} in vols
        assert has == CATALOG[app].mounts_data
    assert CATALOG["sonarr"].mounts_data and CATALOG["qbittorrent"].mounts_data


def test_env_conventions():
    svcs = build_apps_compose(full())["services"]
    assert svcs["sonarr"]["environment"] == {
        "PUID": "1000", "PGID": "1000", "UMASK": "002", "TZ": "Etc/UTC"}
    assert svcs["plex"]["environment"] == {
        "PLEX_UID": "1000", "PLEX_GID": "1000", "TZ": "Etc/UTC"}
    assert svcs["jellyfin"]["user"] == "1000:1000"
    assert svcs["qbittorrent"]["environment"]["WEBUI_PORT"] == "8090"


def test_qbittorrent_behind_gluetun_has_no_own_network_or_ports():
    svcs = build_apps_compose(full())["services"]
    q, g = svcs["qbittorrent"], svcs["gluetun"]
    assert q["network_mode"] == "service:gluetun"
    assert "ports" not in q and "networks" not in q
    assert q["depends_on"] == {"gluetun": {"condition": "service_healthy"}}
    assert g["ports"] == ["8090:8090"]
    assert g["networks"] == ["orama"]
    assert "depends_on" not in g
    assert g["environment"]["VPN_TYPE"] == "wireguard"
    assert g["environment"]["VPN_PORT_FORWARDING"] == "on"
    assert {"type": "bind", "source": "/srv/orama/config/gluetun", "target": "/gluetun"} in g["volumes"]


def test_gluetun_publishes_assigned_qbittorrent_port():
    s = full(ports={**PORTS, "qbittorrent": 8095})
    assert build_apps_compose(s)["services"]["gluetun"]["ports"] == ["8095:8090"]


def test_no_port_forwarding_env_when_disabled():
    s = full(vpn={"provider": "protonvpn", "port_forwarding": False})
    env = build_apps_compose(s)["services"]["gluetun"]["environment"]
    assert "VPN_PORT_FORWARDING" not in env


# The plan's "Fixed values" hook, changed in the Task 2 fix round (qBittorrent keeps
# localhost auth on, so the hook logs in first) and again in the Plan 2a security fix
# (the password goes in a private temp file, never on wget's command line). '$$' is
# Compose's escape for a literal '$'; the password comes from Gluetun's env file.
PORT_FORWARD_COMMAND = (
    r"""/bin/sh -c 'umask 077; """
    r"""printf "username=orama&password=%s" "$$QBT_WEBUI_PASSWORD" > /tmp/qbt.login && """
    r"""wget -qO- --retry-connrefused --save-cookies /tmp/qbt.cookies --keep-session-cookies """
    r"""--post-file=/tmp/qbt.login http://127.0.0.1:8090/api/v2/auth/login; """
    r"""rc=$$?; rm -f /tmp/qbt.login; """
    r"""[ $$rc -eq 0 ] && wget -qO- --load-cookies /tmp/qbt.cookies """
    r"""--post-data "json={\"listen_port\":{{PORTS}}}" """
    r"""http://127.0.0.1:8090/api/v2/app/setPreferences 2>&1'"""
)


def test_port_forward_command_is_the_fixed_value():
    assert GLUETUN_PORT_FORWARD_UP_COMMAND == PORT_FORWARD_COMMAND
    assert f"http://127.0.0.1:{QBITTORRENT_WEBUI_PORT}/" in GLUETUN_PORT_FORWARD_UP_COMMAND
    # Every '$' is escaped for Compose ('$$'): the Agent refuses every other '$'.
    assert "$" not in GLUETUN_PORT_FORWARD_UP_COMMAND.replace("$$", "")
    assert f"username={QBITTORRENT_WEBUI_USER}&" in GLUETUN_PORT_FORWARD_UP_COMMAND
    # The password is only ever a printf argument (a shell builtin), never wget's.
    assert GLUETUN_PORT_FORWARD_UP_COMMAND.count("QBT_WEBUI_PASSWORD") == 1
    assert '"username=orama&password=%s" "$$QBT_WEBUI_PASSWORD"' in GLUETUN_PORT_FORWARD_UP_COMMAND


def _fake_wget(bin_dir: Path, log: Path, login_rc: int) -> None:
    """A wget that records its argv, and the post file's mode and body, then exits."""
    bin_dir.mkdir()
    script = bin_dir / "wget"
    script.write_text(
        "#!/bin/sh\n"
        f'log="{log}"\n'
        'printf "%s\\n" "$@" >> "$log"; echo ---- >> "$log"\n'
        'for a in "$@"; do case "$a" in --post-file=*)\n'
        '  f="${a#--post-file=}"; stat -c %a "$f" > "$log.mode"; cat "$f" > "$log.body";;\n'
        "esac; done\n"
        f'case "$*" in *auth/login*) exit {login_rc};; esac\n'
        "exit 0\n"
    )
    script.chmod(0o755)


def _run_hook(tmp_path: Path, password: str, login_rc: int) -> Path:
    """Run the hook the way Gluetun does: Compose turns '$$' into '$', Gluetun fills
    {{PORTS}} and splits the line (quotes, no expansion), and the shell runs it."""
    import shlex

    line = (
        GLUETUN_PORT_FORWARD_UP_COMMAND.replace("$$", "$")
        .replace("{{PORTS}}", "6881")
        .replace("/tmp/qbt.", f"{tmp_path}/qbt.")
    )
    argv = shlex.split(line)
    assert argv[:2] == ["/bin/sh", "-c"] and len(argv) == 3
    log = tmp_path / "wget.log"
    _fake_wget(tmp_path / "bin", log, login_rc)
    env = {"PATH": f"{tmp_path / 'bin'}:/usr/bin:/bin", "QBT_WEBUI_PASSWORD": password}
    subprocess.run(argv, env=env, check=False, capture_output=True, timeout=30)
    return log


@pytest.mark.parametrize("login_rc", [0, 4])
def test_port_forward_hook_keeps_the_password_out_of_argv(tmp_path, login_rc):
    import secrets

    password = secrets.token_urlsafe(24)
    log = _run_hook(tmp_path, password, login_rc)
    calls = [c for c in log.read_text().split("----\n") if c]
    assert password not in log.read_text()  # no wget argv held it
    assert (tmp_path / "wget.log.body").read_text() == f"username=orama&password={password}"
    assert (tmp_path / "wget.log.mode").read_text().strip() == "600"
    assert not (tmp_path / "qbt.login").exists()  # removed, even when login failed
    assert "http://127.0.0.1:8090/api/v2/auth/login" in calls[0]
    if login_rc == 0:
        assert len(calls) == 2
        assert '--post-data\njson={"listen_port":6881}\n' in calls[1]
    else:
        assert len(calls) == 1  # no setPreferences after a failed login


def _compose_config(tmp_path: Path, doc: dict) -> dict:
    path = tmp_path / "compose.yaml"
    path.write_text(dump_yaml(doc), encoding="utf-8")
    result = subprocess.run(
        ["docker", "compose", "-f", str(path), "config", "--format", "json"],
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert result.returncode == 0, result.stderr
    import json

    return json.loads(result.stdout)


def _gluetun_only(tmp_path: Path, env_text: str) -> dict:
    s = full(vpn={"provider": "protonvpn", "port_forwarding": True, "route_prowlarr": True})
    doc = build_apps_compose(s)
    env_file = tmp_path / "gluetun.env"
    env_file.write_text(env_text)
    gluetun = {**doc["services"]["gluetun"], "env_file": [str(env_file)]}
    return {**doc, "services": {"gluetun": gluetun}}


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI not available")
def test_real_compose_renders_the_hook_with_single_dollars_and_reads_env_values(tmp_path):
    import base64
    import secrets

    from orama_agent.server import _env_file_text

    values = {
        "WIREGUARD_PRIVATE_KEY": base64.b64encode(os.urandom(32)).decode(),
        "HTTPPROXY_PASSWORD": secrets.token_urlsafe(24),
        "QBT_WEBUI_PASSWORD": secrets.token_urlsafe(24),
    }
    rendered = _compose_config(tmp_path, _gluetun_only(tmp_path, _env_file_text(values).decode()))
    env = rendered["services"]["gluetun"]["environment"]
    # `config` prints a canonical compose file, so each literal '$' comes back as '$$'.
    # Unchanged output means every '$$' became a literal '$' for the shell; a bare '$'
    # would have been interpolated away (shown below).
    assert env["VPN_PORT_FORWARDING_UP_COMMAND"] == GLUETUN_PORT_FORWARD_UP_COMMAND
    for key, value in values.items():  # Compose reads back exactly what the Agent wrote
        assert env[key] == value

    unescaped = _gluetun_only(tmp_path, _env_file_text(values).decode())
    unescaped["services"]["gluetun"]["environment"]["VPN_PORT_FORWARDING_UP_COMMAND"] = (
        GLUETUN_PORT_FORWARD_UP_COMMAND.replace("$$", "$")
    )
    env = _compose_config(tmp_path, unescaped)["services"]["gluetun"]["environment"]
    assert "QBT_WEBUI_PASSWORD" not in env["VPN_PORT_FORWARDING_UP_COMMAND"]


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI not available")
def test_real_compose_shows_why_a_trailing_backslash_is_refused(tmp_path):
    # Documents the attack the Agent refuses: Compose reads "\'" as an escaped quote, so
    # the next line's value becomes a setting of its own. The Agent's parser refuses it.
    from orama_agent.server import _parse_env_file

    injected = "FIREWALL_ENABLED_DISABLING_IT_SHOOTS_YOU_IN_YOUR_FOOT"
    text = f"WIREGUARD_PRIVATE_KEY='x\\'\nHTTPPROXY_PASSWORD='{injected}=off #'\n"
    env = _compose_config(tmp_path, _gluetun_only(tmp_path, text))["services"]["gluetun"][
        "environment"
    ]
    assert env[injected] == "off"
    assert _parse_env_file(text) is None


@pytest.mark.parametrize("enabled", [True, False])
def test_gluetun_port_forward_env_only_when_enabled(enabled):
    s = full(vpn={"provider": "protonvpn", "port_forwarding": enabled})
    env = build_apps_compose(s)["services"]["gluetun"]["environment"]
    if enabled:
        assert env["VPN_PORT_FORWARDING"] == "on"
        assert env["VPN_PORT_FORWARDING_UP_COMMAND"] == PORT_FORWARD_COMMAND
        assert env["PORT_FORWARD_ONLY"] == "on"
    else:
        for key in ("VPN_PORT_FORWARDING", "VPN_PORT_FORWARDING_UP_COMMAND", "PORT_FORWARD_ONLY"):
            assert key not in env


def test_port_forward_command_survives_yaml():
    doc = yaml.safe_load(dump_yaml(build_apps_compose(full())))
    env = doc["services"]["gluetun"]["environment"]
    assert env["VPN_PORT_FORWARDING_UP_COMMAND"] == PORT_FORWARD_COMMAND


def test_port_forward_only_only_for_protonvpn():
    s = full(vpn={"provider": "mullvad", "port_forwarding": True})
    env = build_apps_compose(s)["services"]["gluetun"]["environment"]
    assert env["VPN_PORT_FORWARDING"] == "on"
    assert env["VPN_PORT_FORWARDING_UP_COMMAND"] == PORT_FORWARD_COMMAND
    assert "PORT_FORWARD_ONLY" not in env


@pytest.mark.parametrize("route", [True, False])
def test_gluetun_httpproxy_only_when_route_prowlarr(route):
    s = full(vpn={"provider": "protonvpn", "port_forwarding": False, "route_prowlarr": route})
    env = build_apps_compose(s)["services"]["gluetun"]["environment"]
    assert "HTTPPROXY_PASSWORD" not in env
    if route:
        assert env["HTTPPROXY"] == "on"
        assert env["HTTPPROXY_USER"] == GLUETUN_PROXY_USER == "orama"
    else:
        assert "HTTPPROXY" not in env and "HTTPPROXY_USER" not in env


def test_proxy_password_never_in_compose_yaml(tmp_path):
    s = full(vpn={"provider": "protonvpn", "route_prowlarr": True})
    store = SecretStore(tmp_path / "s.bin")
    store.set("vpn:wireguard_private_key", "k" * 8)
    ensure_app_keys(store, enabled_apps(s), gluetun_proxy=True)
    password = env_files_for(s, store)["gluetun"]["HTTPPROXY_PASSWORD"]
    text = dump_yaml(build_apps_compose(s))
    assert password not in text and "HTTPPROXY_PASSWORD" not in text
    gluetun = yaml.safe_load(text)["services"]["gluetun"]
    assert gluetun["env_file"] == [str(app_env_file(s.config_root, "gluetun"))]


def test_apps_compose_uses_external_shared_network():
    doc = build_apps_compose(full())
    assert SHARED_NETWORK == "orama-net"
    assert doc["networks"] == {"orama": {"name": "orama-net", "external": True}}
    for app, svc in doc["services"].items():
        if "network_mode" not in svc:
            assert svc["networks"] == ["orama"], app


def test_core_compose_creates_shared_network_and_orama_joins_it():
    c = core()
    assert c["networks"] == {"orama": {"name": "orama-net", "driver": "bridge"}}
    assert c["services"]["orama"]["networks"] == ["orama"]
    agent = c["services"]["orama-agent"]
    assert agent["network_mode"] == "none"
    assert "networks" not in agent


def test_qbittorrent_without_vpn_has_its_own_port():
    s = make_setup(download_methods={"torrent"}, ports=PORTS)
    svcs = build_apps_compose(s)["services"]
    assert "gluetun" not in svcs
    q = svcs["qbittorrent"]
    assert q["ports"] == ["8090:8090"] and q["networks"] == ["orama"]
    assert "network_mode" not in q


def test_ports_are_host_container_strings():
    svcs = build_apps_compose(full(ports={**PORTS, "sonarr": 9001}))["services"]
    assert svcs["sonarr"]["ports"] == ["9001:8989"]
    assert "ports" not in svcs["sabnzbd"] or svcs["sabnzbd"]["ports"] == ["8080:8080"]


def test_missing_port_raises():
    with pytest.raises(ValueError, match="plex"):
        build_apps_compose(make_setup(ports={}))


def test_render_gid_is_numeric_string_in_group_add():
    svcs = build_apps_compose(full())["services"]
    for app in ("plex", "jellyfin"):
        assert svcs[app]["group_add"] == ["105"]
        assert svcs[app]["devices"] == ["/dev/dri:/dev/dri"]
        assert svcs[app]["deploy"]["resources"]["reservations"]["devices"] == [
            {"driver": "nvidia", "capabilities": ["gpu"]}]
    assert "group_add" not in svcs["sonarr"]


def test_no_dri_device_when_render_gid_none():
    doc = build_apps_compose(make_setup(render_gid=None, ports=PORTS))
    assert "devices" not in doc["services"]["plex"] and "group_add" not in doc["services"]["plex"]
    assert "deploy" not in doc["services"]["plex"]


def test_compose_contains_no_secret_env_values():
    s = full()
    text = dump_yaml(build_apps_compose(s))
    doc = yaml.safe_load(text)
    for app in enabled_apps(s):
        svc = doc["services"][app]
        for name in CATALOG[app].secret_env:
            assert name not in svc.get("environment", {})
        if CATALOG[app].secret_env:
            assert svc["env_file"] == [str(app_env_file(s.config_root, app))]
        else:
            assert "env_file" not in svc
    assert "WIREGUARD_PRIVATE_KEY" not in text


def test_only_orama_agent_mounts_docker_socket():
    c = core()
    socket_users = [n for n, s in c["services"].items()
                    if any(v["source"] == "/var/run/docker.sock" for v in s.get("volumes", []))]
    assert socket_users == ["orama-agent"]
    assert "ports" not in c["services"]["orama-agent"]
    assert c["services"]["orama-agent"]["network_mode"] == "none"


def test_core_layout():
    c = core()
    assert c["name"] == "orama-core"
    o, a = c["services"]["orama"], c["services"]["orama-agent"]
    assert o["container_name"] == "orama-server" and a["container_name"] == "orama-server-agent"
    assert o["image"] == CORE["orama"].image and a["image"] == CORE["orama-agent"].image
    assert o["ports"] == ["8765:8765"] and o["user"] == "1000:1000"
    assert {"type": "bind", "source": "/srv/orama/orama", "target": "/srv/orama/orama"} in (
        o["volumes"]
    )
    assert {"type": "volume", "source": "orama-run", "target": "/run/orama"} in o["volumes"]
    for p in ("/srv/orama", "/srv/data"):
        assert {"type": "bind", "source": p, "target": p} in a["volumes"]
    assert a["group_add"] == ["998"]
    assert a["environment"] == {
        "ORAMA_DATA_ROOT": "/srv/data", "ORAMA_CONFIG_ROOT": "/srv/orama",
        "ORAMA_PUID": "1000", "ORAMA_PGID": "1000"}


def test_orama_mounts_only_its_state_dir():
    # Orama must not be able to rewrite app config dirs or env files (the Agent mounts those).
    binds = [v for v in core()["services"]["orama"]["volumes"] if v["type"] == "bind"]
    assert binds == [{"type": "bind", "source": "/srv/orama/orama", "target": "/srv/orama/orama"}]
    for v in binds:
        assert v["source"] != "/srv/orama"
        assert not P(v["source"]).is_relative_to("/srv/data")


def test_core_image_overrides():
    c = build_core_compose(P("/a"), P("/b"), 1, 1, 1, 80, orama_image="o:1", agent_image="a:1")
    assert c["services"]["orama"]["image"] == "o:1"
    assert c["services"]["orama-agent"]["image"] == "a:1"


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI not available")
@pytest.mark.parametrize("kind", ["full", "awkward", "core"])
def test_real_compose_accepts_generated_yaml(tmp_path, kind):
    if kind == "core":
        doc = build_core_compose(P(str(tmp_path / "cfg")), P(str(tmp_path / "data")),
                                 1000, 1000, 998, 8765)
    else:
        kw = {"data_root": AWKWARD} if kind == "awkward" else {}
        s = full(config_root=str(tmp_path / "cfg"), **kw)
        for app in enabled_apps(s):
            if CATALOG[app].secret_env:
                env = Path(app_env_file(s.config_root, app))
                env.parent.mkdir(parents=True, exist_ok=True)
                env.write_text("".join(f"{n}=placeholder\n" for n in CATALOG[app].secret_env))
        doc = build_apps_compose(s)
    f = tmp_path / "compose.yaml"
    f.write_text(dump_yaml(doc), encoding="utf-8")
    r = subprocess.run(["docker", "compose", "-f", str(f), "config", "-q"],
                       check=False, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_gluetun_has_no_ports_when_qbittorrent_removed():
    s = full(removed_apps={"qbittorrent"})
    g = build_apps_compose(s)["services"]["gluetun"]
    assert "ports" not in g


def test_removing_gluetun_while_vpn_on_is_refused():
    with pytest.raises(ValueError, match="turn the VPN off"):
        full(removed_apps={"gluetun"})


@pytest.mark.parametrize("doc_kind", ["apps", "core"])
def test_no_mount_exposes_app_env_dir_except_agent(doc_kind):
    # Env files live in an Agent-only folder: no app (and not Orama) may mount it or a parent.
    s = full()
    doc = build_apps_compose(s) if doc_kind == "apps" else core()
    env_dir = app_env_dir(s.config_root)
    for name, svc in doc["services"].items():
        if name == "orama-agent":
            continue
        for v in svc.get("volumes", []):
            if v["type"] != "bind":
                continue
            src = P(v["source"])
            assert not src.is_relative_to(env_dir), (name, src)
            assert not env_dir.is_relative_to(src), (name, src)


def test_env_file_pin_is_in_app_env_dir():
    s = full()
    doc = build_apps_compose(s)
    assert doc["services"]["sonarr"]["env_file"] == ["/srv/orama/app-env/sonarr.env"]
    assert doc["services"]["gluetun"]["env_file"] == ["/srv/orama/app-env/gluetun.env"]


def test_qbittorrent_password_never_in_compose_yaml(tmp_path):
    s = full(vpn={"provider": "protonvpn", "port_forwarding": True})
    store = SecretStore(tmp_path / "s.bin")
    store.set("vpn:wireguard_private_key", "k" * 8)
    ensure_app_keys(store, enabled_apps(s))
    password = env_files_for(s, store)["gluetun"]["QBT_WEBUI_PASSWORD"]
    assert password == store.get("password:qbittorrent")
    text = dump_yaml(build_apps_compose(s))
    assert password not in text
    env = yaml.safe_load(text)["services"]["gluetun"]["environment"]
    assert "QBT_WEBUI_PASSWORD" not in env
    assert "$$QBT_WEBUI_PASSWORD" in env["VPN_PORT_FORWARDING_UP_COMMAND"]
