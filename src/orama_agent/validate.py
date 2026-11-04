"""Validation of everything Orama sends to the Agent: the security boundary.

The Agent is the only component with the Docker socket. It accepts an apps compose
document and per-app env files only if they have exactly the shape that
``orama.compose.build_apps_compose`` emits, checked against the catalogue baked into the
Agent's own image and the roots and ids read from the Agent's own environment. Nothing
sent over the socket is trusted to describe what is allowed: mounts, ids and environment
are pinned to what the Agent computes itself.

Refusal messages are plain language, never contain environment or secret values, and
show untrusted names only through ``_show`` (repr, truncated).
"""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Self

from orama.catalog import (
    CATALOG,
    GLUETUN_PORT_FORWARD_UP_COMMAND,
    GLUETUN_PROXY_USER,
    ServiceSpec,
)
from orama.compose import (
    NETWORK,
    PORT_FORWARD_ONLY_PROVIDER,
    QBITTORRENT_WEBUI_PORT,
    SHARED_NETWORK,
)
from orama.layout import app_config_dir, app_env_file

PROJECT_NAME = "orama-apps"
RESTART = "unless-stopped"
DOCKER_SOCKETS = ("/var/run/docker.sock", "/run/docker.sock")
MEDIA_SERVERS = frozenset({"plex", "jellyfin"})
RENDER_DEVICE = "/dev/dri:/dev/dri"
GPU_DEPLOY = {
    "resources": {"reservations": {"devices": [{"driver": "nvidia", "capabilities": ["gpu"]}]}}
}
VPN_SERVICE = "gluetun"
VPN_CLIENT = "qbittorrent"
VPN_NETWORK_MODE = f"service:{VPN_SERVICE}"
VPN_DEPENDS_ON = {VPN_SERVICE: {"condition": "service_healthy"}}
_HOOK_PATH = ("services", VPN_SERVICE, "environment", "VPN_PORT_FORWARDING_UP_COMMAND")

TOP_LEVEL_KEYS = frozenset({"name", "services", "networks"})
SERVICE_KEYS = frozenset({
    "image", "container_name", "restart", "volumes", "env_file", "environment", "ports",
    "networks", "network_mode", "depends_on", "devices", "group_add", "user", "cap_add",
    "deploy",
})
DANGEROUS_KEYS = frozenset({
    "privileged", "pid", "ipc", "security_opt", "userns_mode", "volumes_from",
})
FORBIDDEN_ENV_CHARS = ("'", "\n", "\r", "\0")
SECRET_ENV_NAMES = frozenset(name for spec in CATALOG.values() for name in spec.secret_env)

_DIGITS = re.compile(r"[0-9]+")
_PORT = re.compile(r"([0-9]{1,5}):([0-9]{1,5})")
_WORD = re.compile(r"[A-Za-z0-9_+./-]{1,64}")  # TZ and VPN provider names
_SHOW_MAX = 80
_MAX_DEPTH = 20  # the builder's deepest value (the GPU reservation) is about 10 levels down


class Refused(Exception):
    """The Agent will not act on the request. The message is safe to show and log."""


@dataclass(frozen=True)
class Roots:
    """What the Agent itself was started with; never taken from a request."""

    data: Path
    config: Path
    puid: int
    pgid: int

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Self:
        """Read roots (realpath-resolved) and app ids from the Agent's own environment."""
        env = os.environ if env is None else env
        data = _root_from(env, "ORAMA_DATA_ROOT")
        config = _root_from(env, "ORAMA_CONFIG_ROOT")
        if data == config or _inside(data, config) or _inside(config, data):
            raise Refused("The data folder and the config folder must be separate folders.")
        return cls(
            data=data,
            config=config,
            puid=_id_from(env, "ORAMA_PUID"),
            pgid=_id_from(env, "ORAMA_PGID"),
        )


def _root_from(env: Mapping[str, str], key: str) -> Path:
    value = env.get(key)
    if not value:
        raise Refused(f"The Agent was started without {key}.")
    if "\0" in value or not value.startswith("/") or ".." in PurePosixPath(value).parts:
        raise Refused(f"{key} must be an absolute folder path without '..'.")
    real = Path(os.path.realpath(value))
    if real == Path("/"):
        raise Refused(f"{key} must not be the whole disk ('/').")
    return real


def _id_from(env: Mapping[str, str], key: str) -> int:
    value = env.get(key)
    if not value or not _DIGITS.fullmatch(value):
        raise Refused(f"The Agent was started without a numeric {key}.")
    if int(value) == 0:
        raise Refused(f"{key} must not be 0 (root); apps run as a normal user.")
    return int(value)


