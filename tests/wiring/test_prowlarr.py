import copy
import json
import sys
from hashlib import sha256

from conftest import make_setup

from orama.catalog import GLUETUN_PROXY_USER
from orama.secrets import GLUETUN_PROXY_PASSWORD_NAME, SecretStore
from orama.setup_model import IndexerChoice, VpnConfig
from orama.wiring.engine import run_steps
from orama.wiring.http import FakeHttp, Response
from orama.wiring.prowlarr import prowlarr_steps

PROWLARR_KEY = "prowlarrkey" * 3
SONARR_KEY = "sonarrkey" * 3
RADARR_KEY = "radarrkey" * 3
PROXY_PASS = "proxy&pass 1"
TRICKY = "&=%+ #Ταινίες"
MASK = "********"
API = "/api/v1"


def f(name, value=None):
    return {"name": name, "value": value, "label": name.title(), "type": "textbox"}


def app_schema():
    return [
        {"implementation": "Sonarr", "configContract": "SonarrSettings", "name": "Sonarr",
         "syncLevel": "disabled", "tags": [],
         "fields": [f("prowlarrUrl"), f("baseUrl"), f("apiKey"), f("syncCategories", [5000])]},
        {"implementation": "Radarr", "configContract": "RadarrSettings", "name": "Radarr",
         "syncLevel": "disabled", "tags": [],
         "fields": [f("prowlarrUrl"), f("baseUrl"), f("apiKey"), f("syncCategories", [2000])]},
    ]


def indexer_schema():
    return [
        {"definitionName": "other", "implementation": "Cardigann", "name": "Other",
         "enable": False, "appProfileId": 1, "tags": [], "fields": [f("apikey")]},
        {"definitionName": "geek", "implementation": "Cardigann", "name": "Geek",
         "configContract": "CardigannSettings", "enable": False, "appProfileId": 1, "tags": [],
         "protocol": "usenet",
         "fields": [f("definitionFile", "geek"), f("baseUrl"), f("apikey"), f("password"),
                    f("extra", "dflt")]},
    ]


def proxy_schema():
    return [{"implementation": "Http", "configContract": "HttpSettings", "name": "Http",
             "tags": [],
             "fields": [f("host"), f("port", 8080), f("username"), f("password")]}]


class FakeProwlarr:
    """Just enough of Prowlarr's v1 API. Secret fields come back masked."""

    SECRET = ("apiKey", "apikey", "password", "passkey")

    def __init__(self):
        self.apps, self.indexers, self.proxies, self.tags = {}, {}, {}, []
        self.next_id = 1
        self.writes = []
        self.commands = []
        self.test_status = 200
        self.tested = []
        self.http = FakeHttp()
        r = self.http.route
        r("GET", f"{API}/system/status", self.guard(lambda c: self.ok({"version": "2"})))
        r("GET", f"{API}/tag", self.guard(lambda c: self.ok(self.tags)))
        r("POST", f"{API}/tag", self.guard(self.post_tag))
        r("POST", f"{API}/command", self.guard(self.post_command))
        r("POST", f"{API}/indexer/test", self.guard(self.post_test))
        for kind, store, schema in (("applications", self.apps, app_schema()),
                                    ("indexer", self.indexers, indexer_schema()),
                                    ("indexerProxy", self.proxies, proxy_schema())):
            r("GET", f"{API}/{kind}/schema", self.guard(lambda c, s=schema: self.ok(s)))
            r("GET", f"{API}/{kind}", self.guard(lambda c, st=store: self.ok([self.masked(x) for x in st.values()])))
            r("POST", f"{API}/{kind}", self.guard(lambda c, k=kind, st=store: self.post(c, k, st)))

    def guard(self, handler):
        def run(call):
            assert call.headers["X-Api-Key"] == PROWLARR_KEY
            if call.method != "GET" and call.path != f"{API}/indexer/test":
                self.writes.append((call.method, call.path))
            return handler(call)

        return run

    @staticmethod
    def ok(data, status=200):
        return Response(status, {}, json.dumps(data).encode())

    def masked(self, item):
        out = copy.deepcopy(item)
        for fld in out["fields"]:
            if fld["name"] in self.SECRET:
                fld["value"] = MASK
        return out

    def post_tag(self, call):
        tag = {"id": len(self.tags) + 1, "label": call.json["label"]}
        self.tags.append(tag)
        return self.ok(tag, 201)

    def post_command(self, call):
        self.commands.append(call.json)
        return self.ok({"id": 1}, 201)

    def post_test(self, call):
        self.tested.append(copy.deepcopy(call.json))
        # Prowlarr 2.7 fails the test of a Newznab indexer sent as it was before saving
        # (no id, capabilities not read yet); the saved one, as listed, passes.
        if "id" not in call.json:
            return Response(400, {}, b"[]")
        return Response(self.test_status, {}, b"[]")

    def post(self, call, kind, store):
        assert call.params == {"forceSave": "true"}
        item = copy.deepcopy(call.json)
        item["id"] = self.next_id
        self.next_id += 1
        store[item["id"]] = item
        put = lambda c, st=store: self.put(c, st)
        self.http.route("PUT", f"{API}/{kind}/{item['id']}", self.guard(put))
        return self.ok(item, 201)

    def put(self, call, store):
        assert call.params == {"forceSave": "true"}
        store[call.json["id"]] = copy.deepcopy(call.json)
        return self.ok(call.json, 202)

    @staticmethod
    def by_name(store, name):
        return next((x for x in store.values() if x["name"] == name), None)

    @staticmethod
    def value(item, name):
        return next(x["value"] for x in item["fields"] if x["name"] == name)

    def add_user_indexer(self, name="My Indexer"):
        item = copy.deepcopy(indexer_schema()[0])
        item.update(name=name, id=self.next_id, enable=True)
        self.next_id += 1
        self.indexers[item["id"]] = item
        self.http.route("PUT", f"{API}/indexer/{item['id']}", self.guard(lambda c: self.put(c, self.indexers)))
        return item


