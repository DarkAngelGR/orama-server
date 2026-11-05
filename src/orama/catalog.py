"""Service catalogue: pinned images, ports, mounts and env conventions.

Image tags checked on 2026-11-03 against the registries (GHCR tag lists for
LinuxServer images, Docker Hub for the rest). Every tag is an exact version;
"latest" is forbidden. LinuxServer images are referenced as ghcr.io/linuxserver/<app>, the
registry whose tags were actually verified (lscr.io is an alias of it).
"""

from __future__ import annotations

import re
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
# - The password never appears in any command line (readable by every host user through
#   /proc): the shell's builtin printf writes the login form to a temp file created 0600
#   (umask 077), wget sends it with --post-file, and the file is removed whether or not
#   the login worked.
# - '$$' is Compose's escape for a literal '$'. This exact string is the only '$' the
#   Agent accepts anywhere in a compose document, and every '$' in it is escaped.
# - qBittorrent accepts a login with no Origin/Referer (not cross-site); the password is
#   token_urlsafe, so it needs no form encoding.
# Shared by the compose builder and the Agent's pin so the two cannot drift apart.
GLUETUN_PORT_FORWARD_UP_COMMAND = (
    r"""/bin/sh -c 'umask 077; """
    r"""printf "username=orama&password=%s" "$$QBT_WEBUI_PASSWORD" > /tmp/qbt.login && """
    r"""wget -qO- --retry-connrefused --save-cookies /tmp/qbt.cookies --keep-session-cookies """
    r"""--post-file=/tmp/qbt.login http://127.0.0.1:8090/api/v2/auth/login; """
    r"""rc=$$?; rm -f /tmp/qbt.login; """
    r"""[ $$rc -eq 0 ] && wget -qO- --load-cookies /tmp/qbt.cookies """
    r"""--post-data "json={\"listen_port\":{{PORTS}}}" """
    r"""http://127.0.0.1:8090/api/v2/app/setPreferences 2>&1'"""
)

# Every value in an app's secrets (env) file must match this, checked by the Agent and by
# `set-secret` for values bound for an env file. Orama's generated secrets (token_hex,
# token_urlsafe) and WireGuard keys (standard base64) fit. Nothing Compose's env-file
# parser treats specially can: no quotes, backslash, '$', '#', spaces or line breaks.
ENV_FILE_VALUE = re.compile(r"[A-Za-z0-9_+/=-]{1,512}")

# User name for Gluetun's HTTP proxy (Prowlarr via the VPN). Its password is a secret in
# Gluetun's env file. Without auth any container on orama-net could use the proxy to reach
# qBittorrent's WebUI from "localhost" (qBittorrent still asks for its password there).
GLUETUN_PROXY_USER = "orama"


@dataclass(frozen=True)
class VpnProvider:
    """A VPN service Orama offers, with WireGuard, through Gluetun."""

    name: str  # Gluetun's VPN_SERVICE_PROVIDER value
    label: str  # as the service spells its own name
    port_forwarding: bool  # Gluetun forwards a port for it (and keeps to servers that can)
    needs_address: bool  # Gluetun needs WIREGUARD_ADDRESSES besides the private key


# Checked against Gluetun v3.41.3's source (internal/configuration/settings):
# - wireguard.go: all five take WireGuard; every one needs an interface address, and
#   Gluetun fills one in only for nordvpn and protonvpn, so the others need the address
#   from the provider's WireGuard configuration file.
# - portforward.go / serverselection.go: of these five, only protonvpn can forward a port,
#   and only it (with "private internet access") takes PORT_FORWARD_ONLY.
# Shared by the setup model, the compose builder and the Agent's pins.
VPN_PROVIDERS: dict[str, VpnProvider] = {
    p.name: p
    for p in (
        VpnProvider("protonvpn", "Proton VPN", port_forwarding=True, needs_address=False),
        VpnProvider("nordvpn", "NordVPN", port_forwarding=False, needs_address=False),
        VpnProvider("mullvad", "Mullvad", port_forwarding=False, needs_address=True),
        VpnProvider("ivpn", "IVPN", port_forwarding=False, needs_address=True),
        VpnProvider("surfshark", "Surfshark", port_forwarding=False, needs_address=True),
    )
}

# Gluetun's control server (its HTTP API) listens on every interface by default, so any
# app on orama-net could reach it. Bound to localhost it is reachable only inside Gluetun's
# own network namespace (Gluetun and qBittorrent), whatever its auth defaults are.
GLUETUN_CONTROL_SERVER_ADDRESS = "127.0.0.1:8000"

# UGOS Pro web UI ports (HTTP / HTTPS) plus common system ports.
# verify on device
UGOS_RESERVED_PORTS: frozenset[int] = frozenset({22, 80, 443, 9999, 9443})
