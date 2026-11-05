"""Plex wiring: account sign-in (PIN), claiming the server, libraries, LAN and hardware.

Account calls go only to the fixed origin ``https://plex.tv`` (the shared Http never follows
redirects); the server is ``http://plex:32400`` on orama-net. The account token is sent only
in the ``X-Plex-Token`` header, never in a URL, a message or a log line.

Assumed from Plex's public API (owner-only, nothing in CI has a Plex account):
- ``POST /api/v2/pins?strong=true`` answers ``{id, code, expiresIn}``; ``GET /api/v2/pins/<id>``
  (same client id) gives ``authToken`` once the owner approves, 404 once the PIN is gone.
- ``GET /api/claim/token.json`` gives ``{token: "claim-…"}``; the server takes it as
  ``POST /myplex/claim?token=…`` (Plex's documented form; short-lived and single-use).
- ``GET /api/v2/resources`` lists the account's servers (``clientIdentifier``, ``owned``).
- ``GET /api/v2/user`` has ``subscription.active`` (Plex Pass).
Observed in CI (Plan 2b notes): ``/identity`` fields, library create with ``%20`` encoding,
the two hidden prefs answer 200 but cannot be read back. Orama therefore remembers what it
sent for those prefs, per Plex server (machine identifier), in the secret store.
"""

from __future__ import annotations

import ipaddress
import re
import time
import urllib.parse
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from orama.secrets import PLEX_CLIENT_ID_NAME, PLEX_TOKEN_NAME
from orama.setup_model import DesiredSetup
from orama.wiring.engine import Step, StepError, StepSkipped, wait_until
from orama.wiring.http import Http, Response, Unreachable

PLEX_TV = "https://plex.tv"
AUTH_APP = "https://app.plex.tv/auth#?"
BASE = "http://plex:32400"
PRODUCT = "Orama"
START_TIMEOUT = 300.0
NETWORK_SENT_MARKER = "plex:network:sent"
HARDWARE_SENT_MARKER = "plex:hardware:sent"
CLAIMED_MARKER = "plex:claimed:"  # + machine identifier: Orama claimed this server itself

WAITING_FOR_SIGN_IN = "Plex is waiting for sign-in — run `python -m orama.cli plex-sign-in`"
OTHER_ACCOUNT = "This Plex server belongs to another Plex account — Orama won't take it over."
NO_PASS = "Hardware transcoding needs Plex Pass — playback still works with direct play."
SIGNED_OUT = (
    "Plex no longer accepts Orama's sign-in — run `python -m orama.cli plex-sign-in` again."
)
UNREADABLE_TV = "plex.tv gave an answer Orama could not read — run apply again."
UNREADABLE = "Plex gave an answer Orama could not read — run apply again."

# name, Plex type, agent, scanner, folder (spec §4.3)
LIBRARIES = (
    ("Movies", "movie", "tv.plex.agents.movie", "Plex Movie", "/data/media/movies"),
    ("TV Shows", "show", "tv.plex.agents.series", "Plex TV Series", "/data/media/tv"),
)

# A token goes into a header: one short run of visible characters, nothing else.
_TOKEN = re.compile(r"[\x21-\x7e]{1,512}")
_PIN_CODE = re.compile(r"[A-Za-z0-9]{1,128}")
_MACHINE_ID = re.compile(r"[A-Za-z0-9-]{1,128}")


_SIGN_IN_MESSAGES = {
    "unreachable": "plex.tv could not be reached — check the internet connection and "
                   "try again.",
    "refused": "plex.tv refused the sign-in request — try again in a minute.",
    "unreadable": "plex.tv gave an answer Orama could not read — try again.",
}


class SignInError(Exception):
    """Sign-in could not start. ``message`` is always one of the fixed sentences above."""

    def __init__(self, kind: Literal["unreachable", "refused", "unreadable"]) -> None:
        self.message = _SIGN_IN_MESSAGES[kind]
        super().__init__(self.message)


@dataclass(frozen=True)
class SignIn:
    code: str
    url: str
    pin_id: int
    expires_at: float  # time.time() seconds


def _client_id(store: Any) -> str:
    value = store.get(PLEX_CLIENT_ID_NAME)
    if not value:
        value = str(uuid.uuid4())
        store.set(PLEX_CLIENT_ID_NAME, value)
    return value


def _tv_headers(store: Any, token: str | None = None) -> dict[str, str]:
    headers = {
        "Accept": "application/json",
        "X-Plex-Product": PRODUCT,
        "X-Plex-Client-Identifier": _client_id(store),
    }
    if token is not None:
        headers["X-Plex-Token"] = token
    return headers


