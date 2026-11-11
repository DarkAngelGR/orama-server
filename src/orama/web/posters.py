"""Same-origin poster proxy selected by validated media ID.

Never proxies a browser-supplied URL. Looks up the media item via MediaClient, verifies the
poster URL against pinned HTTPS image origins, enforces strict MIME and size bounds, refuses
redirects, and falls back to a local SVG placeholder.

The item is looked up by its exact catalogue id (TMDB for movies, TVDB for series). When the
app does not return that very id, the placeholder is shown: never another title's poster.
"""

from __future__ import annotations

import threading
import urllib.parse
from collections import OrderedDict

from flask import Blueprint, Flask, abort

from orama.requests.client import MediaClient, RequestError
from orama.web import auth
from orama.wiring.http import Unreachable

TRUSTED_POSTER_HOSTS: frozenset[str] = frozenset(
    {"image.tmdb.org", "artworks.thetvdb.com", "thetvdb.com"}
)
ALLOWED_MIMES: frozenset[str] = frozenset({"image/jpeg", "image/png", "image/webp"})
MAX_POSTER_BYTES = 2 * 1024 * 1024
MAX_CACHE_ENTRIES = 128

PLACEHOLDER_SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 300" width="200" height="300">'
    b'<rect width="200" height="300" fill="#252320"/>'
    b'<rect x="24" y="24" width="152" height="252" rx="6" fill="none" stroke="#3d3934" stroke-width="2"/>'
    b'<circle cx="100" cy="130" r="24" fill="none" stroke="#6e675f" stroke-width="2"/>'
    b'<path d="M70 200 L100 170 L130 200" fill="none" stroke="#6e675f" stroke-width="2"/>'
    b"</svg>"
)


class PosterCache:
    """Thread-safe bounded LRU cache for validated poster bytes."""

    def __init__(self, max_entries: int = MAX_CACHE_ENTRIES) -> None:
        self._max_entries = max_entries
        self._lock = threading.Lock()
        self._items: OrderedDict[tuple[str, int], tuple[bytes, str]] = OrderedDict()

    def get(self, key: tuple[str, int]) -> tuple[bytes, str] | None:
        with self._lock:
            val = self._items.get(key)
            if val is not None:
                self._items.move_to_end(key)
            return val

    def put(self, key: tuple[str, int], data: bytes, mime: str) -> None:
        with self._lock:
            self._items[key] = (data, mime)
            self._items.move_to_end(key)
            while len(self._items) > self._max_entries:
                self._items.popitem(last=False)


def validate_poster_url(url: str | None) -> str | None:
    """Return the URL only if it is an HTTPS URL on a trusted poster host with no credentials."""
    if not url or not isinstance(url, str):
        return None
    try:
        parts = urllib.parse.urlsplit(url.strip())
    except ValueError:
        return None
    if parts.scheme != "https":
        return None
    if parts.username or parts.password or parts.port is not None:
        return None
    host = (parts.hostname or "").lower()
    if host not in TRUSTED_POSTER_HOSTS:
        return None
    if not parts.path or not parts.path.startswith("/"):
        return None
    return urllib.parse.urlunsplit(("https", host, parts.path, parts.query, ""))


def _placeholder_response():
    return (
        PLACEHOLDER_SVG,
        200,
        {
            "Content-Type": "image/svg+xml; charset=utf-8",
            "Cache-Control": "private, max-age=300",
        },
    )


def register_posters(app: Flask) -> None:
    cache = PosterCache()
    app.extensions["poster_cache"] = cache
    bp = Blueprint("posters", __name__)

    @bp.get("/posters/<kind>/<source_id>")
    @auth.requires("requests:read")
    def poster(kind: str, source_id: str):
        if kind not in ("movie", "series"):
            abort(404)
        try:
            sid = int(source_id)
        except ValueError:
            abort(400)
        if sid <= 0 or sid > 100_000_000:
            abort(400)

        cache_key = (kind, sid)
        cached = cache.get(cache_key)
        if cached is not None:
            body, mime = cached
            return body, 200, {"Content-Type": mime, "Cache-Control": "private, max-age=3600"}

        st = auth.web_state()
        setup = st.setup.current()
        if setup is None:
            return _placeholder_response()

        client = MediaClient(setup=setup, store=st.store, http=st.http)
        try:
            matched = client.find(kind, sid)  # type: ignore[arg-type]
        except RequestError:
            return _placeholder_response()
        if matched is None:
            return _placeholder_response()

        safe_url = validate_poster_url(matched.poster_ref)
        if safe_url is None:
            return _placeholder_response()

        try:
            resp = st.http.request("GET", safe_url, timeout=10.0)
        except Unreachable:
            return _placeholder_response()

        if resp.status != 200 or not resp.body or len(resp.body) > MAX_POSTER_BYTES:
            return _placeholder_response()

        raw_ct = ""
        for k, v in resp.headers.items():
            if k.lower() == "content-type":
                raw_ct = v
                break
        mime = raw_ct.split(";", 1)[0].strip().lower()
        if mime not in ALLOWED_MIMES:
            return _placeholder_response()

        cache.put(cache_key, resp.body, mime)
        return resp.body, 200, {"Content-Type": mime, "Cache-Control": "private, max-age=3600"}

    app.register_blueprint(bp)
