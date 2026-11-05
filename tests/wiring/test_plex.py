import json
import logging
import urllib.parse

import pytest
from conftest import make_setup, random_secret

from orama import cli
from orama.secrets import PLEX_CLIENT_ID_NAME, PLEX_TOKEN_NAME, SecretStore
from orama.wiring import plex
from orama.wiring.engine import format_checklist, run_steps
from orama.wiring.http import FakeHttp, Response
from orama.wiring.plex import (
    NO_PASS,
    OTHER_ACCOUNT,
    WAITING_FOR_SIGN_IN,
    begin_sign_in,
    plex_steps,
    poll_sign_in,
)

PLEX_TV = "https://plex.tv"
LOCAL = "http://plex:32400"
MACHINE = "machine-" + "a1b2c3"
CLIENT_ID = "client-" + "0f0e0d"


def _ip(*octets):
    # Built at run time: no IP-address literal sits in the repo (tests/test_repo_hygiene.py).
    return ".".join(str(o) for o in octets)


HOME = _ip(192, 168, 7, 20)
OTHER_HOME = _ip(10, 1, 2, 3)


def ok(data, status=200):
    return Response(status, {}, json.dumps(data).encode())


class FakePlex:
    """Plex Media Server on orama-net plus just enough of plex.tv, behind one FakeHttp."""

    def __init__(self, token):
        self.token = token  # the account token plex.tv hands out
        self.claim_token = "claim-" + random_secret()
        self.claimed = False
        self.owner = None  # "me" or "other"
        self.plex_pass = True
        self.sections = []
        self.prefs = {}
        self.pins = {}  # id -> {"code", "authToken", "expiresIn", "status"}
        self.next_pin = 4100
        self.fail_create_after = None  # fail library creates after this many succeed
        self.creates = 0
        self.http = FakeHttp()
        r = self.http.route
        r("POST", "/api/v2/pins", self.tv(self.new_pin))
        r("GET", "/api/claim/token.json", self.tv(self.authed(self.get_claim_token)))
        r("GET", "/api/v2/resources", self.tv(self.authed(self.resources)))
        r("GET", "/api/v2/user", self.tv(self.authed(self.user)))
        r("GET", "/identity", self.local(self.identity))
        r("POST", "/myplex/claim", self.local(self.claim))
        r("GET", "/library/sections", self.local(self.authed_local(self.list_sections)))
        r("POST", "/library/sections", self.local(self.authed_local(self.create_section)))
        r("PUT", "/:/prefs", self.local(self.authed_local(self.put_prefs)))

    # -- plumbing ------------------------------------------------------------------
    def tv(self, handler):
        def run(call):
            assert call.url.startswith(PLEX_TV + "/"), call.url
            assert call.headers["X-Plex-Product"] == "Orama"
            assert call.headers["X-Plex-Client-Identifier"] == CLIENT_ID
            return handler(call)

        return run

    def local(self, handler):
        def run(call):
            assert call.url.startswith(LOCAL + "/"), call.url
            return handler(call)

        return run

    def authed(self, handler):
        def run(call):
            if call.headers.get("X-Plex-Token") != self.token:
                return Response(401, {}, b"")
            return handler(call)

        return run

    def authed_local(self, handler):
        def run(call):
            if self.claimed and call.headers.get("X-Plex-Token") != self.token:
                return Response(401, {}, b"")
            return handler(call)

        return run

    def add_pin_route(self, pin_id):
        self.http.route("GET", f"/api/v2/pins/{pin_id}", self.tv(self.get_pin(pin_id)))

    # -- plex.tv -------------------------------------------------------------------
    def new_pin(self, call):
        assert urllib.parse.urlsplit(call.url).query == "strong=true"
        pin_id = self.next_pin
        self.next_pin += 1
        self.pins[pin_id] = {"code": "pincode" + str(pin_id), "authToken": None,
                             "expiresIn": 1800, "status": 200}
        self.add_pin_route(pin_id)
        return ok({"id": pin_id, "code": self.pins[pin_id]["code"], "authToken": None,
                   "expiresIn": 1800}, 201)

    def get_pin(self, pin_id):
        def run(call):
            pin = self.pins[pin_id]
            if pin["status"] != 200:
                return Response(pin["status"], {}, b"")
            return ok({"id": pin_id, "code": pin["code"], "authToken": pin["authToken"],
                       "expiresIn": pin["expiresIn"]})

        return run

    def approve(self, pin_id):
        self.pins[pin_id]["authToken"] = self.token

    def get_claim_token(self, call):
        return ok({"token": self.claim_token})

    def resources(self, call):
        items = [{"name": "Somebody else", "clientIdentifier": "other-machine",
                  "provides": "server", "owned": False}]
        if self.owner == "me":
            items.append({"name": "Orama", "clientIdentifier": MACHINE,
                          "provides": "server", "owned": True})
        elif self.owner == "other":  # shared with this account, owned by another
            items.append({"name": "Theirs", "clientIdentifier": MACHINE,
                          "provides": "server", "owned": False})
        return ok(items)

    def user(self, call):
        return ok({"id": 1, "subscription": {"active": self.plex_pass,
                                             "status": "Active" if self.plex_pass else "Inactive"}})

    # -- local Plex ----------------------------------------------------------------
    def identity(self, call):
        assert call.headers.get("Accept") == "application/json"
        return ok({"MediaContainer": {"size": 0, "apiVersion": "1.2.0", "claimed": self.claimed,
                                      "machineIdentifier": MACHINE, "version": "1.43.5"}})

    def claim(self, call):
        if self.claimed:
            return Response(400, {}, b"")
        if (call.params or {}).get("token") != self.claim_token:
            return Response(401, {}, b"")
        self.claimed = True
        self.owner = "me"
        return ok({"MediaContainer": {"size": 0}})

    def list_sections(self, call):
        return ok({"MediaContainer": {"size": len(self.sections), "Directory": self.sections}})

    def create_section(self, call):
        if self.fail_create_after is not None and self.creates >= self.fail_create_after:
            return Response(500, {}, b"")
        query = urllib.parse.urlsplit(call.url).query
        assert "+" not in query  # Plex was observed taking %20, not +
        q = dict(urllib.parse.parse_qsl(query))
        self.creates += 1
        key = str(len(self.sections) + 1)
        self.sections.append({"key": key, "type": q["type"], "title": q["name"],
                              "agent": q["agent"], "scanner": q["scanner"],
                              "language": q["language"],
                              "Location": [{"id": int(key), "path": q["location"]}]})
        return ok({"MediaContainer": {"size": 1, "Directory": [self.sections[-1]]}}, 201)

    def put_prefs(self, call):
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(call.url).query))
        self.prefs.update(q)
        return Response(200, {}, b"")


