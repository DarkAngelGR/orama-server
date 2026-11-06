"""SABnzbd wiring: folders, categories and Usenet servers, over its query API."""

from __future__ import annotations

import hashlib
from typing import Any

from orama.setup_model import DesiredSetup, UsenetServer
from orama.wiring.engine import Step, StepError, run_steps, wait_until
from orama.wiring.http import Http, Unreachable

REMOVE_PREFIX = "remove:usenet:"
BASE = "http://sabnzbd:8080/api"
COMPLETE_DIR = "/data/downloads/usenet"
INCOMPLETE_DIR = "/data/downloads/usenet/incomplete"
CATEGORIES = {"tv": "tv", "movies": "movies"}
START_TIMEOUT = 300.0
# Written as the sent marker before a server is created. If SABnzbd saves it but the answer
# is lost (a timeout, or Orama stopped in between), the next run finds the server with this
# marker and treats it as Orama's: it sends the settings again and records the real marker.
PENDING_MARKER = "pending"


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
            raise StepError("The API key is missing — run apply again.")
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
                    f"No password is saved for the Usenet server {server.slug} — "
                    "enter it in Settings, then run apply again."
                )
            want = _server_values(server, password)
            have = next((s for s in client.config("servers") if s.get("name") == server.slug), None)
            sent = store.get(sent_marker_name(server.slug))
            if have is not None and sent is None:
                raise StepError(
                    f"SABnzbd already has a server named {server.slug} that Orama did not set up."
                )
            password_sent = sent == _digest(password)
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
            if have is None:
                store.set(sent_marker_name(server.slug), PENDING_MARKER)
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
                    f"No password is saved for the Usenet server {server.slug} — "
                    "enter it in Settings, then run apply again."
                )
            result = client.call(
                mode="config", name="test_server", server=server.slug,
                **_server_values(server, password),
            )
            value = result.get("value") if isinstance(result, dict) else None
            if not isinstance(value, dict) or value.get("result") is not True:
                raise StepError(
                    f"SABnzbd could not connect or log in to {server.host} — check the server "
                    "address, port, username and password."
                )
            return None

        return Step(
            f"sabnzbd:test:{server.slug}", "sabnzbd", run,
            needs=(f"sabnzbd:server:{server.slug}",),
        )

    def remove_step(slug: str) -> Step:
        def run() -> str | None:
            client = api()
            message = None
            if any(s.get("name") == slug for s in client.config("servers")):
                client.call(mode="del_config", section="servers", keyword=slug)
                message = f"removed the Usenet server {slug}"
            # only now, after the delete worked (or it was already gone)
            store.delete(sent_marker_name(slug))
            store.delete(_password_name(slug))
            store.delete(f"test-ok:usenet:{slug}")
            store.delete(f"{REMOVE_PREFIX}{slug}")
            return message

        return Step(f"sabnzbd:remove:{slug}", "sabnzbd", run, needs=("wait:sabnzbd",))

    steps = [
        Step("wait:sabnzbd", "sabnzbd", wait),
        Step("sabnzbd:folders", "sabnzbd", folders, needs=("wait:sabnzbd",)),
        Step("sabnzbd:categories", "sabnzbd", categories, needs=("wait:sabnzbd",)),
    ]
    for entry in sorted(store.names()):
        if entry.startswith(REMOVE_PREFIX):
            slug = store.get(entry)
            if slug and not any(server.slug == slug for server in setup.usenet_servers):
                steps.append(remove_step(slug))
    for server in setup.usenet_servers:
        steps.append(server_step(server))
        steps.append(test_step(server))
    return steps


def test_usenet_server(setup: DesiredSetup, store: Any, http: Http, slug: str) -> str | None:
    """Test one Usenet server from the setup, running nothing else.

    Returns None when it passed (and records ``test-ok:usenet:<slug>``), else the plain
    message of the step that failed or was skipped (and clears the record).
    """
    wanted = ("wait:sabnzbd", f"sabnzbd:server:{slug}", f"sabnzbd:test:{slug}")
    steps = [s for s in sabnzbd_steps(setup, store, http) if s.name in wanted]
    message: str | None = None
    if len(steps) < 3:
        message = "Orama does not know this Usenet server yet — save the settings first."
    else:
        for result in run_steps(steps):
            if result.status in ("failed", "skipped"):
                message = result.message
                break
    ok_name = f"test-ok:usenet:{slug}"
    if message is None:
        store.set(ok_name, "1")
    else:
        store.delete(ok_name)
    return message


test_usenet_server.__test__ = False  # type: ignore[attr-defined]  # not a pytest test
