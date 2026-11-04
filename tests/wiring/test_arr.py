import copy
import json
from hashlib import sha256

import pytest
from conftest import make_setup

from orama.secrets import SecretStore
from orama.wiring.arr import arr_steps
from orama.wiring.engine import run_steps
from orama.wiring.http import FakeHttp, Response

APP_KEY = "appkey" * 4
SAB_KEY = "sabkey" * 4
QB_PASS = "qb&pass=1 Ταινίες"
MASK = "********"

SPECS = {
    "sonarr": {"port": 8989, "root": "/data/media/tv", "cat_field": "tvCategory", "cat": "tv"},
    "radarr": {"port": 7878, "root": "/data/media/movies", "cat_field": "movieCategory", "cat": "movies"},
}


def schema(cat_field):
    def f(name, value=None):
        return {"name": name, "value": value, "label": name.title(), "type": "textbox"}

    return [
        {"implementation": "Sabnzbd", "configContract": "SabnzbdSettings", "protocol": "usenet",
         "name": "Sabnzbd", "enable": False, "priority": 1, "tags": [],
         "fields": [f("host"), f("port", 8080), f("useSsl", False), f("apiKey"), f(cat_field),
                    f("recentTvPriority", -100)]},
        {"implementation": "QBittorrent", "configContract": "QBittorrentSettings",
         "protocol": "torrent", "name": "qBittorrent", "enable": False, "priority": 1, "tags": [],
         "fields": [f("host"), f("port", 8080), f("useSsl", False), f("username"), f("password"),
                    f(cat_field)]},
    ]


class FakeArr:
    """Just enough of the v3 API of Sonarr or Radarr. Secrets come back masked."""

    def __init__(self, app):
        self.app = app
        self.spec = SPECS[app]
        self.api = "/api/v3"
        self.media = {"id": 1, "copyUsingHardlinks": False, "recycleBin": "/keep/me"}
        self.roots = []
        self.clients = {}
        self.next_id = 1
        self.writes = []
        self.http = FakeHttp()
        r = self.http.route
        r("GET", f"{self.api}/system/status", self.guard(lambda c: self.ok({"version": "4"})))
        r("GET", f"{self.api}/rootfolder", self.guard(lambda c: self.ok(self.roots)))
        r("POST", f"{self.api}/rootfolder", self.guard(self.add_root))
        r("GET", f"{self.api}/config/mediamanagement", self.guard(lambda c: self.ok(self.media)))
        r("PUT", f"{self.api}/config/mediamanagement/1", self.guard(self.put_media))
        r("GET", f"{self.api}/downloadclient/schema",
          self.guard(lambda c: self.ok(schema(self.spec["cat_field"]))))
        r("GET", f"{self.api}/downloadclient", self.guard(self.list_clients))
        r("POST", f"{self.api}/downloadclient", self.guard(self.post_client))

    def guard(self, handler):
        def run(call):
            assert call.headers["X-Api-Key"] == APP_KEY
            if call.method != "GET":
                self.writes.append((call.method, call.path))
            return handler(call)

        return run

    @staticmethod
    def ok(data, status=200):
        return Response(status, {}, json.dumps(data).encode())

    def add_root(self, call):
        self.roots.append({"id": len(self.roots) + 1, "path": call.json["path"]})
        return self.ok(self.roots[-1], 201)

    def put_media(self, call):
        self.media = call.json
        return self.ok(self.media)

    def masked(self, client):
        out = copy.deepcopy(client)
        for f in out["fields"]:
            if f["name"] in ("apiKey", "password"):
                f["value"] = MASK
        return out

    def list_clients(self, call):
        return self.ok([self.masked(c) for c in self.clients.values()])

    def post_client(self, call):
        assert call.params == {"forceSave": "true"}
        client = copy.deepcopy(call.json)
        client["id"] = self.next_id
        self.next_id += 1
        self.clients[client["id"]] = client
        self.http.route("PUT", f"{self.api}/downloadclient/{client['id']}", self.guard(self.put_client))
        self.http.route("DELETE", f"{self.api}/downloadclient/{client['id']}", self.guard(self.delete_client))
        return self.ok(client, 201)

    def put_client(self, call):
        assert call.params == {"forceSave": "true"}
        self.clients[call.json["id"]] = copy.deepcopy(call.json)
        return self.ok(call.json, 202)

    def delete_client(self, call):
        cid = int(call.path.rsplit("/", 1)[1])
        del self.clients[cid]
        return self.ok({})

    def by_name(self, name):
        return next((c for c in self.clients.values() if c["name"] == name), None)

    @staticmethod
    def field(client, name):
        return next(f["value"] for f in client["fields"] if f["name"] == name)

    def add_user_client(self, name="My SAB"):
        client = copy.deepcopy(schema(self.spec["cat_field"])[0])
        client.update(name=name, id=self.next_id, enable=True)
        for f in client["fields"]:
            if f["name"] == "host":
                f["value"] = "elsewhere"
        self.clients[client["id"]] = client
        self.http.route("PUT", f"{self.api}/downloadclient/{client['id']}", self.guard(self.put_client))
        self.http.route("DELETE", f"{self.api}/downloadclient/{client['id']}", self.guard(self.delete_client))
        self.next_id += 1
        return client