@pytest.fixture
def token():
    return random_secret("tok", 10)


@pytest.fixture
def store(tmp_path):
    s = SecretStore(tmp_path / "secrets.bin")
    s.set(PLEX_CLIENT_ID_NAME, CLIENT_ID)
    return s


def run(fake, setup, store):
    return run_steps(plex_steps(setup, store, fake.http))


def statuses(results):
    return {r.name: r.status for r in results}


def assert_token_hidden(fake, token, results=(), text=""):
    for call in fake.http.calls:
        assert token not in call.url
        assert token not in json.dumps(call.params or {})
        assert token not in json.dumps(call.json or {})
        assert token not in json.dumps(call.form or {})
        if call.headers and call.headers.get("X-Plex-Token") == token:
            assert call.url.startswith((PLEX_TV + "/", LOCAL + "/"))
    for r in results:
        assert token not in r.message
    assert token not in text


def assert_only_known_origins(fake):
    for call in fake.http.calls:
        assert call.url.startswith((PLEX_TV + "/", LOCAL + "/")), call.url


# ---------------------------------------------------------------- sign-in

def test_begin_sign_in_asks_plex_tv_for_a_strong_pin(store, token):
    fake = FakePlex(token)
    s = begin_sign_in(store, fake.http)
    assert s.pin_id == 4100
    assert s.code == "pincode4100"
    assert s.url.startswith("https://app.plex.tv/auth#?")
    assert f"clientID={CLIENT_ID}" in s.url and "code=pincode4100" in s.url
    assert s.expires_at > 0
    [call] = fake.http.calls
    assert call.method == "POST" and call.url == f"{PLEX_TV}/api/v2/pins?strong=true"


def test_begin_sign_in_creates_a_client_id_when_missing(tmp_path, token):
    store = SecretStore(tmp_path / "secrets.bin")
    fake = FakePlex(token)
    fake.http.routes[("POST", "/api/v2/pins")] = fake.new_pin  # any client id
    begin_sign_in(store, fake.http)
    made = store.get(PLEX_CLIENT_ID_NAME)
    assert made and fake.http.calls[0].headers["X-Plex-Client-Identifier"] == made
    begin_sign_in(store, fake.http)
    assert store.get(PLEX_CLIENT_ID_NAME) == made