def _json(resp: Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return None


def begin_sign_in(
    store: Any, http: Http, *, clock: Callable[[], float] | None = None
) -> SignIn:
    """Ask plex.tv for a sign-in PIN; the owner approves it in a browser."""
    headers = _tv_headers(store)
    try:
        resp = http.request("POST", f"{PLEX_TV}/api/v2/pins?strong=true", headers=headers)
    except Unreachable:
        raise SignInError("unreachable") from None
    if resp.status not in (200, 201):
        raise SignInError("refused")
    data = _json(resp)
    if not isinstance(data, dict):
        raise SignInError("unreadable")
    pin_id, code, expires = data.get("id"), data.get("code"), data.get("expiresIn", 900)
    if (
        type(pin_id) is not int
        or pin_id <= 0
        or not isinstance(code, str)
        or not _PIN_CODE.fullmatch(code)
        or type(expires) is not int
    ):
        raise SignInError("unreadable")
    query = urllib.parse.urlencode(
        {
            "clientID": headers["X-Plex-Client-Identifier"],
            "code": code,
            "context[device][product]": PRODUCT,
        },
        quote_via=urllib.parse.quote,
    )
    return SignIn(code=code, url=AUTH_APP + query, pin_id=pin_id,
                  expires_at=(clock or time.time)() + max(expires, 0))


def poll_sign_in(store: Any, http: Http, pin_id: int) -> Literal["waiting", "done", "expired"]:
    """Check the PIN once. On approval the token is stored; only the status comes back."""
    if type(pin_id) is not int or pin_id <= 0:
        raise ValueError("not a Plex PIN id")
    try:
        resp = http.request("GET", f"{PLEX_TV}/api/v2/pins/{pin_id}",
                            headers=_tv_headers(store))
    except Unreachable:
        return "waiting"  # a short network blip; the caller stops at the PIN's expiry
    if resp.status == 429:
        return "waiting"  # asked too often: slow down, the PIN is still good
    if 400 <= resp.status < 500:
        return "expired"  # gone, used up, or refused for this client: start again
    if resp.status != 200:
        return "waiting"
    data = _json(resp)
    if not isinstance(data, dict):
        return "waiting"
    token = data.get("authToken")
    if isinstance(token, str) and _TOKEN.fullmatch(token):
        store.set(PLEX_TOKEN_NAME, token)
        return "done"
    expires = data.get("expiresIn")
    if type(expires) is int and expires <= 0:
        return "expired"
    return "waiting"


def plex_steps(
    setup: DesiredSetup, store: Any, http: Http, *, timeout: float = START_TIMEOUT
) -> list[Step]:
    def token() -> str:
        value = store.get(PLEX_TOKEN_NAME)
        if not value:
            raise StepSkipped(WAITING_FOR_SIGN_IN)
        return value

    def tv(path: str) -> Any:
        try:
            resp = http.request("GET", f"{PLEX_TV}{path}", headers=_tv_headers(store, token()))
        except Unreachable:
            raise StepError(
                "plex.tv could not be reached — check the internet connection and run apply "
                "again."
            ) from None
        if resp.status in (401, 403):
            raise StepError(SIGNED_OUT)
        if resp.status != 200:
            raise StepError(f"plex.tv refused a request (HTTP {resp.status}) — run apply again.")
        return _json(resp)

    def local(method: str, path: str, *, signed: bool = True) -> Response:
        headers = {"Accept": "application/json"}
        if signed:
            headers["X-Plex-Token"] = token()
        try:
            resp = http.request(method, f"{BASE}{path}", headers=headers)
        except Unreachable:
            raise StepError("Plex could not be reached — run apply again.") from None
        if resp.status in (401, 403):
            raise StepError(SIGNED_OUT)
        if resp.status >= 300:
            raise StepError(f"Plex refused a request (HTTP {resp.status}) — run apply again.")
        return resp

    def identity() -> tuple[bool, str]:
        data = _json(local("GET", "/identity", signed=False))
        box = data.get("MediaContainer") if isinstance(data, dict) else None
        if not isinstance(box, dict):
            raise StepError(UNREADABLE)
        claimed, machine = box.get("claimed"), box.get("machineIdentifier")
        if not isinstance(claimed, bool) or not isinstance(machine, str) or not (
            _MACHINE_ID.fullmatch(machine)
        ):
            raise StepError(UNREADABLE)
        return claimed, machine

    def owned_by_this_account(machine: str) -> bool:
        data = tv("/api/v2/resources")
        if not isinstance(data, list):
            raise StepError(UNREADABLE_TV)
        return any(
            isinstance(r, dict) and r.get("clientIdentifier") == machine and r.get("owned") is True
            for r in data
        )

    def server_id() -> str:
        # plex:claim (a dependency) has checked the server is this account's; plex.tv may
        # take a while to list a server claimed a moment ago, so it is not asked again.
        return identity()[1]

    def wait() -> str | None:
        def ready() -> bool:
            resp = http.request("GET", f"{BASE}/identity", headers={"Accept": "application/json"})
            return resp.status == 200

        wait_until(ready, app="plex", timeout=timeout)
        return None

    def claim() -> str | None:
        token()  # skip plainly before any call when nobody has signed in
        claimed, machine = identity()
        if claimed:
            # plex.tv may not list a server claimed a moment ago: Orama's own claim counts too.
            if store.get(CLAIMED_MARKER + machine) == "1" or owned_by_this_account(machine):
                return None
            raise StepError(OTHER_ACCOUNT)
        data = tv("/api/claim/token.json")
        claim_token = data.get("token") if isinstance(data, dict) else None
        if not isinstance(claim_token, str) or not _TOKEN.fullmatch(claim_token) or not (
            claim_token.startswith("claim-")
        ):
            raise StepError(UNREADABLE_TV)
        try:
            # Plex's documented claim call takes the short-lived claim token as a parameter;
            # the account token itself is never put in a URL.
            resp = http.request("POST", f"{BASE}/myplex/claim",
                                headers={"Accept": "application/json"},
                                params={"token": claim_token})
        except Unreachable:
            raise StepError("Plex could not be reached — run apply again.") from None
        if resp.status >= 300:
            raise StepError("Plex did not accept the sign-in — run apply again.")
        store.set(CLAIMED_MARKER + machine, "1")
        return "signed this Plex server in to your Plex account"

    def libraries() -> str | None:
        server_id()  # readable and up before anything is sent
        data = _json(local("GET", "/library/sections"))
        box = data.get("MediaContainer") if isinstance(data, dict) else None
        if not isinstance(box, dict):
            raise StepError(UNREADABLE)
        have = set()
        for section in box.get("Directory") or []:
            if not isinstance(section, dict):
                continue
            for location in section.get("Location") or []:
                if isinstance(location, dict) and isinstance(location.get("path"), str):
                    have.add(location["path"].rstrip("/"))
        added = []
        for name, kind, agent, scanner, folder in LIBRARIES:
            if folder in have:  # found by folder: an owner's own name for it is kept
                continue
            query = urllib.parse.urlencode(
                {"name": name, "type": kind, "agent": agent, "scanner": scanner,
                 "language": "en-US", "location": folder},
                quote_via=urllib.parse.quote,  # %20, as observed; "+" was not tested
            )
            local("POST", f"/library/sections?{query}")
            added.append(name)
        if not added:
            return None
        if len(added) == 1:
            return f"added the {added[0]} library"
        return f"added the {' and '.join(added)} libraries"

    def set_pref(marker: str, server: str, key: str, value: str) -> bool:
        """Send one hidden pref unless this server already got this value from Orama."""
        sent = f"{server}:{value}"
        if store.get(marker) == sent:
            return False
        query = urllib.parse.urlencode({key: value}, quote_via=urllib.parse.quote, safe="/")
        local("PUT", f"/:/prefs?{query}")
        store.set(marker, sent)
        return True

    def network() -> str | None:
        server = server_id()
        if setup.lan_address is None:
            return None
        lan = str(ipaddress.ip_network(f"{setup.lan_address}/24", strict=False))
        if not set_pref(NETWORK_SENT_MARKER, server, "LanNetworksBandwidth", lan):
            return None
        return "treated your home network as local, so playback there is not limited"

    def hardware() -> str | None:
        server = server_id()
        if setup.render_gid is None and not setup.nvidia:
            return None  # no /dev/dri and no NVIDIA: nothing to turn on
        if store.get(HARDWARE_SENT_MARKER) == f"{server}:1":
            return None
        user = tv("/api/v2/user")
        subscription = user.get("subscription") if isinstance(user, dict) else None
        if not isinstance(subscription, dict):
            raise StepError(UNREADABLE_TV)
        if subscription.get("active") is not True:
            # Said once per server; checked again each apply, so buying Plex Pass later works.
            if store.get(HARDWARE_SENT_MARKER) == f"{server}:no-pass":
                return None
            store.set(HARDWARE_SENT_MARKER, f"{server}:no-pass")
            return NO_PASS
        set_pref(HARDWARE_SENT_MARKER, server, "HardwareAcceleratedCodecs", "1")
        return "turned on hardware transcoding"

    signed_in = ("wait:plex", "plex:claim")
    return [
        Step("wait:plex", "plex", wait),
        Step("plex:claim", "plex", claim, needs=("wait:plex",)),
        Step("plex:libraries", "plex", libraries, needs=signed_in),
        Step("plex:network", "plex", network, needs=signed_in),
        Step("plex:hardware", "plex", hardware, needs=signed_in),
    ]
