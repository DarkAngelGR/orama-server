"""The Apps page: each enabled app, how it is doing in plain words, and a link to open it.

Read-only: nothing here changes state. The states come from the Agent; its raw words are
mapped to four plain ones and never printed.
"""

from __future__ import annotations

from dataclasses import dataclass

from flask import Blueprint, Flask, render_template, request

from orama.agent_client import AgentError
from orama.setup_model import DesiredSetup, enabled_apps
from orama.web import auth, security
from orama.wiring.engine import display_name

NOT_FOUND = "Not found"
_WORDS = {
    "running": "Running",
    "starting": "Starting", "restarting": "Starting", "created": "Starting",
    "exited": "Stopped", "stopped": "Stopped", "dead": "Stopped", "paused": "Stopped",
}
FIRST = ("plex", "jellyfin")
NO_LINK = frozenset({"gluetun"})  # a VPN, not a web page (Recyclarr is never listed)


@dataclass(frozen=True)
class AppRow:
    key: str
    name: str
    state: str | None
    url: str | None


def state_word(raw: object) -> str:
    return _WORDS.get(raw, NOT_FOUND) if isinstance(raw, str) else NOT_FOUND


def agent_states() -> dict[str, str] | None:
    """The Agent's states, or None when it cannot be reached."""
    try:
        states = auth.web_state().agent.status()
    except (AgentError, OSError):
        return None
    return states if isinstance(states, dict) else None


def request_host() -> str:
    """The Host of this request (already checked against the allowlist), for a URL."""
    name = security.normalize_host(request.environ.get("HTTP_HOST", "")) or "localhost"
    return f"[{name}]" if ":" in name else name


def app_rows(setup: DesiredSetup, states: dict[str, str] | None) -> list[AppRow]:
    apps = enabled_apps(setup)
    apps = [a for a in FIRST if a in apps] + [a for a in apps if a not in FIRST]
    host = request_host()
    rows = []
    for app in apps:
        port = setup.ports.get(app)
        url = f"http://{host}:{port}/" if port and app not in NO_LINK else None
        rows.append(AppRow(app, display_name(app),
                           None if states is None else state_word(states.get(app)), url))
    return rows


def running_summary(setup: DesiredSetup) -> str | None:
    states = agent_states()
    if states is None:
        return None
    apps = enabled_apps(setup)
    up = sum(1 for a in apps if state_word(states.get(a)) == "Running")
    return f"{up} of {len(apps)} apps running"


def disk_warning(setup: DesiredSetup | None) -> str | None:
    """Return a plain low-storage warning if free space is below the configured threshold."""
    if setup is None:
        return None
    agent = auth.web_state().agent
    if not hasattr(agent, "data_space"):
        return None
    try:
        space = agent.data_space()
    except (AgentError, OSError):
        return None
    if not isinstance(space, dict):
        return None
    total = space.get("total_bytes")
    free = space.get("free_bytes")
    if (
        type(total) is not int
        or type(free) is not int
        or total <= 0
        or free < 0
    ):
        return None
    threshold = getattr(setup, "disk_warning_percent", 5)
    if free * 100 < total * threshold:
        pct = int((free * 100) // total)
        return f"Storage space is running low ({pct}% free, warning below {threshold}%)."
    return None


def register_apps(app: Flask) -> None:
    app.register_blueprint(_pages())


def _pages() -> Blueprint:
    pages = Blueprint("apps", __name__)

    def context():
        setup = auth.web_state().setup.current()
        if setup is None:
            return {"rows": [], "down": False}
        states = agent_states()
        return {"rows": app_rows(setup, states), "down": states is None}

    @pages.get("/apps")
    @auth.requires("apps")
    def apps():
        return render_template("apps.html", live=True, **context())

    @pages.get("/apps/list")
    @auth.requires("apps")
    def apps_list():
        return render_template("_apps_list.html", **context())

    return pages
