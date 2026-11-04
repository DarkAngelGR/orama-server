import copy
import os

import pytest
from conftest import make_setup

from orama.catalog import CATALOG, GLUETUN_PORT_FORWARD_UP_COMMAND, GLUETUN_PROXY_USER
from orama.compose import build_apps_compose
from orama.layout import app_config_dir, app_env_dir, app_env_file
from orama.setup_model import enabled_apps
from orama_agent.validate import (
    Refused,
    Roots,
    check_host_paths,
    safe_path,
    validate_compose,
    validate_env_files,
    validate_secrets_present,
)

PORTS = {a: s.default_host_port for a, s in CATALOG.items() if s.default_host_port}


@pytest.fixture
def roots(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "cfg").mkdir()
    return Roots(
        data=type(tmp_path)(os.path.realpath(tmp_path / "data")),
        config=type(tmp_path)(os.path.realpath(tmp_path / "cfg")),
        puid=1000,
        pgid=1000,
    )


def setup_for(roots, **kw):
    values = {"data_root": str(roots.data), "config_root": str(roots.config), "ports": PORTS}
    values.update(kw)
    return make_setup(**values)


def full_setup(roots):
    return setup_for(
        roots,
        media_servers={"plex", "jellyfin"},
        download_methods={"usenet", "torrent"},
        vpn={"provider": "protonvpn", "port_forwarding": True, "route_prowlarr": True},
        render_gid=105,
        nvidia=True,
    )


def good_doc(roots):
    return build_apps_compose(full_setup(roots))


def refused(doc, roots):
    with pytest.raises(Refused):
        validate_compose(doc, roots)


def refused_with(doc, roots, match):
    with pytest.raises(Refused, match=match):
        validate_compose(doc, roots)


# ---------------------------------------------------------------- accepts


@pytest.mark.parametrize(
    "kind",
    ["minimal", "full", "torrent_no_vpn", "jellyfin_render_only", "vpn_other_provider"],
)
def test_accepts_builder_output(roots, kind):
    setup = {
        "minimal": lambda: setup_for(roots),
        "full": lambda: full_setup(roots),
        "torrent_no_vpn": lambda: setup_for(roots, download_methods={"torrent"}),
        "jellyfin_render_only": lambda: setup_for(
            roots, media_servers={"jellyfin"}, render_gid=44
        ),
        "vpn_other_provider": lambda: setup_for(
            roots,
            download_methods={"torrent"},
            vpn={"provider": "mullvad", "port_forwarding": True, "route_prowlarr": True},
        ),
    }[kind]()
    validate_compose(build_apps_compose(setup), roots)


def test_accepts_builder_output_through_yaml(roots):
    import yaml

    from orama.compose import dump_yaml

    validate_compose(yaml.safe_load(dump_yaml(good_doc(roots))), roots)


# ---------------------------------------------------------------- Roots


def _env(monkeypatch, data, cfg, puid="1000", pgid="1000"):
    for key, value in (
        ("ORAMA_DATA_ROOT", data),
        ("ORAMA_CONFIG_ROOT", cfg),
        ("ORAMA_PUID", puid),
        ("ORAMA_PGID", pgid),
    ):
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)


def test_roots_from_env(monkeypatch, tmp_path):
    (tmp_path / "d").mkdir()
    (tmp_path / "c").mkdir()
    _env(monkeypatch, str(tmp_path / "d"), str(tmp_path / "c"))
    r = Roots.from_env()
    assert r.data == type(tmp_path)(os.path.realpath(tmp_path / "d"))
    assert r.config == type(tmp_path)(os.path.realpath(tmp_path / "c"))
    assert (r.puid, r.pgid) == (1000, 1000)


@pytest.mark.parametrize(
    "puid,pgid",
    [(None, "1000"), ("1000", None), ("0", "1000"), ("1000", "0"), ("abc", "1000"),
     ("", "1000"), ("-1", "1000"), ("1000", "10 0")],
)
def test_roots_from_env_refuses_bad_ids(monkeypatch, tmp_path, puid, pgid):
    (tmp_path / "d").mkdir()
    (tmp_path / "c").mkdir()
    _env(monkeypatch, str(tmp_path / "d"), str(tmp_path / "c"), puid, pgid)
    with pytest.raises(Refused):
        Roots.from_env()


def test_roots_are_realpath_resolved(monkeypatch, tmp_path):
    (tmp_path / "d").mkdir()
    (tmp_path / "c").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "d")
    _env(monkeypatch, str(tmp_path / "link"), str(tmp_path / "c"))
    assert Roots.from_env().data == type(tmp_path)(os.path.realpath(tmp_path / "d"))


@pytest.mark.parametrize(
    "data,cfg",
    [
        (None, "/srv/cfg"),
        ("/srv/data", None),
        ("", "/srv/cfg"),
        ("relative", "/srv/cfg"),
        ("/", "/srv/cfg"),
        ("/srv/data/../x", "/srv/cfg"),
        ("/srv/same", "/srv/same"),
        ("/srv/data", "/srv/data/cfg"),
        ("/srv/data/sub", "/srv/data"),
    ],
)
def test_roots_from_env_refuses_bad_roots(monkeypatch, data, cfg):
    _env(monkeypatch, data, cfg)
    with pytest.raises(Refused):
        Roots.from_env()


def test_roots_from_env_refuses_nested_via_symlink(monkeypatch, tmp_path):
    (tmp_path / "d" / "c").mkdir(parents=True)
    (tmp_path / "clink").symlink_to(tmp_path / "d" / "c")
    _env(monkeypatch, str(tmp_path / "d"), str(tmp_path / "clink"))
    with pytest.raises(Refused):
        Roots.from_env()


# ---------------------------------------------------------------- safe_path


