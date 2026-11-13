"""Tests for playable-release waiting and per-request quality fallback."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from orama.requests.client import MediaClient, RequestError
from orama.requests.fallback import (
    FALLBACK_PROFILE_NAME,
    apply_fallback,
    ensure_fallback_profile,
    fallback_due,
    retry_request,
)
from orama.requests.models import RequestRecord, RequestSpec
from orama.requests.store import RequestStore
from orama.secrets import SecretStore
from orama.wiring.http import FakeHttp, Response, Unreachable


def _fake_store() -> SecretStore:
    store = SecretStore.__new__(SecretStore)
    store.get = lambda name: "fake-key"  # type: ignore[method-assign]
    return store


def _strict_profile() -> dict:
    return {
        "id": 1,
        "name": "Orama",
        "upgradeAllowed": True,
        "cutoff": 4,
        "minFormatScore": 0,
        "cutoffFormatScore": 10000,
        "formatItems": [
            {"format": 10, "name": "DTS", "score": -10000},
            {"format": 11, "name": "HDR", "score": 1500},
            {"format": 12, "name": "BR-DISK", "score": -10000},
        ],
        "items": [
            {"quality": {"id": 4, "name": "WEBDL-1080p"}, "items": [], "allowed": True},
            {"quality": {"id": 1, "name": "SDTV"}, "items": [], "allowed": False},
        ],
    }


def test_fallback_due_23_59_versus_24_00():
    created_at = 100_000.0
    setup_24h = SimpleNamespace(request_wait_mode="timed", request_wait_hours=24)
    rec = RequestRecord(
        id="rec-1",
        spec=RequestSpec(kind="movie", source_id=123),
        created_at=created_at,
        updated_at=created_at,
        revision=1,
        stage="downloading",
    )

    # 23:59 elapsed -> not due
    assert fallback_due(rec, setup_24h, created_at + (23 * 3600 + 59 * 60)) is False
    # 24:00 elapsed -> due
    assert fallback_due(rec, setup_24h, created_at + (24 * 3600)) is True

    # Ready items never need fallback
    rec_ready = RequestRecord(
        id="rec-2",
        spec=RequestSpec(kind="movie", source_id=124),
        created_at=created_at,
        updated_at=created_at,
        revision=1,
        stage="ready",
    )
    assert fallback_due(rec_ready, setup_24h, created_at + (48 * 3600)) is False


def test_fallback_due_always_never_and_custom_hours():
    created_at = 100_000.0
    rec = RequestRecord(
        id="rec-1",
        spec=RequestSpec(kind="movie", source_id=123),
        created_at=created_at,
        updated_at=created_at,
        revision=1,
        stage="downloading",
    )

    # "always" = always wait for playable release -> never fall back
    setup_always = SimpleNamespace(request_wait_mode="always", request_wait_hours=24)
    assert fallback_due(rec, setup_always, created_at + 60) is False
    assert fallback_due(rec, setup_always, created_at + 1000 * 3600) is False

    # "never" = never wait -> fallback due immediately
    setup_never = SimpleNamespace(request_wait_mode="never", request_wait_hours=24)
    assert fallback_due(rec, setup_never, created_at) is True
    assert fallback_due(rec, setup_never, created_at + 1) is True

    # Custom hours (e.g. 6 hours)
    setup_6h = SimpleNamespace(request_wait_mode="timed", request_wait_hours=6)
    assert fallback_due(rec, setup_6h, created_at + 5 * 3600 + 59 * 60) is False
    assert fallback_due(rec, setup_6h, created_at + 6 * 3600) is True


def test_restart_retains_deadline_and_explicit_retry_uses_current_policy(tmp_path):
    db_path = tmp_path / "requests.sqlite3"
    created_at = 100_000.0
    initial_setup = SimpleNamespace(request_wait_mode="timed", request_wait_hours=12)

    store1 = RequestStore(db_path)
    rec, created = store1.create(
        RequestSpec(kind="movie", source_id=500), created_at, setup=initial_setup
    )
    assert created is True
    assert rec.wait_mode == "timed"
    assert rec.fallback_deadline == created_at + 12 * 3600

    # Reopen store after restart; changing setup later does not silently alter existing deadline
    store2 = RequestStore(db_path)
    loaded = store2.get(rec.id)
    assert loaded is not None
    assert loaded.wait_mode == "timed"
    assert loaded.fallback_deadline == created_at + 12 * 3600

    later_setup = SimpleNamespace(request_wait_mode="timed", request_wait_hours=48)
    # At 13 hours, original 12h deadline is due even though global setup changed to 48h
    assert fallback_due(loaded, later_setup, created_at + 13 * 3600) is True

    # Explicit Retry adopts current policy (e.g. 6 hours) without resetting elapsed wait
    shorter_setup = SimpleNamespace(request_wait_mode="timed", request_wait_hours=6)
    retried = retry_request(loaded.id, store2, shorter_setup, now=created_at + 7 * 3600)
    assert retried is not None
    assert retried.created_at == created_at
    assert retried.fallback_deadline == created_at + 6 * 3600
    assert fallback_due(retried, shorter_setup, created_at + 7 * 3600) is True


def test_ensure_fallback_profile_preserves_strict_profile_and_qualities():
    http = FakeHttp()
    strict = _strict_profile()
    created_profiles: list[dict] = []

    def get_profiles(call):
        return Response(200, {}, json.dumps([strict, *created_profiles]).encode())

    def post_profile(call):
        body = dict(call.json)
        saved = {**body, "id": 2}
        created_profiles.append(saved)
        return Response(201, {}, json.dumps(saved).encode())

    http.route("GET", "/api/v3/qualityprofile", get_profiles)
    http.route("POST", "/api/v3/qualityprofile", post_profile)

    client = MediaClient(setup=SimpleNamespace(ports={"radarr": 7878}), store=_fake_store(), http=http)
    fallback_id = ensure_fallback_profile("radarr", client)
    assert fallback_id == 2
    assert len(created_profiles) == 1

    fb = created_profiles[0]
    assert fb["name"] == FALLBACK_PROFILE_NAME
    # Strict quality items and cutoff are preserved identically
    assert fb["items"] == strict["items"]
    assert fb["cutoff"] == strict["cutoff"]
    assert fb["upgradeAllowed"] == strict["upgradeAllowed"]
    # Threshold allows Orama's device-blocked releases (-10000)
    assert fb["minFormatScore"] == -10000
    # Strict shared profile was never modified
    assert strict["minFormatScore"] == 0
    assert strict["name"] == "Orama"


def test_user_profile_unchanged_on_fallback(tmp_path):
    store = RequestStore(tmp_path / "requests.sqlite3")
    created_at = 100_000.0
    setup = SimpleNamespace(
        ports={"radarr": 7878}, request_wait_mode="timed", request_wait_hours=24
    )
    rec, _ = store.create(
        RequestSpec(kind="movie", source_id=27205), created_at, setup=setup
    )

    http = FakeHttp()
    strict = _strict_profile()
    user_profile = {**strict, "id": 99, "name": "My Custom Profile"}
    movie_obj = {"id": 10, "tmdbId": 27205, "qualityProfileId": 99, "hasFile": False}
    put_calls: list = []

    http.route(
        "GET",
        "/api/v3/qualityprofile",
        lambda call: Response(200, {}, json.dumps([strict, user_profile]).encode()),
    )
    http.route(
        "GET",
        "/api/v3/movie",
        lambda call: Response(200, {}, json.dumps([movie_obj]).encode()),
    )
    http.route(
        "PUT",
        "/api/v3/movie/10",
        lambda call: (put_calls.append(call), Response(202, {}, json.dumps(movie_obj).encode()))[1],
    )

    client = MediaClient(setup=setup, store=_fake_store(), http=http)
    update = apply_fallback(rec, client, store, setup, now=created_at + 25 * 3600)

    # User profile must not be overwritten
    assert put_calls == []
    assert movie_obj["qualityProfileId"] == 99
    assert update.waiting_reason is not None
    assert "custom" in update.waiting_reason.lower()


def test_existing_unrelated_series_blocks_automatic_fallback_with_clear_reason(tmp_path):
    store = RequestStore(tmp_path / "requests.sqlite3")
    created_at = 100_000.0
    setup = SimpleNamespace(
        ports={"sonarr": 8989}, request_wait_mode="timed", request_wait_hours=24
    )
    # Request only S01E01
    rec, _ = store.create(
        RequestSpec(kind="episode", source_id=81189, season=1, episode=1),
        created_at,
        setup=setup,
    )

    http = FakeHttp()
    strict = _strict_profile()
    series_obj = {"id": 5, "tvdbId": 81189, "qualityProfileId": 1, "monitored": True}
    # Episode 1 (requested) and Episode 2 (unrelated monitored episode!)
    episodes = [
        {"id": 101, "seriesId": 5, "seasonNumber": 1, "episodeNumber": 1, "monitored": True, "hasFile": False},
        {"id": 102, "seriesId": 5, "seasonNumber": 1, "episodeNumber": 2, "monitored": True, "hasFile": False},
    ]
    put_calls: list = []

    http.route(
        "GET",
        "/api/v3/qualityprofile",
        lambda call: Response(200, {}, json.dumps([strict]).encode()),
    )
    http.route(
        "GET",
        "/api/v3/series",
        lambda call: Response(200, {}, json.dumps([series_obj]).encode()),
    )
    http.route(
        "GET",
        "/api/v3/episode",
        lambda call: Response(200, {}, json.dumps(episodes).encode()),
    )
    http.route(
        "PUT",
        "/api/v3/series/5",
        lambda call: (put_calls.append(call), Response(202, {}, json.dumps(series_obj).encode()))[1],
    )

    client = MediaClient(setup=setup, store=_fake_store(), http=http)
    update = apply_fallback(rec, client, store, setup, now=created_at + 25 * 3600)

    # Series profile must stay strict because S01E02 is monitored unrelatedly
    assert put_calls == []
    assert series_obj["qualityProfileId"] == 1
    assert update.waiting_reason is not None
    assert "other" in update.waiting_reason.lower() or "monitored" in update.waiting_reason.lower()


def test_series_level_fallback_applies_when_safe_for_all_monitored_targets(tmp_path):
    store = RequestStore(tmp_path / "requests.sqlite3")
    created_at = 100_000.0
    setup = SimpleNamespace(
        ports={"sonarr": 8989}, request_wait_mode="timed", request_wait_hours=24
    )
    # Request only S01E01, and ONLY S01E01 is monitored in Sonarr
    rec, _ = store.create(
        RequestSpec(kind="episode", source_id=81189, season=1, episode=1),
        created_at,
        setup=setup,
    )

    http = FakeHttp()
    strict = _strict_profile()
    fallback_prof = {**strict, "id": 2, "name": FALLBACK_PROFILE_NAME, "minFormatScore": -10000}
    series_obj = {"id": 5, "tvdbId": 81189, "qualityProfileId": 1, "monitored": True}
    episodes = [
        {"id": 101, "seriesId": 5, "seasonNumber": 1, "episodeNumber": 1, "monitored": True, "hasFile": False},
        {"id": 102, "seriesId": 5, "seasonNumber": 1, "episodeNumber": 2, "monitored": False, "hasFile": False},
    ]
    put_bodies: list[dict] = []
    commands: list[dict] = []

    http.route(
        "GET",
        "/api/v3/qualityprofile",
        lambda call: Response(200, {}, json.dumps([strict, fallback_prof]).encode()),
    )
    http.route(
        "GET",
        "/api/v3/series",
        lambda call: Response(200, {}, json.dumps([series_obj]).encode()),
    )
    http.route(
        "GET",
        "/api/v3/episode",
        lambda call: Response(200, {}, json.dumps(episodes).encode()),
    )

    def put_series(call):
        body = dict(call.json)
        put_bodies.append(body)
        series_obj["qualityProfileId"] = body["qualityProfileId"]
        return Response(202, {}, json.dumps(series_obj).encode())

    def post_command(call):
        body = dict(call.json)
        commands.append(body)
        return Response(201, {}, json.dumps({"id": 77, **body}).encode())

    http.route("PUT", "/api/v3/series/5", put_series)
    http.route("POST", "/api/v3/command", post_command)

    client = MediaClient(setup=setup, store=_fake_store(), http=http)
    update = apply_fallback(rec, client, store, setup, now=created_at + 25 * 3600)

    assert len(put_bodies) == 1
    assert put_bodies[0]["qualityProfileId"] == 2
    assert len(commands) == 1
    assert commands[0] == {"name": "EpisodeSearch", "episodeIds": [101]}
    assert update.fallback_applied is True


def test_failed_fallback_assignment_retries_by_read_back(tmp_path):
    store = RequestStore(tmp_path / "requests.sqlite3")
    created_at = 100_000.0
    setup = SimpleNamespace(
        ports={"radarr": 7878}, request_wait_mode="timed", request_wait_hours=24
    )
    rec, _ = store.create(
        RequestSpec(kind="movie", source_id=27205), created_at, setup=setup
    )

    http = FakeHttp()
    strict = _strict_profile()
    fallback_prof = {**strict, "id": 2, "name": FALLBACK_PROFILE_NAME, "minFormatScore": -10000}
    movie_obj = {"id": 10, "tmdbId": 27205, "qualityProfileId": 1, "hasFile": False}
    put_attempts = 0
    commands: list[dict] = []

    http.route(
        "GET",
        "/api/v3/qualityprofile",
        lambda call: Response(200, {}, json.dumps([strict, fallback_prof]).encode()),
    )
    http.route(
        "GET",
        "/api/v3/movie",
        lambda call: Response(200, {}, json.dumps([movie_obj]).encode()),
    )

    def put_movie_timeout_after_applying(call):
        nonlocal put_attempts
        put_attempts += 1
        # Server applies change before connection drops
        body = dict(call.json)
        movie_obj["qualityProfileId"] = body["qualityProfileId"]
        raise Unreachable("connection reset after write")

    def post_command(call):
        body = dict(call.json)
        commands.append(body)
        return Response(201, {}, json.dumps({"id": 88, **body}).encode())

    http.route("PUT", "/api/v3/movie/10", put_movie_timeout_after_applying)
    http.route("POST", "/api/v3/command", post_command)

    client = MediaClient(setup=setup, store=_fake_store(), http=http)

    # First attempt raises RequestError when PUT drops connection
    with pytest.raises(RequestError):
        apply_fallback(rec, client, store, setup, now=created_at + 25 * 3600)
    assert put_attempts == 1
    assert commands == []

    # Second attempt reads back movie_obj (which already has qualityProfileId == 2),
    # does not PUT again, and completes the search + state update
    rec_reloaded = store.get(rec.id)
    assert rec_reloaded is not None
    update = apply_fallback(rec_reloaded, client, store, setup, now=created_at + 25 * 3600 + 15)
    assert put_attempts == 1
    assert len(commands) == 1
    assert commands[0] == {"name": "MoviesSearch", "movieIds": [10]}
    assert update.fallback_applied is True


def test_fallback_refuses_when_strict_orama_profile_missing(tmp_path):
    store = RequestStore(tmp_path / "requests.sqlite3")
    created_at = 100_000.0
    setup = SimpleNamespace(
        ports={"radarr": 7878}, request_wait_mode="timed", request_wait_hours=24
    )
    rec, _ = store.create(
        RequestSpec(kind="movie", source_id=27205), created_at, setup=setup
    )

    http = FakeHttp()
    unrelated_profile = {**_strict_profile(), "id": 1, "name": "Any"}
    movie_obj = {"id": 10, "tmdbId": 27205, "qualityProfileId": 1, "hasFile": False}
    put_calls: list = []
    post_calls: list = []

    http.route(
        "GET",
        "/api/v3/qualityprofile",
        lambda call: Response(200, {}, json.dumps([unrelated_profile]).encode()),
    )
    http.route(
        "GET",
        "/api/v3/movie",
        lambda call: Response(200, {}, json.dumps([movie_obj]).encode()),
    )
    http.route(
        "POST",
        "/api/v3/qualityprofile",
        lambda call: (post_calls.append(call), Response(201, {}, json.dumps({"id": 2}).encode()))[1],
    )
    http.route(
        "PUT",
        "/api/v3/movie/10",
        lambda call: (put_calls.append(call), Response(202, {}, json.dumps(movie_obj).encode()))[1],
    )

    client = MediaClient(setup=setup, store=_fake_store(), http=http)

    with pytest.raises(RequestError, match="Orama quality profile is not configured"):
        ensure_fallback_profile("radarr", client)
    assert post_calls == []

    with pytest.raises(RequestError, match="Orama quality profile is not configured"):
        apply_fallback(rec, client, store, setup, now=created_at + 25 * 3600)
    assert post_calls == []
    assert put_calls == []


# ---------------------------------------------------------------- back to the strict profile

def _restore_env(tmp_path, kind="series", item_profile=7, others=()):
    """A store with one finished fallback request, a client, and Sonarr/Radarr routes."""
    store = RequestStore(tmp_path / "requests.db")
    spec = RequestSpec(kind=kind, source_id=500, season=1 if kind == "season" else None)
    rec, _ = store.create(spec, 1000.0)
    from orama.requests.models import RequestUpdate
    store.update(rec.id, rec.revision, RequestUpdate(stage="ready", remote_id=42,
                                                     fallback_applied=True))
    for other_spec, other_update in others:
        o, _ = store.create(other_spec, 1000.0)
        store.update(o.id, o.revision, other_update)
    app = "radarr" if kind == "movie" else "sonarr"
    resource = "movie" if kind == "movie" else "series"
    http = FakeHttp()
    item = {"id": 42, "title": "Show", "qualityProfileId": item_profile}
    http.route("GET", "/api/v3/qualityprofile", lambda c: Response(200, {}, json.dumps([
        {"id": 1, "name": "Orama"}, {"id": 7, "name": FALLBACK_PROFILE_NAME}]).encode()))
    http.route("GET", f"/api/v3/{resource}/42", lambda c: Response(200, {}, json.dumps(item).encode()))
    http.route("PUT", f"/api/v3/{resource}/42", lambda c: Response(202, {}, json.dumps(c.json).encode()))
    client = MediaClient(setup=SimpleNamespace(ports={app: 1}), store=_fake_store(), http=http)
    return store, store.get(rec.id), client, http, resource


def _puts(http, resource):
    return [c for c in http.calls if c.method == "PUT" and c.path == f"/api/v3/{resource}/42"]


@pytest.mark.parametrize("kind", ["movie", "series", "season"])
def test_a_finished_fallback_request_puts_the_strict_profile_back(tmp_path, kind):
    """Fallback is per request: once it is Ready, the title waits for playable releases again
    (for a followed show, every later episode would otherwise skip the wait)."""
    from orama.requests.fallback import restore_strict_profile

    store, rec, client, http, resource = _restore_env(tmp_path, kind)
    assert restore_strict_profile(rec, client, store) is True
    (put,) = _puts(http, resource)
    assert put.json["qualityProfileId"] == 1 and put.json["title"] == "Show"


def test_strict_profile_is_not_put_back_while_another_request_still_needs_fallback(tmp_path):
    from orama.requests.fallback import restore_strict_profile
    from orama.requests.models import RequestUpdate

    still_waiting = (RequestSpec(kind="season", source_id=500, season=2),
                     RequestUpdate(stage="downloading", remote_id=42, fallback_applied=True))
    store, rec, client, http, resource = _restore_env(tmp_path, "season", others=[still_waiting])
    assert restore_strict_profile(rec, client, store) is False
    assert _puts(http, resource) == []


@pytest.mark.parametrize("profile", [1, 99])
def test_strict_or_owner_profile_is_left_alone(tmp_path, profile):
    """Already strict: nothing to do. A profile the owner chose by hand: never changed."""
    from orama.requests.fallback import restore_strict_profile

    store, rec, client, http, resource = _restore_env(tmp_path, "movie", item_profile=profile)
    assert restore_strict_profile(rec, client, store) is False
    assert _puts(http, resource) == []


def test_worker_puts_the_strict_profile_back_when_a_fallback_request_turns_ready(tmp_path):
    from requests.test_media_refresh import _Env

    from orama.requests.models import RequestUpdate

    env = _Env(tmp_path)
    env.secrets.set("apikey:radarr", "radarr-key-for-test")
    rec = env.downloading_request()
    rec = env.store.get(rec.id)
    env.store.update(rec.id, rec.revision, RequestUpdate(fallback_applied=True))
    movie = {"id": 7, "title": "Inception", "qualityProfileId": 7}
    env.http.route("GET", "/api/v3/qualityprofile", lambda c: Response(200, {}, json.dumps([
        {"id": 1, "name": "Orama"}, {"id": 7, "name": FALLBACK_PROFILE_NAME}]).encode()))
    env.http.route("GET", "/api/v3/movie/7", lambda c: Response(200, {}, json.dumps(movie).encode()))
    env.http.route("PUT", "/api/v3/movie/7", lambda c: Response(202, {}, json.dumps(c.json).encode()))

    assert env.worker.tick(env.now) == 1
    assert env.store.get(rec.id).stage == "ready"
    (put,) = [c for c in env.http.calls if c.method == "PUT" and c.path == "/api/v3/movie/7"]
    assert put.json["qualityProfileId"] == 1


def test_worker_still_marks_ready_when_putting_the_profile_back_fails(tmp_path):
    from requests.test_media_refresh import _Env

    from orama.requests.models import RequestUpdate

    env = _Env(tmp_path)
    rec = env.downloading_request()
    rec = env.store.get(rec.id)
    env.store.update(rec.id, rec.revision, RequestUpdate(fallback_applied=True))
    env.http.route("GET", "/api/v3/qualityprofile", lambda c: Response(500, {}, b""))
    assert env.worker.tick(env.now) == 1
    got = env.store.get(rec.id)
    assert got.stage == "ready" and got.claim_owner is None