GEEK = IndexerChoice(slug="geek", definition="geek", name="NZBGeek",
                     fields={"baseUrl": "https://geek.example"}, secret_fields=("apikey",))


def make(tmp_path, indexers=(GEEK,), vpn=None, **overrides):
    store = SecretStore(tmp_path / "secrets.bin")
    store.set("apikey:prowlarr", PROWLARR_KEY)
    store.set("apikey:sonarr", SONARR_KEY)
    store.set("apikey:radarr", RADARR_KEY)
    store.set("indexer:geek:apikey", TRICKY)
    store.set(GLUETUN_PROXY_PASSWORD_NAME, PROXY_PASS)
    setup = make_setup(download_methods={"usenet", "torrent"} if vpn else {"usenet"},
                       vpn=vpn, indexers=tuple(indexers), **overrides)
    return setup, store


def statuses(results):
    return {r.name: r.status for r in results}


def run(setup, store, pw):
    return run_steps(prowlarr_steps(setup, store, pw.http))


def test_step_names_and_dependencies(tmp_path):
    setup, store = make(tmp_path)
    steps = {s.name: s for s in prowlarr_steps(setup, store, FakeHttp())}
    assert list(steps) == ["wait:prowlarr", "prowlarr:app:sonarr", "prowlarr:app:radarr",
                           "prowlarr:proxy-off", "prowlarr:indexer:geek", "prowlarr:sync"]
    assert steps["prowlarr:app:sonarr"].needs == ("wait:prowlarr", "wait:sonarr")
    assert steps["prowlarr:app:radarr"].needs == ("wait:prowlarr", "wait:radarr")
    assert steps["prowlarr:indexer:geek"].needs == (
        "wait:prowlarr", "prowlarr:app:sonarr", "prowlarr:app:radarr", "prowlarr:proxy-off")
    # sync waits for the apps and the proxy step, never for an indexer
    assert steps["prowlarr:sync"].needs == (
        "wait:prowlarr", "prowlarr:app:sonarr", "prowlarr:app:radarr", "prowlarr:proxy-off")