def make(tmp_path, app, methods=("usenet", "torrent"), vpn=None):
    store = SecretStore(tmp_path / "secrets.bin")
    store.set(f"apikey:{app}", APP_KEY)
    store.set("apikey:sabnzbd", SAB_KEY)
    store.set("password:qbittorrent", QB_PASS)
    setup = make_setup(download_methods=set(methods), vpn=vpn)
    return setup, store


def statuses(results):
    return {r.name: r.status for r in results}


@pytest.fixture(params=["sonarr", "radarr"])
def app(request):
    return request.param


def test_step_names_and_dependencies(tmp_path, app):
    setup, store = make(tmp_path, app)
    steps = {s.name: s for s in arr_steps(app, setup, store, FakeHttp())}
    assert list(steps) == [
        f"wait:{app}", f"{app}:root-folder", f"{app}:media-management",
        f"{app}:client:sabnzbd", f"{app}:client:qbittorrent",
    ]
    assert steps[f"{app}:client:sabnzbd"].needs == (f"wait:{app}", "sabnzbd:categories")
    assert steps[f"{app}:client:qbittorrent"].needs == (f"wait:{app}", "qbittorrent:categories")
    assert steps[f"{app}:root-folder"].needs == (f"wait:{app}",)


def test_first_run_creates_then_second_run_is_unchanged(tmp_path, app):
    arr = FakeArr(app)
    setup, store = make(tmp_path, app)
    first = run_steps(arr_steps(app, setup, store, arr.http))
    assert all(s == "changed" for n, s in statuses(first).items() if n != f"wait:{app}")
    assert [r["path"] for r in arr.roots] == [arr.spec["root"]]
    assert arr.media["copyUsingHardlinks"] is True and arr.media["recycleBin"] == "/keep/me"
    sab = arr.by_name("SABnzbd (Orama)")
    assert sab["implementation"] == "Sabnzbd" and sab["enable"] is True
    assert arr.field(sab, "host") == "sabnzbd" and arr.field(sab, "port") == 8080
    assert arr.field(sab, "apiKey") == SAB_KEY
    assert arr.field(sab, arr.spec["cat_field"]) == arr.spec["cat"]
    assert arr.field(sab, "recentTvPriority") == -100  # schema defaults kept
    qb = arr.by_name("qBittorrent (Orama)")
    assert qb["implementation"] == "QBittorrent" and qb["enable"] is True
    assert arr.field(qb, "host") == "qbittorrent" and arr.field(qb, "port") == 8090
    assert arr.field(qb, "username") == "orama" and arr.field(qb, "password") == QB_PASS
    assert arr.field(qb, arr.spec["cat_field"]) == arr.spec["cat"]
    before = len(arr.writes)
    second = run_steps(arr_steps(app, setup, store, arr.http))
    assert all(s == "unchanged" for s in statuses(second).values())
    assert len(arr.writes) == before
    assert len(arr.clients) == 2


def test_qbittorrent_host_is_gluetun_with_vpn(tmp_path, app):
    from orama.setup_model import VpnConfig

    arr = FakeArr(app)
    setup, store = make(tmp_path, app, vpn=VpnConfig())
    run_steps(arr_steps(app, setup, store, arr.http))
    assert arr.field(arr.by_name("qBittorrent (Orama)"), "host") == "gluetun"


def test_user_edit_of_orama_client_is_restored_but_user_client_is_left(tmp_path, app):
    arr = FakeArr(app)
    setup, store = make(tmp_path, app)
    run_steps(arr_steps(app, setup, store, arr.http))
    mine = arr.add_user_client()
    mine_before = copy.deepcopy(mine)
    sab = arr.by_name("SABnzbd (Orama)")
    next(f for f in sab["fields"] if f["name"] == "host")["value"] = "edited"
    sab["priority"] = 7  # not managed by Orama: must survive
    arr.writes.clear()
    results = run_steps(arr_steps(app, setup, store, arr.http))
    assert statuses(results)[f"{app}:client:sabnzbd"] == "changed"
    assert statuses(results)[f"{app}:client:qbittorrent"] == "unchanged"
    assert arr.field(arr.by_name("SABnzbd (Orama)"), "host") == "sabnzbd"
    assert arr.by_name("SABnzbd (Orama)")["priority"] == 7
    assert arr.field(arr.by_name("SABnzbd (Orama)"), "apiKey") == SAB_KEY
    assert arr.clients[mine["id"]] == mine_before
    assert all(not path.endswith(f"/downloadclient/{mine['id']}") for _, path in arr.writes)
    assert [w[0] for w in arr.writes] == ["PUT"]


