"""Tests for idempotent movie and TV submission."""

import json

from orama.requests.client import MediaClient
from orama.requests.models import RequestSpec
from orama.requests.store import RequestStore
from orama.requests.submit import submit
from orama.secrets import SecretStore
from orama.wiring.http import FakeHttp, Response


def json_resp(data, status=200):
    return Response(status, {}, json.dumps(data).encode("utf-8"))


def make_setup():
    from types import SimpleNamespace
    return SimpleNamespace(
        media_servers=["plex", "jellyfin"],
        media_dir="/data/media",
        ports={"radarr": 7878, "sonarr": 8989},
    )


def make_client(fake_http):
    store = SecretStore.__new__(SecretStore)
    store._secrets = {"radarr": "radarr-key", "sonarr": "sonarr-key"}
    store.get = lambda name: store._secrets.get(name)
    return MediaClient(setup=make_setup(), store=store, http=fake_http)


def test_submit_movie_absent(tmp_path):
    store = RequestStore(tmp_path / "requests.db")
    now = 1000.0
    rec, _ = store.create(RequestSpec(kind="movie", source_id=101), now)

    http = FakeHttp()
    # lookup movie
    http.route("GET", "/api/v3/movie/lookup", lambda call: json_resp([{"tmdbId": 101, "title": "Test Movie", "year": 2024}]))
    # check existing movie list (absent)
    http.route("GET", "/api/v3/movie", lambda call: json_resp([]))
    # get quality profiles
    http.route("GET", "/api/v3/qualityprofile", lambda call: json_resp([{"id": 1, "name": "Orama"}]))
    # get root folder
    http.route("GET", "/api/v3/rootfolder", lambda call: json_resp([{"id": 1, "path": "/data/media/movies"}]))
    # add movie
    http.route("POST", "/api/v3/movie", lambda call: json_resp({"id": 55, "title": "Test Movie"}))
    # search command
    http.route("POST", "/api/v3/command", lambda call: json_resp({"id": 999, "name": "MoviesSearch"}))

    client = make_client(http)
    submit(rec, client, store, now)

    updated = store.get(rec.id)
    assert updated.stage in ("downloading", "submitted")
    assert any(c.path == "/api/v3/command" for c in http.calls)


def test_submit_movie_already_imported(tmp_path):
    store = RequestStore(tmp_path / "requests.db")
    now = 1000.0
    rec, _ = store.create(RequestSpec(kind="movie", source_id=202), now)

    http = FakeHttp()
    http.route("GET", "/api/v3/movie/lookup", lambda call: json_resp([{"tmdbId": 202, "title": "Already Here"}]))
    # Existing in library with file
    http.route("GET", "/api/v3/movie", lambda call: json_resp([{"id": 88, "tmdbId": 202, "hasFile": True}]))

    client = make_client(http)
    submit(rec, client, store, now)

    updated = store.get(rec.id)
    assert updated.stage == "ready"
    # No search command issued
    assert not any(c.path == "/api/v3/command" for c in http.calls)


def test_submit_single_episode(tmp_path):
    store = RequestStore(tmp_path / "requests.db")
    now = 1000.0
    rec, _ = store.create(RequestSpec(kind="episode", source_id=303, season=1, episode=2), now)

    http = FakeHttp()
    http.route("GET", "/api/v3/series/lookup", lambda call: json_resp([{"tvdbId": 303, "title": "Show A"}]))
    http.route("GET", "/api/v3/series", lambda call: json_resp([]))
    http.route("GET", "/api/v3/qualityprofile", lambda call: json_resp([{"id": 2, "name": "Orama"}]))
    http.route("GET", "/api/v3/rootfolder", lambda call: json_resp([{"id": 1, "path": "/data/media/tv"}]))
    # add series with addOptions (no auto-search)
    http.route("POST", "/api/v3/series", lambda call: json_resp({"id": 77, "title": "Show A"}))
    # get episodes for series
    http.route(
        "GET",
        "/api/v3/episode",
        lambda call: json_resp([
            {"id": 1001, "seriesId": 77, "seasonNumber": 1, "episodeNumber": 1, "monitored": False},
            {"id": 1002, "seriesId": 77, "seasonNumber": 1, "episodeNumber": 2, "monitored": False},
        ]),
    )
    # monitor target episode
    http.route("PUT", "/api/v3/episode/monitor", lambda call: json_resp({}))
    # search episode command
    http.route("POST", "/api/v3/command", lambda call: json_resp({"id": 888, "name": "EpisodeSearch"}))

    client = make_client(http)
    submit(rec, client, store, now)

    updated = store.get(rec.id)
    assert updated.stage in ("downloading", "submitted")

    # Verify that only episode 1002 was monitored
    monitor_calls = [c for c in http.calls if c.path == "/api/v3/episode/monitor"]
    assert len(monitor_calls) == 1
    assert monitor_calls[0].json == {"episodeIds": [1002], "monitored": True}