def test_removed_app_is_not_waited_for_and_its_connection_is_deleted(tmp_path):
    pw = FakeProwlarr()
    setup, store = make(tmp_path)
    run(setup, store, pw)
    assert pw.by_name(pw.apps, "Radarr (Orama)") is not None
    mine = {"id": 99, "name": "My Radarr", "fields": []}
    pw.apps[99] = mine
    setup, _ = make(tmp_path, removed_apps=frozenset({"radarr"}))
    steps = {s.name: s for s in prowlarr_steps(setup, store, pw.http)}
    assert steps["prowlarr:app:radarr"].needs == ("wait:prowlarr",)
    assert steps["prowlarr:indexer:geek"].needs == ("wait:prowlarr", "prowlarr:app:sonarr", "prowlarr:proxy-off")
    pw.commands.clear()
    pw.http.route("DELETE", f"{API}/applications/2", pw.guard(lambda c: pw.ok(pw.apps.pop(2) and {})))
    by = statuses(run_steps(list(steps.values())))
    assert by["prowlarr:app:radarr"] == "changed"
    assert pw.by_name(pw.apps, "Radarr (Orama)") is None
    assert pw.apps[99] == mine and pw.by_name(pw.apps, "Sonarr (Orama)") is not None
    assert len(pw.commands) == 1
    again = statuses(run_steps(prowlarr_steps(setup, store, pw.http)))
    assert again["prowlarr:app:radarr"] == "unchanged"


def test_removed_prowlarr_has_no_steps(tmp_path):
    setup, store = make(tmp_path, removed_apps=frozenset({"prowlarr"}))
    assert prowlarr_steps(setup, store, FakeHttp()) == []


def test_create_then_unchanged_on_second_run(tmp_path):
    pw = FakeProwlarr()
    setup, store = make(tmp_path)
    first = statuses(run(setup, store, pw))
    assert all(s == "changed" for n, s in first.items() if n not in ("wait:prowlarr", "prowlarr:proxy-off"))
    sonarr = pw.by_name(pw.apps, "Sonarr (Orama)")
    assert sonarr["implementation"] == "Sonarr" and sonarr["syncLevel"] == "fullSync"
    assert pw.value(sonarr, "prowlarrUrl") == "http://prowlarr:9696"
    assert pw.value(sonarr, "baseUrl") == "http://sonarr:8989"
    assert pw.value(sonarr, "apiKey") == SONARR_KEY
    assert pw.value(sonarr, "syncCategories") == [5000]  # schema default kept
    radarr = pw.by_name(pw.apps, "Radarr (Orama)")
    assert pw.value(radarr, "baseUrl") == "http://radarr:7878"
    assert pw.value(radarr, "apiKey") == RADARR_KEY
    geek = pw.by_name(pw.indexers, "NZBGeek")
    assert geek["definitionName"] == "geek" and geek["enable"] is True
    assert pw.value(geek, "baseUrl") == "https://geek.example"
    assert pw.value(geek, "extra") == "dflt"
    assert pw.commands == [{"name": "ApplicationIndexerSync"}]
    before = len(pw.writes)
    second = run(setup, store, pw)
    assert all(s == "unchanged" for s in statuses(second).values())
    assert len(pw.writes) == before and len(pw.commands) == 1


def test_generic_newznab_is_picked_from_prowlarr_2_schema(tmp_path, monkeypatch):
    # Prowlarr 2.7: definitionName "Newznab" (capital N) for the generic entry AND for
    # every named Newznab site listed before it.
    named = {"definitionName": "Newznab", "implementation": "Newznab", "name": "abNZB",
             "enable": False, "appProfileId": 1, "tags": [], "fields": [f("baseUrl"), f("apiKey")]}
    generic = {**named, "name": "Generic Newznab"}
    monkeypatch.setattr(sys.modules[__name__], "indexer_schema", lambda: [named, generic])
    choice = IndexerChoice(slug="fake", definition="newznab", name="Fake Newznab",
                           fields={"baseUrl": "http://fake-newznab:8000"}, secret_fields=("apiKey",))
    setup, store = make(tmp_path, indexers=(choice,))
    store.set("indexer:fake:apiKey", TRICKY)
    pw = FakeProwlarr()
    assert statuses(run(setup, store, pw))["prowlarr:indexer:fake"] == "changed"
    item = pw.by_name(pw.indexers, "Fake Newznab")
    assert item["definitionName"] == "Newznab" and pw.value(item, "baseUrl") == "http://fake-newznab:8000"
    pw.writes.clear()
    again = run(setup, store, pw)
    assert all(r.status == "unchanged" for r in again)
    assert pw.writes == []