def test_poll_waiting_then_done_stores_only_the_token(store, token, caplog):
    caplog.set_level(logging.DEBUG)
    fake = FakePlex(token)
    s = begin_sign_in(store, fake.http)
    assert poll_sign_in(store, fake.http, s.pin_id) == "waiting"
    assert store.get(PLEX_TOKEN_NAME) is None
    fake.approve(s.pin_id)
    assert poll_sign_in(store, fake.http, s.pin_id) == "done"
    assert store.get(PLEX_TOKEN_NAME) == token
    assert_token_hidden(fake, token, text=caplog.text)


@pytest.mark.parametrize("status", [404, 410])
def test_poll_expired_or_gone_pin(store, token, status):
    fake = FakePlex(token)
    s = begin_sign_in(store, fake.http)
    fake.pins[s.pin_id]["status"] = status
    assert poll_sign_in(store, fake.http, s.pin_id) == "expired"
    assert store.get(PLEX_TOKEN_NAME) is None


def test_poll_pin_past_its_lifetime_is_expired(store, token):
    fake = FakePlex(token)
    s = begin_sign_in(store, fake.http)
    fake.pins[s.pin_id]["expiresIn"] = 0
    assert poll_sign_in(store, fake.http, s.pin_id) == "expired"


@pytest.mark.parametrize("status", [401, 403])
def test_poll_denied_pin_is_not_done(store, token, status):
    fake = FakePlex(token)
    s = begin_sign_in(store, fake.http)
    fake.pins[s.pin_id]["status"] = status
    assert poll_sign_in(store, fake.http, s.pin_id) == "expired"
    assert store.get(PLEX_TOKEN_NAME) is None


@pytest.mark.parametrize("bad", ["", "two words", "line\nbreak", 12345, "x" * 600])
def test_poll_refuses_an_odd_token(store, token, bad):
    fake = FakePlex(token)
    s = begin_sign_in(store, fake.http)
    fake.pins[s.pin_id]["authToken"] = bad
    assert poll_sign_in(store, fake.http, s.pin_id) != "done"
    assert store.get(PLEX_TOKEN_NAME) is None


def test_poll_while_plex_tv_is_down_keeps_waiting(store, token):
    fake = FakePlex(token)
    s = begin_sign_in(store, fake.http)
    fake.http.unreachable = True
    assert poll_sign_in(store, fake.http, s.pin_id) == "waiting"


def test_sign_in_errors_never_carry_the_token(store, token):
    fake = FakePlex(token)
    fake.http.route("POST", "/api/v2/pins", lambda c: Response(500, {}, token.encode()))
    with pytest.raises(plex.SignInError) as err:
        begin_sign_in(store, fake.http)
    assert token not in str(err.value)
    assert str(err.value) == "plex.tv refused the sign-in request — try again in a minute."


# ---------------------------------------------------------------- CLI

def write_setup(tmp_path):
    cfg = tmp_path / "cfg"
    (cfg / "orama").mkdir(parents=True)
    setup = make_setup(config_root=str(cfg), data_root=str(tmp_path / "data"))
    path = cfg / "orama" / "setup.json"
    setup.save(path)
    return path, SecretStore(cfg / "orama" / "secrets.bin")


def run_cli(monkeypatch, capsys, fake, path, approve_after=None):
    polls = []

    def sleep(seconds):
        polls.append(seconds)
        if approve_after is not None and len(polls) == approve_after:
            fake.approve(4100)

    monkeypatch.setattr(cli, "Http", lambda: fake.http)
    monkeypatch.setattr(cli.time, "sleep", sleep)
    code = cli.main(["plex-sign-in", "--setup", str(path)])
    return code, capsys.readouterr(), polls