def test_safe_path_accepts_inside_and_root(roots):
    assert safe_path(str(roots.data), roots) == roots.data
    assert safe_path(str(roots.config / "config" / "sonarr"), roots) == (
        roots.config / "config" / "sonarr"
    )


@pytest.mark.parametrize("src", ["/etc", "/", "relative", "/srv/data/../../etc", ""])
def test_safe_path_refuses(roots, src):
    with pytest.raises(Refused):
        safe_path(src, roots)


def test_safe_path_refuses_dotdot_even_if_inside(roots):
    with pytest.raises(Refused):
        safe_path(str(roots.data) + "/media/../media", roots)


def test_safe_path_refuses_nul(roots):
    with pytest.raises(Refused):
        safe_path(str(roots.data) + "/a\0b", roots)


def test_safe_path_refuses_sibling_prefix(roots):
    sibling = str(roots.data) + "2"
    os.mkdir(sibling)
    with pytest.raises(Refused):
        safe_path(sibling, roots)


def test_safe_path_refuses_non_string(roots):
    with pytest.raises(Refused):
        safe_path(roots.data, roots)  # type: ignore[arg-type]


def test_refuses_symlink_escape(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "evil").symlink_to("/etc")
    with pytest.raises(Refused):
        safe_path(
            str(tmp_path / "data" / "evil"),
            Roots(tmp_path / "data", tmp_path / "cfg", 1000, 1000),
        )


def test_safe_path_returns_resolved_path(roots):
    (roots.data / "real").mkdir()
    (roots.data / "link").symlink_to(roots.data / "real")
    assert safe_path(str(roots.data / "link"), roots) == roots.data / "real"


def test_safe_path_refuses_docker_socket_even_inside_a_root(tmp_path):
    (tmp_path / "cfg").mkdir()
    roots = Roots(type(tmp_path)("/run"), tmp_path / "cfg", 1000, 1000)
    with pytest.raises(Refused):
        safe_path("/run/docker.sock", roots)
    with pytest.raises(Refused):
        safe_path("/var/run/docker.sock", roots)


# ---------------------------------------------------------------- top level


def test_refuses_non_dict(roots):
    refused([], roots)


def test_refuses_unknown_top_level_key(roots):
    doc = good_doc(roots)
    doc["volumes"] = {"x": {}}
    refused(doc, roots)


def test_refuses_wrong_project_name(roots):
    doc = good_doc(roots)
    doc["name"] = "orama-core"
    refused(doc, roots)


def test_full_setup_has_both_gluetun_options(roots):
    env = good_doc(roots)["services"]["gluetun"]["environment"]
    assert env["VPN_PORT_FORWARDING_UP_COMMAND"] == GLUETUN_PORT_FORWARD_UP_COMMAND
    assert env["PORT_FORWARD_ONLY"] == "on" and env["HTTPPROXY"] == "on"
    assert env["HTTPPROXY_USER"] == GLUETUN_PROXY_USER


@pytest.mark.parametrize(
    "networks",
    [
        {"orama": {"driver": "bridge"}},
        {"orama": {"name": "host", "external": True}},
        {"orama": {"name": "orama-net", "external": True, "driver": "host"}},
        {"orama": {"name": "orama-net", "external": True}, "other": {"driver": "bridge"}},
        {"orama": {"name": "orama-net", "external": False}},
        {"orama": {"name": "orama-net", "external": 1}},
        {"orama": {"name": "orama-net", "external": "true"}},
        {"orama": {"name": ["orama-net"], "external": True}},
        {"orama": ["orama-net"]},
        {"orama": {"name": "orama-net"}},
        {"orama": {"external": True}},
        {"orama": {"driver": "host"}},
        {"host": {"name": "orama-net", "external": True}},
        ["orama"],
    ],
)
def test_refuses_other_network_definitions(roots, networks):
    doc = good_doc(roots)
    doc["networks"] = networks
    refused(doc, roots)


def test_refuses_missing_network_definition(roots):
    doc = good_doc(roots)
    del doc["networks"]
    refused(doc, roots)


# ---------------------------------------------------------------- services


def test_refuses_unknown_service(roots):
    doc = good_doc(roots)
    doc["services"]["evil"] = copy.deepcopy(doc["services"]["sonarr"])
    refused(doc, roots)


def test_refuses_core_service_name(roots):
    doc = good_doc(roots)
    doc["services"]["orama-agent"] = copy.deepcopy(doc["services"]["sonarr"])
    refused(doc, roots)


def test_refuses_swapped_image(roots):
    doc = good_doc(roots)
    doc["services"]["sonarr"]["image"] = "evil/sonarr:4.0.20.3014-ls329"
    refused(doc, roots)


@pytest.mark.parametrize(
    "key,value",
    [
        ("privileged", True),
        ("pid", "host"),
        ("ipc", "host"),
        ("security_opt", ["seccomp:unconfined"]),
        ("userns_mode", "host"),
        ("volumes_from", ["gluetun"]),
    ],
)
def test_refuses_dangerous_keys(roots, key, value):
    doc = good_doc(roots)
    doc["services"]["sonarr"][key] = value
    refused(doc, roots)


def test_refuses_privileged(roots):
    doc = good_doc(roots)
    doc["services"]["sonarr"]["privileged"] = True
    refused(doc, roots)


@pytest.mark.parametrize(
    "key,value",
    [
        ("command", ["sh", "-c", "id"]),
        ("entrypoint", "/bin/sh"),
        ("sysctls", {"net.ipv4.ip_forward": "1"}),
        ("cgroup", "host"),
        ("extra_hosts", ["a:1.2.3.4"]),
        ("healthcheck", {"test": ["CMD", "true"]}),
        ("labels", {"a": "b"}),
        ("cap_drop", ["ALL"]),
        ("tmpfs", ["/tmp"]),
    ],
)
def test_refuses_keys_outside_allowlist(roots, key, value):
    doc = good_doc(roots)
    doc["services"]["sonarr"][key] = value
    refused(doc, roots)