def test_secret_with_special_characters_is_sent_unchanged(tmp_path):
    pw = FakeProwlarr()
    setup, store = make(tmp_path)
    run(setup, store, pw)
    assert pw.value(pw.by_name(pw.indexers, "NZBGeek"), "apikey") == TRICKY
    post = next(c for c in pw.http.calls if c.method == "POST" and c.path == f"{API}/indexer")
    assert TRICKY in [x["value"] for x in post.json["fields"]]


def test_missing_secret_fails_with_exact_message(tmp_path):
    pw = FakeProwlarr()
    setup, store = make(tmp_path)
    other = SecretStore(tmp_path / "other.bin")
    for k in ("apikey:prowlarr", "apikey:sonarr", "apikey:radarr"):
        other.set(k, store.get(k))
    by = {r.name: r for r in run(setup, other, pw)}
    assert by["prowlarr:indexer:geek"].status == "failed"
    assert by["prowlarr:indexer:geek"].message == (
        "The NZBGeek indexer has no API key or password yet — enter it, then run apply again.")
    assert by["prowlarr:sync"].status == "changed"  # the app connections were added
    assert pw.by_name(pw.indexers, "NZBGeek") is None


def test_failed_test_fails_with_exact_message_and_retests_next_run(tmp_path):
    pw = FakeProwlarr()
    pw.test_status = 400
    setup, store = make(tmp_path)
    by = {r.name: r for r in run(setup, store, pw)}
    assert by["prowlarr:indexer:geek"].status == "failed"
    assert by["prowlarr:indexer:geek"].message == (
        "NZBGeek did not pass Prowlarr's test — check its address and API key.")
    assert by["prowlarr:sync"].status == "changed"  # a failing indexer does not block the sync
    again = {r.name: r for r in run(setup, store, pw)}  # still failing: not silently passed
    assert again["prowlarr:indexer:geek"].status == "failed"
    assert again["prowlarr:sync"].status == "unchanged"
    pw.test_status = 200
    third = run(setup, store, pw)
    assert statuses(third)["prowlarr:indexer:geek"] == "unchanged"
    assert len(pw.commands) == 1


def test_new_indexer_is_tested_as_prowlarr_saved_it(tmp_path):
    pw = FakeProwlarr()
    setup, store = make(tmp_path)
    by = {r.name: r for r in run(setup, store, pw)}
    assert by["prowlarr:indexer:geek"].status == "changed"
    assert by["prowlarr:sync"].status == "changed"
    saved = pw.by_name(pw.indexers, "NZBGeek")
    assert [t["id"] for t in pw.tested] == [saved["id"]]
    assert TRICKY not in json.dumps(pw.tested)  # the listed copy, secrets masked


def test_proxy_on_creates_tag_proxy_and_tags_indexer(tmp_path):
    pw = FakeProwlarr()
    setup, store = make(tmp_path, vpn=VpnConfig(route_prowlarr=True))
    steps = prowlarr_steps(setup, store, pw.http)
    names = [s.name for s in steps]
    assert names.index("prowlarr:proxy") < names.index("prowlarr:indexer:geek")
    run_steps(steps)
    assert [t["label"] for t in pw.tags] == ["vpn"]
    proxy = pw.by_name(pw.proxies, "Gluetun (Orama)")
    assert proxy["implementation"] == "Http" and proxy["tags"] == [1]
    assert pw.value(proxy, "host") == "gluetun" and pw.value(proxy, "port") == 8888
    assert pw.value(proxy, "username") == GLUETUN_PROXY_USER == "orama"
    assert pw.value(proxy, "password") == PROXY_PASS
    assert pw.by_name(pw.indexers, "NZBGeek")["tags"] == [1]
    before = len(pw.writes)
    second = run_steps(prowlarr_steps(setup, store, pw.http))
    assert all(s == "unchanged" for s in statuses(second).values())
    assert len(pw.writes) == before and len(pw.tags) == 1


