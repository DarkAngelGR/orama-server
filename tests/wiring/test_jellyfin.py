import json
import urllib.parse

import pytest
from conftest import make_setup, random_secret

from orama.secrets import JELLYFIN_KEY_NAME, JELLYFIN_PASSWORD_NAME, SecretStore
from orama.wiring.engine import StepError, run_steps
from orama.wiring.http import FakeHttp, Response
from orama.wiring.jellyfin import ADMIN_USER, WRONG_PASSWORD, jellyfin_steps

BASE = "http://jellyfin:8096"


def ok(data, status=200):
    return Response(status, {}, json.dumps(data).encode())


class FakeJellyfin:
    """Jellyfin on orama-net, just enough of its API, behind one FakeHttp."""

    def __init__(self, password):
        self.password = password  # what the admin account really uses
        self.completed = False
        self.startup_name = ""  # the wizard's single user
        self.users_created = 0
        self.config_posts = 0
        self.fail_complete = 0  # fail this many /Startup/Complete calls
        self.config = {"ServerName": "x", "UICulture": "en-US", "MetadataCountryCode": "US",
                       "PreferredMetadataLanguage": "en"}
        self.session_tokens = set()
        self.keys = []  # {"AccessToken", "AppName"}
        self.folders = []  # {"Name", "CollectionType", "Locations"}
        self.encoding = {"HardwareAccelerationType": "none", "VaapiDevice": "",
                         "EnableHardwareEncoding": True, "EncoderPreset": "auto"}
        self.encoding_posts = []
        self.http = FakeHttp()
        r = self.http.route
        r("GET", "/System/Info/Public", lambda c: ok({"StartupWizardCompleted": self.completed}))
        r("GET", "/Startup/Configuration", self.wizard(lambda c: ok(self.config)))
        r("POST", "/Startup/Configuration", self.wizard(self.post_config))
        r("GET", "/Startup/User", self.wizard(lambda c: ok({"Name": self.startup_name})))
        r("POST", "/Startup/User", self.wizard(self.post_user))
        r("POST", "/Startup/Complete", self.wizard(self.complete))
        r("POST", "/Users/AuthenticateByName", self.authenticate)
        r("GET", "/Auth/Keys", self.authed(lambda c: ok({"Items": self.keys})))
        r("POST", "/Auth/Keys", self.authed(self.new_key))
        r("GET", "/Library/VirtualFolders", self.authed(lambda c: ok(self.folders)))
        r("POST", "/Library/VirtualFolders", self.authed(self.new_folder))
        r("GET", "/System/Configuration/encoding", self.authed(lambda c: ok(self.encoding)))
        r("POST", "/System/Configuration/encoding", self.authed(self.post_encoding))

    def wizard(self, handler):
        def run(call):
            if self.completed:
                return Response(401, {}, b"")
            return handler(call)
        return run

    def authed(self, handler):
        def run(call):
            header = call.headers.get("Authorization", "")
            token = header.split('Token="')[1].rstrip('"') if 'Token="' in header else None
            valid = self.session_tokens | {k["AccessToken"] for k in self.keys}
            if not header.startswith("MediaBrowser ") or token not in valid:
                return Response(401, {}, b"")
            return handler(call)
        return run

    def post_config(self, call):
        self.config_posts += 1
        return Response(204, {}, b"")

    def post_user(self, call):
        if self.startup_name == "":
            self.users_created += 1
        self.startup_name = call.json["Name"]
        self.password = call.json["Password"]
        return Response(204, {}, b"")

    def complete(self, call):
        if self.fail_complete:
            self.fail_complete -= 1
            return Response(500, {}, b"")
        self.completed = True
        return Response(204, {}, b"")

    def authenticate(self, call):
        assert call.headers["Authorization"].startswith("MediaBrowser Client=")
        body = call.json
        if (not self.completed and not self.startup_name) or body["Username"] != (
            self.startup_name
        ) or body["Pw"] != self.password:
            return Response(401, {}, b"")
        token = random_secret("sess")
        self.session_tokens.add(token)
        return ok({"AccessToken": token, "User": {"Name": self.startup_name}})

    def new_key(self, call):
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(call.url).query)
        self.keys.append({"AccessToken": random_secret("key"), "AppName": query["app"][0]})
        return Response(204, {}, b"")

    def new_folder(self, call):
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(call.url).query)
        paths = [p["Path"] for p in call.json["LibraryOptions"]["PathInfos"]]
        self.folders.append({"Name": query["name"][0], "CollectionType": query["collectionType"][0],
                             "Locations": paths})
        return Response(204, {}, b"")

    def post_encoding(self, call):
        self.encoding = call.json
        self.encoding_posts.append(call.json)
        return Response(204, {}, b"")

    def writes(self):
        return [c for c in self.http.calls if c.method != "GET"]