def test_refuses_wrong_container_name(roots):
    doc = good_doc(roots)
    doc["services"]["sonarr"]["container_name"] = "orama-server-agent"
    refused(doc, roots)


def test_refuses_wrong_restart(roots):
    doc = good_doc(roots)
    doc["services"]["sonarr"]["restart"] = "always"
    refused(doc, roots)


# ---------------------------------------------------------------- network_mode


@pytest.mark.parametrize("mode", ["host", "container:orama-server-agent", "none", "bridge"])
def test_refuses_other_network_modes(roots, mode):
    doc = good_doc(roots)
    doc["services"]["qbittorrent"]["network_mode"] = mode
    refused(doc, roots)


def test_refuses_gluetun_network_mode_on_other_service(roots):
    doc = good_doc(roots)
    svc = doc["services"]["sonarr"]
    del svc["networks"]
    del svc["ports"]
    svc["network_mode"] = "service:gluetun"
    refused(doc, roots)


def test_refuses_gluetun_network_mode_without_gluetun(roots):
    doc = good_doc(roots)
    del doc["services"]["gluetun"]
    refused(doc, roots)


def test_refuses_network_mode_with_networks(roots):
    doc = good_doc(roots)
    doc["services"]["qbittorrent"]["networks"] = ["orama"]
    refused(doc, roots)


@pytest.mark.parametrize("networks", [["default"], ["orama", "other"], "orama", []])
def test_refuses_bad_service_networks(roots, networks):
    doc = good_doc(roots)
    doc["services"]["sonarr"]["networks"] = networks
    refused(doc, roots)


# ---------------------------------------------------------------- depends_on


@pytest.mark.parametrize(
    "deps",
    [
        {"gluetun": {"condition": "service_started"}},
        {"sonarr": {"condition": "service_healthy"}},
        ["gluetun"],
    ],
)
def test_refuses_bad_depends_on(roots, deps):
    doc = good_doc(roots)
    doc["services"]["qbittorrent"]["depends_on"] = deps
    refused(doc, roots)


def test_refuses_depends_on_on_other_service(roots):
    doc = good_doc(roots)
    doc["services"]["sonarr"]["depends_on"] = {"gluetun": {"condition": "service_healthy"}}
    refused(doc, roots)


# ---------------------------------------------------------------- cap_add / devices / deploy


def test_refuses_cap_add_not_in_catalogue(roots):
    doc = good_doc(roots)
    doc["services"]["sonarr"]["cap_add"] = ["NET_ADMIN"]
    refused(doc, roots)


def test_refuses_extra_cap_on_gluetun(roots):
    doc = good_doc(roots)
    doc["services"]["gluetun"]["cap_add"] = ["NET_ADMIN", "SYS_ADMIN"]
    refused(doc, roots)


@pytest.mark.parametrize(
    "app,devices",
    [
        ("sonarr", ["/dev/dri:/dev/dri"]),
        ("plex", ["/dev/sda:/dev/sda"]),
        ("plex", ["/dev/dri:/dev/dri", "/dev/mem:/dev/mem"]),
        ("gluetun", ["/dev/net/tun:/dev/net/tun", "/dev/kmsg:/dev/kmsg"]),
        ("plex", "/dev/dri:/dev/dri"),
    ],
)
def test_refuses_bad_devices(roots, app, devices):
    doc = good_doc(roots)
    doc["services"][app]["devices"] = devices
    refused(doc, roots)


def test_refuses_gpu_reservation_on_non_media_server(roots):
    doc = good_doc(roots)
    doc["services"]["sonarr"]["deploy"] = copy.deepcopy(doc["services"]["plex"]["deploy"])
    refused(doc, roots)


@pytest.mark.parametrize(
    "deploy",
    [
        {"resources": {"limits": {"cpus": "4"}}},
        {"mode": "global"},
        {"resources": {"reservations": {"devices": [
            {"driver": "nvidia", "capabilities": ["gpu"], "count": "all", "options": {"x": "y"}}
        ]}}},
    ],
)
def test_refuses_other_deploy_shapes(roots, deploy):
    doc = good_doc(roots)
    doc["services"]["plex"]["deploy"] = deploy
    refused(doc, roots)


# ---------------------------------------------------------------- user / group_add


@pytest.mark.parametrize("user", ["root", "1000:wheel", "1000:", ":1000", 1000, "10 00"])
def test_refuses_non_numeric_user(roots, user):
    doc = good_doc(roots)
    doc["services"]["jellyfin"]["user"] = user
    refused(doc, roots)


@pytest.mark.parametrize("groups", [["video"], [105], "105", ["10a"]])
def test_refuses_non_numeric_group_add(roots, groups):
    doc = good_doc(roots)
    doc["services"]["plex"]["group_add"] = groups
    refused(doc, roots)


# ---------------------------------------------------------------- volumes


@pytest.mark.parametrize("src", ["/etc", "/", "relative", "/srv/data/../../etc"])
def test_refuses_mounts_outside_roots(roots, src):
    doc = good_doc(roots)
    doc["services"]["sonarr"]["volumes"][0]["source"] = src
    refused(doc, roots)


def test_refuses_docker_socket_mount(roots):
    doc = good_doc(roots)
    doc["services"]["sonarr"]["volumes"].append(
        {"type": "bind", "source": "/var/run/docker.sock", "target": "/var/run/docker.sock"}
    )
    refused(doc, roots)