def test_proxy_off_makes_no_proxy_and_no_tag(tmp_path):
    pw = FakeProwlarr()
    setup, store = make(tmp_path, vpn=VpnConfig(route_prowlarr=False))
    names = [s.name for s in prowlarr_steps(setup, store, pw.http)]
    assert "prowlarr:proxy" not in names
    run(setup, store, pw)
    assert pw.tags == [] and pw.by_name(pw.indexers, "NZBGeek")["tags"] == []


def test_proxy_password_missing_fails_plainly(tmp_path):
    pw = FakeProwlarr()
    setup, store = make(tmp_path, vpn=VpnConfig(route_prowlarr=True))
    other = SecretStore(tmp_path / "other.bin")
    for k in ("apikey:prowlarr", "apikey:sonarr", "apikey:radarr", "indexer:geek:apikey"):
        other.set(k, store.get(k))
    by = {r.name: r for r in run(setup, other, pw)}
    assert by["prowlarr:proxy"].status == "failed"
    assert "proxy password is missing" in by["prowlarr:proxy"].message
    assert by["prowlarr:indexer:geek"].status == "skipped"


def test_existing_vpn_tag_is_reused(tmp_path):
    pw = FakeProwlarr()
    pw.tags = [{"id": 7, "label": "vpn"}]
    setup, store = make(tmp_path, vpn=VpnConfig(route_prowlarr=True))
    run(setup, store, pw)
    assert len(pw.tags) == 1
    assert pw.by_name(pw.proxies, "Gluetun (Orama)")["tags"] == [7]


def test_sync_not_triggered_when_nothing_changed(tmp_path):
    pw = FakeProwlarr()
    setup, store = make(tmp_path)
    run(setup, store, pw)
    pw.commands.clear()
    results = run(setup, store, pw)
    assert statuses(results)["prowlarr:sync"] == "unchanged"
    assert pw.commands == []


def test_sync_runs_and_reports_when_something_changed(tmp_path):
    pw = FakeProwlarr()
    setup, store = make(tmp_path)
    results = {r.name: r for r in run(setup, store, pw)}
    assert results["prowlarr:sync"].message == "Indexers sent to Sonarr and Radarr"


def test_user_indexer_is_never_touched(tmp_path):
    pw = FakeProwlarr()
    setup, store = make(tmp_path)
    run(setup, store, pw)
    mine = pw.add_user_indexer()
    mine_before = copy.deepcopy(mine)
    pw.writes.clear()
    run(setup, store, pw)
    assert pw.indexers[mine["id"]] == mine_before
    assert pw.writes == []
    assert all(not p.endswith(f"/indexer/{mine['id']}") for _, p in pw.writes)


def test_drifted_orama_app_is_restored_and_unmanaged_fields_kept(tmp_path):
    pw = FakeProwlarr()
    setup, store = make(tmp_path)
    run(setup, store, pw)
    sonarr = pw.by_name(pw.apps, "Sonarr (Orama)")
    next(x for x in sonarr["fields"] if x["name"] == "baseUrl")["value"] = "http://elsewhere"
    next(x for x in sonarr["fields"] if x["name"] == "syncCategories")["value"] = [1, 2]
    pw.writes.clear()
    by = statuses(run(setup, store, pw))
    assert by["prowlarr:app:sonarr"] == "changed" and by["prowlarr:app:radarr"] == "unchanged"
    fixed = pw.by_name(pw.apps, "Sonarr (Orama)")
    assert pw.value(fixed, "baseUrl") == "http://sonarr:8989"
    assert pw.value(fixed, "syncCategories") == [1, 2]
    assert pw.writes == [("PUT", f"{API}/applications/1"), ("POST", f"{API}/command")]
    assert len(pw.commands) == 2


def test_changed_indexer_secret_is_resent_via_marker(tmp_path):
    pw = FakeProwlarr()
    setup, store = make(tmp_path)
    run(setup, store, pw)
    assert store.get("prowlarr:indexer:geek:sent")
    store.set("indexer:geek:apikey", "new key")
    by = statuses(run(setup, store, pw))
    assert by["prowlarr:indexer:geek"] == "changed"
    assert pw.value(pw.by_name(pw.indexers, "NZBGeek"), "apikey") == "new key"
    assert statuses(run(setup, store, pw))["prowlarr:indexer:geek"] == "unchanged"


