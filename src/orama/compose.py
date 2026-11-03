"""Compose document builders. Output is plain data dumped with yaml.safe_dump."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any

import yaml

from orama.catalog import CATALOG, CORE, ServiceSpec
from orama.layout import app_config_dir, app_env_file
from orama.setup_model import DesiredSetup, enabled_apps

NETWORK = "orama"
QBITTORRENT_WEBUI_PORT = 8090


def _bind(source: object, target: str) -> dict[str, str]:
    return {"type": "bind", "source": str(source), "target": target}


def _environment(spec: ServiceSpec, setup: DesiredSetup) -> dict[str, str]:
    puid, pgid, tz = str(setup.puid), str(setup.pgid), setup.timezone
    if spec.env_style == "linuxserver":
        env = {"PUID": puid, "PGID": pgid, "UMASK": "002", "TZ": tz}
        if spec.name == "qbittorrent":
            env["WEBUI_PORT"] = str(QBITTORRENT_WEBUI_PORT)
        return env
    if spec.env_style == "plex":
        return {"PLEX_UID": puid, "PLEX_GID": pgid, "TZ": tz}
    if spec.env_style == "jellyfin":
        return {"TZ": tz}
    if spec.env_style == "gluetun":
        assert setup.vpn is not None
        env = {
            "VPN_SERVICE_PROVIDER": setup.vpn.provider,
            "VPN_TYPE": "wireguard",
            "TZ": tz,
        }
        if setup.vpn.port_forwarding:
            env["VPN_PORT_FORWARDING"] = "on"
        return env
    return {}


def _host_port(spec: ServiceSpec, setup: DesiredSetup) -> int | None:
    if not spec.container_port:
        return None
    port = setup.ports.get(spec.name)
    if port is None:
        raise ValueError(f"no host port assigned for {spec.name}")
    return port


def _service(spec: ServiceSpec, setup: DesiredSetup, vpn_on: bool) -> dict[str, Any]:
    svc: dict[str, Any] = {
        "image": spec.image,
        "container_name": f"orama-{spec.name}",
        "restart": "unless-stopped",
    }
    environment = _environment(spec, setup)
    if environment:
        svc["environment"] = environment
    if spec.env_style == "jellyfin":
        svc["user"] = f"{setup.puid}:{setup.pgid}"

    volumes = []
    if spec.config_mount:
        volumes.append(_bind(app_config_dir(setup.config_root, spec.name), spec.config_mount))
    if spec.mounts_data:
        volumes.append(_bind(setup.data_root, "/data"))
    if volumes:
        svc["volumes"] = volumes
    if spec.secret_env:
        svc["env_file"] = [str(app_env_file(setup.config_root, spec.name))]

    if spec.name in ("plex", "jellyfin"):
        if setup.render_gid is not None:
            svc["devices"] = ["/dev/dri:/dev/dri"]
            svc["group_add"] = [str(setup.render_gid)]
        if setup.nvidia:
            svc["deploy"] = {
                "resources": {
                    "reservations": {"devices": [{"driver": "nvidia", "capabilities": ["gpu"]}]}
                }
            }
    if spec.cap_add:
        svc["cap_add"] = list(spec.cap_add)
    if spec.devices:
        svc.setdefault("devices", []).extend(f"{d}:{d}" for d in spec.devices)

    if spec.name == "qbittorrent" and vpn_on:
        svc["network_mode"] = "service:gluetun"
        svc["depends_on"] = {"gluetun": {"condition": "service_healthy"}}
        return svc

    svc["networks"] = [NETWORK]
    if spec.name == "gluetun":
        # Gluetun owns qBittorrent's network, so it publishes qBittorrent's port.
        svc["ports"] = [f"{_host_port(CATALOG['qbittorrent'], setup)}:{QBITTORRENT_WEBUI_PORT}"]
    else:
        host_port = _host_port(spec, setup)
        if host_port is not None:
            svc["ports"] = [f"{host_port}:{spec.container_port}"]
    return svc


def build_apps_compose(setup: DesiredSetup) -> dict[str, Any]:
    vpn_on = setup.vpn is not None
    services = {
        app: _service(CATALOG[app], setup, vpn_on) for app in enabled_apps(setup)
    }
    return {
        "name": "orama-apps",
        "services": services,
        "networks": {NETWORK: {"driver": "bridge"}},
    }


def build_core_compose(
    config_root: PurePosixPath,
    data_root: PurePosixPath,
    puid: int,
    pgid: int,
    docker_gid: int,
    orama_port: int,
    orama_image: str | None = None,
    agent_image: str | None = None,
) -> dict[str, Any]:
    run_volume = {"type": "volume", "source": "orama-run", "target": "/run/orama"}
    orama_spec, agent_spec = CORE["orama"], CORE["orama-agent"]
    return {
        "name": "orama-core",
        "services": {
            "orama": {
                "image": orama_image or orama_spec.image,
                "container_name": "orama-orama",
                "restart": "unless-stopped",
                "user": f"{puid}:{pgid}",
                "ports": [f"{orama_port}:{orama_spec.container_port}"],
                "volumes": [_bind(config_root, str(config_root)), dict(run_volume)],
            },
            "orama-agent": {
                "image": agent_image or agent_spec.image,
                "container_name": "orama-orama-agent",
                "restart": "unless-stopped",
                "network_mode": "none",
                "group_add": [str(docker_gid)],
                "environment": {
                    "ORAMA_DATA_ROOT": str(data_root),
                    "ORAMA_CONFIG_ROOT": str(config_root),
                    "ORAMA_PUID": str(puid),
                    "ORAMA_PGID": str(pgid),
                },
                "volumes": [
                    _bind("/var/run/docker.sock", "/var/run/docker.sock"),
                    dict(run_volume),
                    _bind(config_root, str(config_root)),
                    _bind(data_root, str(data_root)),
                ],
            },
        },
        "volumes": {"orama-run": {}},
    }


def dump_yaml(doc: dict[str, Any]) -> str:
    return yaml.safe_dump(doc, sort_keys=False, allow_unicode=True)