def _inside(path: Path, root: Path) -> bool:
    """True if path is root or below it, compared by path components."""
    return path == root or path.is_relative_to(root)


def _show(value: Any) -> str:
    """An untrusted name, quoted and truncated, for a refusal message."""
    text = repr(value)
    return text if len(text) <= _SHOW_MAX else text[: _SHOW_MAX - 3] + "..."


def safe_path(p: str, roots: Roots) -> Path:
    """Resolve a host path and check it stays inside one of the roots.

    Returns the resolved path; callers must act on that, not on ``p``.
    """
    if not isinstance(p, str) or not p.startswith("/"):
        raise Refused("A folder path must be an absolute path.")
    if "\0" in p or "$" in p or ".." in PurePosixPath(p).parts:
        raise Refused("A folder path must not contain '..', '$' or hidden characters.")
    real = Path(os.path.realpath(p))
    if real in {Path(os.path.realpath(s)) for s in DOCKER_SOCKETS}:
        raise Refused("The Docker socket may not be used by apps.")
    if not (_inside(real, roots.data) or _inside(real, roots.config)):
        raise Refused(f"The folder {_show(p)} is outside Orama's data and config folders.")
    return real


# ---------------------------------------------------------------- compose


def validate_compose(doc: dict, roots: Roots) -> None:
    """Refuse anything the apps compose builder would not have produced."""
    if not isinstance(doc, dict):
        raise Refused("The apps description is not a mapping.")
    # Compose interpolates $VAR / ${VAR:-x} after we validate, so no string may hold '$',
    # except the one pinned port-forward hook at its one place.
    _refuse_interpolation(doc, 0, ())
    extra = set(doc) - TOP_LEVEL_KEYS
    if extra:
        raise Refused(f"The apps description has unsupported sections: {_names(extra)}.")
    if doc.get("name") != PROJECT_NAME:
        raise Refused(f"The apps project must be called {PROJECT_NAME}.")
    if not _is_shared_network(doc.get("networks")):
        raise Refused(f"The apps may only join Orama's shared network '{SHARED_NETWORK}'.")
    services = doc.get("services")
    if not isinstance(services, dict):
        raise Refused("The apps description has no services.")
    for name, svc in services.items():
        if not isinstance(name, str) or name not in CATALOG:
            raise Refused(f"{_show(name)} is not an app Orama knows about.")
        if not isinstance(svc, dict):
            raise Refused(f"The settings for {name} are not a mapping.")
        _validate_service(name, CATALOG[name], svc, services, roots)
    # Folders may not exist before the first deploy; whatever exists must not be a link.
    _check_host_paths(doc, roots, must_exist=False)


def _is_shared_network(networks: Any) -> bool:
    """Exactly ``{orama: {name: orama-net, external: true}}``, compared by type too.

    The core compose creates the shared network; the apps may only join it, never define
    one. ``==`` alone would accept ``external: 1``.
    """
    if not isinstance(networks, dict) or list(networks) != [NETWORK]:
        return False
    net = networks[NETWORK]
    return (
        isinstance(net, dict)
        and set(net) == {"name", "external"}
        and type(net["name"]) is str
        and net["name"] == SHARED_NETWORK
        and net["external"] is True
    )


def _refuse_interpolation(node: Any, depth: int, path: tuple[Any, ...]) -> None:
    """Refuse '$' in every key and value, but for the exact pinned hook string as the
    value of gluetun's ``VPN_PORT_FORWARDING_UP_COMMAND`` (its ``$$`` renders as ``$``)."""
    if depth > _MAX_DEPTH:
        raise Refused("The apps description is nested too deeply.")
    if isinstance(node, str):
        if "$" in node and not (path == _HOOK_PATH and node == GLUETUN_PORT_FORWARD_UP_COMMAND):
            raise Refused("The apps description may not contain '$' (variable substitution).")
    elif isinstance(node, dict):
        for key, value in node.items():
            _refuse_interpolation(key, depth + 1, ())
            _refuse_interpolation(value, depth + 1, (*path, key))
    elif isinstance(node, list):
        for item in node:
            _refuse_interpolation(item, depth + 1, (*path, None))


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
    _validate_volumes(name, spec, svc, roots)
    _validate_env_file(name, spec, svc, roots)
    _validate_environment(name, spec, svc, roots)
    _validate_hardware(name, spec, svc)
    _validate_identity(name, spec, svc, roots)


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


