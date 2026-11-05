import json
from urllib.parse import parse_qs, urlencode

from conftest import make_setup

from orama.secrets import SecretStore
from orama.wiring.engine import run_steps
from orama.wiring.http import FakeHttp, Response
from orama.wiring.sabnzbd import sabnzbd_steps, sent_marker_name
from orama.wiring.sabnzbd import test_usenet_server as check_usenet_server

AWKWARD = "a&b=c%d+e #f Ταινίες"
KEY = "key" * 4


class FakeSab:
    """Just enough of SABnzbd's query API; passwords are hidden like the real one."""

    def __init__(self, login_ok=True):
        self.misc = {"complete_dir": "Downloads/complete", "download_dir": "Downloads/incomplete"}
        self.cats = None  # SABnzbd 5 has no categories section until it lists them once
        self.servers = {}
        self.passwords = {}
        self.login_ok = login_ok
        self.deleted = []
        self.http = FakeHttp()
        self.http.route("POST", "/api", self.handle)

    def writes(self):
        return [c for c in self.http.calls if c.form["mode"] == "set_config"]

    def handle(self, call):
        p = call.form
        assert p["apikey"] == KEY and p["output"] == "json"
        mode = p["mode"]
        if mode == "version":
            return Response(200, {}, b'{"version": "5.1.3"}')
        if mode == "get_config":
            section = p["section"]
            # SABnzbd 5.1: a list section is absent until something was added to it.
            if (section == "categories" and self.cats is None) or (
                section == "servers" and not self.servers
            ):
                return self.ok({"config": {}})
            if section == "misc":
                data = dict(self.misc)
            elif section == "categories":
                data = self.listed_cats()
            else:
                data = [dict(s, password="*" * 8) for s in self.servers.values()]
            return self.ok({"config": {section: data}})
        if mode == "set_config":
            if p["section"] == "misc":
                self.misc[p["keyword"]] = p["value"]
            elif p["section"] == "categories":
                self.cats = self.cats or {}
                self.cats[p["name"]] = {"name": p["name"], "dir": p["dir"]}
            else:
                row = {k: v for k, v in p.items() if k not in ("mode", "section", "apikey", "output", "password")}
                self.servers[p["name"]] = row
                self.passwords[p["name"]] = p["password"]
            return self.ok({"config": {}})
        if mode == "del_config":
            assert p["section"] == "servers"
            del self.servers[p["keyword"]]
            self.deleted.append(p["keyword"])
            return self.ok({"status": True})
        if mode == "get_cats":
            return self.ok({"categories": [c["name"] for c in self.listed_cats()]})
        if mode == "config" and p["name"] == "test_server":
            return self.ok({"value": {"result": self.login_ok, "message": "x"}})
        raise AssertionError(mode)

    def listed_cats(self):
        """Like SABnzbd 5's get_categories(): once the list is read without its "*" entry,
        the default categories are (re)created, replacing any tv/movies set before."""
        self.cats = self.cats or {}
        if "*" not in self.cats:
            for name in ("*", "movies", "tv", "audio", "software"):
                self.cats[name] = {"name": name, "dir": ""}
        return list(self.cats.values())

    @staticmethod
    def ok(data):
        return Response(200, {}, json.dumps(data).encode())


def make(tmp_path, servers=({"slug": "eweka", "host": "news.example.net", "username": "me"},)):
    store = SecretStore(tmp_path / "secrets.bin")
    store.set("apikey:sabnzbd", KEY)
    store.set("usenet:eweka:password", AWKWARD)
    setup = make_setup(usenet_servers=list(servers))
    return setup, store


def statuses(results):
    return {r.name: r.status for r in results}


def test_first_run_changes_everything_then_second_run_unchanged(tmp_path):
    sab = FakeSab()
    setup, store = make(tmp_path)
    first = run_steps(sabnzbd_steps(setup, store, sab.http))
    assert statuses(first) == {
        "wait:sabnzbd": "unchanged", "sabnzbd:folders": "changed", "sabnzbd:categories": "changed",
        "sabnzbd:server:eweka": "changed", "sabnzbd:test:eweka": "unchanged",
    }
    assert sab.misc == {"complete_dir": "/data/downloads/usenet",
                        "download_dir": "/data/downloads/usenet/incomplete"}
    assert sab.cats["tv"]["dir"] == "tv" and sab.cats["movies"]["dir"] == "movies"
    assert sab.servers["eweka"]["host"] == "news.example.net"
    sent = len(sab.writes())
    assert sent == 2 + 2 + 1  # two folders, two categories, one server
    second = run_steps(sabnzbd_steps(setup, store, sab.http))
    assert all(s == "unchanged" for s in statuses(second).values())
    assert len(sab.writes()) == sent


def test_server_changes_are_resent_only_when_needed(tmp_path):
    sab = FakeSab()
    setup, store = make(tmp_path)
    run_steps(sabnzbd_steps(setup, store, sab.http))
    sab.servers["eweka"]["host"] = "edited.example.org"  # user edit in SABnzbd's own UI
    n = len(sab.writes())
    results = run_steps(sabnzbd_steps(setup, store, sab.http))
    assert statuses(results)["sabnzbd:server:eweka"] == "changed"
    assert len(sab.writes()) == n + 1
    assert sab.servers["eweka"]["host"] == "news.example.net"
    store.set("usenet:eweka:password", "new password")  # password changed in the store
    results = run_steps(sabnzbd_steps(setup, store, sab.http))
    assert statuses(results)["sabnzbd:server:eweka"] == "changed"
    assert sab.passwords["eweka"] == "new password"


