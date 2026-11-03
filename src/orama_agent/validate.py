"""Validation of everything Orama sends to the Agent: the security boundary.

The Agent is the only component with the Docker socket. It accepts an apps compose
document and per-app env files only if they have exactly the shape that
``orama.compose.build_apps_compose`` emits, checked against the catalogue baked into the
Agent's own image and the roots read from the Agent's own environment. Nothing sent over
the socket is trusted to describe what is allowed.

Refusal messages are plain language and never contain environment or secret values.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Self

from orama.catalog import CATALOG, ServiceSpec
from orama.layout import app_env_file

PROJECT_NAME = "orama-apps"
NETWORK = "orama"
NETWORKS = {NETWORK: {"driver": "bridge"}}
RESTART = "unless-stopped"
DOCKER_SOCKET = "/var/run/docker.sock"
MEDIA_SERVERS = frozenset({"plex", "jellyfin"})
RENDER_DEVICE = "/dev/dri:/dev/dri"
GPU_DEPLOY = {
    "resources": {"reservations": {"devices": [{"driver": "nvidia", "capabilities": ["gpu"]}]}}
}
VPN_SERVICE = "gluetun"
VPN_CLIENT = "qbittorrent"
VPN_NETWORK_MODE = f"service:{VPN_SERVICE}"
VPN_DEPENDS_ON = {VPN_SERVICE: {"condition": "service_healthy"}}

TOP_LEVEL_KEYS = frozenset({"name", "services", "networks"})
SERVICE_KEYS = frozenset({
    "image", "container_name", "restart", "volumes", "env_file", "environment", "ports",
    "networks", "network_mode", "depends_on", "devices", "group_add", "user", "cap_add",
    "deploy",
})
DANGEROUS_KEYS = frozenset({
    "privileged", "pid", "ipc", "security_opt", "userns_mode", "volumes_from",
})
VOLUME_KEYS = frozenset({"type", "source", "target", "read_only"})
FORBIDDEN_ENV_CHARS = ("'", "\n", "\r", "\0")
SECRET_ENV_NAMES = frozenset(name for spec in CATALOG.values() for name in spec.secret_env)

_DIGITS = re.compile(r"[0-9]+")
_USER = re.compile(r"[0-9]+(:[0-9]+)?")
_PORT = re.compile(r"([0-9]{1,5}):([0-9]{1,5})")


class Refused(Exception):
    """The Agent will not act on the request. The message is safe to show and log."""


@dataclass(frozen=True)
class Roots:
    data: Path
    config: Path

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Self:
        """Read the roots from the Agent's own environment, realpath-resolved."""
        env = os.environ if env is None else env
        data = _root_from(env, "ORAMA_DATA_ROOT")
        config = _root_from(env, "ORAMA_CONFIG_ROOT")
        if data == config or _inside(data, config) or _inside(config, data):
            raise Refused("The data folder and the config folder must be separate folders.")
        return cls(data=data, config=config)


def _root_from(env: Any, key: str) -> Path:
    value = env.get(key)
    if not value:
        raise Refused(f"The Agent was started without {key}.")
    if "\0" in value or not value.startswith("/") or ".." in PurePosixPath(value).parts:
        raise Refused(f"{key} must be an absolute folder path without '..'.")
    real = Path(os.path.realpath(value))
    if real == Path("/"):
        raise Refused(f"{key} must not be the whole disk ('/').")
    return real


def _inside(path: Path, root: Path) -> bool:
    """True if path is root or below it, compared by path components."""
    return path == root or path.is_relative_to(root)


def safe_path(p: str, roots: Roots) -> Path:
    """Resolve a host path and check it stays inside one of the roots."""
    if not isinstance(p, str) or not p.startswith("/"):
        raise Refused("A folder path must be an absolute path.")
    if "\0" in p or ".." in PurePosixPath(p).parts:
        raise Refused("A folder path must not contain '..' or hidden characters.")
    real = Path(os.path.realpath(p))
    if not (_inside(real, roots.data) or _inside(real, roots.config)):
        raise Refused(f"The folder {p} is outside Orama's data and config folders.")
    return real


# ---------------------------------------------------------------- compose