def _expected_volumes(name: str, spec: ServiceSpec, roots: Roots) -> list[dict[str, str]]:
    """Exactly the binds the builder emits, computed from the Agent's own roots."""
    volumes = []
    if spec.config_mount:
        source = app_config_dir(PurePosixPath(roots.config), name)
        volumes.append({"type": "bind", "source": str(source), "target": spec.config_mount})
    if spec.mounts_data:
        volumes.append({"type": "bind", "source": str(roots.data), "target": "/data"})
    return volumes


def _validate_volumes(name: str, spec: ServiceSpec, svc: dict, roots: Roots) -> None:
    # Pinned, not checked by path: a validated path could be swapped for a symlink before
    # Docker mounts it, and an in-root path could still expose secrets or other apps.
    if svc.get("volumes", []) != _expected_volumes(name, spec, roots):
        raise Refused(f"{name} may only use its own config folder and the shared data folder.")


def _validate_env_file(name: str, spec: ServiceSpec, svc: dict, roots: Roots) -> None:
    if "env_file" not in svc:
        return
    if not spec.secret_env:
        raise Refused(f"{name} does not use a secrets file.")
    if svc["env_file"] != [str(app_env_file(PurePosixPath(roots.config), name))]:
        raise Refused(f"{name} may only read its own secrets file.")


_EnvRule = str | re.Pattern[str]


def _expected_environment(
    name: str, spec: ServiceSpec, roots: Roots
) -> tuple[dict[str, _EnvRule], dict[str, _EnvRule]]:
    """(required, optional) settings mirroring ``orama.compose._environment``."""
    puid, pgid = str(roots.puid), str(roots.pgid)
    if spec.env_style == "linuxserver":
        required: dict[str, _EnvRule] = {"PUID": puid, "PGID": pgid, "UMASK": "002", "TZ": _WORD}
        if name == VPN_CLIENT:
            required["WEBUI_PORT"] = str(QBITTORRENT_WEBUI_PORT)
        return required, {}
    if spec.env_style == "plex":
        return {"PLEX_UID": puid, "PLEX_GID": pgid, "TZ": _WORD}, {}
    if spec.env_style == "jellyfin":
        return {"TZ": _WORD}, {}
    if spec.env_style == "gluetun":
        required = {"VPN_SERVICE_PROVIDER": _WORD, "VPN_TYPE": "wireguard", "TZ": _WORD}
        return required, {
            "VPN_PORT_FORWARDING": "on",
            "VPN_PORT_FORWARDING_UP_COMMAND": GLUETUN_PORT_FORWARD_UP_COMMAND,
            "PORT_FORWARD_ONLY": "on",
            "HTTPPROXY": "on",
            "HTTPPROXY_USER": GLUETUN_PROXY_USER,
        }
    return {}, {}


def _validate_environment(name: str, spec: ServiceSpec, svc: dict, roots: Roots) -> None:
    environment = svc.get("environment", {})
    if not isinstance(environment, dict):
        raise Refused(f"The settings list for {name} is not a mapping.")
    required, optional = _expected_environment(name, spec, roots)
    for key, value in environment.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise Refused(f"{name} has a setting whose name or value is not text.")
        if key in SECRET_ENV_NAMES:
            raise Refused(
                f"{name} may not carry {key} in its settings; secrets go in its secrets file."
            )
        rule = required.get(key, optional.get(key))
        if rule is None:
            raise Refused(f"{name} may not have the setting {_show(key)}.")
        ok = rule.fullmatch(value) if isinstance(rule, re.Pattern) else value == rule
        if not ok:
            raise Refused(f"{name} has a value for {key} that Orama does not allow.")
    missing = set(required) - set(environment)
    if missing:
        raise Refused(f"{name} is missing required settings: {_names(missing)}.")
    if spec.env_style == "gluetun":
        _validate_gluetun_options(name, environment)


def _validate_gluetun_options(name: str, environment: dict[str, str]) -> None:
    """Only the combinations the builder emits; each value is already pinned."""
    port_forwarding = "VPN_PORT_FORWARDING" in environment
    for key in ("VPN_PORT_FORWARDING_UP_COMMAND", "PORT_FORWARD_ONLY"):
        if key in environment and not port_forwarding:
            raise Refused(f"{name} may only set {key} when port forwarding is on.")
    if (
        "PORT_FORWARD_ONLY" in environment
        and environment["VPN_SERVICE_PROVIDER"] != PORT_FORWARD_ONLY_PROVIDER
    ):
        raise Refused(f"{name} may only limit itself to port-forwarding servers with ProtonVPN.")
    # The proxy must ask for a password: without one any app could use it to reach
    # qBittorrent's web page as "localhost".
    if ("HTTPPROXY" in environment) != ("HTTPPROXY_USER" in environment):
        raise Refused(f"{name} may only turn on its web proxy with Orama's proxy user.")


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