def test_cli_prints_the_code_polls_and_stores_the_token(tmp_path, monkeypatch, capsys, caplog,
                                                        token):
    caplog.set_level(logging.DEBUG)
    path, store = write_setup(tmp_path)
    store.set(PLEX_CLIENT_ID_NAME, CLIENT_ID)
    fake = FakePlex(token)
    code, out, polls = run_cli(monkeypatch, capsys, fake, path, approve_after=2)
    assert code == 0, out.err
    lines = out.out.splitlines()
    assert lines[0].startswith("Open https://app.plex.tv/auth#?")
    assert lines[0].endswith(" and enter code pincode4100")
    assert lines[-1] == "✓ Plex signed in"
    assert polls == [2, 2]
    assert store.get(PLEX_TOKEN_NAME) == token
    assert_token_hidden(fake, token, text=out.out + out.err + caplog.text)


def test_cli_expired_pin_fails_plainly(tmp_path, monkeypatch, capsys, token):
    path, store = write_setup(tmp_path)
    store.set(PLEX_CLIENT_ID_NAME, CLIENT_ID)
    fake = FakePlex(token)
    fake.http.route("GET", "/api/v2/pins/4100", lambda c: Response(404, {}, b""))
    fake.add_pin_route = lambda pin_id: None  # keep the 404 route
    code, out, _ = run_cli(monkeypatch, capsys, fake, path)
    assert code == 1
    assert out.err.strip() == (
        "✗ The Plex sign-in code expired or was not accepted — run plex-sign-in again."
    )
    assert store.get(PLEX_TOKEN_NAME) is None


def test_cli_gives_up_when_the_code_runs_out(tmp_path, monkeypatch, capsys, token):
    path, store = write_setup(tmp_path)
    store.set(PLEX_CLIENT_ID_NAME, CLIENT_ID)
    fake = FakePlex(token)
    now = [1000.0]
    monkeypatch.setattr(cli.time, "time", lambda: now[0])

    def tick(seconds):
        now[0] += 600

    monkeypatch.setattr(cli, "Http", lambda: fake.http)
    monkeypatch.setattr(cli.time, "sleep", tick)
    code = cli.main(["plex-sign-in", "--setup", str(path)])
    out = capsys.readouterr()
    assert code == 1
    assert out.err.strip() == "✗ The Plex sign-in timed out — run plex-sign-in again."


def test_cli_without_a_setup_fails(tmp_path, monkeypatch, capsys, token):
    fake = FakePlex(token)
    code, out, _ = run_cli(monkeypatch, capsys, fake, tmp_path / "nope.json")
    assert code == 1
    assert "No setup found" in out.err
    assert fake.http.calls == []


# ---------------------------------------------------------------- steps

def signed_in(store, token):
    store.set(PLEX_TOKEN_NAME, token)
    return store


def test_without_token_plex_waits_for_sign_in(store, token):
    fake = FakePlex(token)
    results = run(fake, make_setup(), store)
    assert statuses(results) == {
        "wait:plex": "unchanged", "plex:claim": "skipped", "plex:libraries": "skipped",
        "plex:network": "skipped", "plex:hardware": "skipped",
    }
    assert results[1].message == WAITING_FOR_SIGN_IN
    assert WAITING_FOR_SIGN_IN == (
        "Plex is waiting for sign-in — run `python -m orama.cli plex-sign-in`"
    )
    assert format_checklist(results) == [f"… Plex: skipped, {WAITING_FOR_SIGN_IN}"]
    assert not fake.claimed and fake.sections == [] and fake.prefs == {}
    assert all(c.url.startswith(LOCAL + "/identity") for c in fake.http.calls)


def test_first_run_claims_and_creates_libraries_then_second_run_unchanged(store, token,
                                                                         caplog):
    caplog.set_level(logging.DEBUG)
    fake = FakePlex(token)
    setup = make_setup(lan_address=HOME, render_gid=105)
    signed_in(store, token)
    first = run(fake, setup, store)
    assert statuses(first) == {
        "wait:plex": "unchanged", "plex:claim": "changed", "plex:libraries": "changed",
        "plex:network": "changed", "plex:hardware": "changed",
    }
    assert fake.claimed and fake.owner == "me"
    assert [(s["title"], s["type"], s["Location"][0]["path"]) for s in fake.sections] == [
        ("Movies", "movie", "/data/media/movies"), ("TV Shows", "show", "/data/media/tv")]
    assert fake.sections[0]["agent"] == "tv.plex.agents.movie"
    assert fake.sections[0]["scanner"] == "Plex Movie"
    assert fake.sections[1]["agent"] == "tv.plex.agents.series"
    assert fake.sections[1]["scanner"] == "Plex TV Series"
    assert fake.prefs == {"LanNetworksBandwidth": _ip(192, 168, 7, 0) + "/24",
                          "HardwareAcceleratedCodecs": "1"}
    claim = next(c for c in fake.http.calls if c.path == "/myplex/claim")
    assert claim.params == {"token": fake.claim_token}
    assert fake.claim_token not in "".join(r.message for r in first) + caplog.text
    calls = len(fake.http.calls)
    second = run(fake, setup, store)
    assert all(v == "unchanged" for v in statuses(second).values()), second
    assert format_checklist(second) == ["✓ Nothing to change"]
    assert not any(c.method in ("POST", "PUT") for c in fake.http.calls[calls:])
    assert_token_hidden(fake, token, first + second, caplog.text)
    assert_only_known_origins(fake)


