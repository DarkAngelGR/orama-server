"""Service catalogue: pinned images, ports, mounts and env conventions.

Image tags checked on 2026-11-03 against the registries (GHCR tag lists for
LinuxServer images, Docker Hub for the rest). Every tag is an exact version;
"latest" is forbidden. LinuxServer images are referenced as ghcr.io/linuxserver/<app>, the
registry whose tags were actually verified (lscr.io is an alias of it).
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Literal

EnvStyle = Literal["linuxserver", "plex", "jellyfin", "gluetun", "none"]


@dataclass(frozen=True)
class ServiceSpec:
    name: str
    image: str  # repo:tag
    container_port: int  # 0 = no port of its own
    default_host_port: int  # 0 = no host port of its own
    config_mount: str  # path inside the container ("" = none)
    mounts_data: bool
    env_style: EnvStyle
    secret_env: tuple[str, ...]  # env var names allowed in its env file
    cap_add: tuple[str, ...] = ()
    devices: tuple[str, ...] = ()


def orama_version() -> str:
    """Installed package version, falling back to the pyproject version."""
    try:
        return metadata.version("orama")
    except metadata.PackageNotFoundError:
        pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
        return tomllib.loads(pyproject.read_text())["project"]["version"]


def _lsio(name: str, tag: str, port: int, *, mounts_data: bool = True,
          secret: tuple[str, ...] = ()) -> ServiceSpec:
    return ServiceSpec(
        name=name, image=f"ghcr.io/linuxserver/{name}:{tag}",
        container_port=port, default_host_port=port, config_mount="/config",
        mounts_data=mounts_data, env_style="linuxserver", secret_env=secret,
    )


CATALOG: dict[str, ServiceSpec] = {
    s.name: s
    for s in (
        ServiceSpec("plex", "plexinc/pms-docker:1.43.5.11079-b3b13e506", 32400, 32400,
                    "/config", True, "plex", ()),
        ServiceSpec("jellyfin", "jellyfin/jellyfin:12.4.20261026-211555", 8096, 8096,
                    "/config", True, "jellyfin", ()),
        _lsio("sonarr", "4.0.20.3014-ls329", 8989, secret=("SONARR__AUTH__APIKEY",)),
        _lsio("radarr", "6.4.4.10685-ls325", 7878, secret=("RADARR__AUTH__APIKEY",)),
        _lsio("prowlarr", "2.7.0.5723-ls166", 9696, mounts_data=False,
              secret=("PROWLARR__AUTH__APIKEY",)),
        _lsio("bazarr", "v1.6.2-ls370", 6767),
        _lsio("sabnzbd", "5.1.3-ls279", 8080),
        # No host port of its own; with VPN on it publishes qbittorrent's port.
        ServiceSpec("gluetun", "qmcgaw/gluetun:v3.41.3", 0, 0, "/gluetun", False,
                    "gluetun",
                    ("WIREGUARD_PRIVATE_KEY", "HTTPPROXY_PASSWORD", "QBT_WEBUI_PASSWORD"),
                    cap_add=("NET_ADMIN",), devices=("/dev/net/tun",)),
        _lsio("qbittorrent", "5.3.0_v2.1.2-ls486", 8090),
        ServiceSpec("recyclarr", "recyclarr/recyclarr:8.8.1", 0, 0, "/config", False,
                    "none", ()),
    )
}

_VERSION = orama_version()

CORE: dict[str, ServiceSpec] = {
    s.name: s
    for s in (
        ServiceSpec("orama", f"ghcr.io/darkangelgr/orama:{_VERSION}", 8765, 8765,
                    "/config", False, "none", ()),
        ServiceSpec("orama-agent", f"ghcr.io/darkangelgr/orama-agent:{_VERSION}", 0, 0,
                    "", False, "none", ()),
    )
}

# qBittorrent's WebUI user (orama-seeded); its password is the secret password:qbittorrent.
QBITTORRENT_WEBUI_USER = "orama"

# Gluetun runs this whenever the VPN forwards a new port, telling qBittorrent (in Gluetun's
# network, WebUI port 8090) to listen on it. qBittorrent keeps localhost auth on, so the
# hook logs in first and keeps the session cookie in Gluetun's /tmp.
# - Gluetun v3.41.3 substitutes {{PORTS}}, splits the line itself (quotes, no expansion)
#   and runs it with its own environment, which holds QBT_WEBUI_PASSWORD from its env
#   file; the shell expands it. The password is never in compose.yaml.
# - '$$' is Compose's escape for a literal '$'. This exact string is the only '$' the
#   Agent accepts anywhere in a compose document.
# - qBittorrent accepts a login with no Origin/Referer (not cross-site); the password is
#   token_urlsafe, so it needs no form encoding.
# Shared by the compose builder and the Agent's pin so the two cannot drift apart.
GLUETUN_PORT_FORWARD_UP_COMMAND = (
    r"""/bin/sh -c 'wget -qO- --retry-connrefused --save-cookies /tmp/qbt.cookies """
    r"""--keep-session-cookies --post-data "username=orama&password=$$QBT_WEBUI_PASSWORD" """
    r"""http://127.0.0.1:8090/api/v2/auth/login && wget -qO- --load-cookies /tmp/qbt.cookies """
    r"""--post-data "json={\"listen_port\":{{PORTS}}}" """
    r"""http://127.0.0.1:8090/api/v2/app/setPreferences 2>&1'"""
)

# User name for Gluetun's HTTP proxy (Prowlarr via the VPN). Its password is a secret in
# Gluetun's env file. Without auth any container on orama-net could use the proxy to reach
# qBittorrent's WebUI from "localhost" (qBittorrent still asks for its password there).
GLUETUN_PROXY_USER = "orama"

# UGOS Pro web UI ports (HTTP / HTTPS) plus common system ports.
# verify on device
UGOS_RESERVED_PORTS: frozenset[int] = frozenset({22, 80, 443, 9999, 9443})
