"""Prowlarr wiring: applications (Sonarr, Radarr), the VPN proxy, indexers and sync.

Everything Orama creates is named with "(Orama)" or is an indexer from the setup. Only
those are ever changed; anything else the user added in Prowlarr is left alone.
Prowlarr masks secret fields when it reads them back, so what was last sent is kept as
a SHA-256 marker in the secret store, the same way the Sonarr/Radarr wiring does.
"""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

from orama.catalog import GLUETUN_PROXY_USER
from orama.secrets import GLUETUN_PROXY_PASSWORD_NAME
from orama.setup_model import DesiredSetup, IndexerChoice, enabled_apps
from orama.wiring.arr import MASK, SPECS
from orama.wiring.engine import Step, StepError, display_name, wait_until
from orama.wiring.http import Http, Response, Unreachable

START_TIMEOUT = 600.0
BASE = "http://prowlarr:9696/api/v1"
PROWLARR_URL = "http://prowlarr:9696"
PROXY_HOST = "gluetun"
PROXY_PORT = 8888
VPN_TAG = "vpn"
SYNC_PENDING = "prowlarr:sync:pending"
_NAME = "Prowlarr"


def sent_marker_name(kind: str) -> str:
    return f"prowlarr:{kind}:sent"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _is_definition(schema: dict[str, Any], definition: str) -> bool:
    """Does this indexer template match the chosen definition?

    Prowlarr 2.x names the generic ones "Newznab" / "Torznab" (capital letter) in
    ``definitionName``, and lists every named Newznab site (NZBgeek, ...) under the same
    ``definitionName``; only the one called "Generic ..." is the plain protocol.
    """
    if str(schema.get("definitionName", "")).lower() != definition.lower():
        return False
    if schema.get("implementation") in ("Newznab", "Torznab"):
        return str(schema.get("name", "")).startswith("Generic ")
    return True


