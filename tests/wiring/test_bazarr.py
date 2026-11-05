import json

from conftest import make_setup

from orama.secrets import SecretStore
from orama.wiring.bazarr import NO_ACCOUNT_PROVIDERS, bazarr_steps
from orama.wiring.engine import run_steps
from orama.wiring.http import FakeHttp, Response

KEY = "bzkey" * 4
SONARR_KEY = "sonkey" * 4
RADARR_KEY = "radkey" * 4
OS_USER = "os-user"
OS_PASS = "os&pass=1 Ταινίες"

API = "/api/system"


class FakeBazarr:
    """Just enough of Bazarr 1.6: settings in sections, written by form fields."""

    def __init__(self):
        self.settings = {
            "general": {
                "use_sonarr": False, "use_radarr": False, "enabled_providers": [],
                "serie_default_enabled": False, "serie_default_profile": "",
                "movie_default_enabled": False, "movie_default_profile": "",
                "ignore_ass_subs": False, "ignore_pgs_subs": False,
                "ignore_vobsub_subs": False, "minimum_score": 90,
            },
            "sonarr": {"ip": "127.0.0.1", "port": 8989, "base_url": "/", "apikey": "", "ssl": False},
            "radarr": {"ip": "127.0.0.1", "port": 7878, "base_url": "/", "apikey": "", "ssl": False},
            "opensubtitlescom": {"username": "", "password": ""},
        }
        self.languages = {"el": False, "en": False, "fr": False}
        self.profiles = []
        self.writes = []  # one dict of form fields per POST
        self.http = FakeHttp()
        r = self.http.route
        r("GET", f"{API}/status", self.guard(lambda c: self.ok({"data": {}})))
        r("GET", f"{API}/settings", self.guard(lambda c: self.ok(self.settings)))
        r("POST", f"{API}/settings", self.guard(self.post))
        r("GET", f"{API}/languages", self.guard(self.get_languages))
        r("GET", f"{API}/languages/profiles", self.guard(lambda c: self.ok(self.profiles)))

    def guard(self, handler):
        def run(call):
            assert call.headers == {"X-API-KEY": KEY}
            assert call.params is None or "apikey" not in call.params
            return handler(call)

        return run

    @staticmethod
    def ok(data):
        return Response(200, {}, json.dumps(data).encode())

    def get_languages(self, call):
        return self.ok([{"code2": c, "code3": c, "name": c, "enabled": e}
                        for c, e in self.languages.items()])

    @staticmethod
    def typed(value):
        if value in ("true", "false"):
            return value == "true"
        return int(value) if value.isdigit() else value

    def post(self, call):
        form = dict(call.form)
        self.writes.append(form)
        for name, value in form.items():
            if name == "languages-enabled":
                self.languages = {c: c in value for c in self.languages}
            elif name == "languages-profiles":
                self.profiles = json.loads(value)
            else:
                _, section, field = name.split("-", 2)
                if field == "enabled_providers":
                    self.settings[section][field] = list(value)
                else:
                    self.settings[section][field] = self.typed(value)
        return Response(204, {}, b"")

    def fields_written(self):
        return [sorted(w) for w in self.writes]

    def orama_profile(self):
        return next(p for p in self.profiles if p["name"] == "Orama")


def make(tmp_path, *, opensubtitles=False, **overrides):
    store = SecretStore(tmp_path / "secrets.bin")
    store.set("apikey:bazarr", KEY)
    store.set("apikey:sonarr", SONARR_KEY)
    store.set("apikey:radarr", RADARR_KEY)
    if opensubtitles:
        store.set("opensubtitles:username", OS_USER)
        store.set("opensubtitles:password", OS_PASS)
    return make_setup(**overrides), store


def statuses(results):
    return {r.name: r.status for r in results}


def run(bz, setup, store):
    return run_steps(bazarr_steps(setup, store, bz.http))


def test_first_run_writes_each_once_then_second_run_unchanged(tmp_path):
    bz = FakeBazarr()
    setup, store = make(tmp_path)
    first = run(bz, setup, store)
    assert statuses(first) == {
        "wait:bazarr": "unchanged", "bazarr:apps": "changed", "bazarr:languages": "changed",
        "bazarr:providers": "changed", "bazarr:format": "changed",
    }
    s = bz.settings
    assert s["general"]["use_sonarr"] is True and s["general"]["use_radarr"] is True
    assert (s["sonarr"]["ip"], s["sonarr"]["port"], s["sonarr"]["apikey"]) == (
        "sonarr", 8989, SONARR_KEY)
    assert (s["radarr"]["ip"], s["radarr"]["port"], s["radarr"]["apikey"]) == (
        "radarr", 7878, RADARR_KEY)
    assert bz.languages == {"el": True, "en": True, "fr": False}
    profile = bz.orama_profile()
    assert [i["language"] for i in profile["items"]] == ["el", "en"]
    assert s["general"]["serie_default_enabled"] is True
    assert s["general"]["serie_default_profile"] == profile["profileId"]
    assert s["general"]["movie_default_profile"] == profile["profileId"]
    assert set(s["general"]["enabled_providers"]) == set(NO_ACCOUNT_PROVIDERS)
    assert s["general"]["ignore_ass_subs"] is True
    assert s["general"]["ignore_pgs_subs"] is True
    assert s["general"]["ignore_vobsub_subs"] is True
    assert next(r for r in first if r.name == "bazarr:apps").message == (
        "linked Bazarr to Sonarr and Radarr")
    assert len(bz.writes) == 4  # apps, languages, providers, format: one write each
    second = run(bz, setup, store)
    assert all(v == "unchanged" for v in statuses(second).values())
    assert len(bz.writes) == 4