def _validate_identity(name: str, spec: ServiceSpec, svc: dict, roots: Roots) -> None:
    runs_as_user = spec.env_style == "jellyfin"
    if runs_as_user and svc.get("user") != f"{roots.puid}:{roots.pgid}":
        raise Refused(f"{name} must run as Orama's app user ({roots.puid}:{roots.pgid}).")
    if not runs_as_user and "user" in svc:
        raise Refused(f"{name} may not choose its user.")
    if "group_add" in svc:
        groups = svc["group_add"]
        if (
            name not in MEDIA_SERVERS
            or not isinstance(groups, list)
            or len(groups) != 1
            or not isinstance(groups[0], str)
            or not _DIGITS.fullmatch(groups[0])
            or int(groups[0]) == 0
        ):
            raise Refused(f"{name} may only join one extra group, by a non-zero number.")


def _names(keys: set) -> str:
    return ", ".join(sorted(_show(k) for k in keys))


# ---------------------------------------------------------------- host paths


def check_host_paths(doc: dict, roots: Roots) -> None:
    """Check on disk that the pinned config mounts and env files are what they claim.

    ``validate_compose`` pins these paths as strings and already refuses any existing
    component below the roots that is a symlink (Docker would follow it to a host folder
    such as /etc). This strict form also requires every folder to exist. Task 7's apply
    must call it on a doc that passed ``validate_compose``, after the app config folders
    have been created, immediately before ``docker compose up``.
    """
    _check_host_paths(doc, roots, must_exist=True)


def _check_host_paths(doc: dict, roots: Roots, *, must_exist: bool) -> None:
    for root in (roots.data, roots.config):
        _check_real_dir(root, must_exist=True)
    for name, svc in doc["services"].items():
        for vol in svc.get("volumes", []):
            source = Path(vol["source"])
            if source != roots.config and _inside(source, roots.config):
                _check_dirs_below(source, roots.config, must_exist)
        for env_file in svc.get("env_file", []):
            path = Path(env_file)
            if _check_dirs_below(path.parent, roots.config, must_exist):
                _check_env_file(name, path)


def _check_dirs_below(path: Path, root: Path, must_exist: bool) -> bool:
    """Walk each component below root; True if all of them exist as real folders."""
    current = root
    for part in path.relative_to(root).parts:
        current = current / part
        if not _check_real_dir(current, must_exist):
            return False
    return True


def _check_real_dir(path: Path, must_exist: bool) -> bool:
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        if must_exist:
            raise Refused(f"The folder {_show(str(path))} does not exist yet.") from None
        return False
    if stat.S_ISLNK(mode):
        raise Refused(f"The folder {_show(str(path))} is a link; Orama only uses real folders.")
    if not stat.S_ISDIR(mode):
        raise Refused(f"{_show(str(path))} is not a folder.")
    return True


def _check_env_file(name: str, path: Path) -> None:
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        return  # written later by the Agent itself
    if not stat.S_ISREG(mode):
        raise Refused(f"The secrets file for {name} is a link or not a regular file.")


# ---------------------------------------------------------------- env files


def validate_secrets_present(doc: dict, env_files: dict[str, dict[str, str]]) -> None:
    """Refuse an apply whose compose needs a secret its env files leave out.

    Call after ``validate_compose`` and ``validate_env_files``. Gluetun's proxy user without
    a password would leave the proxy open to every app; the port-forward hook without
    qBittorrent's password could not log in.
    """
    gluetun = doc["services"].get("gluetun") or {}
    environment = gluetun.get("environment", {})
    secrets = env_files.get("gluetun", {})
    if "HTTPPROXY_USER" in environment and not secrets.get("HTTPPROXY_PASSWORD"):
        raise Refused("The VPN's web proxy is on, but its password was not sent.")
    if "VPN_PORT_FORWARDING_UP_COMMAND" in environment and not secrets.get("QBT_WEBUI_PASSWORD"):
        raise Refused(
            "VPN port forwarding is on, but the qBittorrent password it logs in with was "
            "not sent."
        )


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
            raise Refused(f"{_show(app)} does not have a secrets file.")
        if not isinstance(values, dict):
            raise Refused(f"The secrets for {app} are not a mapping.")
        for key, value in values.items():
            if key not in spec.secret_env:
                raise Refused(f"{app} does not accept the secret {_show(key)}.")
            if not isinstance(value, str) or not value:
                raise Refused(f"The secret {key} for {app} is empty or not text.")
            if any(c in value for c in FORBIDDEN_ENV_CHARS):
                raise Refused(
                    f"The secret {key} for {app} contains quotes, line breaks or hidden "
                    "characters."
                )
