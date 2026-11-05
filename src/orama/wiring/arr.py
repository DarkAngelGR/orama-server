"""Sonarr and Radarr wiring: root folder, hardlinks and the two download clients.

Both apps speak the same v3 API. The only differences are the port, the root folder
and the name of the category field, so one module serves both.
"""

from __future__ import annotations

import copy
import hashlib
from dataclasses import dataclass
from typing import Any, Literal

from orama.catalog import QBITTORRENT_WEBUI_USER
from orama.secrets import JELLYFIN_KEY_NAME, PLEX_TOKEN_NAME
from orama.setup_model import DesiredSetup, enabled_apps
from orama.wiring.engine import Step, StepError, display_name, wait_until
from orama.wiring.http import Http, Response, Unreachable

START_TIMEOUT = 600.0  # the first start migrates its database
MASK = "********"
SAB_PORT = 8080
QBITTORRENT_PORT = 8090
PLEX_PORT = 32400
JELLYFIN_PORT = 8096


@dataclass(frozen=True)
class _AppSpec:
    port: int
    root_folder: str
    category_field: str
    category: str


SPECS = {
    "sonarr": _AppSpec(8989, "/data/media/tv", "tvCategory", "tv"),
    "radarr": _AppSpec(7878, "/data/media/movies", "movieCategory", "movies"),
}


def sent_marker_name(app: str, client: str) -> str:
    return f"arr:{app}:{client}:sent"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _norm(value: object) -> str:
    return str(value or "").rstrip("/")


