"""Host port assignment: stable, unique, never on reserved or default ports."""

from __future__ import annotations

from orama.catalog import CATALOG, CORE, UGOS_RESERVED_PORTS

_MAX_PORT = 65535


def assign_ports(apps: list[str], previous: dict[str, int], taken: set[int]) -> dict[str, int]:
    """Assign one host port per app that has one.

    Previous assignments are kept as-is (they may show up in `taken` because
    our own containers hold them). Otherwise the default port is used when
    free; if not, scan upward for a port that is not taken, reserved, any
    service's default, or already assigned.
    """
    specs = {**CATALOG, **CORE}
    unknown = [a for a in apps if a not in specs]
    if unknown:
        raise ValueError(f"unknown app(s): {', '.join(unknown)}")

    defaults = {s.default_host_port for s in specs.values() if s.default_host_port}
    blocked = defaults | UGOS_RESERVED_PORTS | set(taken)
    assigned: dict[str, int] = {}

    wanted = [a for a in apps if specs[a].default_host_port]
    # Pass 1: previous assignments are fixed points.
    for app in wanted:
        if app in previous:
            assigned[app] = previous[app]
    used = set(assigned.values())

    for app in wanted:
        if app in assigned:
            continue
        default = specs[app].default_host_port
        if default not in set(taken) | UGOS_RESERVED_PORTS | used:
            port = default
        else:
            port = default + 1
            while port in blocked or port in used:
                port += 1
            if port > _MAX_PORT:
                raise ValueError(f"no free host port available for {app}")
        assigned[app] = port
        used.add(port)
    return assigned