def test_refuses_docker_socket_symlinked_inside_root(roots):
    link = roots.data / "sock"
    link.symlink_to("/var/run/docker.sock")
    doc = good_doc(roots)
    doc["services"]["sonarr"]["volumes"].append(
        {"type": "bind", "source": str(link), "target": "/var/run/docker.sock"}
    )
    refused(doc, roots)


@pytest.mark.parametrize(
    "vol",
    [
        {"type": "volume", "source": "orama-run", "target": "/run"},
        {"type": "tmpfs", "target": "/tmp"},
        {"type": "bind", "source": "SRC", "target": "/x", "bind": {"propagation": "shared"}},
        {"type": "bind", "source": "SRC", "target": "/x", "read_only": "yes"},
        {"type": "bind", "source": "SRC"},
        {"type": "bind", "source": "SRC", "target": "relative"},
        "SRC:/data",
    ],
)
def test_refuses_bad_volume_entries(roots, vol):
    if isinstance(vol, dict) and vol.get("source") == "SRC":
        vol["source"] = str(roots.data)
    elif vol == "SRC:/data":
        vol = f"{roots.data}:/data"
    doc = good_doc(roots)
    doc["services"]["sonarr"]["volumes"].append(vol)
    refused(doc, roots)


def test_refuses_read_only_variant_of_pinned_mount(roots):
    doc = good_doc(roots)
    doc["services"]["sonarr"]["volumes"][1]["read_only"] = True
    refused(doc, roots)


def test_refuses_interpolation_escape_in_mount(roots):
    # ${X:-..} is not literally '..' and realpaths inside the root, but compose
    # interpolates it to '../..', escaping to the host.
    payload = "/".join(["${X:-..}"] * 12)
    doc = good_doc(roots)
    doc["services"]["sonarr"]["volumes"].append(
        {"type": "bind", "source": f"{roots.data}/{payload}", "target": "/host"}
    )
    refused(doc, roots)


def test_refuses_dollar_in_environment(roots):
    doc = good_doc(roots)
    doc["services"]["sonarr"]["environment"]["TZ"] = "${HOME}"
    refused(doc, roots)


def test_refuses_dollar_anywhere(roots):
    doc = good_doc(roots)
    doc["networks"] = {"orama": {"driver": "$bridge"}}
    refused(doc, roots)
    doc = good_doc(roots)
    doc["services"]["sonarr"]["image"] += "$"
    refused(doc, roots)


@pytest.mark.parametrize(
    "source",
    ["CFG", "CFG/orama", "CFG/config/radarr", "CFG/app-env", "CFG/app-env/sonarr.env",
     "DATA/media"],
)
def test_refuses_mounts_other_than_pinned(roots, source):
    source = source.replace("CFG", str(roots.config)).replace("DATA", str(roots.data))
    doc = good_doc(roots)
    doc["services"]["sonarr"]["volumes"].append(
        {"type": "bind", "source": source, "target": "/extra"}
    )
    refused(doc, roots)


def test_refuses_pinned_mount_moved(roots):
    doc = good_doc(roots)
    doc["services"]["sonarr"]["volumes"][0]["source"] = str(roots.config / "config" / "radarr")
    refused(doc, roots)
    doc = good_doc(roots)
    doc["services"]["sonarr"]["volumes"][1]["target"] = "/etc"
    refused(doc, roots)


def test_refuses_data_mount_on_app_without_data(roots):
    doc = good_doc(roots)
    doc["services"]["prowlarr"]["volumes"].append(
        {"type": "bind", "source": str(roots.data), "target": "/data"}
    )
    refused(doc, roots)


# ---------------------------------------------------------------- env_file


def test_refuses_env_file_elsewhere(roots):
    doc = good_doc(roots)
    doc["services"]["sonarr"]["env_file"] = [str(app_env_file(roots.config, "radarr"))]
    refused(doc, roots)


def test_refuses_env_file_on_app_without_secrets(roots):
    doc = good_doc(roots)
    doc["services"]["bazarr"]["env_file"] = [str(app_env_file(roots.config, "bazarr"))]
    refused(doc, roots)


def test_refuses_env_file_extra_entry(roots):
    doc = good_doc(roots)
    doc["services"]["sonarr"]["env_file"].append(str(roots.data / "x.env"))
    refused(doc, roots)


def test_env_file_computed_from_agent_roots(roots, tmp_path):
    other_cfg = tmp_path / "othercfg"
    other_cfg.mkdir()
    doc = good_doc(roots)
    doc["services"]["sonarr"]["env_file"] = [str(app_env_file(other_cfg, "sonarr"))]
    refused(doc, roots)


# ---------------------------------------------------------------- environment


def test_refuses_secret_name_in_environment(roots):
    doc = good_doc(roots)
    doc["services"]["sonarr"]["environment"]["SONARR__AUTH__APIKEY"] = "x"
    refused(doc, roots)


def test_refuses_other_apps_secret_name_in_environment(roots):
    doc = good_doc(roots)
    doc["services"]["bazarr"]["environment"]["WIREGUARD_PRIVATE_KEY"] = "x"
    refused(doc, roots)


@pytest.mark.parametrize("env", [{"PUID": 1000}, ["PUID=1000"], {1: "x"}])
def test_refuses_non_string_environment(roots, env):
    doc = good_doc(roots)
    doc["services"]["sonarr"]["environment"] = env
    refused(doc, roots)