def validate_compose(doc: dict, roots: Roots) -> None:
    """Refuse anything the apps compose builder would not have produced."""
    if not isinstance(doc, dict):
        raise Refused("The apps description is not a mapping.")
    extra = set(doc) - TOP_LEVEL_KEYS
    if extra:
        raise Refused(f"The apps description has unsupported sections: {_names(extra)}.")
    if doc.get("name") != PROJECT_NAME:
        raise Refused(f"The apps project must be called {PROJECT_NAME}.")
    if "networks" in doc and doc["networks"] != NETWORKS:
        raise Refused(f"Only the '{NETWORK}' bridge network may be defined.")
    services = doc.get("services")
    if not isinstance(services, dict):
        raise Refused("The apps description has no services.")
    for name, svc in services.items():
        if not isinstance(name, str) or name not in CATALOG:
            raise Refused(f"'{name}' is not an app Orama knows about.")
        if not isinstance(svc, dict):
            raise Refused(f"The settings for {name} are not a mapping.")
        _validate_service(name, CATALOG[name], svc, services, roots)


def _validate_service(
    name: str, spec: ServiceSpec, svc: dict, services: dict, roots: Roots
) -> None:
    dangerous = set(svc) & DANGEROUS_KEYS
    if dangerous:
        raise Refused(f"{name} asks for elevated access ({_names(dangerous)}).")
    extra = set(svc) - SERVICE_KEYS
    if extra:
        raise Refused(f"{name} has unsupported settings: {_names(extra)}.")

    if svc.get("image") != spec.image:
        raise Refused(f"{name} must use the image {spec.image}.")
    if svc.get("container_name") != f"orama-{name}":
        raise Refused(f"{name} must be named orama-{name}.")
    if svc.get("restart") != RESTART:
        raise Refused(f"{name} must use restart policy {RESTART}.")

    _validate_networking(name, spec, svc, services)
    _validate_volumes(name, svc.get("volumes", []), roots)
    _validate_env_file(name, spec, svc, roots)
    _validate_environment(name, svc.get("environment", {}))
    _validate_hardware(name, spec, svc)
    _validate_identity(name, spec, svc)


def _validate_networking(name: str, spec: ServiceSpec, svc: dict, services: dict) -> None:
    behind_vpn = "network_mode" in svc
    if behind_vpn:
        if name != VPN_CLIENT or svc["network_mode"] != VPN_NETWORK_MODE:
            raise Refused(f"{name} may not change its network mode.")
        if VPN_SERVICE not in services:
            raise Refused(f"{name} is set to use the VPN, but the VPN app is missing.")
        if "networks" in svc or "ports" in svc:
            raise Refused(
                f"{name} uses the VPN's network, so it cannot join others or publish ports."
            )
    elif svc.get("networks") != [NETWORK]:
        raise Refused(f"{name} must join only the '{NETWORK}' network.")

    if "depends_on" in svc and (not behind_vpn or svc["depends_on"] != VPN_DEPENDS_ON):
        raise Refused(f"{name} may only wait for the VPN, when it uses the VPN.")

    if "ports" in svc:
        _validate_ports(name, spec, svc["ports"])


def _validate_ports(name: str, spec: ServiceSpec, ports: Any) -> None:
    allowed = CATALOG[VPN_CLIENT].container_port if name == VPN_SERVICE else spec.container_port
    if not isinstance(ports, list) or len(ports) != 1 or not isinstance(ports[0], str):
        raise Refused(f"{name} may publish exactly one port, written as host:container.")
    match = _PORT.fullmatch(ports[0])
    if match is None:
        raise Refused(f"{name} has a port that is not written as host:container.")
    host, container = int(match[1]), int(match[2])
    if not allowed or container != allowed:
        raise Refused(f"{name} may only publish its own web port.")
    if not 1 <= host <= 65535:
        raise Refused(f"{name} has a host port outside 1-65535.")


