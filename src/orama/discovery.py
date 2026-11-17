"""Discovery and adoption of existing apps and containers running on the host."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from orama.catalog import CATALOG

# Image patterns matching known distributions of each app
APP_IMAGE_PATTERNS: dict[str, tuple[str, ...]] = {
    "plex": ("plexinc/pms-docker", "linuxserver/plex", "binhex/arch-plex"),
    "jellyfin": ("jellyfin/jellyfin", "linuxserver/jellyfin"),
    "sonarr": ("linuxserver/sonarr", "binhex/arch-sonarr", "hotio/sonarr"),
    "radarr": ("linuxserver/radarr", "binhex/arch-radarr", "hotio/radarr"),
    "prowlarr": ("linuxserver/prowlarr", "hotio/prowlarr"),
    "bazarr": ("linuxserver/bazarr", "hotio/bazarr"),
    "sabnzbd": ("linuxserver/sabnzbd", "binhex/arch-sabnzbd"),
    "qbittorrent": (
        "linuxserver/qbittorrent",
        "binhex/arch-qbittorrent",
        "markusmcnubs/qbittorrent",
    ),
}

DEFAULT_PORTS: dict[str, int] = {
    name: spec.default_host_port
    for name, spec in CATALOG.items()
    if spec.default_host_port
}

PORT_TO_APP: dict[int, str] = {
    port: name for name, port in DEFAULT_PORTS.items()
}


@dataclass(frozen=True)
class DiscoveredApp:
    name: str
    container_name: str | None = None
    image: str | None = None
    host_port: int | None = None
    source: str = "docker"  # "docker" or "port"
    api_key: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "container_name": self.container_name,
            "image": self.image,
            "host_port": self.host_port,
            "source": self.source,
            "api_key": self.api_key,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DiscoveredApp:
        return cls(
            name=data["name"],
            container_name=data.get("container_name"),
            image=data.get("image"),
            host_port=data.get("host_port"),
            source=data.get("source", "docker"),
            api_key=data.get("api_key"),
        )


def is_orama_container(name: str) -> bool:
    """True if the container is managed by Orama itself."""
    clean = name.lstrip("/")
    return clean.startswith("orama-") or clean in ("orama", "orama-server", "orama-server-agent")


def identify_app(
    container_name: str,
    image: str = "",
    published_ports: list[int] | None = None,
) -> str | None:
    """Match a container to a known catalog app by image, name, or default port."""
    if is_orama_container(container_name):
        return None

    clean_name = container_name.lstrip("/").lower()
    clean_image = image.lower()
    ports = published_ports or []

    # 1. Match by image repository
    for app, patterns in APP_IMAGE_PATTERNS.items():
        if any(pat in clean_image for pat in patterns):
            return app

    # 2. Match by container name (whole word or prefix/suffix)
    for app in CATALOG:
        # e.g. "sonarr", "my-sonarr", "sonarr_app"
        pattern = rf"(^|[^a-z0-9]){re.escape(app)}([^a-z0-9]|$)"
        if re.search(pattern, clean_name):
            return app

    # 3. Match by published port matching default port
    for port in ports:
        if port in PORT_TO_APP:
            return PORT_TO_APP[port]

    return None


def extract_host_port(ports_str: str, target_container_port: int | None = None) -> int | None:
    """Parse docker ports string (e.g., '0.0.0.0:8989->8989/tcp') to extract the host port."""
    if not ports_str:
        return None

    # Matches host_port->container_port or :::host_port->container_port
    matches = re.findall(r"(?:0\.0\.0\.0|\[::\]|127\.0\.0\.1):(\d+)->(\d+)/tcp", ports_str)
    if matches:
        if target_container_port is not None:
            for host_p, cont_p in matches:
                if int(cont_p) == target_container_port:
                    return int(host_p)
        return int(matches[0][0])
    return None


def extract_api_key_from_xml(xml_content: str) -> str | None:
    """Extract ApiKey from an Arr or Bazarr config.xml file."""
    match = re.search(r"<ApiKey>\s*([A-Za-z0-9_-]{1,128})\s*</ApiKey>", xml_content, re.IGNORECASE)
    if match:
        return match.group(1)
    return None