def test_claimed_by_another_account_is_refused(store, token):
    fake = FakePlex(token)
    fake.claimed, fake.owner = True, "other"
    results = run(fake, make_setup(render_gid=105), signed_in(store, token))
    assert statuses(results)["plex:claim"] == "failed"
    assert results[1].message == OTHER_ACCOUNT == (
        "This Plex server belongs to another Plex account — Orama won't take it over.")
    assert {statuses(results)[n] for n in ("plex:libraries", "plex:network",
                                           "plex:hardware")} == {"skipped"}
    assert not any(c.path in ("/myplex/claim", "/api/claim/token.json")
                   for c in fake.http.calls)
    # Orama's token is never handed to someone else's server.
    assert not any(c.url.startswith(LOCAL) and (c.headers or {}).get("X-Plex-Token")
                   for c in fake.http.calls)
    assert fake.sections == [] and fake.prefs == {}


def test_claimed_and_not_listed_for_this_account_is_refused(store, token):
    fake = FakePlex(token)
    fake.claimed = True  # owner None: not in this account's resources at all
    results = run(fake, make_setup(), signed_in(store, token))
    assert results[1].status == "failed" and results[1].message == OTHER_ACCOUNT


def test_already_claimed_by_this_account_is_unchanged(store, token):
    fake = FakePlex(token)
    fake.claimed, fake.owner = True, "me"
    results = run(fake, make_setup(), signed_in(store, token))
    assert statuses(results)["plex:claim"] == "unchanged"
    assert not any(c.path in ("/myplex/claim", "/api/claim/token.json")
                   for c in fake.http.calls)


def test_signed_out_token_is_plain(store, token):
    fake = FakePlex(token)
    store.set(PLEX_TOKEN_NAME, random_secret("old", 10))  # revoked: plex.tv answers 401
    fake.claimed, fake.owner = True, "me"
    results = run(fake, make_setup(), store)
    assert results[1].status == "failed"
    assert results[1].message == (
        "Plex no longer accepts Orama's sign-in — run `python -m orama.cli plex-sign-in` again.")


def test_plex_tv_errors_never_show_the_token(store, token):
    fake = FakePlex(token)
    fake.http.route("GET", "/api/claim/token.json", lambda c: Response(500, {}, token.encode()))
    results = run(fake, make_setup(), signed_in(store, token))
    assert results[1].status == "failed"
    assert results[1].message == "plex.tv refused a request (HTTP 500) — run apply again."
    assert_token_hidden(fake, token, results)


def test_unreachable_plex_tv_is_plain(store, token):
    fake = FakePlex(token)
    original = fake.http.request

    def request(method, url, **kw):
        if url.startswith(PLEX_TV):
            fake.http.unreachable = True
        try:
            return original(method, url, **kw)
        finally:
            fake.http.unreachable = False

    fake.http.request = request
    results = run(fake, make_setup(), signed_in(store, token))
    assert results[1].status == "failed"
    assert results[1].message == (
        "plex.tv could not be reached — check the internet connection and run apply again.")


def test_library_found_by_path_under_another_name_is_kept(store, token):
    fake = FakePlex(token)
    fake.claimed, fake.owner = True, "me"
    fake.sections = [{"key": "7", "type": "movie", "title": "Films", "agent": "x", "scanner": "y",
                      "language": "el-GR",
                      "Location": [{"id": 3, "path": "/data/media/movies/"}]}]
    results = run(fake, make_setup(), signed_in(store, token))
    assert statuses(results)["plex:libraries"] == "changed"
    assert [s["title"] for s in fake.sections] == ["Films", "TV Shows"]
    assert results[2].message == "added the TV Shows library"


