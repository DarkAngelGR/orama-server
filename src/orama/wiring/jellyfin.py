"""Jellyfin wiring: first-run wizard, admin sign-in, API key, libraries and hardware.

Endpoints and field names are the ones observed in CI (Plan 2b notes, "Jellyfin 12.4"):
``/Startup/*`` answers 401 once the wizard is complete; ``/Users/AuthenticateByName`` takes
``{Username, Pw}`` and an ``Authorization: MediaBrowser ...`` header; the same header with
``Token="..."`` carries the session token or an API key (``X-Emby-Token`` alone gave 401).
Turning on a hardware type against a real GPU is assumed (no GPU in CI): owner check.

The admin password, the API key and the session token are only ever sent in a request body
or the ``Authorization`` header, never in a URL, a message or a log line. The session token
is kept in memory for the length of one apply.
"""

from __future__ import annotations

import urllib.parse
from typing import Any

from orama.secrets import JELLYFIN_KEY_NAME, JELLYFIN_PASSWORD_NAME
from orama.setup_model import DesiredSetup
from orama.wiring.engine import Step, StepError, wait_until
from orama.wiring.http import Http, Response, Unreachable

BASE = "http://jellyfin:8096"
START_TIMEOUT = 300.0
ADMIN_USER = "orama-admin"
KEY_APP = "Orama"
VAAPI_DEVICE = "/dev/dri/renderD128"

WRONG_PASSWORD = (
    "Jellyfin already has an admin account that Orama can't sign in to. Orama won't reset "
    "it — restore its saved Jellyfin password, or remove Jellyfin's config folder and run "
    "apply again."
)
UNREADABLE = "Jellyfin gave an answer Orama could not read — run apply again."
NOT_ACCEPTED = "Jellyfin did not accept Orama's sign-in — run apply again."

# name, Jellyfin collection type, folder (spec §4.3)
LIBRARIES = (
    ("Movies", "movies", "/data/media/movies"),
    ("TV Shows", "tvshows", "/data/media/tv"),
)

_CLIENT = 'Client="Orama", Device="Orama", DeviceId="orama", Version="1"'