@pytest.mark.parametrize(
    "app,key,value",
    [
        ("sonarr", "DOCKER_MODS", "evil/mod:1"),
        ("sonarr", "PUID", "0"),
        ("sonarr", "PGID", "0"),
        ("sonarr", "UMASK", "000"),
        ("qbittorrent", "WEBUI_PORT", "22"),
        ("plex", "PLEX_UID", "0"),
        ("plex", "PLEX_GID", "1001"),
        ("gluetun", "VPN_TYPE", "openvpn"),
        ("gluetun", "VPN_PORT_FORWARDING", "off"),
        ("gluetun", "VPN_SERVICE_PROVIDER", "a b"),
        ("gluetun", "VPN_SERVICE_PROVIDER", ""),
        ("jellyfin", "PUID", "1000"),
        ("sonarr", "TZ", "x" * 65),
        ("sonarr", "TZ", "Europe/Athens;rm"),
        ("sonarr", "WEBUI_PORT", "8090"),
    ],
)
def test_environment_is_pinned(roots, app, key, value):
    doc = good_doc(roots)
    doc["services"][app]["environment"][key] = value
    refused(doc, roots)


@pytest.mark.parametrize(
    "command",
    [
        GLUETUN_PORT_FORWARD_UP_COMMAND + " ",
        GLUETUN_PORT_FORWARD_UP_COMMAND.replace("127.0.0.1:8090", "127.0.0.1:8091"),
        GLUETUN_PORT_FORWARD_UP_COMMAND.replace("wget", "curl"),
        GLUETUN_PORT_FORWARD_UP_COMMAND.replace(" 2>&1'", "; id' 2>&1"),
        GLUETUN_PORT_FORWARD_UP_COMMAND.upper(),
        "/bin/sh -c id",
        "",
    ],
)
def test_refuses_altered_port_forward_command(roots, command):
    doc = good_doc(roots)
    doc["services"]["gluetun"]["environment"]["VPN_PORT_FORWARDING_UP_COMMAND"] = command
    refused(doc, roots)


@pytest.mark.parametrize("value", ["off", "On", "true", "on ", ""])
def test_refuses_httpproxy_value_other_than_on(roots, value):
    doc = good_doc(roots)
    doc["services"]["gluetun"]["environment"]["HTTPPROXY"] = value
    refused(doc, roots)


def gluetun_env(doc):
    return doc["services"]["gluetun"]["environment"]


def test_refuses_httpproxy_without_user(roots):
    doc = good_doc(roots)
    del gluetun_env(doc)["HTTPPROXY_USER"]
    refused_with(doc, roots, "proxy")


def test_refuses_proxy_user_without_httpproxy(roots):
    doc = good_doc(roots)
    del gluetun_env(doc)["HTTPPROXY"]
    refused_with(doc, roots, "proxy")


@pytest.mark.parametrize("user", ["admin", "Orama", "", "orama "])
def test_refuses_other_proxy_user(roots, user):
    doc = good_doc(roots)
    gluetun_env(doc)["HTTPPROXY_USER"] = user
    refused(doc, roots)


def test_refuses_proxy_password_in_compose(roots):
    doc = good_doc(roots)
    gluetun_env(doc)["HTTPPROXY_PASSWORD"] = "x" * 12
    refused_with(doc, roots, "secrets file")


def mullvad_doc(roots):
    return build_apps_compose(setup_for(
        roots, download_methods={"torrent"}, vpn={"provider": "mullvad", "port_forwarding": True},
    ))


def test_refuses_port_forward_only_for_other_provider(roots):
    doc = mullvad_doc(roots)
    validate_compose(doc, roots)
    gluetun_env(doc)["PORT_FORWARD_ONLY"] = "on"
    refused_with(doc, roots, "ProtonVPN")


@pytest.mark.parametrize("key", ["PORT_FORWARD_ONLY", "VPN_PORT_FORWARDING_UP_COMMAND"])
def test_refuses_port_forward_options_without_port_forwarding(roots, key):
    doc = good_doc(roots)
    del gluetun_env(doc)["VPN_PORT_FORWARDING"]
    refused_with(doc, roots, "port forwarding")


@pytest.mark.parametrize("value", ["off", "yes", ""])
def test_refuses_port_forward_only_value_other_than_on(roots, value):
    doc = good_doc(roots)
    doc["services"]["gluetun"]["environment"]["PORT_FORWARD_ONLY"] = value
    refused(doc, roots)


@pytest.mark.parametrize(
    "key", ["VPN_PORT_FORWARDING_UP_COMMAND", "PORT_FORWARD_ONLY", "HTTPPROXY", "HTTPPROXY_USER"]
)
def test_gluetun_options_refused_on_other_apps(roots, key):
    doc = good_doc(roots)
    doc["services"]["sonarr"]["environment"][key] = (
        GLUETUN_PORT_FORWARD_UP_COMMAND if key == "VPN_PORT_FORWARDING_UP_COMMAND" else "on"
    )
    refused(doc, roots)


HOOK = GLUETUN_PORT_FORWARD_UP_COMMAND
DOLLAR = "may not contain '\\$'"


def test_pinned_hook_is_the_one_dollar_accepted(roots):
    doc = good_doc(roots)
    assert "$" in gluetun_env(doc)["VPN_PORT_FORWARDING_UP_COMMAND"]
    validate_compose(doc, roots)


@pytest.mark.parametrize(
    "command",
    [
        HOOK.replace("{{PORTS}}", "${PORTS}"),
        HOOK.replace("$$QBT_WEBUI_PASSWORD", "$QBT_WEBUI_PASSWORD"),
        HOOK.replace("$$QBT_WEBUI_PASSWORD", "${QBT_WEBUI_PASSWORD}"),
        HOOK.replace("$$QBT_WEBUI_PASSWORD", "$$HOME"),
        HOOK + "$$",
        HOOK + " ",
        "$$QBT_WEBUI_PASSWORD",
    ],
)
def test_altered_hook_with_dollar_refused(roots, command):
    doc = good_doc(roots)
    gluetun_env(doc)["VPN_PORT_FORWARDING_UP_COMMAND"] = command
    refused_with(doc, roots, DOLLAR)


