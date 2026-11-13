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
    http.route("GET", "/api/v3/series/99", lambda call: json_resp(
        {"id": 99, "monitored": True, "monitorNewItems": "all", "seasons": []}))

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


def test_submit_refuses_when_orama_quality_profile_missing(tmp_path):
    import pytest

    from orama.requests.client import RequestError

    store = RequestStore(tmp_path / "requests.db")
    now = 1000.0
    movie_rec, _ = store.create(RequestSpec(kind="movie", source_id=101), now)
    series_rec, _ = store.create(RequestSpec(kind="episode", source_id=303, season=1, episode=1), now)

    http = FakeHttp()
    http.route("GET", "/api/v3/movie/lookup", lambda call: json_resp([{"tmdbId": 101, "title": "Test Movie"}]))
    http.route("GET", "/api/v3/movie", lambda call: json_resp([]))
    http.route("GET", "/api/v3/series/lookup", lambda call: json_resp([{"tvdbId": 303, "title": "Show A"}]))
    http.route("GET", "/api/v3/series", lambda call: json_resp([]))
    # Only default non-Orama quality profiles exist
    http.route("GET", "/api/v3/qualityprofile", lambda call: json_resp([{"id": 1, "name": "Any"}, {"id": 2, "name": "HD-1080p"}]))
    http.route("GET", "/api/v3/rootfolder", lambda call: json_resp([{"id": 1, "path": "/data/media/movies"}]))
    http.route("POST", "/api/v3/movie", lambda call: json_resp({"id": 55, "title": "Test Movie"}))
    http.route("POST", "/api/v3/series", lambda call: json_resp({"id": 77, "title": "Show A"}))

    client = make_client(http)
    with pytest.raises(RequestError, match="Orama quality profile is not configured"):
        submit(movie_rec, client, store, now)
    with pytest.raises(RequestError, match="Orama quality profile is not configured"):
        submit(series_rec, client, store, now)

    # Neither movie nor series was posted with an unintended fallback profile
    assert not any(c.method == "POST" and c.path in ("/api/v3/movie", "/api/v3/series") for c in http.calls)



def _ensure_http(kind: str, lookup_items) -> FakeHttp:
    app_path = "movie" if kind == "movie" else "series"
    http = FakeHttp()
    http.route("GET", f"/api/v3/{app_path}", lambda call: json_resp([]))
    http.route("GET", f"/api/v3/{app_path}/lookup", lambda call: json_resp(lookup_items))
    http.route("GET", "/api/v3/qualityprofile", lambda call: json_resp([{"id": 1, "name": "Orama"}]))
    http.route("GET", "/api/v3/rootfolder", lambda call: json_resp([{"id": 1, "path": "/data/media/x"}]))
    http.route("POST", f"/api/v3/{app_path}", lambda call: json_resp({"id": 55}))
    return http


def test_ensure_movie_never_adds_a_different_title():
    import pytest

    from orama.requests.client import RequestError

    # A text search for "300" finds the film called 300, which is not TMDB id 300.
    http = _ensure_http("movie", [
        {"tmdbId": 1271, "title": "300"},
        {"id": 300, "title": "Row 300, another film"},
    ])
    with pytest.raises(RequestError, match="Could not locate movie metadata"):
        make_client(http).ensure_movie(300)
    assert [c for c in http.calls if c.method == "POST"] == []

    # The exact id is used wherever it is in the answer.
    http = _ensure_http("movie", [{"tmdbId": 1271, "title": "300"},
                                  {"tmdbId": 300, "title": "The right one"}])
    assert make_client(http).ensure_movie(300) == (55, False)
    posted = next(c for c in http.calls if c.method == "POST")
    assert posted.json["tmdbId"] == 300 and posted.json["title"] == "The right one"


def test_ensure_series_never_adds_a_different_show():
    import pytest

    from orama.requests.client import RequestError

    http = _ensure_http("series", [
        {"tvdbId": 9999, "title": "24"},
        {"tmdbId": 24, "title": "Wrong catalogue"},
    ])
    with pytest.raises(RequestError, match="Could not locate series metadata"):
        make_client(http).ensure_series(24)
    assert [c for c in http.calls if c.method == "POST"] == []

    http = _ensure_http("series", [{"tvdbId": 9999, "title": "24"},
                                   {"tvdbId": 24, "title": "The right show"}])
    assert make_client(http).ensure_series(24)[0] == 55
    posted = next(c for c in http.calls if c.method == "POST")
    assert posted.json["tvdbId"] == 24 and posted.json["title"] == "The right show"