@pytest.fixture
def password():
    return random_secret("pw")


@pytest.fixture
def store(tmp_path, password):
    s = SecretStore(tmp_path / "secrets.bin")
    s.set(JELLYFIN_PASSWORD_NAME, password)
    return s


def run(fake, store, **setup):
    steps = jellyfin_steps(make_setup(media_servers={"jellyfin"}, **setup), store, fake.http)
    return {r.name: r for r in run_steps(steps)}


def assert_hidden(fake, store, results, *secrets):
    text = " ".join(r.message for r in results.values())
    for secret in (*secrets, store.get(JELLYFIN_KEY_NAME)):
        if secret:
            assert secret not in text


def test_fresh_server_runs_the_wizard_once_then_is_unchanged(store, password):
    fake = FakeJellyfin(password)
    first = run(fake, store)
    assert [r.status for r in first.values()] == [
        "unchanged", "changed", "unchanged", "changed", "changed", "unchanged"]
    assert fake.completed and fake.users_created == 1 and fake.startup_name == ADMIN_USER
    assert fake.config_posts == 1
    assert [f["Locations"] for f in fake.folders] == [["/data/media/movies"], ["/data/media/tv"]]
    assert [f["CollectionType"] for f in fake.folders] == ["movies", "tvshows"]
    assert [f["Name"] for f in fake.folders] == ["Movies", "TV Shows"]
    assert store.get(JELLYFIN_KEY_NAME) == fake.keys[0]["AccessToken"]
    assert fake.keys[0]["AppName"] == "Orama"
    fake.http.calls.clear()
    second = run(fake, store)
    assert {r.status for r in second.values()} == {"unchanged"}
    assert fake.writes() == [c for c in fake.writes() if c.path == "/Users/AuthenticateByName"]


def test_failure_after_the_user_is_made_resumes_without_a_second_admin(store, password):
    fake = FakeJellyfin(password)
    fake.fail_complete = 1
    first = run(fake, store)
    assert first["jellyfin:startup"].status == "failed"
    assert first["jellyfin:auth"].status == "skipped"
    assert not fake.completed and fake.users_created == 1
    again = run(fake, store)
    assert again["jellyfin:startup"].status == "changed"
    assert fake.completed and fake.users_created == 1
    assert again["jellyfin:libraries"].status == "changed"


def test_completed_server_with_another_password_gets_the_plain_message(store, password):
    fake = FakeJellyfin(random_secret("other"))
    fake.completed = True
    fake.startup_name = ADMIN_USER
    results = run(fake, store)
    assert results["jellyfin:auth"].status == "failed"
    assert results["jellyfin:auth"].message == WRONG_PASSWORD
    for name in ("jellyfin:api-key", "jellyfin:libraries", "jellyfin:hardware"):
        assert results[name].status == "skipped"
    assert all(c.path not in ("/Startup/User", "/Startup/Complete") or c.method == "GET"
               for c in fake.http.calls if c.path.startswith("/Startup"))
    assert fake.writes() and all(c.path == "/Users/AuthenticateByName" for c in fake.writes())
    assert_hidden(fake, store, results, password, fake.password)


