"""Bazarr wiring: the Sonarr/Radarr links, subtitle languages, providers and file format.

Bazarr keeps everything in one settings object. It is read with GET and written with a
form POST of `settings-<section>-<field>` fields (lists repeat the field); a change applies
at once. Orama only writes the fields below, and only when they differ.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from orama.setup_model import DesiredSetup, enabled_apps
from orama.wiring.engine import Step, StepError, wait_until
from orama.wiring.http import Http, Response, Unreachable

BASE = "http://bazarr:6767/api/system"
START_TIMEOUT = 300.0
PROFILE_NAME = "Orama"
ARRS = {"sonarr": 8989, "radarr": 7878}
# Providers that need no account. Assumed from Bazarr's settings (they have no credential
# section), not tested for Greek results: an owner check in the Plan 2b notes.
NO_ACCOUNT_PROVIDERS = (
    "podnapisi",
    "yifysubtitles",
    "tvsubtitles",
    "subf2m",
    "supersubtitles",
    "subtitlecat",
    "embeddedsubtitles",
)
OPENSUBTITLES = "opensubtitlescom"
IGNORED_SUBTITLE_SWITCHES = ("ignore_ass_subs", "ignore_pgs_subs", "ignore_vobsub_subs")
OPENSUBTITLES_SENT_MARKER = "bazarr:opensubtitles:sent"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _text(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value if value is not None else "")


def _same(have: object, want: object) -> bool:
    return _text(have).lower() == _text(want).lower()


def _same_path(have: object, want: object) -> bool:
    return _text(have).strip("/") == _text(want).strip("/")


def bazarr_steps(setup: DesiredSetup, store: Any, http: Http) -> list[Step]:
    present = set(enabled_apps(setup))

    def key() -> str:
        value = store.get("apikey:bazarr")
        if value is None:
            raise StepError("Bazarr's API key is missing — run apply again.")
        return value

    def call(method: str, path: str, *, form: dict[str, Any] | None = None) -> Response:
        try:
            resp = http.request(
                method, f"{BASE}/{path}", headers={"X-API-KEY": key()}, form=form
            )
        except Unreachable:
            raise StepError("Bazarr could not be reached — run apply again.") from None
        if resp.status in (401, 403):
            raise StepError("Bazarr did not accept Orama's API key — run apply again.")
        if resp.status >= 300:
            raise StepError(f"Bazarr refused a request (HTTP {resp.status}) — run apply again.")
        return resp

    def read(path: str) -> Any:
        try:
            return call("GET", path).json()
        except ValueError:
            raise StepError("Bazarr gave an answer Orama could not read.") from None

    def settings() -> dict[str, Any]:
        data = read("settings")
        if not isinstance(data, dict):
            raise StepError("Bazarr gave an answer Orama could not read.")
        return data

    def section(data: dict[str, Any], name: str) -> dict[str, Any]:
        found = data.get(name)
        return found if isinstance(found, dict) else {}

    def write(form: dict[str, Any]) -> None:
        call("POST", "settings", form=form)

    def wait() -> str | None:
        key()  # fail early and plainly when the key is missing

        def ready() -> bool:
            resp = http.request("GET", f"{BASE}/status", headers={"X-API-KEY": key()})
            if resp.status in (401, 403):
                raise StepError("Bazarr did not accept Orama's API key — run apply again.")
            return resp.status == 200

        wait_until(ready, app="bazarr", timeout=START_TIMEOUT)
        return None

    def apps() -> str | None:
        data = settings()
        general = section(data, "general")
        form: dict[str, Any] = {}
        linked: list[str] = []
        unlinked: list[str] = []
        for app, port in ARRS.items():
            link = section(data, app)
            before = len(form)
            if app in present:
                arr_key = store.get(f"apikey:{app}")
                if arr_key is None:
                    raise StepError(f"The {app.title()} API key is missing — run apply again.")
                wanted = {"ip": app, "port": port, "base_url": "/", "apikey": arr_key,
                          "ssl": False}
                if not _same(general.get(f"use_{app}"), True):
                    form[f"settings-general-use_{app}"] = "true"
                for field, value in wanted.items():
                    same = _same_path if field == "base_url" else _same
                    if not same(link.get(field), value):
                        form[f"settings-{app}-{field}"] = _text(value)
                if len(form) > before:
                    linked.append(app.title())
            elif _same(general.get(f"use_{app}"), True):
                form[f"settings-general-use_{app}"] = "false"
                unlinked.append(app.title())
        if not form:
            return None
        write(form)
        said = []
        if linked:
            said.append(f"linked Bazarr to {' and '.join(linked)}")
        if unlinked:
            said.append(f"unlinked Bazarr from {' and '.join(unlinked)}")
        return ", ".join(said)

    def languages() -> str | None:
        wanted = list(setup.subtitle_languages)
        form: dict[str, Any] = {}
        langs = read("languages")
        enabled = [l["code2"] for l in langs if l.get("enabled")]
        if not set(wanted) <= set(enabled):
            # Only ever add: languages the owner enabled themselves stay enabled.
            form["languages-enabled"] = enabled + [c for c in wanted if c not in enabled]

        profiles = read("languages/profiles")
        mine = next((p for p in profiles if p.get("name") == PROFILE_NAME), None)
        items = [
            {"id": n, "language": code, "audio_exclude": "False", "hi": "False", "forced": "False"}
            for n, code in enumerate(wanted, 1)
        ]
        current = (
            [(i.get("language"), _text(i.get("hi")).lower(), _text(i.get("forced")).lower())
             for i in mine.get("items", [])]
            if mine
            else None
        )
        if current != [(c, "false", "false") for c in wanted]:
            if mine is None:
                ids = [int(p["profileId"]) for p in profiles if str(p.get("profileId")).isdigit()]
                mine = {"profileId": max(ids, default=0) + 1, "name": PROFILE_NAME,
                        "cutoff": None, "mustContain": [], "mustNotContain": [],
                        "originalFormat": None, "tag": None}
            mine = {**mine, "items": items, "cutoff": None}
            others = [p for p in profiles if p.get("name") != PROFILE_NAME]
            at = next((n for n, p in enumerate(profiles) if p.get("name") == PROFILE_NAME), None)
            result = [*others, mine] if at is None else [*others[:at], mine, *others[at:]]
            form["languages-profiles"] = json.dumps(result)
        general = section(settings(), "general")
        for prefix in ("serie", "movie"):
            if not _same(general.get(f"{prefix}_default_enabled"), True):
                form[f"settings-general-{prefix}_default_enabled"] = "true"
            if not _same(general.get(f"{prefix}_default_profile"), mine["profileId"]):
                form[f"settings-general-{prefix}_default_profile"] = _text(mine["profileId"])
        if not form:
            return None
        write(form)
        return "set the subtitle languages"

    def providers() -> str | None:
        data = settings()
        general = section(data, "general")
        have = [str(p) for p in general.get("enabled_providers") or []]
        wanted = list(NO_ACCOUNT_PROVIDERS)
        form: dict[str, Any] = {}
        username = store.get("opensubtitles:username")
        password = store.get("opensubtitles:password")
        if username and password:
            wanted.append(OPENSUBTITLES)
            account = section(data, OPENSUBTITLES)
            sent = store.get(OPENSUBTITLES_SENT_MARKER) == _digest(username + "\0" + password)
            if not (sent and _same(account.get("username"), username)):
                form[f"settings-{OPENSUBTITLES}-username"] = username
                form[f"settings-{OPENSUBTITLES}-password"] = password
        # Only ever add: providers the owner turned on themselves stay on.
        missing = [p for p in wanted if p not in have]
        if missing:
            form["settings-general-enabled_providers"] = have + missing
        if not form:
            return None
        write(form)
        if f"settings-{OPENSUBTITLES}-password" in form:
            store.set(OPENSUBTITLES_SENT_MARKER, _digest(username + "\0" + password))
        return "turned on the subtitle providers"

    def subtitle_format() -> str | None:
        # Bazarr has no "prefer SRT" switch; it saves SRT by default. The closest settings
        # skip styled (ASS) and image (PGS, VobSub) subtitles, which some players cannot show.
        general = section(settings(), "general")
        form = {
            f"settings-general-{field}": "true"
            for field in IGNORED_SUBTITLE_SWITCHES
            if not _same(general.get(field), True)
        }
        if not form:
            return None
        write(form)
        return "skipped styled and image subtitles so plain SRT files are used"

    needs = ("wait:bazarr",)
    return [
        Step("wait:bazarr", "bazarr", wait),
        Step(
            "bazarr:apps", "bazarr", apps,
            needs=needs + tuple(f"wait:{a}" for a in ARRS if a in present),
        ),
        Step("bazarr:languages", "bazarr", languages, needs=needs),
        Step("bazarr:providers", "bazarr", providers, needs=needs),
        Step("bazarr:format", "bazarr", subtitle_format, needs=needs),
    ]