def _existing_series_http(series):
    """Sonarr already has the show (an earlier episode request added it, following nothing)."""
    http = FakeHttp()
    http.route("GET", "/api/v3/series", lambda call: json_resp([series]))
    http.route("GET", f"/api/v3/series/{series['id']}", lambda call: json_resp(series))
    http.route("PUT", f"/api/v3/series/{series['id']}", lambda call: json_resp(call.json))
    http.route("GET", "/api/v3/episode", lambda call: json_resp([
        {"id": 1, "seasonNumber": 0, "episodeNumber": 1, "monitored": False, "hasFile": False},
        {"id": 2, "seasonNumber": 1, "episodeNumber": 1, "monitored": True, "hasFile": False},
        {"id": 3, "seasonNumber": 1, "episodeNumber": 2, "monitored": False, "hasFile": False},
        {"id": 4, "seasonNumber": 2, "episodeNumber": 1, "monitored": False, "hasFile": False},
    ]))
    http.route("PUT", "/api/v3/episode/monitor", lambda call: json_resp([]))
    http.route("POST", "/api/v3/command", lambda call: json_resp({"id": 5, "name": "SeriesSearch"}))
    http.route("GET", "/api/v3/command", lambda call: json_resp([]))
    http.route("GET", "/api/v3/queue", lambda call: json_resp({"records": []}))
    return http


def test_whole_series_request_follows_a_show_sonarr_already_has(tmp_path):
    """Sonarr's series search only looks for followed episodes, and an earlier episode or
    season request added the show following almost nothing: a whole-show request must turn
    following on (regular seasons, their episodes and future ones), or nothing downloads."""
    store = RequestStore(tmp_path / "requests.db")
    rec, _ = store.create(RequestSpec(kind="series", source_id=404), 1000.0)
    series = {"id": 99, "tvdbId": 404, "title": "Show B", "monitored": False,
              "monitorNewItems": "none", "statistics": {"episodeFileCount": 0},
              "seasons": [{"seasonNumber": 0, "monitored": False},
                          {"seasonNumber": 1, "monitored": False},
                          {"seasonNumber": 2, "monitored": False}]}
    http = _existing_series_http(series)

    submit(rec, make_client(http), store, 1000.0)

    put_series = next(c for c in http.calls if c.method == "PUT" and c.path == "/api/v3/series/99")
    assert put_series.json["monitored"] is True
    assert put_series.json["monitorNewItems"] == "all"
    assert {s["seasonNumber"]: s["monitored"] for s in put_series.json["seasons"]} == {
        0: False, 1: True, 2: True}  # specials stay as they were
    put_eps = next(c for c in http.calls if c.path == "/api/v3/episode/monitor")
    assert sorted(put_eps.json["episodeIds"]) == [3, 4] and put_eps.json["monitored"] is True
    # Following is switched on before the search is asked for.
    order = [(c.method, c.path) for c in http.calls]
    assert order.index(("PUT", "/api/v3/series/99")) < order.index(("POST", "/api/v3/command"))
    assert store.get(rec.id).stage == "downloading"


def test_whole_series_request_changes_nothing_when_the_show_is_already_followed(tmp_path):
    store = RequestStore(tmp_path / "requests.db")
    rec, _ = store.create(RequestSpec(kind="series", source_id=404), 1000.0)
    series = {"id": 99, "tvdbId": 404, "title": "Show B", "monitored": True,
              "monitorNewItems": "all", "statistics": {"episodeFileCount": 0},
              "seasons": [{"seasonNumber": 0, "monitored": False},
                          {"seasonNumber": 1, "monitored": True},
                          {"seasonNumber": 2, "monitored": True}]}
    http = _existing_series_http(series)
    http.route("GET", "/api/v3/episode", lambda call: json_resp([
        {"id": 2, "seasonNumber": 1, "episodeNumber": 1, "monitored": True, "hasFile": False}]))

    submit(rec, make_client(http), store, 1000.0)

    assert not any(c.method == "PUT" for c in http.calls)