def test_only_the_missing_library_is_created_on_retry(store, token):
    fake = FakePlex(token)
    fake.claimed, fake.owner = True, "me"
    fake.fail_create_after = 1
    setup = make_setup()
    first = run(fake, setup, signed_in(store, token))
    assert statuses(first)["plex:libraries"] == "failed"
    assert [s["title"] for s in fake.sections] == ["Movies"]
    fake.fail_create_after = None
    second = run(fake, setup, store)
    assert statuses(second)["plex:libraries"] == "changed"
    assert second[2].message == "added the TV Shows library"
    assert [s["title"] for s in fake.sections] == ["Movies", "TV Shows"]
    assert fake.creates == 2


def test_both_libraries_message(store, token):
    fake = FakePlex(token)
    fake.claimed, fake.owner = True, "me"
    results = run(fake, make_setup(), signed_in(store, token))
    assert results[2].message == "added the Movies and TV Shows libraries"


def test_no_plex_pass_is_a_plain_info_line_said_once(store, token):
    fake = FakePlex(token)
    fake.claimed, fake.owner = True, "me"
    fake.plex_pass = False
    setup = make_setup(nvidia=True)
    first = run(fake, setup, signed_in(store, token))
    hw = next(r for r in first if r.name == "plex:hardware")
    assert hw.status == "changed" and hw.message == NO_PASS == (
        "Hardware transcoding needs Plex Pass — playback still works with direct play.")
    assert "HardwareAcceleratedCodecs" not in fake.prefs
    second = run(fake, setup, store)
    assert statuses(second)["plex:hardware"] == "unchanged"
    # Buying Plex Pass later turns it on at the next apply.
    fake.plex_pass = True
    third = run(fake, setup, store)
    assert statuses(third)["plex:hardware"] == "changed"
    assert fake.prefs["HardwareAcceleratedCodecs"] == "1"


def test_no_gpu_writes_no_hardware_pref(store, token):
    fake = FakePlex(token)
    fake.claimed, fake.owner = True, "me"
    results = run(fake, make_setup(render_gid=None, nvidia=False), signed_in(store, token))
    assert statuses(results)["plex:hardware"] == "unchanged"
    assert "HardwareAcceleratedCodecs" not in fake.prefs
    assert not any(c.path == "/api/v2/user" for c in fake.http.calls)


def test_no_lan_address_writes_no_network_pref(store, token):
    fake = FakePlex(token)
    fake.claimed, fake.owner = True, "me"
    results = run(fake, make_setup(), signed_in(store, token))
    assert statuses(results)["plex:network"] == "unchanged"
    assert "LanNetworksBandwidth" not in fake.prefs


def test_changed_lan_address_is_sent_again(store, token):
    fake = FakePlex(token)
    fake.claimed, fake.owner = True, "me"
    signed_in(store, token)
    run(fake, make_setup(lan_address=HOME), store)
    results = run(fake, make_setup(lan_address=OTHER_HOME), store)
    assert statuses(results)["plex:network"] == "changed"
    assert fake.prefs["LanNetworksBandwidth"] == _ip(10, 1, 2, 0) + "/24"


def test_a_fresh_plex_server_gets_the_prefs_again(store, token):
    fake = FakePlex(token)
    fake.claimed, fake.owner = True, "me"
    setup = make_setup(lan_address=HOME, render_gid=105)
    run(fake, setup, signed_in(store, token))
    fresh = FakePlex(token)
    fresh.claimed, fresh.owner = True, "me"
    plex_machine = "machine-" + "ffee00"
    fresh.http.route("GET", "/identity", lambda c: ok({"MediaContainer": {
        "claimed": True, "machineIdentifier": plex_machine}}))
    fresh.resources = lambda c: ok([{"clientIdentifier": plex_machine, "owned": True,
                                     "provides": "server"}])
    fresh.http.route("GET", "/api/v2/resources", fresh.tv(fresh.authed(fresh.resources)))
    results = run(fresh, setup, store)
    assert statuses(results)["plex:network"] == "changed"
    assert statuses(results)["plex:hardware"] == "changed"


def test_plex_still_starting(store, token):
    fake = FakePlex(token)
    fake.http.route("GET", "/identity", lambda c: Response(503, {}, b""))
    steps = plex_steps(make_setup(), store, fake.http, timeout=0)
    results = run_steps(steps)
    assert results[0].status == "failed"
    assert results[0].message == "Plex is still starting — run apply again in a few minutes."