def test_submit_whole_series(tmp_path):
    store = RequestStore(tmp_path / "requests.db")
    now = 1000.0
    rec, _ = store.create(RequestSpec(kind="series", source_id=404), now)

    http = FakeHttp()
    http.route("GET", "/api/v3/series/lookup", lambda call: json_resp([{"tvdbId": 404, "title": "Show B"}]))
    http.route("GET", "/api/v3/series", lambda call: json_resp([]))
    http.route("GET", "/api/v3/qualityprofile", lambda call: json_resp([{"id": 2, "name": "Orama"}]))
    http.route("GET", "/api/v3/rootfolder", lambda call: json_resp([{"id": 1, "path": "/data/media/tv"}]))
    http.route("POST", "/api/v3/series", lambda call: json_resp({"id": 99, "title": "Show B"}))
    http.route("POST", "/api/v3/command", lambda call: json_resp({"id": 777, "name": "SeriesSearch"}))

    client = make_client(http)
    submit(rec, client, store, now)

    updated = store.get(rec.id)
    assert updated.stage in ("downloading", "submitted")
    cmd_call = next(c for c in http.calls if c.path == "/api/v3/command")
    assert cmd_call.json.get("name") == "SeriesSearch"
    assert cmd_call.json.get("seriesId") == 99


def test_submit_episode_with_missing_metadata_does_not_trigger_season_or_series_search(tmp_path):
    store = RequestStore(tmp_path / "requests.db")
    now = 1000.0
    rec, _ = store.create(
        RequestSpec(kind="episode", source_id=303, season=1, episode=99), now
    )

    http = FakeHttp()
    http.route("GET", "/api/v3/series", lambda call: json_resp([{"id": 77, "tvdbId": 303, "title": "Show A"}]))
    # Episode 99 is not in Sonarr's episode list yet
    http.route(
        "GET",
        "/api/v3/episode",
        lambda call: json_resp([
            {"id": 1001, "seriesId": 77, "seasonNumber": 1, "episodeNumber": 1, "monitored": False},
        ]),
    )
    http.route("POST", "/api/v3/command", lambda call: json_resp({"id": 888}))

    client = make_client(http)
    submit(rec, client, store, now)

    updated = store.get(rec.id)
    assert updated is not None
    assert updated.remote_id == 77
    assert updated.stage == "searching"
    assert updated.waiting_reason is not None and "metadata" in updated.waiting_reason.lower()
    # Must NEVER fall through to SeasonSearch or SeriesSearch!
    assert not any(c.path == "/api/v3/command" and c.method == "POST" for c in http.calls)
    assert client.search_targets("sonarr", 77, season=1, episode_ids=[]) == 0
    assert not any(c.path == "/api/v3/command" and c.method == "POST" for c in http.calls)


def test_submit_retry_skips_duplicate_search_when_command_or_queue_active(tmp_path):
    store = RequestStore(tmp_path / "requests.db")
    now = 1000.0
    rec, _ = store.create(RequestSpec(kind="movie", source_id=101), now)

    http = FakeHttp()
    http.route("GET", "/api/v3/movie", lambda call: json_resp([{"id": 55, "tmdbId": 101, "hasFile": False}]))
    http.route(
        "GET",
        "/api/v3/command",
        lambda call: json_resp([
            {"id": 900, "name": "MoviesSearch", "status": "started", "body": {"movieIds": [55]}}
        ]),
    )
    http.route("POST", "/api/v3/command", lambda call: json_resp({"id": 901, "name": "MoviesSearch"}))

    client = make_client(http)
    submit(rec, client, store, now)

    updated = store.get(rec.id)
    assert updated is not None
    assert updated.remote_id == 55
    assert updated.stage == "downloading"
    # No duplicate POST /api/v3/command was sent
    assert not any(c.path == "/api/v3/command" and c.method == "POST" for c in http.calls)