def _validate_volumes(name: str, volumes: Any, roots: Roots) -> None:
    if not isinstance(volumes, list):
        raise Refused(f"The folders for {name} are not a list.")
    for vol in volumes:
        if not isinstance(vol, dict):
            raise Refused(f"{name} has a folder entry in an unsupported form.")
        extra = set(vol) - VOLUME_KEYS
        if extra:
            raise Refused(f"{name} has folder options that are not allowed: {_names(extra)}.")
        if vol.get("type") != "bind":
            raise Refused(f"{name} may only use plain host folders.")
        source, target = vol.get("source"), vol.get("target")
        if not isinstance(target, str) or not target.startswith("/"):
            raise Refused(f"{name} has a folder without an absolute container path.")
        if "read_only" in vol and not isinstance(vol["read_only"], bool):
            raise Refused(f"{name} has a read-only flag that is not true or false.")
        if isinstance(source, str) and os.path.normpath(source) == DOCKER_SOCKET:
            raise Refused(f"{name} may not use the Docker socket.")
        safe_path(source, roots)


def _validate_env_file(name: str, spec: ServiceSpec, svc: dict, roots: Roots) -> None:
    if "env_file" not in svc:
        return
    if not spec.secret_env:
        raise Refused(f"{name} does not use a secrets file.")
    if svc["env_file"] != [str(app_env_file(roots.config, name))]:
        raise Refused(f"{name} may only read its own secrets file.")


def _validate_environment(name: str, environment: Any) -> None:
    if not isinstance(environment, dict):
        raise Refused(f"The settings list for {name} is not a mapping.")
    for key, value in environment.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise Refused(f"{name} has a setting whose name or value is not text.")
        if key in SECRET_ENV_NAMES:
            raise Refused(
                f"{name} may not carry {key} in its settings; secrets go in its secrets file."
            )


def _validate_hardware(name: str, spec: ServiceSpec, svc: dict) -> None:
    if svc.get("cap_add", []) != list(spec.cap_add):
        raise Refused(f"{name} asks for system permissions it is not allowed.")

    allowed_devices = {f"{d}:{d}" for d in spec.devices}
    if name in MEDIA_SERVERS:
        allowed_devices.add(RENDER_DEVICE)
    devices = svc.get("devices", [])
    if not isinstance(devices, list) or not all(isinstance(d, str) for d in devices):
        raise Refused(f"The devices for {name} are not a list.")
    if not set(devices) <= allowed_devices:
        raise Refused(f"{name} asks for a device it is not allowed.")

    if "deploy" in svc and (name not in MEDIA_SERVERS or svc["deploy"] != GPU_DEPLOY):
        raise Refused(f"{name} may not reserve hardware.")


def _validate_identity(name: str, spec: ServiceSpec, svc: dict) -> None:
    if "user" in svc:
        user = svc["user"]
        if spec.env_style != "jellyfin" or not isinstance(user, str) or not _USER.fullmatch(user):
            raise Refused(f"{name} must run as a numeric user and group.")
    if "group_add" in svc:
        groups = svc["group_add"]
        if (
            name not in MEDIA_SERVERS
            or not isinstance(groups, list)
            or not all(isinstance(g, str) and _DIGITS.fullmatch(g) for g in groups)
        ):
            raise Refused(f"{name} may only join extra groups by number.")


def _names(keys: set) -> str:
    return ", ".join(sorted(str(k) for k in keys))


# ---------------------------------------------------------------- env files


def validate_env_files(env_files: dict[str, dict[str, str]], roots: Roots) -> None:
    """Refuse env files for unknown apps, unexpected names, or unsafe values.

    Paths are never taken from the request: the Agent derives each file's location from
    its own ``roots``. Values are written single-quoted, so quotes and line breaks are
    refused.
    """
    if not isinstance(env_files, dict):
        raise Refused("The secrets files are not a mapping.")
    for app, values in env_files.items():
        spec = CATALOG.get(app) if isinstance(app, str) else None
        if spec is None or not spec.secret_env:
            raise Refused(f"'{app}' does not have a secrets file.")
        if not isinstance(values, dict):
            raise Refused(f"The secrets for {app} are not a mapping.")
        for key, value in values.items():
            if key not in spec.secret_env:
                raise Refused(f"{app} does not accept the secret {key}.")
            if not isinstance(value, str) or not value:
                raise Refused(f"The secret {key} for {app} is empty or not text.")
            if any(c in value for c in FORBIDDEN_ENV_CHARS):
                raise Refused(
                    f"The secret {key} for {app} contains quotes, line breaks or hidden "
                    "characters."
                )
