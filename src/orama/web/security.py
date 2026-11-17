"""Request checks every page relies on: the Host allowlist, CSRF and response headers.

All of these run before any page code (``before_request``) or on every response
(``after_request``). Rejections are plain pages that never echo the request back.
"""

from __future__ import annotations

import hmac
import ipaddress
import re
import socket
import struct
from pathlib import Path

from flask import g, request

from orama.setup_model import DesiredSetup

LOCAL_NAMES = frozenset({"localhost", "127.0.0.1", "::1"})
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
CSRF_FIELD = "csrf_token"
CSRF_HEADER = "X-CSRF-Token"

HOST_REJECTED = (
    "Orama does not answer at this address. Open it with the address the installer showed, "
    "or add this name in Settings → Addresses."
)
CSRF_REJECTED = "This page expired. Go back, reload it and try again."

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
        "connect-src 'self'; font-src 'self'; form-action 'self'; frame-ancestors 'none'; "
        "base-uri 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    # same-origin, not no-referrer: with no-referrer browsers send "Origin: null" on
    # same-origin form posts, which the Origin check refuses. Nothing (so no first-run
    # code in a URL) is sent to other sites either way.
    "Referrer-Policy": "same-origin",
    "X-Frame-Options": "DENY",
    "Cross-Origin-Opener-Policy": "same-origin",
}

_PORT = re.compile(r"[0-9]{1,5}")
# Characters a host name or an IPv4/IPv6 literal can hold (after lower-casing).
_HOST_CHARS = re.compile(r"[a-z0-9.\-:]+")


def allowed_hosts(setup: DesiredSetup | None) -> frozenset[str]:
    """The names this server answers to, lower case, without ports or trailing dots."""
    names = set(LOCAL_NAMES)
    if setup is not None:
        if setup.lan_address is not None:
            names.add(str(setup.lan_address))
        if setup.host_name:
            names.add(setup.host_name.lower())
            names.add(f"{setup.host_name.lower()}.local")
        names.update(name.lower() for name in setup.extra_host_names)
    return frozenset(names)


def normalize_host(raw: str) -> str | None:
    """The bare name of a ``Host`` header value, or None when it is malformed.

    Lower case; the port and one trailing dot removed; IPv6 brackets removed. A value
    holding a comma (two Host headers joined) is malformed.
    """
    if not raw or len(raw) > 300:
        return None
    host = raw.lower()
    if host.startswith("["):
        end = host.find("]")
        if end < 0:
            return None
        name, rest = host[1:end], host[end + 1 :]
        if rest and not (rest.startswith(":") and _PORT.fullmatch(rest[1:])):
            return None
        if not name or ":" not in name:
            return None  # brackets only for IPv6
    else:
        name, sep, port = host.partition(":")
        if sep and not _PORT.fullmatch(port):
            return None
    name = name.removesuffix(".")
    if not name or not _HOST_CHARS.fullmatch(name):
        return None
    return name


WEBHOOK_HOST = "orama-server"


def host_allowed(environ: dict, setup: DesiredSetup | None) -> bool:
    """True when the request's single Host header names this server."""
    raw = environ.get("HTTP_HOST")
    if not isinstance(raw, str):
        return False
    name = normalize_host(raw)
    return name is not None and name in allowed_hosts(setup)


def webhook_host_allowed(environ: dict, setup: DesiredSetup | None) -> bool:
    """True when the Host header names this server or its internal Docker hostname."""
    raw = environ.get("HTTP_HOST")
    if not isinstance(raw, str):
        return False
    name = normalize_host(raw)
    return name is not None and (name == WEBHOOK_HOST or name in allowed_hosts(setup))


def csrf_token() -> str:
    """The CSRF token of this request's session, for templates (``{{ csrf_token() }}``).

    Only pages that start a pre-sign-in session (the sign-in and first-run pages) create
    one; elsewhere a visitor without a session gets an empty token, which no check accepts.
    """
    from orama.web import auth

    session = auth.current_session(create=bool(g.get("may_start_session")))
    return session.csrf if session is not None else ""


def csrf_ok() -> bool:
    """Check the token (form field or header) and, when sent, the ``Origin`` header."""
    session = g.get("session")
    if session is None:
        return False
    sent = request.headers.get(CSRF_HEADER) or request.form.get(CSRF_FIELD) or ""
    if not hmac.compare_digest(sent.encode(), session.csrf.encode()):
        return False
    origin = request.headers.get("Origin")
    if origin is not None:
        expected = f"{request.scheme}://{request.environ.get('HTTP_HOST', '')}"
        if not hmac.compare_digest(origin.lower().encode(), expected.lower().encode()):
            return False
    return True


def add_security_headers(response):
    """Every response: fixed security headers, no CORS, HTML never cached."""
    for name in [k for k, _ in response.headers.items() if k.lower().startswith("access-control-")]:
        del response.headers[name]
    response.headers.update(SECURITY_HEADERS)
    if request.path.startswith("/hermes"):
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; script-src 'self'; style-src 'self'; "
            "img-src 'self' https://image.tmdb.org; media-src 'self' blob:; "
            "connect-src 'self'; font-src 'self'; form-action 'self'; frame-ancestors 'none'; "
            "base-uri 'none'"
        )
        response.headers["Permissions-Policy"] = "microphone=(self), camera=()"
    if request.endpoint != "static" or response.status_code not in (200, 304):
        response.headers["Cache-Control"] = "no-store"
        response.headers.pop("Expires", None)
    return response


# ---------------------------------------------------------------- Docker's own networks

# Refusal shown to a request that reaches Orama through its own Docker networks.
SERVER_ADDRESS_REJECTED = (
    "Orama cannot be used from the server itself or through Docker's network: the apps on "
    "this server could then reuse your sign-in. Open Orama from another device, such as a "
    "phone or laptop, at the server's network address (the link the installer printed)."
)
# The owner may allow it anyway by creating this file in Orama's state folder. Only root or
# the Orama user can; app containers cannot reach that folder.
SERVER_SIGNIN_FILE = "server-signin.allowed"
_ROUTE_FILE = Path("/proc/net/route")
_DOCKER_MARKER = Path("/.dockerenv")
Network = ipaddress.IPv4Network | ipaddress.IPv6Network


def container_networks(route_file: Path = _ROUTE_FILE,
                       marker: Path = _DOCKER_MARKER) -> tuple[Network, ...]:
    """The IPv4 networks this container is directly attached to (orama-net and the like).

    Empty outside a container: there the host's own networks are the owner's home network.
    """
    try:
        if not marker.exists():
            return ()
        lines = route_file.read_text().splitlines()[1:]
    except OSError:
        return ()
    nets: list[Network] = []
    for line in lines:
        fields = line.split()
        if len(fields) < 8:
            continue
        try:
            dest = socket.inet_ntoa(struct.pack("<I", int(fields[1], 16)))
            mask = socket.inet_ntoa(struct.pack("<I", int(fields[7], 16)))
            if int(fields[2], 16) != 0 or dest == "0.0.0.0":
                continue  # a route through a gateway, or the default route
            net = ipaddress.ip_network(f"{dest}/{mask}", strict=False)
        except (ValueError, struct.error, OSError):
            continue
        if net not in nets:
            nets.append(net)
    return tuple(nets)


def is_internal(address: str, nets: tuple[Network, ...] | list[Network]) -> bool:
    """True when ``address`` lies in one of ``nets`` (IPv4-mapped IPv6 included)."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return any(ip in net for net in nets)
