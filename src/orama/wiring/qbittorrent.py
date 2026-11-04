"""qBittorrent wiring: download folder and categories, over the WebUI API v2."""

from __future__ import annotations

import json
from typing import Any

from orama.catalog import QBITTORRENT_WEBUI_USER
from orama.setup_model import DesiredSetup
from orama.wiring.engine import Step, StepError, wait_until
from orama.wiring.http import Http, Response, Unreachable

PORT = 8090
SAVE_PATH = "/data/downloads/torrents"
CATEGORIES = {"tv": "/data/downloads/torrents/tv", "movies": "/data/downloads/torrents/movies"}
START_TIMEOUT = 300.0
_REFUSED = "qBittorrent refused Orama's login — run apply again; if it keeps failing, see the Orama logs."


def _norm(value: object) -> str:
    return str(value or "").rstrip("/")


def qbittorrent_steps(setup: DesiredSetup, store: Any, http: Http) -> list[Step]:
    host = "gluetun" if setup.vpn is not None else "qbittorrent"
    base = f"http://{host}:{PORT}/api/v2"

    def call(method: str, path: str, **kw: Any) -> Response:
        try:
            resp = http.request(method, f"{base}/{path}", **kw)
        except Unreachable:
            raise StepError("qBittorrent could not be reached — run apply again.") from None
        if resp.status not in (200, 204):
            raise StepError(f"qBittorrent refused a request (HTTP {resp.status}) — run apply again.")
        return resp

    def wait() -> str | None:
        password = store.get("password:qbittorrent")
        if password is None:
            raise StepError("The password is missing — run apply again.")

        def login() -> bool:
            resp = http.request(
                "POST", f"{base}/auth/login",
                form={"username": QBITTORRENT_WEBUI_USER, "password": password},
            )
            # qBittorrent 5 answers 204 with no body; older versions 200 "Ok.".
            body = resp.body.decode(errors="replace").strip()
            if not (resp.status == 204 or (resp.status == 200 and body == "Ok.")):
                raise StepError(_REFUSED)
            return True

        wait_until(login, app="qbittorrent", timeout=START_TIMEOUT)
        return None

    def prefs() -> str | None:
        have = call("GET", "app/preferences").json()
        change: dict[str, Any] = {}
        if _norm(have.get("save_path")) != SAVE_PATH:
            change["save_path"] = SAVE_PATH
        if have.get("temp_path_enabled") is not False:
            change["temp_path_enabled"] = False
        if not change:
            return None
        call("POST", "app/setPreferences", form={"json": json.dumps(change)})
        return "set the download folder"

    def categories() -> str | None:
        have = call("GET", "torrents/categories").json()
        done = []
        for name, path in CATEGORIES.items():
            current = have.get(name)
            if current is None:
                call("POST", "torrents/createCategory", form={"category": name, "savePath": path})
            elif _norm(current.get("savePath")) != path:
                call("POST", "torrents/editCategory", form={"category": name, "savePath": path})
            else:
                continue
            done.append(name)
        return f"set up the {' and '.join(done)} categories" if done else None

    return [
        Step("wait:qbittorrent", "qbittorrent", wait),
        Step("qbittorrent:prefs", "qbittorrent", prefs, needs=("wait:qbittorrent",)),
        Step("qbittorrent:categories", "qbittorrent", categories, needs=("wait:qbittorrent",)),
    ]
