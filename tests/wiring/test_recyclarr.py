"""The recyclarr:sync step: compiled device profile to the Agent, then checked in each *arr."""

import json
import urllib.parse

from conftest import make_setup

from orama.agent_client import AgentError
from orama.profiles.compile import UNPLAYABLE_SCORE, compile_profile
from orama.wiring.engine import run_steps
from orama.wiring.http import Call, Response
from orama.wiring.recyclarr import PROFILE_NAME, format_names, recyclarr_steps
from orama_agent import recyclarr as agent_recyclarr

SONARR_KEY = "sonkey" * 4
RADARR_KEY = "radkey" * 4
KEYS = {"sonarr": SONARR_KEY, "radarr": RADARR_KEY}
PORTS = {"sonarr": 8989, "radarr": 7878}


class Store:
    def __init__(self, values):
        self.values = dict(values)

    def get(self, name):
        return self.values.get(name)


def store():
    return Store({f"apikey:{app}": key for app, key in KEYS.items()})


class FakeArr:
    """Custom formats and quality profiles of one *arr, as Recyclarr leaves them."""

    def __init__(self, app):
        self.app = app
        self.formats = [{"id": 900, "name": "My Own Format"}]
        self.profiles = [{"id": 1, "name": "Any", "formatItems": [
            {"format": 900, "name": "My Own Format", "score": 42}]}]

    def apply(self, spec):
        """What a successful Recyclarr sync does: the one "Orama" profile, exactly its scores
        (any score it was not given reset), whatever the template."""
        names = format_names(self.app)
        by_name = {f["name"]: f["id"] for f in self.formats}
        for trash_id in sorted(names):
            name = names[trash_id]
            if name not in by_name:
                by_name[name] = len(self.formats) + 1
                self.formats.append({"id": by_name[name], "name": name})
        items = [{"format": by_name[names[t]], "name": names[t], "score": s}
                 for t, s in spec["scores"].items()]
        items += [{"format": 900, "name": "My Own Format", "score": 0}]
        mine = next((p for p in self.profiles if p["name"] == "Orama"), None)
        if mine is None:
            mine = {"id": 10, "name": "Orama"}
            self.profiles.append(mine)
        mine["formatItems"] = items
        mine["template"] = spec["templates"][0]

    def route(self, call):
        assert call.headers == {"X-Api-Key": KEYS[self.app]}
        assert call.method == "GET"  # Orama only reads: Recyclarr does the writing
        if call.path == "/api/v3/customformat":
            return Response(200, {}, json.dumps(self.formats).encode())
        if call.path == "/api/v3/qualityprofile":
            return Response(200, {}, json.dumps(self.profiles).encode())
        return Response(404, {}, b"")


class FakeHttp:
    def __init__(self, arrs):
        self.arrs = arrs
        self.calls = []

    def request(self, method, url, *, headers=None, params=None, json=None, form=None,
                timeout=30.0):
        parts = urllib.parse.urlsplit(url)
        call = Call(method.upper(), url, parts.path, headers, params, json, form)
        self.calls.append(call)
        app = parts.hostname
        assert parts.port == PORTS[app]
        return self.arrs[app].route(call)


class FakeAgent:
    """The Agent's recyclarr-sync: unchanged while the request is the same, unless forced."""

    def __init__(self, arrs, *, works=True, error=None):
        self.arrs = arrs
        self.works = works
        self.error = error
        self.calls = []
        self._last = None

    def recyclarr_sync(self, apps, force=False):
        self.calls.append((json.loads(json.dumps(apps)), force))
        if self.error:
            raise AgentError(self.error)
        if apps == self._last and not force:
            return False
        self._last = json.loads(json.dumps(apps))
        if self.works:
            for app, spec in apps.items():
                self.arrs[app].apply(spec)
        return True


def world(**agent_kw):
    arrs = {app: FakeArr(app) for app in PORTS}
    return arrs, FakeHttp(arrs), FakeAgent(arrs, **agent_kw)


def steps_for(setup, http, agent):
    return recyclarr_steps(setup, store(), http, agent)


def test_step_needs_the_arrs_and_sends_the_compiled_profile():
    _arrs, http, agent = world()
    setup = make_setup(device="samsung_s95f", quality="mixed")
    (step,) = steps_for(setup, http, agent)
    assert step.name == "recyclarr:sync"
    assert set(step.needs) == {"wait:sonarr", "wait:radarr"}
    (result,) = run_steps([step])
    assert result.status == "changed", result.message
    (sent, force), = agent.calls
    assert sent == json.loads(json.dumps(compile_profile("samsung_s95f", "mixed")))
    assert force is True  # Orama's profile was not there yet


def test_second_run_unchanged():
    _arrs, http, agent = world()
    setup = make_setup(device="samsung_s95f")
    run_steps(steps_for(setup, http, agent))
    (result,) = run_steps(steps_for(setup, http, agent))
    assert result.status == "unchanged"
    assert agent.calls[-1][1] is False