def test_existing_library_at_the_path_is_kept(store, password):
    fake = FakeJellyfin(password)
    fake.folders = [{"Name": "My films", "CollectionType": "movies",
                     "Locations": ["/data/media/movies/"]}]
    results = run(fake, store)
    assert results["jellyfin:libraries"].message == "added the TV Shows library"
    assert [f["Name"] for f in fake.folders] == ["My films", "TV Shows"]


def test_a_lost_api_key_is_replaced_and_a_good_one_kept(store, password):
    fake = FakeJellyfin(password)
    run(fake, store)
    good = store.get(JELLYFIN_KEY_NAME)
    store.set(JELLYFIN_KEY_NAME, random_secret("gone"))
    results = run(fake, store)
    assert results["jellyfin:api-key"].status == "changed"
    assert len(fake.keys) == 2
    new = store.get(JELLYFIN_KEY_NAME)
    assert new == fake.keys[1]["AccessToken"] and new != good
    assert_hidden(fake, store, results, password, new, good)


def test_no_gpu_means_no_encoding_write(store, password):
    fake = FakeJellyfin(password)
    results = run(fake, store)
    assert results["jellyfin:hardware"].status == "unchanged"
    assert fake.encoding_posts == []


def test_dri_turns_on_vaapi_once(store, password):
    fake = FakeJellyfin(password)
    results = run(fake, store, render_gid=107)
    assert results["jellyfin:hardware"].status == "changed"
    assert fake.encoding["HardwareAccelerationType"] == "vaapi"
    assert fake.encoding["VaapiDevice"] == "/dev/dri/renderD128"
    assert fake.encoding["EncoderPreset"] == "auto"  # the rest is kept
    assert run(fake, store, render_gid=107)["jellyfin:hardware"].status == "unchanged"
    assert len(fake.encoding_posts) == 1


def test_nvidia_turns_on_nvenc(store, password):
    fake = FakeJellyfin(password)
    run(fake, store, nvidia=True)
    assert fake.encoding["HardwareAccelerationType"] == "nvenc"


def test_an_owner_chosen_acceleration_is_respected(store, password):
    fake = FakeJellyfin(password)
    fake.encoding["HardwareAccelerationType"] = "qsv"
    results = run(fake, store, render_gid=107)
    assert results["jellyfin:hardware"].status == "unchanged"
    assert fake.encoding_posts == []


def test_unreachable_jellyfin_fails_plainly(store, password):
    fake = FakeJellyfin(password)
    fake.http.unreachable = True
    steps = jellyfin_steps(make_setup(media_servers={"jellyfin"}), store, fake.http, timeout=0.0)
    results = {r.name: r for r in run_steps(steps)}
    assert results["wait:jellyfin"].status == "failed"
    assert results["jellyfin:startup"].status == "skipped"


def test_wait_step_checks_public_info(store, password):
    fake = FakeJellyfin(password)
    run(fake, store)
    assert fake.http.calls[0].url == f"{BASE}/System/Info/Public"


def test_missing_password_is_a_plain_failure(tmp_path, password):
    fake = FakeJellyfin(password)
    empty = SecretStore(tmp_path / "other.bin")
    steps = jellyfin_steps(make_setup(media_servers={"jellyfin"}), empty, fake.http)
    results = {r.name: r for r in run_steps(steps)}
    assert results["jellyfin:startup"].status == "failed"
    assert not fake.completed
    with pytest.raises(StepError):
        steps[1].run()


def test_wait_keeps_waiting_while_the_server_does_not_yet_know_its_setup_state(store, password):
    fake = FakeJellyfin(password)
    answers = iter([{"ProductName": "Jellyfin"}, {"StartupWizardCompleted": False}])
    fake.http.route("GET", "/System/Info/Public", lambda c: ok(next(answers)))
    wait = jellyfin_steps(make_setup(media_servers={"jellyfin"}), store, fake.http)[0]
    from orama.wiring import engine
    original = engine.time.sleep
    engine.time.sleep = lambda s: None
    try:
        wait.run()
    finally:
        engine.time.sleep = original
    assert len(fake.http.calls) == 2