def test_secret_marker_is_a_sha256_digest_not_the_secret(tmp_path):
    pw = FakeProwlarr()
    setup, store = make(tmp_path)
    run(setup, store, pw)
    assert store.get("prowlarr:app:sonarr:sent") == sha256(SONARR_KEY.encode()).hexdigest()
    marker = store.get("prowlarr:indexer:geek:sent")
    assert len(marker) == 64 and TRICKY not in marker


def test_indexer_definition_not_offered_fails_plainly(tmp_path):
    pw = FakeProwlarr()
    choice = GEEK.model_copy(update={"definition": "nonexistent"})
    setup, store = make(tmp_path, indexers=(choice,))
    by = {r.name: r for r in run(setup, store, pw)}
    assert by["prowlarr:indexer:geek"].status == "failed"
    assert "does not offer" in by["prowlarr:indexer:geek"].message


def test_changed_indexer_definition_fails_without_modifying_existing_indexer(tmp_path):
    pw = FakeProwlarr()
    setup, store = make(tmp_path)
    run(setup, store, pw)
    before = copy.deepcopy(pw.indexers)
    pw.writes.clear()
    changed = GEEK.model_copy(update={"definition": "other"})
    setup = setup.model_copy(update={"indexers": (changed,)})
    by = {r.name: r for r in run(setup, store, pw)}
    assert by["prowlarr:indexer:geek"].status == "failed"
    assert "different type" in by["prowlarr:indexer:geek"].message
    assert by["prowlarr:sync"].status == "unchanged"  # nothing pending, and not blocked
    assert pw.indexers == before
    assert pw.writes == []


def test_indexer_field_not_in_schema_fails_plainly(tmp_path):
    pw = FakeProwlarr()
    choice = GEEK.model_copy(update={"fields": {"nosuchfield": "x"}})
    setup, store = make(tmp_path, indexers=(choice,))
    by = {r.name: r for r in run(setup, store, pw)}
    assert by["prowlarr:indexer:geek"].status == "failed"
    assert "different version" in by["prowlarr:indexer:geek"].message


def test_no_secret_in_results(tmp_path):
    pw = FakeProwlarr()
    setup, store = make(tmp_path, vpn=VpnConfig(route_prowlarr=True))
    text = repr(run(setup, store, pw))
    for secret in (PROWLARR_KEY, SONARR_KEY, RADARR_KEY, TRICKY, PROXY_PASS):
        assert secret not in text


def test_wrong_key_and_unreachable_are_plain(tmp_path):
    pw = FakeProwlarr()
    pw.http.route("GET", f"{API}/system/status", lambda c: Response(401, {}, b""))
    setup, store = make(tmp_path)
    by = {r.name: r for r in run(setup, store, pw)}
    assert by["wait:prowlarr"].status == "failed" and "API key" in by["wait:prowlarr"].message
    assert by["prowlarr:app:sonarr"].status == "skipped"


def test_never_started_reports_still_starting(tmp_path, monkeypatch):
    from orama.wiring import prowlarr as module
    from orama.wiring.engine import wait_until

    seen = {}
    clock = iter(x * 400.0 for x in range(100))

    def fast_wait(check, *, app, timeout, **kw):
        seen["timeout"] = timeout
        wait_until(check, app=app, timeout=timeout, clock=lambda: next(clock), sleep=lambda s: None)

    monkeypatch.setattr(module, "wait_until", fast_wait)
    setup, store = make(tmp_path)
    by = {r.name: r for r in run_steps(prowlarr_steps(setup, store, FakeHttp(unreachable=True)))}
    assert seen["timeout"] == 600.0
    assert by["wait:prowlarr"].message.endswith("is still starting — run apply again in a few minutes.")
    assert by["prowlarr:indexer:geek"].status == "skipped"


