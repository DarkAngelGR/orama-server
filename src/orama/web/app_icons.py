"""Each app's own logo, fetched from the app on orama-net and served from Orama's origin.

Nothing from the browser chooses what is fetched: the app name must be one of the apps below
and part of this setup, and the address is fixed (``http://<app>:<container port><path>``).
Only a small PNG is passed on, with Orama's own Content-Type; anything else (a sign-in page,
an SVG, a redirect, a large file) is refused and the page keeps the icon Orama ships.

Paths seen on the pinned images (2026-11-13). Jellyfin is not here: its icon file names change
with every release. qBittorrent is not here: it asks for a sign-in even for its images.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable

from flask import Blueprint, Flask, abort, current_app

from orama.catalog import CATALOG
from orama.setup_model import enabled_apps
from orama.web import auth
from orama.wiring.http import Http, Unreachable

_SERVARR = "/Content/Images/Icons/apple-touch-icon.png"
ICON_PATHS: dict[str, str] = {
    "plex": "/web/static/icon-iphone@2x.png",
    "sonarr": _SERVARR,
    "radarr": _SERVARR,
    "prowlarr": _SERVARR,
    "bazarr": "/images/logo128.png",
    "sabnzbd": "/staticcfg/ico/apple-touch-icon-180x180-precomposed.png",
}
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
MAX_ICON_BYTES = 256 * 1024
RETRY_AFTER = 300.0  # seconds before an app whose logo failed is asked again
FETCH_TIMEOUT = 5.0


def icon_url(app: str) -> str:
    return f"http://{app}:{CATALOG[app].container_port}{ICON_PATHS[app]}"


class IconCache:
    """Logos that worked (kept), and when each failure may be retried."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._logos: dict[str, bytes] = {}
        self._failed_until: dict[str, float] = {}

    def get(self, app: str, http: Http, now: float) -> bytes | None:
        with self._lock:
            if app in self._logos:
                return self._logos[app]
            if self._failed_until.get(app, 0.0) > now:
                return None
        logo = _fetch(app, http)
        with self._lock:
            if logo is None:
                self._failed_until[app] = now + RETRY_AFTER
            else:
                self._logos[app] = logo
                self._failed_until.pop(app, None)
        return logo


def _fetch(app: str, http: Http) -> bytes | None:
    try:
        resp = http.request("GET", icon_url(app), timeout=FETCH_TIMEOUT)
    except Unreachable:
        return None
    body = resp.body
    if resp.status != 200 or not body.startswith(PNG_MAGIC) or len(body) > MAX_ICON_BYTES:
        return None
    return body


def logos_for(apps: Iterable[str]) -> frozenset[str]:
    """The apps among ``apps`` whose own logo Orama has (fetching it now if due).

    The Apps page shows a logo only for these, so the browser never asks for one that is
    missing: an app that is still starting simply keeps its shipped icon or letter.
    """
    cache: IconCache = current_app.extensions["app_icons"]
    st = auth.web_state()
    now = st.clock()
    return frozenset(a for a in apps
                     if a in ICON_PATHS and cache.get(a, st.http, now) is not None)


def register_app_icons(app: Flask, cache_factory: Callable[[], IconCache] = IconCache) -> None:
    cache = cache_factory()
    app.extensions["app_icons"] = cache
    bp = Blueprint("app_icons", __name__)

    @bp.get("/apps/icon/<name>")
    @auth.requires("apps")
    def icon(name: str):
        st = auth.web_state()
        setup = st.setup.current()
        if name not in ICON_PATHS or setup is None or name not in enabled_apps(setup):
            abort(404)
        logo = cache.get(name, st.http, st.clock())
        if logo is None:
            abort(404)
        # Orama keeps it in memory; the response itself follows the app-wide no-store rule.
        return logo, 200, {"Content-Type": "image/png"}

    app.register_blueprint(bp)