def prowlarr_steps(setup: DesiredSetup, store: Any, http: Http) -> list[Step]:
    apps = enabled_apps(setup)
    if "prowlarr" not in apps:
        return []

    def key() -> str:
        value = store.get("apikey:prowlarr")
        if value is None:
            raise StepError("Prowlarr's API key is missing — run install again.")
        return value

    def call(
        method: str, path: str, *, json: Any = None, params: dict[str, Any] | None = None,
        accept_failure: bool = False,
    ) -> Response:
        try:
            resp = http.request(
                method, f"{BASE}/{path}", headers={"X-Api-Key": key()}, json=json, params=params
            )
        except Unreachable:
            raise StepError("Prowlarr could not be reached — run apply again.") from None
        if resp.status in (401, 403):
            raise StepError("Prowlarr did not accept Orama's API key — run apply again.")
        if resp.status >= 300 and not accept_failure:
            raise StepError(f"Prowlarr refused a request (HTTP {resp.status}) — run apply again.")
        return resp

    def read(path: str) -> Any:
        try:
            return call("GET", path).json()
        except ValueError:
            raise StepError("Prowlarr gave an answer Orama could not read.") from None

    def mark_pending() -> None:
        store.set(SYNC_PENDING, "1")

    def wait() -> str | None:
        key()

        def ready() -> bool:
            try:
                resp = http.request(
                    "GET", f"{BASE}/system/status", headers={"X-Api-Key": key()}
                )
            except Unreachable:
                return False
            if resp.status in (401, 403):
                raise StepError("Prowlarr did not accept Orama's API key — run apply again.")
            return resp.status == 200

        wait_until(ready, app="prowlarr", timeout=START_TIMEOUT)
        return None

    def vpn_tag_id() -> int:
        for tag in read("tag"):
            if str(tag.get("label", "")).lower() == VPN_TAG:
                return int(tag["id"])
        created = call("POST", "tag", json={"label": VPN_TAG}).json()
        return int(created["id"])

    def upsert(
        *, resource: str, label: str, name: str, pick: Any, want: dict[str, Any],
        secret: str, secret_names: frozenset[str], marker: str, top: dict[str, Any],
        tag: int | None, base_item: Any = None,
    ) -> tuple[dict[str, Any], str | None, bool]:
        """Create or fix one item. Returns (item as sent, change message or None, existing)."""
        items = read(resource)
        have = next((i for i in items if i.get("name") == name), None)
        if have is None:
            template = next((s for s in read(f"{resource}/schema") if pick(s)), None)
            if template is None:
                raise StepError(
                    f"{_NAME} does not offer {label} — it may be a different version than Orama expects."
                )
            item = copy.deepcopy(template)
            item["name"] = name
            item.update(top)
            if base_item is not None:
                base_item(item)
            if tag is not None:
                item["tags"] = sorted({*item.get("tags", []), tag})
            _apply_fields(item, want, label)
            mark_pending()
            call("POST", resource, json=item, params={"forceSave": "true"})
            store.set(marker, _digest(secret))
            return item, f"added {label}", False
        current = {f.get("name"): f.get("value") for f in have.get("fields", [])}
        stale = store.get(marker) != _digest(secret)
        stale = stale or any(have.get(k) != v for k, v in top.items())
        stale = stale or (tag is not None and tag not in have.get("tags", []))
        for field, value in want.items():
            shown = current.get(field)
            if field in secret_names:
                if shown not in (None, MASK) and str(shown) != str(value):
                    stale = True
            elif str(shown) != str(value):
                stale = True
        if not stale:
            return have, None, True
        item = copy.deepcopy(have)
        item.update(top)
        if tag is not None:
            item["tags"] = sorted({*item.get("tags", []), tag})
        _apply_fields(item, want, label)
        mark_pending()
        call("PUT", f"{resource}/{have['id']}", json=item, params={"forceSave": "true"})
        store.set(marker, _digest(secret))
        return item, f"restored {label}", True

    def _apply_fields(item: dict[str, Any], want: dict[str, Any], label: str) -> None:
        present = {f.get("name"): f for f in item.get("fields", [])}
        for field, value in want.items():
            if field not in present:
                raise StepError(
                    f"{_NAME} has no '{field}' setting for {label} — "
                    "it may be a different version than Orama expects."
                )
            present[field]["value"] = value

    def app_step(app: str) -> Step:
        spec = SPECS[app]
        label = f"{display_name(app)} (Orama)"

        def run() -> str | None:
            apikey = store.get(f"apikey:{app}")
            if apikey is None:
                raise StepError(f"{display_name(app)}'s API key is missing — run install again.")
            _, message, _ = upsert(
                resource="applications", label=f"the {display_name(app)} connection", name=label,
                pick=lambda s: s.get("implementation") == display_name(app),
                want={"prowlarrUrl": PROWLARR_URL, "baseUrl": f"http://{app}:{spec.port}",
                      "apiKey": apikey},
                secret=apikey, secret_names=frozenset({"apiKey"}),
                marker=sent_marker_name(f"app:{app}"),
                top={"syncLevel": "fullSync"}, tag=None,
            )
            return message

        return Step(f"prowlarr:app:{app}", "prowlarr", run, needs=("wait:prowlarr", f"wait:{app}"))

    def app_remove_step(app: str) -> Step:
        label = f"{display_name(app)} (Orama)"

        def run() -> str | None:
            have = next((a for a in read("applications") if a.get("name") == label), None)
            if have is None:
                return None
            mark_pending()
            call("DELETE", f"applications/{have['id']}")
            store.set(sent_marker_name(f"app:{app}"), "")
            return f"removed the {display_name(app)} connection"

        return Step(f"prowlarr:app:{app}", "prowlarr", run, needs=("wait:prowlarr",))

    def proxy_remove() -> str | None:
        """Proxy is off: stop routing Orama's indexers through it and delete it."""
        changes: list[str] = []
        tag = next(
            (t for t in read("tag") if str(t.get("label", "")).lower() == VPN_TAG), None
        )
        if tag is not None:
            managed = {c.name for c in setup.indexers}
            for item in read("indexer"):
                if item.get("name") in managed and tag["id"] in item.get("tags", []):
                    item["tags"] = [t for t in item["tags"] if t != tag["id"]]
                    mark_pending()
                    call("PUT", f"indexer/{item['id']}", json=item, params={"forceSave": "true"})
                    changes.append("stopped routing indexers through the VPN")
        proxy = next((p for p in read("indexerProxy") if p.get("name") == "Gluetun (Orama)"), None)
        if proxy is not None:
            mark_pending()
            call("DELETE", f"indexerProxy/{proxy['id']}")
            store.set(sent_marker_name("proxy"), "")
            changes.append("removed the VPN proxy")
        return "; ".join(dict.fromkeys(changes)) or None

    def proxy_run() -> str | None:
        password = store.get(GLUETUN_PROXY_PASSWORD_NAME)
        if password is None:
            raise StepError("The VPN proxy password is missing — run install again.")
        _, message, _ = upsert(
            resource="indexerProxy", label="the VPN proxy", name="Gluetun (Orama)",
            pick=lambda s: s.get("implementation") == "Http",
            want={"host": PROXY_HOST, "port": PROXY_PORT, "username": GLUETUN_PROXY_USER,
                  "password": password},
            secret=password, secret_names=frozenset({"password"}),
            marker=sent_marker_name("proxy"), top={}, tag=vpn_tag_id(),
        )
        return message

    def indexer_step(choice: IndexerChoice, needs: tuple[str, ...], proxy_on: bool) -> Step:
        def run() -> str | None:
            secrets: dict[str, str] = {}
            for field in choice.secret_fields:
                value = store.get(f"indexer:{choice.slug}:{field}")
                if value is None:
                    raise StepError(
                        f"The {choice.name} indexer has no API key or password yet — "
                        "enter it, then run apply again."
                    )
                secrets[field] = value
            want: dict[str, Any] = {**choice.fields, **secrets}
            item, message, _ = upsert(
                resource="indexer", label=f"the {choice.name} indexer", name=choice.name,
                pick=lambda s: _is_definition(s, choice.definition),
                want=want, secret=json.dumps(secrets, sort_keys=True),
                secret_names=frozenset(choice.secret_fields),
                marker=sent_marker_name(f"indexer:{choice.slug}"), top={"enable": True},
                tag=vpn_tag_id() if proxy_on else None, base_item=fill_profile,
            )
            if message is not None:
                # Test the indexer as Prowlarr saved it (with its id and the capabilities it
                # read from the indexer); the copy sent before saving fails Prowlarr's test.
                item = next(
                    (i for i in read("indexer") if i.get("name") == choice.name), item
                )
            resp = call("POST", "indexer/test", json=item, accept_failure=True)
            if resp.status >= 300:
                raise StepError(
                    f"{choice.name} did not pass Prowlarr's test — check its address and API key."
                )
            return message

        return Step(f"prowlarr:indexer:{choice.slug}", "prowlarr", run, needs=needs)

    def fill_profile(item: dict[str, Any]) -> None:
        if item.get("appProfileId"):
            return
        profiles = read("appprofile")
        if profiles:
            item["appProfileId"] = profiles[0]["id"]

    def sync() -> str | None:
        if store.get(SYNC_PENDING) != "1":
            return None
        call("POST", "command", json={"name": "ApplicationIndexerSync"})
        store.set(SYNC_PENDING, "")
        return "Indexers sent to Sonarr and Radarr"

    steps = [Step("wait:prowlarr", "prowlarr", wait)]
    app_names = [a for a in ("sonarr", "radarr") if a in apps]
    steps += [app_step(a) if a in apps else app_remove_step(a) for a in ("sonarr", "radarr")]
    app_needs = tuple(f"prowlarr:app:{a}" for a in app_names)
    proxy_on = setup.vpn is not None and setup.vpn.route_prowlarr
    indexer_needs = ("wait:prowlarr", *app_needs)
    if proxy_on:
        steps.append(Step("prowlarr:proxy", "prowlarr", proxy_run, needs=("wait:prowlarr",)))
        indexer_needs += ("prowlarr:proxy",)
    else:
        steps.append(Step("prowlarr:proxy-off", "prowlarr", proxy_remove, needs=("wait:prowlarr",)))
        indexer_needs += ("prowlarr:proxy-off",)
    indexer_steps = [indexer_step(c, indexer_needs, proxy_on) for c in setup.indexers]
    steps += indexer_steps
    sync_needs = ("wait:prowlarr", *(s.name for s in steps[1:]))
    steps.append(Step("prowlarr:sync", "prowlarr", sync, needs=sync_needs))
    return steps