def test_language_order_change_rewrites_only_the_profile(tmp_path):
    bz = FakeBazarr()
    setup, store = make(tmp_path)
    run(bz, setup, store)
    other = {"profileId": 99, "name": "Mine", "cutoff": None, "items": [], "mustContain": [],
             "mustNotContain": [], "originalFormat": None, "tag": None}
    bz.profiles.append(other)
    before = {k: json.dumps(v, sort_keys=True) for k, v in bz.settings.items()}
    n = len(bz.writes)
    swapped = make_setup(subtitle_languages=("en", "el"))
    results = run(bz, swapped, store)
    assert statuses(results)["bazarr:languages"] == "changed"
    assert all(v == "unchanged" for k, v in statuses(results).items() if k != "bazarr:languages")
    assert bz.fields_written()[n:] == [["languages-profiles"]]
    assert [i["language"] for i in bz.orama_profile()["items"]] == ["en", "el"]
    assert other in bz.profiles  # the user's own profile is kept
    assert {k: json.dumps(v, sort_keys=True) for k, v in bz.settings.items()} == before
    back = run(bz, setup, store)  # and back to Greek first
    assert statuses(back)["bazarr:languages"] == "changed"
    assert [i["language"] for i in bz.orama_profile()["items"]] == ["el", "en"]
    assert bz.fields_written()[n + 1:] == [["languages-profiles"]]


def test_profile_keeps_its_id_and_other_languages_stay_enabled(tmp_path):
    bz = FakeBazarr()
    bz.languages["fr"] = True  # user enabled French themselves
    setup, store = make(tmp_path)
    run(bz, setup, store)
    assert bz.languages["fr"] is True
    pid = bz.orama_profile()["profileId"]
    run(bz, make_setup(subtitle_languages=("en", "el")), store)
    assert bz.orama_profile()["profileId"] == pid


def test_new_profile_id_does_not_clash_with_users_profiles(tmp_path):
    bz = FakeBazarr()
    bz.profiles = [{"profileId": 4, "name": "Mine", "cutoff": None, "items": [],
                    "mustContain": [], "mustNotContain": [], "originalFormat": None, "tag": None}]
    setup, store = make(tmp_path)
    run(bz, setup, store)
    assert bz.orama_profile()["profileId"] == 5
    assert len(bz.profiles) == 2


def test_disabled_sonarr_turns_the_link_off_and_is_not_waited_for(tmp_path):
    bz = FakeBazarr()
    setup, store = make(tmp_path)
    run(bz, setup, store)
    no_sonarr = make_setup(removed_apps={"sonarr"})
    steps = bazarr_steps(no_sonarr, store, bz.http)
    assert "wait:sonarr" not in next(s for s in steps if s.name == "bazarr:apps").needs
    results = run_steps(steps)
    assert statuses(results)["bazarr:apps"] == "changed"
    assert bz.settings["general"]["use_sonarr"] is False
    assert bz.settings["general"]["use_radarr"] is True
    assert bz.fields_written()[-1] == ["settings-general-use_sonarr"]
    assert next(r for r in results if r.name == "bazarr:apps").message == (
        "unlinked Bazarr from Sonarr")
    again = run_steps(bazarr_steps(no_sonarr, store, bz.http))
    assert statuses(again)["bazarr:apps"] == "unchanged"


def test_apps_step_waits_for_the_arrs(tmp_path):
    bz = FakeBazarr()
    setup, store = make(tmp_path)
    apps = next(s for s in bazarr_steps(setup, store, bz.http) if s.name == "bazarr:apps")
    assert set(apps.needs) == {"wait:bazarr", "wait:sonarr", "wait:radarr"}