def test_awkward_password_arrives_intact(tmp_path):
    sab = FakeSab()
    setup, store = make(tmp_path)
    run_steps(sabnzbd_steps(setup, store, sab.http))
    [write] = [c for c in sab.writes() if c.form["section"] == "servers"]
    assert write.form["password"] == AWKWARD
    assert write.params is None  # API key and server password are never put in the URL.
    assert all(AWKWARD not in c.url for c in sab.http.calls)
    wire = parse_qs(urlencode(write.form))  # what the real client puts on the wire
    assert wire["password"] == [AWKWARD]
    test_call = next(c for c in sab.http.calls if c.form["mode"] == "config")
    assert test_call.form["password"] == AWKWARD


def test_failed_server_test_is_plain_and_blocks_nothing(tmp_path):
    sab = FakeSab(login_ok=False)
    setup, store = make(tmp_path)
    results = run_steps(sabnzbd_steps(setup, store, sab.http))
    by = {r.name: r for r in results}
    assert by["sabnzbd:test:eweka"].status == "failed"
    assert by["sabnzbd:test:eweka"].message == (
        "SABnzbd could not connect or log in to news.example.net — check the server "
        "address, port, username and password."
    )
    assert by["sabnzbd:categories"].status == "changed"
    assert AWKWARD not in repr(results)


def test_missing_server_password_is_reported_without_secrets(tmp_path):
    sab = FakeSab()
    setup, _ = make(tmp_path)
    store2 = SecretStore(tmp_path / "other.bin")
    store2.set("apikey:sabnzbd", KEY)
    results = run_steps(sabnzbd_steps(setup, store2, sab.http))
    by = {r.name: r for r in results}
    assert by["sabnzbd:server:eweka"].status == "failed"
    assert by["sabnzbd:test:eweka"].status == "skipped"
    assert by["sabnzbd:categories"].status == "changed"


def test_wrong_api_key_is_plain(tmp_path):
    sab = FakeSab()
    sab.http.route("POST", "/api", lambda c: (
        Response(200, {}, b'{"version": "5"}') if c.form["mode"] == "version"
        else Response(200, {}, b'{"status": false, "error": "API Key Incorrect"}')))
    setup, store = make(tmp_path)
    by = {r.name: r for r in run_steps(sabnzbd_steps(setup, store, sab.http))}
    assert by["sabnzbd:folders"].message == "SABnzbd did not accept Orama's API key — run apply again."


def test_no_servers_means_only_shared_steps(tmp_path):
    sab = FakeSab()
    setup, store = make(tmp_path, servers=())
    names = [s.name for s in sabnzbd_steps(setup, store, sab.http)]
    assert names == ["wait:sabnzbd", "sabnzbd:folders", "sabnzbd:categories"]


def test_usenet_server_removed(tmp_path):
    sab = FakeSab()
    setup, store = make(tmp_path, servers=())
    run_steps(sabnzbd_steps(make(tmp_path)[0], store, sab.http))  # server exists in SABnzbd
    store.set("remove:usenet:eweka", "eweka")
    results = {r.name: r for r in run_steps(sabnzbd_steps(setup, store, sab.http))}
    assert results["sabnzbd:remove:eweka"].status == "changed"
    assert results["sabnzbd:remove:eweka"].message == "removed the Usenet server eweka"
    assert sab.deleted == ["eweka"] and sab.servers == {}
    call = next(c for c in sab.http.calls if c.form["mode"] == "del_config")
    assert call.form["keyword"] == "eweka" and call.form["section"] == "servers"
    assert store.get("remove:usenet:eweka") is None
    assert store.get(sent_marker_name("eweka")) is None


def test_usenet_removal_of_missing_server_is_quiet(tmp_path):
    sab = FakeSab()
    setup, store = make(tmp_path, servers=())
    store.set("remove:usenet:eweka", "eweka")
    by = {r.name: r for r in run_steps(sabnzbd_steps(setup, store, sab.http))}
    assert by["sabnzbd:remove:eweka"].status == "unchanged"
    assert sab.deleted == [] and store.get("remove:usenet:eweka") is None


def test_test_usenet_server_sets_and_clears_test_ok(tmp_path):
    sab = FakeSab()
    setup, store = make(tmp_path)
    assert check_usenet_server(setup, store, sab.http, "eweka") is None
    assert store.get("test-ok:usenet:eweka") == "1"
    assert not any(c.form["mode"] == "get_cats" for c in sab.http.calls)  # no other step ran
    sab.login_ok = False
    msg = check_usenet_server(setup, store, sab.http, "eweka")
    assert msg == ("SABnzbd could not connect or log in to news.example.net — check the server "
                   "address, port, username and password.")
    assert store.get("test-ok:usenet:eweka") is None


def test_test_usenet_server_missing_password_points_to_settings(tmp_path):
    sab = FakeSab()
    setup, store = make(tmp_path)
    store.delete("usenet:eweka:password")
    msg = check_usenet_server(setup, store, sab.http, "eweka")
    assert "set-secret" not in msg and "Settings" in msg