def _put(doc, where):
    """Place the pinned hook (or '$$x') somewhere other than its one allowed spot."""
    services = doc["services"]
    if where == "sonarr_same_key":
        services["sonarr"]["environment"]["VPN_PORT_FORWARDING_UP_COMMAND"] = HOOK
    elif where == "gluetun_other_key":
        gluetun_env(doc)["TZ"] = HOOK
    elif where == "gluetun_down_command":
        gluetun_env(doc)["VPN_PORT_FORWARDING_DOWN_COMMAND"] = HOOK
    elif where == "as_key":
        gluetun_env(doc)[HOOK] = "on"
    elif where == "env_as_list":
        services["gluetun"]["environment"] = [f"VPN_PORT_FORWARDING_UP_COMMAND={HOOK}"]
    elif where == "image":
        services["gluetun"]["image"] = HOOK
    elif where == "volume":
        services["sonarr"]["volumes"].append(HOOK)
    elif where == "network":
        doc["networks"]["orama"]["name"] = HOOK
    elif where == "double_dollar_env":
        services["sonarr"]["environment"]["TZ"] = "$$HOME"
    elif where == "double_dollar_gluetun":
        gluetun_env(doc)["HTTPPROXY_USER"] = "$$orama"
    elif where == "top_level_key":
        doc[HOOK] = {}


@pytest.mark.parametrize("where", [
    "sonarr_same_key", "gluetun_other_key", "gluetun_down_command", "as_key", "env_as_list",
    "image", "volume", "network", "double_dollar_env", "double_dollar_gluetun",
    "top_level_key",
])
def test_dollar_refused_everywhere_else(roots, where):
    doc = good_doc(roots)
    _put(doc, where)
    refused_with(doc, roots, DOLLAR)


def test_refuses_missing_environment_key(roots):
    doc = good_doc(roots)
    del doc["services"]["sonarr"]["environment"]["UMASK"]
    refused(doc, roots)


def test_refuses_environment_on_service_without_one(roots):
    doc = build_apps_compose(setup_for(roots))
    doc["services"]["plex"]["environment"]["X"] = "y"
    refused(doc, roots)


def test_accepts_other_timezone_and_provider(roots):
    doc = build_apps_compose(
        setup_for(
            roots,
            download_methods={"torrent"},
            vpn={"provider": "mullvad", "port_forwarding": False},
            timezone="America/Argentina/Buenos_Aires",
        )
    )
    validate_compose(doc, roots)


def test_refuses_ids_not_matching_agent(roots):
    doc = build_apps_compose(setup_for(roots, puid=1001))
    refused(doc, roots)


@pytest.mark.parametrize("user", ["0:0", "1000:0", "1001:1000"])
def test_refuses_user_mismatch(roots, user):
    doc = good_doc(roots)
    doc["services"]["jellyfin"]["user"] = user
    refused(doc, roots)


@pytest.mark.parametrize("groups", [["0"], ["105", "106"], [], ["00"]])
def test_refuses_bad_group_add(roots, groups):
    doc = good_doc(roots)
    doc["services"]["plex"]["group_add"] = groups
    refused(doc, roots)


def test_refusal_message_truncates_untrusted_names(roots):
    doc = good_doc(roots)
    doc["services"]["x" * 500] = {}
    with pytest.raises(Refused) as exc:
        validate_compose(doc, roots)
    assert len(str(exc.value)) < 200


def test_refusal_message_hides_environment_values(roots):
    doc = good_doc(roots)
    doc["services"]["sonarr"]["environment"]["SONARR__AUTH__APIKEY"] = "s3cr3tvalue"
    with pytest.raises(Refused) as exc:
        validate_compose(doc, roots)
    assert "s3cr3tvalue" not in str(exc.value)


# ---------------------------------------------------------------- ports


@pytest.mark.parametrize(
    "ports",
    [
        ["0.0.0.0:8989:8989"],
        ["8989:22"],
        ["0:8989"],
        ["70000:8989"],
        ["8989:8989/udp"],
        [8989],
        "8989:8989",
        ["8989"],
        ["8989:8989", "2222:8989"],
    ],
)
def test_refuses_bad_ports(roots, ports):
    doc = good_doc(roots)
    doc["services"]["sonarr"]["ports"] = ports
    refused(doc, roots)


def test_refuses_ports_on_portless_service(roots):
    doc = good_doc(roots)
    doc["services"]["gluetun"]["ports"] = ["2222:22"]
    refused(doc, roots)


def test_gluetun_may_publish_qbittorrent_port(roots):
    doc = good_doc(roots)
    assert doc["services"]["gluetun"]["ports"] == [f"{PORTS['qbittorrent']}:8090"]
    validate_compose(doc, roots)


def test_refuses_ports_on_qbittorrent_behind_vpn(roots):
    doc = good_doc(roots)
    doc["services"]["qbittorrent"]["ports"] = ["8090:8090"]
    refused(doc, roots)


# ---------------------------------------------------------------- env files


def test_env_files_accepts_builder_shape(roots):
    validate_env_files(
        {
            "sonarr": {"SONARR__AUTH__APIKEY": "abc123"},
            "gluetun": {"WIREGUARD_PRIVATE_KEY": "k+/="},
        },
        roots,
    )


def test_env_files_accept_gluetun_proxy_password(roots):
    validate_env_files(
        {"gluetun": {"WIREGUARD_PRIVATE_KEY": "k+/=", "HTTPPROXY_PASSWORD": "a-b_c"}}, roots
    )
    with pytest.raises(Refused):
        validate_env_files({"sonarr": {"HTTPPROXY_PASSWORD": "a-b_c"}}, roots)