def test_user_edit_to_the_link_is_put_back(tmp_path):
    bz = FakeBazarr()
    setup, store = make(tmp_path)
    run(bz, setup, store)
    bz.settings["radarr"]["port"] = 9999
    n = len(bz.writes)
    results = run(bz, setup, store)
    assert statuses(results)["bazarr:apps"] == "changed"
    assert bz.settings["radarr"]["port"] == 7878
    assert bz.fields_written()[n:] == [["settings-radarr-port"]]
    assert next(r for r in results if r.name == "bazarr:apps").message == (
        "linked Bazarr to Radarr")


def test_format_writes_only_the_image_subtitle_switches_that_differ(tmp_path):
    bz = FakeBazarr()
    bz.settings["general"]["ignore_ass_subs"] = True
    bz.settings["general"]["ignore_vobsub_subs"] = True
    setup, store = make(tmp_path)
    results = run(bz, setup, store)
    assert statuses(results)["bazarr:format"] == "changed"
    assert bz.fields_written()[-1] == ["settings-general-ignore_pgs_subs"]
    assert bz.settings["general"]["ignore_pgs_subs"] is True
    n = len(bz.writes)
    assert statuses(run(bz, setup, store))["bazarr:format"] == "unchanged"
    assert len(bz.writes) == n


def test_without_opensubtitles_credentials_it_is_not_enabled_and_no_error(tmp_path):
    bz = FakeBazarr()
    setup, store = make(tmp_path)
    results = run(bz, setup, store)
    assert all(r.status != "failed" for r in results)
    assert "opensubtitlescom" not in bz.settings["general"]["enabled_providers"]
    assert bz.settings["opensubtitlescom"]["username"] == ""
    store2 = SecretStore(tmp_path / "secrets2.bin")  # only one of the two present
    for name in ("apikey:bazarr", "apikey:sonarr", "apikey:radarr"):
        store2.set(name, store.get(name))
    store2.set("opensubtitles:username", OS_USER)
    bz2 = FakeBazarr()
    run(bz2, setup, store2)
    assert "opensubtitlescom" not in bz2.settings["general"]["enabled_providers"]


def test_opensubtitles_enabled_with_both_secrets_and_sent_once(tmp_path):
    bz = FakeBazarr()
    setup, store = make(tmp_path, opensubtitles=True)
    run(bz, setup, store)
    s = bz.settings
    assert "opensubtitlescom" in s["general"]["enabled_providers"]
    assert s["opensubtitlescom"] == {"username": OS_USER, "password": OS_PASS}
    n = len(bz.writes)
    second = run(bz, setup, store)
    assert statuses(second)["bazarr:providers"] == "unchanged"
    assert len(bz.writes) == n
    store.set("opensubtitles:password", "another pass")
    third = run(bz, setup, store)
    assert statuses(third)["bazarr:providers"] == "changed"
    assert bz.settings["opensubtitlescom"]["password"] == "another pass"


def test_providers_added_by_the_user_are_kept(tmp_path):
    bz = FakeBazarr()
    bz.settings["general"]["enabled_providers"] = ["addic7ed", "podnapisi"]
    setup, store = make(tmp_path)
    run(bz, setup, store)
    enabled = bz.settings["general"]["enabled_providers"]
    assert "addic7ed" in enabled and set(NO_ACCOUNT_PROVIDERS) <= set(enabled)
    assert enabled.count("podnapisi") == 1


def test_keys_and_passwords_never_in_messages_or_urls(tmp_path):
    bz = FakeBazarr()
    setup, store = make(tmp_path, opensubtitles=True)
    results = run(bz, setup, store)
    text = " ".join(r.message for r in results)
    for secret in (KEY, SONARR_KEY, RADARR_KEY, OS_PASS):
        assert secret not in text
        assert all(secret not in c.url for c in bz.http.calls)
        assert all(c.params is None for c in bz.http.calls)


def test_missing_bazarr_key_fails_plainly(tmp_path):
    bz = FakeBazarr()
    setup, _store = make(tmp_path)
    empty = SecretStore(tmp_path / "empty.bin")
    results = run_steps(bazarr_steps(setup, empty, bz.http))
    assert statuses(results)["wait:bazarr"] == "failed"
    assert results[0].message == "Bazarr's API key is missing — run apply again."


def test_unreachable_and_bad_key_are_plain_failures(tmp_path):
    bz = FakeBazarr()
    setup, store = make(tmp_path)
    bz.http.unreachable = True
    steps = [s for s in bazarr_steps(setup, store, bz.http) if s.name != "wait:bazarr"]
    results = run_steps(steps)
    bad = next(r for r in results if r.status == "failed")
    assert "could not be reached" in bad.message and KEY not in bad.message
    bz2 = FakeBazarr()
    bz2.http.route("GET", f"{API}/settings", lambda c: Response(401, {}, b""))
    results = run(bz2, setup, store)
    assert statuses(results)["bazarr:apps"] == "failed"
    assert "did not accept" in next(r for r in results if r.name == "bazarr:apps").message