def test_default_app_profile_is_looked_up_when_schema_has_none(tmp_path):
    pw = FakeProwlarr()
    pw.http.route("GET", f"{API}/indexer/schema", lambda c: pw.ok(
        [dict(s, appProfileId=0) for s in indexer_schema()]))
    pw.http.route("GET", f"{API}/appprofile", lambda c: pw.ok([{"id": 4, "name": "Standard"}]))
    setup, store = make(tmp_path)
    run(setup, store, pw)
    assert pw.by_name(pw.indexers, "NZBGeek")["appProfileId"] == 4


def test_masked_custom_secret_field_does_not_cause_resend(tmp_path):
    pw = FakeProwlarr()
    pw.http.route("GET", f"{API}/indexer/schema", lambda c: pw.ok(
        [dict(s, fields=s["fields"] + [f("passkey")]) for s in indexer_schema()]))
    choice = GEEK.model_copy(update={"secret_fields": ("apikey", "passkey")})
    setup, store = make(tmp_path, indexers=(choice,))
    store.set("indexer:geek:passkey", "pk&1")
    run(setup, store, pw)
    assert pw.value(pw.by_name(pw.indexers, "NZBGeek"), "passkey") == "pk&1"
    pw.writes.clear()
    pw.commands.clear()
    second = run(setup, store, pw)
    assert all(s == "unchanged" for s in statuses(second).values())
    assert pw.writes == [] and pw.commands == []
    store.set("indexer:geek:passkey", "other")
    assert statuses(run(setup, store, pw))["prowlarr:indexer:geek"] == "changed"


def test_proxy_switched_off_removes_proxy_and_tag_from_orama_indexers_only(tmp_path):
    pw = FakeProwlarr()
    on, store = make(tmp_path, vpn=VpnConfig(route_prowlarr=True))
    run(on, store, pw)
    mine = pw.add_user_indexer()
    mine["tags"] = [1]
    mine_before = copy.deepcopy(mine)
    pid = pw.by_name(pw.proxies, "Gluetun (Orama)")["id"]
    pw.http.route("DELETE", f"{API}/indexerProxy/{pid}",
                  pw.guard(lambda c: pw.ok(pw.proxies.pop(pid) and {})))
    off, _ = make(tmp_path, vpn=VpnConfig(route_prowlarr=False))
    pw.commands.clear()
    by = statuses(run(off, store, pw))
    assert by["prowlarr:proxy-off"] == "changed" and by["prowlarr:indexer:geek"] == "unchanged"
    assert pw.by_name(pw.proxies, "Gluetun (Orama)") is None
    assert pw.by_name(pw.indexers, "NZBGeek")["tags"] == []
    assert pw.tags == [{"id": 1, "label": "vpn"}]
    assert pw.indexers[mine["id"]] == mine_before
    assert len(pw.commands) == 1
    pw.writes.clear()
    again = run(off, store, pw)
    assert all(s == "unchanged" for s in statuses(again).values())
    assert pw.writes == []


def test_one_failing_indexer_does_not_block_the_sync_of_the_others(tmp_path):
    pw = FakeProwlarr()
    other = IndexerChoice(slug="other", definition="geek", name="Other",
                          fields={"baseUrl": "https://other.example"}, secret_fields=("apikey",))
    setup, store = make(tmp_path, indexers=(GEEK, other))  # "other" has no stored key
    by = {r.name: r for r in run(setup, store, pw)}
    assert by["prowlarr:indexer:other"].status == "failed"
    assert by["prowlarr:indexer:geek"].status == "changed"
    assert by["prowlarr:sync"].status == "changed"
    assert len(pw.commands) == 1


def test_sync_is_skipped_when_an_app_step_fails(tmp_path):
    pw = FakeProwlarr()
    setup, store = make(tmp_path)
    other = SecretStore(tmp_path / "other.bin")  # no Sonarr key: the Sonarr connection fails
    for k in ("apikey:prowlarr", "apikey:radarr", "indexer:geek:apikey"):
        other.set(k, store.get(k))
    by = {r.name: r for r in run(setup, other, pw)}
    assert by["prowlarr:app:sonarr"].status == "failed"
    assert by["prowlarr:sync"].status == "skipped"
    assert by["prowlarr:sync"].message == "waiting for Prowlarr"