@pytest.mark.parametrize(
    "gluetun_env_file",
    [None, {"WIREGUARD_PRIVATE_KEY": "k"}, {"WIREGUARD_PRIVATE_KEY": "k", "HTTPPROXY_PASSWORD": ""}],
)
def test_proxy_user_requires_a_proxy_password(roots, gluetun_env_file):
    env_files = {} if gluetun_env_file is None else {"gluetun": gluetun_env_file}
    with pytest.raises(Refused, match="proxy"):
        validate_secrets_present(good_doc(roots), env_files)


def test_env_files_accept_qbittorrent_password_for_gluetun_only(roots):
    validate_env_files(
        {"gluetun": {"WIREGUARD_PRIVATE_KEY": "k+/=", "QBT_WEBUI_PASSWORD": "a-b_c"}}, roots
    )
    for app, name in [("qbittorrent", "QBT_WEBUI_PASSWORD"), ("sonarr", "QBT_WEBUI_PASSWORD")]:
        with pytest.raises(Refused):
            validate_env_files({app: {name: "a-b_c"}}, roots)


def test_qbittorrent_password_refused_in_compose(roots):
    doc = good_doc(roots)
    gluetun_env(doc)["QBT_WEBUI_PASSWORD"] = "x" * 12
    refused_with(doc, roots, "secrets file")


@pytest.mark.parametrize("password", [None, ""])
def test_port_forward_hook_requires_qbittorrent_password(roots, password):
    gluetun = {"WIREGUARD_PRIVATE_KEY": "k", "HTTPPROXY_PASSWORD": "p"}
    if password is not None:
        gluetun["QBT_WEBUI_PASSWORD"] = password
    with pytest.raises(Refused, match="qBittorrent"):
        validate_secrets_present(good_doc(roots), {"gluetun": gluetun})


def test_proxy_password_present_is_accepted(roots):
    env_files = {"gluetun": {"WIREGUARD_PRIVATE_KEY": "k", "HTTPPROXY_PASSWORD": "p",
                             "QBT_WEBUI_PASSWORD": "q"}}
    validate_secrets_present(good_doc(roots), env_files)
    validate_secrets_present(build_apps_compose(setup_for(roots)), {})  # no proxy, no need


def test_env_files_only_allowed_names(roots):
    with pytest.raises(Refused):
        validate_env_files({"sonarr": {"LD_PRELOAD": "/x.so"}}, roots)
    with pytest.raises(Refused):
        validate_env_files({"sonarr": {"RADARR__AUTH__APIKEY": "x"}}, roots)


@pytest.mark.parametrize("app", ["bazarr", "evil", "orama-agent", "../sonarr"])
def test_env_files_refuse_apps_without_secrets(roots, app):
    with pytest.raises(Refused):
        validate_env_files({app: {"SONARR__AUTH__APIKEY": "x"}}, roots)


@pytest.mark.parametrize("value", ["", "a'b", "a\nb", "a\rb", "a\0b", 5, None])
def test_env_files_refuse_bad_values(roots, value):
    with pytest.raises(Refused) as exc:
        validate_env_files({"sonarr": {"SONARR__AUTH__APIKEY": value}}, roots)
    if isinstance(value, str) and value:
        assert value not in str(exc.value)


def _marker() -> str:
    # Built at runtime: no key-shaped literal in the repo.
    return os.urandom(6).hex()


# One of each character class Compose's env-file parser could read as something other
# than plain text: escape, interpolation, comment, separator, quotes, line breaks, and
# anything outside ASCII.
FORBIDDEN_CLASSES = ["\\", "$", "#", " ", '"', "'", "`", "\n", "\r", "\t", "\0", "é", "Ω", "\u00a0"]


@pytest.mark.parametrize("bad", FORBIDDEN_CLASSES)
@pytest.mark.parametrize("where", ["start", "middle", "end"])
def test_env_files_refuse_every_unsafe_character(roots, bad, where):
    marker = _marker()
    value = {"start": bad + marker, "middle": marker + bad + marker, "end": marker + bad}[where]
    with pytest.raises(Refused) as exc:
        validate_env_files({"gluetun": {"WIREGUARD_PRIVATE_KEY": value}}, roots)
    assert marker not in str(exc.value)
    assert "letters, digits" in str(exc.value)


def test_env_files_refuse_the_line_swallowing_exploit(roots):
    # The scan's shape: a value ending in a backslash makes Compose read the closing quote
    # as escaped, so the next value would become a new, unpinned setting for Gluetun.
    hook_name = "VPN_PORT_FORWARDING_UP_COMMAND"
    marker = _marker()
    payload = f"{hook_name}=/bin/sh -c {marker} #"
    with pytest.raises(Refused) as exc:
        validate_env_files(
            {"gluetun": {"WIREGUARD_PRIVATE_KEY": marker + "\\", "HTTPPROXY_PASSWORD": payload}},
            roots,
        )
    assert marker not in str(exc.value)
    assert hook_name not in str(exc.value)


def test_env_files_refuse_values_over_512_characters(roots):
    validate_env_files({"sonarr": {"SONARR__AUTH__APIKEY": "a" * 512}}, roots)
    with pytest.raises(Refused) as exc:
        validate_env_files({"sonarr": {"SONARR__AUTH__APIKEY": "a" * 513}}, roots)
    assert "a" * 20 not in str(exc.value)


def test_env_files_accept_real_generated_values(roots):
    import base64
    import secrets

    for _ in range(200):
        validate_env_files(
            {
                "gluetun": {
                    "WIREGUARD_PRIVATE_KEY": base64.b64encode(os.urandom(32)).decode(),
                    "HTTPPROXY_PASSWORD": secrets.token_urlsafe(24),
                    "QBT_WEBUI_PASSWORD": secrets.token_urlsafe(24),
                },
                "sonarr": {"SONARR__AUTH__APIKEY": secrets.token_hex(16)},
            },
            roots,
        )