def arr_steps(
    app: Literal["sonarr", "radarr"], setup: DesiredSetup, store: Any, http: Http
) -> list[Step]:
    spec = SPECS[app]
    name = display_name(app)
    base = f"http://{app}:{spec.port}/api/v3"
    qbittorrent_host = "gluetun" if setup.vpn is not None else "qbittorrent"

    def key() -> str:
        value = store.get(f"apikey:{app}")
        if value is None:
            raise StepError("The API key is missing — run apply again.")
        return value

    def call(
        method: str, path: str, *, json: Any = None, params: dict[str, Any] | None = None
    ) -> Response:
        try:
            resp = http.request(
                method, f"{base}/{path}", headers={"X-Api-Key": key()}, json=json, params=params
            )
        except Unreachable:
            raise StepError(f"{name} could not be reached — run apply again.") from None
        if resp.status in (401, 403):
            raise StepError(f"{name} did not accept Orama's API key — run apply again.")
        if resp.status >= 300:
            raise StepError(f"{name} refused a request (HTTP {resp.status}) — run apply again.")
        return resp

    def read(path: str) -> Any:
        try:
            return call("GET", path).json()
        except ValueError:
            raise StepError(f"{name} gave an answer Orama could not read.") from None

    def wait() -> str | None:
        key()  # fail early and plainly when the key is missing

        def ready() -> bool:
            try:
                resp = http.request(
                    "GET", f"{base}/system/status", headers={"X-Api-Key": key()}
                )
            except Unreachable:
                return False
            if resp.status in (401, 403):
                raise StepError(f"{name} did not accept Orama's API key — run apply again.")
            return resp.status == 200

        wait_until(ready, app=app, timeout=START_TIMEOUT)
        return None

    def root_folder() -> str | None:
        have = {_norm(r.get("path")) for r in read("rootfolder")}
        if spec.root_folder in have:
            return None
        call("POST", "rootfolder", json={"path": spec.root_folder})
        return "added the media folder"

    def media_management() -> str | None:
        config = read("config/mediamanagement")
        if config.get("copyUsingHardlinks") is True:
            return None
        config["copyUsingHardlinks"] = True
        call("PUT", f"config/mediamanagement/{config['id']}", json=config)
        return "turned on hardlinks"

    def client_step(
        slug: str, label: str, implementation: str, enabled: bool,
        needs: tuple[str, ...], values: Any,
        *, kind: Literal["downloadclient", "notification"] = "downloadclient",
    ) -> Step:
        client_name = f"{label} (Orama)"
        noun = "download client" if kind == "downloadclient" else "library update"
        # Flags Orama keeps switched on: a download client's `enable`, and the events a
        # notification fires on (an import, an upgrade, a rename tell the media server).
        flags = (
            ("enable",) if kind == "downloadclient" else ("onDownload", "onUpgrade", "onRename")
        )

        def find(clients: list[dict[str, Any]]) -> dict[str, Any] | None:
            return next((c for c in clients if c.get("name") == client_name), None)

        def _enable(client: dict[str, Any]) -> None:
            for flag in flags:
                client[flag] = True

        def run() -> str | None:
            clients = read(kind)
            have = find(clients)
            if not enabled:
                if have is None:
                    return None
                call("DELETE", f"{kind}/{have['id']}")
                store.set(sent_marker_name(app, slug), "")
                return f"removed the {label} {noun}"
            want, secret = values()  # managed fields, and the secret behind the marker
            marker = sent_marker_name(app, slug)
            if have is None:
                template = next(
                    (s for s in read(f"{kind}/schema")
                     if s.get("implementation") == implementation),
                    None,
                )
                if template is None:
                    raise StepError(
                        f"{name} does not offer {label} — it may be a different version than Orama expects."
                    )
                client = copy.deepcopy(template)
                client["name"] = client_name
                _enable(client)
                _apply_fields(client, want, noun)
                call("POST", kind, json=client, params={"forceSave": "true"})
                store.set(marker, _digest(secret))
                return f"added the {label} {noun}"
            current = {f.get("name"): f.get("value") for f in have.get("fields", [])}
            secret_names = _secret_names(want)
            stale = any(have.get(f) is not True for f in flags) or store.get(marker) != _digest(secret)
            for field, value in want.items():
                shown = current.get(field)
                if field in secret_names:
                    if shown not in (None, MASK) and str(shown) != str(value):
                        stale = True
                elif str(shown) != str(value):
                    stale = True
            if not stale:
                return None
            client = copy.deepcopy(have)
            _enable(client)
            _apply_fields(client, want, noun)
            call("PUT", f"{kind}/{have['id']}", json=client, params={"forceSave": "true"})
            store.set(marker, _digest(secret))
            return f"restored the {label} {noun}"

        step = f"{app}:client:{slug}" if kind == "downloadclient" else f"{app}:notify:{slug}"
        return Step(step, app, run, needs=needs)

    def _secret_names(want: dict[str, Any]) -> set[str]:
        return {n for n in ("apiKey", "password", "authToken") if n in want}

    def _apply_fields(client: dict[str, Any], want: dict[str, Any], noun: str) -> None:
        present = {f.get("name"): f for f in client.get("fields", [])}
        for field, value in want.items():
            if field not in present:
                raise StepError(
                    f"{name} has no '{field}' setting for this {noun} — "
                    "it may be a different version than Orama expects."
                )
            present[field]["value"] = value

    def sab_values() -> tuple[dict[str, Any], str]:
        apikey = store.get("apikey:sabnzbd")
        if apikey is None:
            raise StepError("SABnzbd's API key is missing — run apply again.")
        return (
            {"host": "sabnzbd", "port": SAB_PORT, "apiKey": apikey,
             spec.category_field: spec.category},
            apikey,
        )

    def qbittorrent_values() -> tuple[dict[str, Any], str]:
        password = store.get("password:qbittorrent")
        if password is None:
            raise StepError("qBittorrent's password is missing — run apply again.")
        return (
            {"host": qbittorrent_host, "port": QBITTORRENT_PORT,
             "username": QBITTORRENT_WEBUI_USER, "password": password,
             spec.category_field: spec.category},
            password,
        )

    def plex_values() -> tuple[dict[str, Any], str]:
        token = store.get(PLEX_TOKEN_NAME)
        if not token:
            raise StepError("Orama's Plex sign-in is missing — run apply again.")
        return (
            {"host": "plex", "port": PLEX_PORT, "authToken": token, "updateLibrary": True},
            token,
        )

    def jellyfin_values() -> tuple[dict[str, Any], str]:
        apikey = store.get(JELLYFIN_KEY_NAME)
        if not apikey:
            raise StepError("Jellyfin's API key is missing — run apply again.")
        return (
            # notify=False: refresh the library only, no pop-up on the screens playing.
            {"host": "jellyfin", "port": JELLYFIN_PORT, "apiKey": apikey, "updateLibrary": True,
             "notify": False},
            apikey,
        )

    wait_name = f"wait:{app}"
    apps = enabled_apps(setup)
    usenet = "sabnzbd" in apps
    torrent = "qbittorrent" in apps
    plex = "plex" in apps
    jellyfin = "jellyfin" in apps
    return [
        Step(wait_name, app, wait),
        Step(f"{app}:root-folder", app, root_folder, needs=(wait_name,)),
        Step(f"{app}:media-management", app, media_management, needs=(wait_name,)),
        client_step(
            "sabnzbd", "SABnzbd", "Sabnzbd", usenet,
            (wait_name, "sabnzbd:categories") if usenet else (wait_name,), sab_values,
        ),
        client_step(
            "qbittorrent", "qBittorrent", "QBittorrent", torrent,
            (wait_name, "qbittorrent:categories") if torrent else (wait_name,),
            qbittorrent_values,
        ),
        # A deselected media server loses its "(Orama)" notification and nothing else.
        client_step(
            "plex", "Plex", "PlexServer", plex,
            (wait_name, "plex:libraries") if plex else (wait_name,), plex_values,
            kind="notification",
        ),
        client_step(
            "jellyfin", "Jellyfin", "MediaBrowser", jellyfin,
            (wait_name, "jellyfin:api-key") if jellyfin else (wait_name,), jellyfin_values,
            kind="notification",
        ),
    ]