def _json(resp: Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return None


def jellyfin_steps(
    setup: DesiredSetup, store: Any, http: Http, *, timeout: float = START_TIMEOUT
) -> list[Step]:
    session: dict[str, str] = {}  # the sign-in token, in memory only

    def call(
        method: str, path: str, *, json: Any = None, token: str | None = None,
        query: str = "", headers: dict[str, str] | None = None,
    ) -> Response:
        sent = {"Accept": "application/json", **(headers or {})}
        if token is not None:
            sent["Authorization"] = f'MediaBrowser {_CLIENT}, Token="{token}"'
        try:
            resp = http.request(method, f"{BASE}{path}{query}", headers=sent, json=json)
        except Unreachable:
            raise StepError("Jellyfin could not be reached — run apply again.") from None
        if resp.status in (401, 403):
            raise StepError(NOT_ACCEPTED)
        if resp.status >= 300:
            raise StepError(f"Jellyfin refused a request (HTTP {resp.status}) — run apply again.")
        return resp

    def signed(method: str, path: str, **kw: Any) -> Response:
        return call(method, path, token=session["token"], **kw)

    def read(resp: Response) -> Any:
        data = _json(resp)
        if data is None:
            raise StepError(UNREADABLE)
        return data

    def password() -> str:
        value = store.get(JELLYFIN_PASSWORD_NAME)
        if not value:
            raise StepError("Jellyfin's admin password is missing — run apply again.")
        return value

    def wait() -> str | None:
        def ready() -> bool:
            resp = http.request("GET", f"{BASE}/System/Info/Public")
            # A starting server can answer 200 before it knows whether setup is done.
            data = _json(resp) if resp.status == 200 else None
            return isinstance(data, dict) and isinstance(data.get("StartupWizardCompleted"), bool)

        wait_until(ready, app="jellyfin", timeout=timeout)
        return None

    def startup() -> str | None:
        info = read(call("GET", "/System/Info/Public"))
        done = info.get("StartupWizardCompleted") if isinstance(info, dict) else None
        if not isinstance(done, bool):
            raise StepError(UNREADABLE)
        if done:
            return None
        secret = password()  # fail before touching the wizard
        config = read(call("GET", "/Startup/Configuration"))
        call("POST", "/Startup/Configuration", json=config)
        user = read(call("GET", "/Startup/User"))
        # A retry after a failed finish finds the admin already made: leave it alone.
        if not (isinstance(user, dict) and user.get("Name") == ADMIN_USER):
            call("POST", "/Startup/User", json={"Name": ADMIN_USER, "Password": secret})
        call("POST", "/Startup/Complete", json={})
        return "finished Jellyfin's first-run setup and made its admin account"

    def auth() -> str | None:
        secret = password()
        try:
            resp = http.request(
                "POST", f"{BASE}/Users/AuthenticateByName",
                headers={"Accept": "application/json", "Authorization": f"MediaBrowser {_CLIENT}"},
                json={"Username": ADMIN_USER, "Pw": secret},
            )
        except Unreachable:
            raise StepError("Jellyfin could not be reached — run apply again.") from None
        if resp.status in (400, 401, 403):
            raise StepError(WRONG_PASSWORD)
        if resp.status != 200:
            raise StepError(f"Jellyfin refused the sign-in (HTTP {resp.status}) — run apply again.")
        data = _json(resp)
        token = data.get("AccessToken") if isinstance(data, dict) else None
        if not isinstance(token, str) or not token:
            raise StepError(UNREADABLE)
        session["token"] = token
        return None

    def keys() -> list[dict[str, Any]]:
        data = read(signed("GET", "/Auth/Keys"))
        items = data.get("Items") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise StepError(UNREADABLE)
        return [i for i in items if isinstance(i, dict)]

    def api_key() -> str | None:
        have = keys()
        stored = store.get(JELLYFIN_KEY_NAME)
        if stored and any(i.get("AccessToken") == stored for i in have):
            return None
        before = {i.get("AccessToken") for i in have}
        signed("POST", "/Auth/Keys", query="?" + urllib.parse.urlencode({"app": KEY_APP}))
        new = [
            i.get("AccessToken") for i in keys()
            if i.get("AppName") == KEY_APP and i.get("AccessToken") not in before
        ]
        if len(new) != 1 or not isinstance(new[0], str) or not new[0]:
            raise StepError(UNREADABLE)
        store.set(JELLYFIN_KEY_NAME, new[0])
        return "made an API key for Orama"

    def libraries() -> str | None:
        data = read(signed("GET", "/Library/VirtualFolders"))
        if not isinstance(data, list):
            raise StepError(UNREADABLE)
        have = set()
        for folder in data:
            for location in (folder.get("Locations") if isinstance(folder, dict) else None) or []:
                if isinstance(location, str):
                    have.add(location.rstrip("/"))
        added = []
        for name, kind, folder in LIBRARIES:
            if folder in have:  # found by folder: an owner's own name for it is kept
                continue
            query = urllib.parse.urlencode(
                {"name": name, "collectionType": kind, "refreshLibrary": "false"},
                quote_via=urllib.parse.quote,
            )
            signed("POST", "/Library/VirtualFolders", query=f"?{query}",
                   json={"LibraryOptions": {"PathInfos": [{"Path": folder}]}})
            added.append(name)
        if not added:
            return None
        if len(added) == 1:
            return f"added the {added[0]} library"
        return f"added the {' and '.join(added)} libraries"

    def hardware() -> str | None:
        if setup.nvidia:
            want = "nvenc"
        elif setup.render_gid is not None:
            want = "vaapi"
        else:
            return None  # no /dev/dri and no NVIDIA: nothing to turn on
        config = read(signed("GET", "/System/Configuration/encoding"))
        if not isinstance(config, dict):
            raise StepError(UNREADABLE)
        current = config.get("HardwareAccelerationType")
        if current not in (None, "", "none"):
            return None  # already on, or the owner chose a type: leave it
        config["HardwareAccelerationType"] = want
        config["EnableHardwareEncoding"] = True
        if want == "vaapi":
            config["VaapiDevice"] = VAAPI_DEVICE
        signed("POST", "/System/Configuration/encoding", json=config)
        return "turned on hardware transcoding"

    signed_in = ("wait:jellyfin", "jellyfin:startup", "jellyfin:auth")
    return [
        Step("wait:jellyfin", "jellyfin", wait),
        Step("jellyfin:startup", "jellyfin", startup, needs=("wait:jellyfin",)),
        Step("jellyfin:auth", "jellyfin", auth, needs=("wait:jellyfin", "jellyfin:startup")),
        Step("jellyfin:api-key", "jellyfin", api_key, needs=signed_in),
        Step("jellyfin:libraries", "jellyfin", libraries, needs=signed_in),
        Step("jellyfin:hardware", "jellyfin", hardware, needs=signed_in),
    ]