@pytest.mark.parametrize("env_files", [[], {"sonarr": ["x"]}, {"sonarr": None}])
def test_env_files_refuse_bad_shapes(roots, env_files):
    with pytest.raises(Refused):
        validate_env_files(env_files, roots)


# ---------------------------------------------------------------- nesting depth


def test_refuses_deeply_nested_doc(roots):
    doc = good_doc(roots)
    deep: list = []
    node = deep
    for _ in range(5000):
        child: list = []
        node.append(child)
        node = child
    doc["services"]["sonarr"]["environment"]["TZ"] = deep
    refused_with(doc, roots, "nested too deeply")


# ---------------------------------------------------------------- host paths


def ensure_folders(roots, setup):
    for app in enabled_apps(setup):
        app_config_dir(roots.config, app).mkdir(parents=True, exist_ok=True)
    app_env_dir(roots.config).mkdir(exist_ok=True)


def prepared(roots):
    setup = full_setup(roots)
    ensure_folders(roots, setup)
    doc = build_apps_compose(setup)
    validate_compose(doc, roots)
    return doc


def test_host_paths_accept_real_dirs(roots):
    doc = prepared(roots)
    check_host_paths(doc, roots)
    app_env_file(roots.config, "sonarr").write_text("SONARR__AUTH__APIKEY='x'\n")
    check_host_paths(doc, roots)


def test_host_paths_refuse_symlinked_app_config_dir(roots):
    doc = prepared(roots)
    target = roots.config / "config" / "sonarr"
    target.rmdir()
    target.symlink_to("/etc")
    with pytest.raises(Refused, match="is a link"):
        check_host_paths(doc, roots)


def test_host_paths_refuse_symlinked_config_dir(roots, tmp_path):
    doc = prepared(roots)
    real = tmp_path / "elsewhere"
    (roots.config / "config").rename(real)
    (roots.config / "config").symlink_to(real)
    with pytest.raises(Refused, match="is a link"):
        check_host_paths(doc, roots)


def test_host_paths_refuse_symlinked_env_file(roots):
    doc = prepared(roots)
    app_env_file(roots.config, "sonarr").symlink_to("/etc/passwd")
    with pytest.raises(Refused, match="secrets file for sonarr"):
        check_host_paths(doc, roots)


def test_host_paths_refuse_env_file_that_is_a_dir(roots):
    doc = prepared(roots)
    app_env_file(roots.config, "sonarr").mkdir()
    with pytest.raises(Refused, match="secrets file for sonarr"):
        check_host_paths(doc, roots)


def test_host_paths_refuse_missing_config_dir(roots):
    doc = prepared(roots)
    (roots.config / "config" / "radarr").rmdir()
    with pytest.raises(Refused, match="does not exist yet"):
        check_host_paths(doc, roots)


def test_host_paths_refuse_config_component_that_is_a_file(roots):
    doc = prepared(roots)
    target = roots.config / "config" / "bazarr"
    target.rmdir()
    target.write_text("")
    with pytest.raises(Refused, match="is not a folder"):
        check_host_paths(doc, roots)


def test_host_paths_refuse_swapped_root(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "realcfg").mkdir()
    (tmp_path / "cfg").symlink_to(tmp_path / "realcfg")
    roots = Roots(tmp_path / "data", tmp_path / "cfg", 1000, 1000)
    setup = full_setup(roots)
    ensure_folders(roots, setup)
    with pytest.raises(Refused, match="is a link"):
        check_host_paths(build_apps_compose(setup), roots)


def test_validate_compose_refuses_existing_symlinked_config_dir(roots):
    # The reviewer's PoC: no folders created yet except a symlinked sonarr config dir.
    (roots.config / "config").mkdir()
    (roots.config / "config" / "sonarr").symlink_to("/etc")
    refused_with(good_doc(roots), roots, "is a link")


def test_validate_compose_refuses_existing_symlinked_env_file(roots):
    app_env_dir(roots.config).mkdir()
    app_env_file(roots.config, "sonarr").symlink_to("/etc/passwd")
    refused_with(good_doc(roots), roots, "secrets file for sonarr")


def test_validate_compose_tolerates_missing_folders(roots):
    validate_compose(good_doc(roots), roots)  # nothing under roots.config exists yet


def test_env_file_pin_is_agent_only_dir(roots):
    doc = good_doc(roots)
    assert doc["services"]["sonarr"]["env_file"] == [str(roots.config / "app-env" / "sonarr.env")]
    validate_compose(doc, roots)
    doc["services"]["sonarr"]["env_file"] = [str(roots.config / "config/sonarr/orama.env")]
    refused_with(doc, roots, "its own secrets file")


def test_host_paths_refuse_symlinked_app_env_dir(roots, tmp_path):
    doc = prepared(roots)
    real = tmp_path / "elsewhere-env"
    app_env_dir(roots.config).rename(real)
    app_env_dir(roots.config).symlink_to(real)
    with pytest.raises(Refused, match="is a link"):
        check_host_paths(doc, roots)
    refused_with(doc, roots, "is a link")


def test_host_paths_refuse_missing_app_env_dir(roots):
    doc = prepared(roots)
    app_env_dir(roots.config).rmdir()
    with pytest.raises(Refused, match="does not exist yet"):
        check_host_paths(doc, roots)


def test_no_app_mount_reaches_app_env_dir(roots):
    env_dir = app_env_dir(roots.config)
    for svc in good_doc(roots)["services"].values():
        for v in svc.get("volumes", []):
            src = type(env_dir)(v["source"])
            assert not src.is_relative_to(env_dir) and not env_dir.is_relative_to(src)
