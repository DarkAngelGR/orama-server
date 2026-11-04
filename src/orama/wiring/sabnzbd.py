"""SABnzbd wiring: folders, categories and Usenet servers, over its query API."""

from __future__ import annotations

import hashlib
from typing import Any

from orama.setup_model import DesiredSetup, UsenetServer
from orama.wiring.engine import Step, StepError, wait_until
from orama.wiring.http import Http, Unreachable

BASE = "http://sabnzbd:8080/api"
COMPLETE_DIR = "/data/downloads/usenet"
INCOMPLETE_DIR = "/data/downloads/usenet/incomplete"
CATEGORIES = {"tv": "tv", "movies": "movies"}
START_TIMEOUT = 300.0


def sent_marker_name(slug: str) -> str:
    return f"usenet:{slug}:sent"


def _password_name(slug: str) -> str:
    return f"usenet:{slug}:password"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class _Api:
    def __init__(self, http: Http, apikey: str) -> None:
        self.http = http
        self.apikey = apikey

    def call(self, **params: Any) -> Any:
        query = {"apikey": self.apikey, "output": "json", **params}
        try:
            resp = self.http.request("POST", BASE, form=query)
        except Unreachable:
            raise StepError("SABnzbd could not be reached — run apply again.") from None
        if resp.status != 200:
            raise StepError(f"SABnzbd refused a request (HTTP {resp.status}) — run apply again.")
        try:
            data = resp.json()
        except ValueError:
            raise StepError("SABnzbd gave an answer Orama could not read.") from None
        if isinstance(data, dict) and data.get("status") is False:
            raise StepError("SABnzbd did not accept Orama's API key — run apply again.")
        return data

    def config(self, section: str) -> Any:
        # SABnzbd 5 leaves a list section out until something has been added to it.
        found = self.call(mode="get_config", section=section)["config"].get(section)
        return [] if found is None and section in ("categories", "servers") else found


def _norm_dir(value: object) -> str:
    return str(value or "").rstrip("/")


def _server_values(server: UsenetServer, password: str) -> dict[str, Any]:
    return {
        "host": server.host,
        "port": server.port,
        "ssl": int(server.ssl),
        "connections": server.connections,
        "username": server.username,
        "password": password,
    }


def sabnzbd_steps(setup: DesiredSetup, store: Any, http: Http) -> list[Step]:
    def api() -> _Api:
        key = store.get("apikey:sabnzbd")
        if key is None:
            raise StepError("SABnzbd's API key is missing — run install again.")
        return _Api(http, key)

    def wait() -> str | None:
        client = api()

        def ready() -> bool:
            return http.request("POST", BASE, form={"mode": "version", "apikey": client.apikey,
                                                     "output": "json"}).status == 200

        wait_until(ready, app="sabnzbd", timeout=START_TIMEOUT)
        return None

    def folders() -> str | None:
        client = api()
        misc = client.config("misc")
        changed = []
        for keyword, want in (("complete_dir", COMPLETE_DIR), ("download_dir", INCOMPLETE_DIR)):
            if _norm_dir(misc.get(keyword)) != want:
                client.call(mode="set_config", section="misc", keyword=keyword, value=want)
                changed.append(keyword)
        return "set the download folders" if changed else None

    def categories() -> str | None:
        client = api()
        # A fresh SABnzbd 5 creates its default categories (tv and movies among them, with
        # no folder) the first time the list is read, replacing any set before. List them
        # once first, so the folders set below stay.
        client.call(mode="get_cats")
        have = {c["name"]: c for c in client.config("categories")}
        added = []
        for name, directory in CATEGORIES.items():
            current = have.get(name)
            if current is not None and _norm_dir(current.get("dir")) == directory:
                continue
            client.call(mode="set_config", section="categories", name=name, dir=directory)
            added.append(name)
        return f"set up the {' and '.join(added)} categories" if added else None

    def server_step(server: UsenetServer) -> Step:
        def run() -> str | None:
            client = api()
            password = store.get(_password_name(server.slug))
            if password is None:
                raise StepError(
                    f"No password is saved for the Usenet server {server.slug} — save it with "
                    f"set-secret {_password_name(server.slug)}."
                )
            want = _server_values(server, password)
            have = next((s for s in client.config("servers") if s.get("name") == server.slug), None)
            password_sent = store.get(sent_marker_name(server.slug)) == _digest(password)
            if (
                have is not None
                and password_sent
                and all(
                    str(have.get(k, "")) == str(v)
                    for k, v in want.items()
                    if k != "password"
                )
            ):
                return None
            client.call(mode="set_config", section="servers", name=server.slug, **want)
            store.set(sent_marker_name(server.slug), _digest(password))
            return f"set up the Usenet server {server.slug}"

        return Step(f"sabnzbd:server:{server.slug}", "sabnzbd", run, needs=("wait:sabnzbd",))

    def test_step(server: UsenetServer) -> Step:
        def run() -> str | None:
            client = api()
            password = store.get(_password_name(server.slug))
            if password is None:
                raise StepError(
                    f"No password is saved for the Usenet server {server.slug} — save it with "
                    f"set-secret {_password_name(server.slug)}."
                )
            result = client.call(
                mode="config", name="test_server", server=server.slug,
                **_server_values(server, password),
            )
            value = result.get("value") if isinstance(result, dict) else None
            if not isinstance(value, dict) or value.get("result") is not True:
                raise StepError(
                    f"SABnzbd could not log in to {server.host} — check the username and password."
                )
            return None

        return Step(
            f"sabnzbd:test:{server.slug}", "sabnzbd", run,
            needs=(f"sabnzbd:server:{server.slug}",),
        )

    steps = [
        Step("wait:sabnzbd", "sabnzbd", wait),
        Step("sabnzbd:folders", "sabnzbd", folders, needs=("wait:sabnzbd",)),
        Step("sabnzbd:categories", "sabnzbd", categories, needs=("wait:sabnzbd",)),
    ]
    for server in setup.usenet_servers:
        steps.append(server_step(server))
        steps.append(test_step(server))
    return steps
