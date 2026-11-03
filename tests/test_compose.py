import shutil
import subprocess
from pathlib import Path
from pathlib import PurePosixPath as P

import pytest
import yaml
from conftest import make_setup

from orama.catalog import CATALOG, CORE
from orama.compose import build_apps_compose, build_core_compose, dump_yaml
from orama.layout import app_env_file
from orama.setup_model import enabled_apps

AWKWARD = '/volume1/Ταινίες & TV #1: "x"'
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
    assert doc["networks"] == {"orama": {"driver": "bridge"}}
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
    assert o["container_name"] == "orama-orama" and a["container_name"] == "orama-orama-agent"
    assert o["image"] == CORE["orama"].image and a["image"] == CORE["orama-agent"].image
    assert o["ports"] == ["8765:8765"] and o["user"] == "1000:1000"
    assert {"type": "bind", "source": "/srv/orama", "target": "/srv/orama"} in o["volumes"]
    assert {"type": "volume", "source": "orama-run", "target": "/run/orama"} in o["volumes"]
    for p in ("/srv/orama", "/srv/data"):
        assert {"type": "bind", "source": p, "target": p} in a["volumes"]
    assert a["group_add"] == ["998"]
    assert a["environment"] == {
        "ORAMA_DATA_ROOT": "/srv/data", "ORAMA_CONFIG_ROOT": "/srv/orama",
        "ORAMA_PUID": "1000", "ORAMA_PGID": "1000"}


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
