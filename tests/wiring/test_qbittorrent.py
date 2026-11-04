import json

from conftest import make_setup

from orama.secrets import SecretStore
from orama.wiring.engine import run_steps
from orama.wiring.http import FakeHttp, Response
from orama.wiring.qbittorrent import qbittorrent_steps

PASSWORD = "pw-" + "x" * 10


class FakeQbt:
    def __init__(self, login_ok=True):
        self.prefs = {"save_path": "/downloads/", "temp_path_enabled": True, "listen_port": 6881}
        self.cats = {}
        self.login_ok = login_ok
        self.http = FakeHttp()
        self.http.route("POST", "/api/v2/auth/login", self.login)
        self.http.route("POST", "/api/v2/torrents/createCategory", self.create)
        self.http.route("POST", "/api/v2/torrents/editCategory", self.edit)
        self.http.route("POST", "/api/v2/app/setPreferences", self.set_prefs)
        self.http.route("GET", "/api/v2/app/preferences",
                        lambda c: Response(200, {}, json.dumps(self.prefs).encode()))
        self.http.route("GET", "/api/v2/torrents/categories",
                        lambda c: Response(200, {}, json.dumps(self.cats).encode()))

    def login(self, call):
        ok = self.login_ok and call.form == {"username": "orama", "password": PASSWORD}
        # qBittorrent 5.x: 204 and no body on success.
        return Response(204, {}, b"") if ok else Response(403, {}, b"Fails.")

    def set_prefs(self, call):
        self.prefs.update(json.loads(call.form["json"]))
        return Response(200, {}, b"")

    def create(self, call):
        self.cats[call.form["category"]] = {"name": call.form["category"],
                                            "savePath": call.form["savePath"]}
        return Response(200, {}, b"")

    edit = create

    def posts(self, suffix):
        return [c for c in self.http.calls if c.path.endswith(suffix)]


def make(tmp_path, **setup):
    store = SecretStore(tmp_path / "secrets.bin")
    store.set("password:qbittorrent", PASSWORD)
    return make_setup(download_methods={"torrent"}, **setup), store


def statuses(results):
    return {r.name: r.status for r in results}


def test_first_run_then_unchanged(tmp_path):
    q = FakeQbt()
    setup, store = make(tmp_path)
    first = run_steps(qbittorrent_steps(setup, store, q.http))
    assert statuses(first) == {"wait:qbittorrent": "unchanged", "qbittorrent:prefs": "changed",
                               "qbittorrent:categories": "changed"}
    assert q.prefs["save_path"] == "/data/downloads/torrents"
    assert q.prefs["temp_path_enabled"] is False
    assert q.prefs["listen_port"] == 6881  # never touched
    assert [json.loads(c.form["json"]) for c in q.posts("setPreferences")] == [
        {"save_path": "/data/downloads/torrents", "temp_path_enabled": False}]
    assert q.cats["tv"]["savePath"] == "/data/downloads/torrents/tv"
    assert q.cats["movies"]["savePath"] == "/data/downloads/torrents/movies"
    assert len(q.posts("createCategory")) == 2
    n = len(q.http.calls)
    second = run_steps(qbittorrent_steps(setup, store, q.http))
    assert all(s == "unchanged" for s in statuses(second).values())
    assert [c.path for c in q.http.calls[n:]].count("/api/v2/app/setPreferences") == 0
    assert len(q.posts("createCategory")) == 2 and not q.posts("editCategory")


def test_wrong_category_path_is_edited(tmp_path):
    q = FakeQbt()
    q.cats["tv"] = {"name": "tv", "savePath": "/elsewhere"}
    setup, store = make(tmp_path)
    run_steps(qbittorrent_steps(setup, store, q.http))
    assert len(q.posts("editCategory")) == 1 and len(q.posts("createCategory")) == 1
    assert q.cats["tv"]["savePath"] == "/data/downloads/torrents/tv"


def test_host_is_gluetun_with_vpn(tmp_path):
    q = FakeQbt()
    setup, store = make(tmp_path, vpn={"provider": "protonvpn"})
    run_steps(qbittorrent_steps(setup, store, q.http))
    assert {c.url.split("/api")[0] for c in q.http.calls} == {"http://gluetun:8090"}
    q2 = FakeQbt()
    setup, store = make(tmp_path)
    run_steps(qbittorrent_steps(setup, store, q2.http))
    assert {c.url.split("/api")[0] for c in q2.http.calls} == {"http://qbittorrent:8090"}


def test_older_qbittorrent_login_answer_is_accepted(tmp_path):
    q = FakeQbt()
    q.login = lambda call: Response(200, {}, b"Ok.")
    q.http.route("POST", "/api/v2/auth/login", q.login)
    setup, store = make(tmp_path)
    by = {r.name: r for r in run_steps(qbittorrent_steps(setup, store, q.http))}
    assert by["wait:qbittorrent"].status == "unchanged"


def test_login_failure_skips_the_rest(tmp_path):
    q = FakeQbt(login_ok=False)
    setup, store = make(tmp_path)
    results = run_steps(qbittorrent_steps(setup, store, q.http))
    by = {r.name: r for r in results}
    assert by["wait:qbittorrent"].status == "failed"
    assert by["wait:qbittorrent"].message == (
        "qBittorrent refused Orama's login — run apply again; if it keeps failing, "
        "see the Orama logs.")
    assert by["qbittorrent:prefs"].status == "skipped"
    assert by["qbittorrent:categories"].status == "skipped"
    assert PASSWORD not in repr(results)
