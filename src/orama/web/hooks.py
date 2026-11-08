"""Authenticated webhook refresh routes for Sonarr and Radarr."""

from __future__ import annotations

import hmac
import json
from typing import Literal

from flask import Blueprint, Flask, Request, current_app, g, request

from orama.secrets import SecretStore, webhook_token_name
from orama.web import auth, security

WEBHOOK_PATHS: dict[str, Literal["sonarr", "radarr"]] = {
    "/hooks/sonarr": "sonarr",
    "/hooks/radarr": "radarr",
}
MAX_WEBHOOK_BYTES = 64 * 1024
ALLOWED_EVENTS = frozenset(
    {
        "Test",
        "Grab",
        "Download",
        "Upgrade",
        "Rename",
        "SeriesAdd",
        "SeriesDelete",
        "EpisodeFileDelete",
        "MovieAdded",
        "MovieDelete",
        "MovieFileDelete",
        "ManualInteractionRequired",
        "Health",
        "HealthRestored",
        "ApplicationUpdate",
    }
)


def webhook_token_ok(req: Request, store: SecretStore) -> bool:
    """Constant-time check of the app-specific Bearer token for an exact webhook route."""
    app_name = WEBHOOK_PATHS.get(req.path)
    if app_name is None or req.method != "POST":
        return False
    expected = store.get(webhook_token_name(app_name))
    if not expected:
        return False
    auth_header = req.headers.get("Authorization", "")
    prefix = "Bearer "
    if not auth_header.startswith(prefix):
        return False
    provided = auth_header[len(prefix) :]
    if not provided:
        return False
    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


def _handle_webhook(app_name: Literal["sonarr", "radarr"]):
    from orama.web import MESSAGES, rejected

    if g.get("webhook_app") != app_name:
        return rejected(security.CSRF_REJECTED, 403)

    if request.content_length is not None and request.content_length > MAX_WEBHOOK_BYTES:
        return rejected(MESSAGES[413], 413)

    raw = request.stream.read(MAX_WEBHOOK_BYTES + 1)
    if len(raw) > MAX_WEBHOOK_BYTES:
        return rejected(MESSAGES[413], 413)
    if not raw:
        return rejected(MESSAGES[400], 400)

    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return rejected(MESSAGES[400], 400)

    if not isinstance(payload, dict):
        return rejected(MESSAGES[400], 400)

    event_type = payload.get("eventType")
    if not isinstance(event_type, str) or event_type not in ALLOWED_EVENTS:
        return rejected(MESSAGES[400], 400)

    if event_type == "Test":
        return "ok", 200, {"Content-Type": "text/plain; charset=utf-8"}

    media_key = "series" if app_name == "sonarr" else "movie"
    media = payload.get(media_key)
    if media is None:
        return "ok", 200, {"Content-Type": "text/plain; charset=utf-8"}
    if not isinstance(media, dict):
        return rejected(MESSAGES[400], 400)

    remote_id = media.get("id")
    if type(remote_id) is not int or remote_id <= 0:
        return rejected(MESSAGES[400], 400)

    st = auth.web_state()
    st.requests.enqueue_refresh(app_name, remote_id, st.clock())
    worker = current_app.extensions.get("request_worker")
    if worker is not None and hasattr(worker, "wake"):
        worker.wake()

    return "ok", 200, {"Content-Type": "text/plain; charset=utf-8"}


def register_hooks(app: Flask) -> None:
    """Register the exact POST /hooks/sonarr and POST /hooks/radarr routes."""
    bp = Blueprint("hooks", __name__)

    @bp.post("/hooks/sonarr")
    def sonarr_hook():
        return _handle_webhook("sonarr")

    @bp.post("/hooks/radarr")
    def radarr_hook():
        return _handle_webhook("radarr")

    app.register_blueprint(bp)