def test_disabled_client_is_enabled_again(tmp_path, app):
    arr = FakeArr(app)
    setup, store = make(tmp_path, app)
    run_steps(arr_steps(app, setup, store, arr.http))
    arr.by_name("qBittorrent (Orama)")["enable"] = False
    results = run_steps(arr_steps(app, setup, store, arr.http))
    assert statuses(results)[f"{app}:client:qbittorrent"] == "changed"
    assert arr.by_name("qBittorrent (Orama)")["enable"] is True


def test_changed_secret_is_resent_and_unchanged_secret_is_not(tmp_path, app):
    arr = FakeArr(app)
    setup, store = make(tmp_path, app)
    run_steps(arr_steps(app, setup, store, arr.http))
    marker = store.get(f"arr:{app}:qbittorrent:sent")
    assert marker == sha256(QB_PASS.encode()).hexdigest()
    store.set("password:qbittorrent", "new pass")
    results = run_steps(arr_steps(app, setup, store, arr.http))
    assert statuses(results)[f"{app}:client:qbittorrent"] == "changed"
    assert arr.field(arr.by_name("qBittorrent (Orama)"), "password") == "new pass"
    assert store.get(f"arr:{app}:qbittorrent:sent") == sha256(b"new pass").hexdigest()
    assert store.get(f"arr:{app}:sabnzbd:sent") == sha256(SAB_KEY.encode()).hexdigest()


def test_lost_marker_resends_once(tmp_path, app):
    arr = FakeArr(app)
    setup, store = make(tmp_path, app)
    run_steps(arr_steps(app, setup, store, arr.http))
    store.set(f"arr:{app}:sabnzbd:sent", "")
    results = run_steps(arr_steps(app, setup, store, arr.http))
    assert statuses(results)[f"{app}:client:sabnzbd"] == "changed"
    again = run_steps(arr_steps(app, setup, store, arr.http))
    assert statuses(again)[f"{app}:client:sabnzbd"] == "unchanged"


def test_method_switched_off_removes_only_the_orama_client(tmp_path, app):
    arr = FakeArr(app)
    setup, store = make(tmp_path, app)
    run_steps(arr_steps(app, setup, store, arr.http))
    mine = arr.add_user_client()
    torrent_off, _ = make(tmp_path, app, methods=("usenet",))
    steps = arr_steps(app, torrent_off, store, arr.http)
    assert [s.name for s in steps][-2:] == [f"{app}:client:sabnzbd", f"{app}:client:qbittorrent"]
    qb_step = steps[-1]
    assert qb_step.needs == (f"wait:{app}",)  # does not wait for qBittorrent to exist
    results = run_steps(steps)
    assert statuses(results)[f"{app}:client:qbittorrent"] == "changed"
    assert arr.by_name("qBittorrent (Orama)") is None
    assert arr.by_name("SABnzbd (Orama)") is not None
    assert arr.clients[mine["id"]]["name"] == "My SAB"
    again = run_steps(arr_steps(app, torrent_off, store, arr.http))
    assert statuses(again)[f"{app}:client:qbittorrent"] == "unchanged"


def test_usenet_switched_off_removes_sabnzbd_client(tmp_path, app):
    arr = FakeArr(app)
    setup, store = make(tmp_path, app)
    run_steps(arr_steps(app, setup, store, arr.http))
    usenet_off, _ = make(tmp_path, app, methods=("torrent",))
    run_steps(arr_steps(app, usenet_off, store, arr.http))
    assert arr.by_name("SABnzbd (Orama)") is None
    assert arr.by_name("qBittorrent (Orama)") is not None


@pytest.mark.parametrize("removed,label,kept", [
    ("sabnzbd", "SABnzbd (Orama)", "qBittorrent (Orama)"),
    ("qbittorrent", "qBittorrent (Orama)", "SABnzbd (Orama)"),
])
def test_removed_downloader_removes_only_its_managed_client(tmp_path, app, removed, label, kept):
    arr = FakeArr(app)
    setup, store = make(tmp_path, app)
    run_steps(arr_steps(app, setup, store, arr.http))
    mine = arr.add_user_client()
    before = copy.deepcopy(mine)
    setup = setup.model_copy(update={"removed_apps": frozenset({removed})})
    steps = arr_steps(app, setup, store, arr.http)
    results = run_steps(steps)
    assert statuses(results)[f"{app}:client:{removed}"] == "changed"
    assert arr.by_name(label) is None
    assert arr.by_name(kept) is not None
    assert arr.clients[mine["id"]] == before
    assert next(s for s in steps if s.name == f"{app}:client:{removed}").needs == (f"wait:{app}",)
    arr.writes.clear()
    again = run_steps(arr_steps(app, setup, store, arr.http))
    assert all(r.status == "unchanged" for r in again)
    assert arr.writes == []