def test_changed_only_after_verification():
    _arrs, http, agent = world(works=False)  # Recyclarr "succeeds" but sets nothing
    (result,) = run_steps(steps_for(make_setup(), http, agent))
    assert result.status == "failed"
    assert result.message == (
        "Sonarr and Radarr do not have Orama's quality scores yet — run apply again."
    )
    assert SONARR_KEY not in result.message and RADARR_KEY not in result.message


def test_agent_failure_is_a_plain_step_failure():
    msg = "Updating the quality profiles failed. To see why, run: sudo docker logs x"
    _arrs, http, agent = world(error=msg)
    (result,) = run_steps(steps_for(make_setup(), http, agent))
    assert result.status == "failed" and result.message == msg


def test_scores_lost_in_the_app_are_restored_with_force():
    arrs, http, agent = world()
    setup = make_setup(device="samsung_s95f")
    run_steps(steps_for(setup, http, agent))
    # Someone reset Radarr's profile by hand: same request, but the scores are gone.
    for p in arrs["radarr"].profiles:
        for item in p["formatItems"]:
            item["score"] = 0
    (result,) = run_steps(steps_for(setup, http, agent))
    assert result.status == "changed"
    assert agent.calls[-1][1] is True


def test_device_and_quality_switch_use_the_one_orama_profile():  # [RF3]
    arrs, http, agent = world()
    run_steps(steps_for(make_setup(device="samsung_s95f", quality="4k"), http, agent))
    setup = make_setup(device="apple_tv", quality="1080p")
    (result,) = run_steps(steps_for(setup, http, agent))
    assert result.status == "changed"
    assert result.message == "set the quality profiles for your playback device"
    wanted = compile_profile("apple_tv", "1080p")
    for app, arr in arrs.items():
        orama = [p for p in arr.profiles if p["name"] != "Any"]
        assert [p["name"] for p in orama] == ["Orama"]  # no profile left behind
        assert orama[0]["template"] == wanted[app]["templates"][0]
        names = format_names(app)
        blocked = {i["name"] for i in orama[0]["formatItems"] if i["score"] == UNPLAYABLE_SCORE}
        assert blocked == {names[t] for t, s in wanted[app]["scores"].items()
                           if s == UNPLAYABLE_SCORE}


def test_verification_only_needs_the_scores_orama_sent():
    arrs, http, agent = world()
    run_steps(steps_for(make_setup(device="samsung_s95f"), http, agent))
    # A -10000 Orama no longer sends (or TRaSH's own score) does not fail the check:
    # Recyclarr's reset_unmatched_scores owns clearing it.
    new = compile_profile("apple_tv", "mixed")["radarr"]["scores"]
    names = format_names("radarr")
    extra = next(t for t in names if t not in new)
    fid = next(f["id"] for f in arrs["radarr"].formats if f["name"] == names[extra])
    run_steps(steps_for(make_setup(device="apple_tv"), http, agent))
    orama = next(p for p in arrs["radarr"].profiles if p["name"] == "Orama")
    orama["formatItems"].append({"format": fid, "name": names[extra], "score": UNPLAYABLE_SCORE})
    (result,) = run_steps(steps_for(make_setup(device="apple_tv"), http, agent))
    assert result.status == "unchanged"


def test_profile_name_matches_the_agents():
    assert PROFILE_NAME == agent_recyclarr.PROFILE_NAME == "Orama"


def test_user_formats_and_profiles_untouched():
    arrs, http, agent = world()
    run_steps(steps_for(make_setup(), http, agent))
    assert all(c.method == "GET" for c in http.calls)
    for arr in arrs.values():
        mine = next(p for p in arr.profiles if p["name"] == "Any")
        assert mine["formatItems"] == [{"format": 900, "name": "My Own Format", "score": 42}]
        assert {"id": 900, "name": "My Own Format"} in arr.formats


def test_only_enabled_arrs():
    _arrs, http, agent = world()
    (step,) = steps_for(make_setup(removed_apps={"sonarr"}), http, agent)
    assert step.needs == ("wait:radarr",)
    (result,) = run_steps([step])
    assert result.status == "changed"
    assert set(agent.calls[0][0]) == {"radarr"}
    assert not any(c.url.startswith("http://sonarr") for c in http.calls)
    both_gone = make_setup(removed_apps={"sonarr", "radarr"})
    assert steps_for(both_gone, http, agent) == []


def test_missing_key_is_plain():
    _arrs, http, agent = world()
    (result,) = run_steps(recyclarr_steps(make_setup(), Store({}), http, agent))
    assert result.status == "failed"
    assert "API key is missing" in result.message
    assert agent.calls == []


def test_every_orama_format_has_a_name():
    for app in PORTS:
        names = format_names(app)
        for device in ("samsung_s95f", "lg", "apple_tv", "nvidia_shield", "safest"):
            assert set(compile_profile(device, "4k")[app]["scores"]) <= set(names)