@pytest.mark.parametrize("removed,label", [
    ("sabnzbd", "SABnzbd (Orama)"), ("qbittorrent", "qBittorrent (Orama)"),
])
def test_removed_downloader_client_is_not_created_on_first_apply(tmp_path, app, removed, label):
    arr = FakeArr(app)
    setup, store = make(tmp_path, app)
    setup = setup.model_copy(update={"removed_apps": frozenset({removed})})
    results = run_steps(arr_steps(app, setup, store, arr.http))
    assert statuses(results)[f"{app}:client:{removed}"] == "unchanged"
    assert arr.by_name(label) is None


def test_hardlinks_already_true_means_no_put(tmp_path, app):
    arr = FakeArr(app)
    arr.media["copyUsingHardlinks"] = True
    setup, store = make(tmp_path, app)
    results = run_steps(arr_steps(app, setup, store, arr.http))
    assert statuses(results)[f"{app}:media-management"] == "unchanged"
    assert ("PUT", f"{arr.api}/config/mediamanagement/1") not in arr.writes


def test_existing_root_folder_is_not_added_again(tmp_path, app):
    arr = FakeArr(app)
    arr.roots = [{"id": 1, "path": arr.spec["root"] + "/"}]
    setup, store = make(tmp_path, app)
    results = run_steps(arr_steps(app, setup, store, arr.http))
    assert statuses(results)[f"{app}:root-folder"] == "unchanged"
    assert ("POST", f"{arr.api}/rootfolder") not in arr.writes


def test_api_key_header_is_sent_and_no_secret_in_messages(tmp_path, app):
    arr = FakeArr(app)  # its routes assert the X-Api-Key header on every call
    setup, store = make(tmp_path, app)
    results = run_steps(arr_steps(app, setup, store, arr.http))
    assert arr.http.calls and all(c.headers["X-Api-Key"] == APP_KEY for c in arr.http.calls)
    text = repr(results)
    for secret in (APP_KEY, SAB_KEY, QB_PASS):
        assert secret not in text


def test_missing_secrets_are_reported_without_secrets(tmp_path, app):
    arr = FakeArr(app)
    setup, _ = make(tmp_path, app)
    empty = SecretStore(tmp_path / "other.bin")
    empty.set(f"apikey:{app}", APP_KEY)
    results = run_steps(arr_steps(app, setup, empty, arr.http))
    by = {r.name: r for r in results}
    assert by[f"{app}:client:sabnzbd"].status == "failed"
    assert by[f"{app}:client:qbittorrent"].status == "failed"
    assert by[f"{app}:root-folder"].status == "changed"


def test_wrong_app_key_and_unreachable_are_plain(tmp_path, app):
    arr = FakeArr(app)
    arr.http.route("GET", f"{arr.api}/system/status", lambda c: Response(401, {}, b""))
    setup, store = make(tmp_path, app)
    by = {r.name: r for r in run_steps(arr_steps(app, setup, store, arr.http))}
    assert by[f"wait:{app}"].status == "failed"
    assert "API key" in by[f"wait:{app}"].message
    assert by[f"{app}:root-folder"].status == "skipped"


def test_app_that_never_comes_up_reports_still_starting(tmp_path, app, monkeypatch):
    from orama.wiring import arr as arr_module
    from orama.wiring.engine import wait_until

    seen = {}
    clock = iter(x * 400.0 for x in range(100))

    def fast_wait(check, *, app, timeout, **kw):
        seen["timeout"] = timeout
        wait_until(check, app=app, timeout=timeout, clock=lambda: next(clock), sleep=lambda s: None)

    monkeypatch.setattr(arr_module, "wait_until", fast_wait)
    setup, store = make(tmp_path, app)
    results = run_steps(arr_steps(app, setup, store, FakeHttp(unreachable=True)))
    by = {r.name: r for r in results}
    assert seen["timeout"] == 600.0
    assert by[f"wait:{app}"].message.endswith("is still starting — run apply again in a few minutes.")
    assert by[f"{app}:client:sabnzbd"].status == "skipped"


def test_schema_without_expected_field_fails_plainly(tmp_path, app):
    arr = FakeArr(app)
    arr.http.route(
        "GET", f"{arr.api}/downloadclient/schema",
        lambda c: arr.ok([dict(s, fields=[]) for s in schema(arr.spec["cat_field"])]),
    )
    setup, store = make(tmp_path, app)
    by = {r.name: r for r in run_steps(arr_steps(app, setup, store, arr.http))}
    assert by[f"{app}:client:sabnzbd"].status == "failed"
    assert "different version" in by[f"{app}:client:sabnzbd"].message
