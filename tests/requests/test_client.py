"""Tests for MediaClient (lookup, detail, and snapshot read client)."""

import json

import pytest

from orama.requests.client import (
    AppSnapshot,
    MediaClient,
    RequestError,
)
from orama.secrets import SecretStore
from orama.wiring.http import FakeHttp, Response


def json_resp(data, status=200):
    return Response(status, {}, json.dumps(data).encode("utf-8"))


def make_setup():
    from types import SimpleNamespace
    return SimpleNamespace(
        media_servers=["plex", "jellyfin"],
        ports={"radarr": 7878, "sonarr": 8989},
    )


def make_client(fake_http, secrets=None):
    store = SecretStore.__new__(SecretStore)
    store._secrets = secrets if secrets is not None else {
        "radarr": "radarr-key-secret",
        "sonarr": "sonarr-key-secret",
    }
    def get(name):
        return store._secrets.get(name)
    store.get = get
    return MediaClient(setup=make_setup(), store=store, http=fake_http)


def test_query_validation():
    client = make_client(FakeHttp())
    with pytest.raises(RequestError, match="Query must be"):
        client.search("movie", "")
    with pytest.raises(RequestError, match="Query must be"):
        client.search("movie", "   ")
    with pytest.raises(RequestError, match="Query must be"):
        client.search("movie", "a" * 201)


def test_search_movie_radarr_calls_correct_endpoint():
    http = FakeHttp()
    http.route(
        "GET",
        "/api/v3/movie/lookup",
        lambda call: json_resp([
            {
                "tmdbId": 12345,
                "title": "Dune: Part Two",
                "year": 2024,
                "hasFile": True,
                "images": [{"coverType": "poster", "remoteUrl": "https://image.tmdb.org/t/p/w500/poster.jpg"}],
            }
        ]),
    )
    client = make_client(http)
    results = client.search("movie", "Dune")

    assert len(results) == 1
    r = results[0]
    assert r.kind == "movie"
    assert r.source_id == 12345
    assert r.title == "Dune: Part Two"
    assert r.year == 2024
    assert r.in_library is True
    assert r.poster_ref == "https://image.tmdb.org/t/p/w500/poster.jpg"

    assert len(http.calls) == 1
    call = http.calls[0]
    assert call.url == "http://127.0.0.1:7878/api/v3/movie/lookup?term=Dune"
    assert call.headers["X-Api-Key"] == "radarr-key-secret"


def test_search_series_sonarr_calls_correct_endpoint():
    http = FakeHttp()
    http.route(
        "GET",
        "/api/v3/series/lookup",
        lambda call: json_resp([
            {
                "tvdbId": 8888,
                "title": "Severance",
                "year": 2022,
                "statistics": {"episodeFileCount": 9, "episodeCount": 9},
                "images": [{"coverType": "poster", "remoteUrl": "https://artworks.thetvdb.com/banners/poster.jpg"}],
            }
        ]),
    )
    client = make_client(http)
    results = client.search("series", "Severance")

    assert len(results) == 1
    r = results[0]
    assert r.kind == "series"
    assert r.source_id == 8888
    assert r.title == "Severance"
    assert r.in_library is True

    call = http.calls[0]
    assert call.url == "http://127.0.0.1:8989/api/v3/series/lookup?term=Severance"
    assert call.headers["X-Api-Key"] == "sonarr-key-secret"


def test_search_sanitizes_titles_and_rejects_unsafe_urls():
    http = FakeHttp()
    http.route(
        "GET",
        "/api/v3/movie/lookup",
        lambda call: json_resp([
            {
                "tmdbId": 999,
                "title": "<script>alert('xss')</script>Safe Movie",
                "year": 2023,
                "hasFile": False,
                "images": [{"coverType": "poster", "remoteUrl": "javascript:alert(1)"}],
            }
        ]),
    )
    client = make_client(http)
    results = client.search("movie", "Test")

    assert len(results) == 1
    assert "<script>" not in results[0].title
    assert results[0].poster_ref is None


def test_missing_app_or_missing_api_key():
    client = make_client(FakeHttp(), secrets={})
    with pytest.raises(RequestError, match="not configured or key missing"):
        client.search("movie", "Test")


def test_upstream_unreachable_or_timeout():
    http = FakeHttp(unreachable=True)
    client = make_client(http)

    with pytest.raises(RequestError, match="Radarr is unreachable"):
        client.search("movie", "Test")


def test_upstream_401_or_error_status_does_not_echo_secret():
    http = FakeHttp()
    http.route(
        "GET",
        "/api/v3/movie/lookup",
        lambda call: Response(401, {}, b"Invalid API key: radarr-key-secret"),
    )
    client = make_client(http)

    with pytest.raises(RequestError) as exc_info:
        client.search("movie", "Test")
    assert "radarr-key-secret" not in str(exc_info.value)
    assert "Authentication failed" in str(exc_info.value)


def test_detail_movie():
    http = FakeHttp()
    http.route(
        "GET",
        "/api/v3/movie/lookup",
        lambda call: json_resp([
            {
                "id": 42,
                "tmdbId": 500,
                "title": "Gladiator II",
                "year": 2024,
                "hasFile": False,
                "images": [],
            }
        ]),
    )
    http.route(
        "GET",
        "/api/v3/movie",
        lambda call: json_resp([
            {
                "id": 42,
                "tmdbId": 500,
                "title": "Gladiator II",
                "hasFile": False,
            }
        ]),
    )
    client = make_client(http)
    detail = client.detail("movie", 500)
    assert detail.result.source_id == 500
    assert detail.remote_id == 42
    assert detail.seasons == []
    assert detail.episodes == []


def test_snapshot_radarr_bounded():
    http = FakeHttp()
    http.route(
        "GET",
        "/api/v3/movie",
        lambda call: json_resp([{"id": 1, "title": "Movie 1", "hasFile": True}]),
    )
    http.route(
        "GET",
        "/api/v3/queue",
        lambda call: json_resp({"records": [{"id": 10, "movieId": 1, "status": "downloading"}], "totalRecords": 1}),
    )
    http.route(
        "GET",
        "/api/v3/history",
        lambda call: json_resp({"records": [], "totalRecords": 0}),
    )
    client = make_client(http)
    snapshot = client.snapshot("radarr", remote_ids={1})

    assert isinstance(snapshot, AppSnapshot)
    assert snapshot.complete is True
    assert 1 in snapshot.items
    assert len(snapshot.queue) == 1


def test_file_media_info_and_subtitles_never_fall_back_to_wrong_episode():
    http = FakeHttp()
    # Episode 1 has file 10 (AAC), Episode 2 has file 20 (DTS), Episode 3 has no file
    http.route(
        "GET",
        "/api/v3/episode",
        lambda call: json_resp([
            {"id": 101, "seriesId": 7, "seasonNumber": 1, "episodeNumber": 1, "hasFile": True, "episodeFileId": 10},
            {"id": 102, "seriesId": 7, "seasonNumber": 1, "episodeNumber": 2, "hasFile": True, "episodeFileId": 20},
            {"id": 103, "seriesId": 7, "seasonNumber": 1, "episodeNumber": 3, "hasFile": False, "episodeFileId": 0},
        ]),
    )
    http.route(
        "GET",
        "/api/v3/episodefile",
        lambda call: json_resp([
            {"id": 10, "seriesId": 7, "seasonNumber": 1, "mediaInfo": {"videoCodec": "h264", "audioCodec": "AAC"}},
            {"id": 20, "seriesId": 7, "seasonNumber": 1, "mediaInfo": {"videoCodec": "hevc", "audioCodec": "DTS"}},
        ]),
    )
    http.route(
        "GET",
        "/api/episodes",
        lambda call: json_resp({
            "data": [
                {"season": 1, "episode": 1, "episode_number": "1x1", "subtitles": [{"code2": "el"}]},
                {"season": 1, "episode": 2, "episode_number": "1x2", "subtitles": [{"code2": "en"}]},
            ]
        }),
    )

    client = make_client(
        http,
        secrets={
            "radarr": "radarr-key-secret",
            "sonarr": "sonarr-key-secret",
            "bazarr": "bazarr-key-secret",
        },
    )

    # S01E02 gets file 20 (DTS) and EN subs, NOT S01E01's file 10 (AAC) or EL subs
    info_ep2 = client.file_media_info("sonarr", 7, season=1, episode=2)
    assert info_ep2 == {"videoCodec": "hevc", "audioCodec": "DTS"}
    subs_ep2 = client.subtitle_languages("episode", 7, season=1, episode=2)
    assert subs_ep2 == [{"code2": "en"}]

    # S01E03 (no file/row yet) gets None, NEVER S01E01's file or subtitles
    assert client.file_media_info("sonarr", 7, season=1, episode=3) is None
    assert client.subtitle_languages("episode", 7, season=1, episode=3) is None

    # Season 2 (no file/row yet) gets None, NEVER Season 1's file or subtitles
    assert client.file_media_info("sonarr", 7, season=2) is None
    assert client.subtitle_languages("season", 7, season=2) is None



@pytest.mark.parametrize("status", [400, 404, 409, 500, 502, 503])
def test_unexpected_status_is_plain_words_with_the_detail_only_in_the_log(status, caplog):
    http = FakeHttp()
    http.route(
        "GET",
        "/api/v3/movie/lookup",
        lambda call: Response(status, {}, b"NzbDrone.Core.Exceptions: radarr-key-secret"),
    )
    client = make_client(http)

    with caplog.at_level("WARNING", logger="orama.requests"), \
            pytest.raises(RequestError) as exc_info:
        client.search("movie", "Secret Title")
    message = str(exc_info.value)
    assert message == "Radarr did not answer as expected. Try again in a minute."
    assert "HTTP" not in message and not any(ch.isdigit() for ch in message)
    # The log says what happened, without the key, the search words or the app's own text.
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert f"radarr answered GET movie/lookup with status {status}" in logged
    for private in ("radarr-key-secret", "Secret", "NzbDrone"):
        assert private not in logged


def test_unreadable_answer_is_plain_words():
    http = FakeHttp()
    http.route("GET", "/api/v3/series/lookup", lambda call: Response(200, {}, b"<html>oops"))
    client = make_client(http)
    with pytest.raises(RequestError) as exc_info:
        client.search("series", "Test")
    message = str(exc_info.value)
    assert message == "Sonarr did not answer as expected. Try again in a minute."
    assert "JSON" not in message


def test_find_returns_only_the_exact_catalogue_id():
    http = FakeHttp()
    http.route("GET", "/api/v3/movie/lookup", lambda call: json_resp([
        {"tmdbId": 999, "title": "Other"},
        {"id": 550, "title": "Row number, not a TMDB id"},
        {"tmdbId": 550, "title": "Fight Club", "year": 1999},
    ]))
    http.route("GET", "/api/v3/series/lookup", lambda call: json_resp([
        {"tmdbId": 81189, "title": "Wrong catalogue"},
    ]))
    client = make_client(http)
    found = client.find("movie", 550)
    assert found is not None and (found.source_id, found.title) == (550, "Fight Club")
    assert client.find("movie", 551) is None
    assert client.find("series", 81189) is None
    assert [c.url.split("?", 1)[1] for c in http.calls] == [
        "term=tmdb%3A550", "term=tmdb%3A551", "term=tvdb%3A81189"]


def test_detail_never_describes_another_title():
    http = FakeHttp()
    http.route("GET", "/api/v3/movie/lookup", lambda call: json_resp([
        {"tmdbId": 999, "title": "Other Film", "year": 1990,
         "images": [{"coverType": "poster", "remoteUrl": "https://image.tmdb.org/t/p/o.jpg"}]},
        {"id": 500, "title": "Row 500, not TMDB 500"},
    ]))
    http.route("GET", "/api/v3/movie", lambda call: json_resp([]))
    client = make_client(http)
    detail = client.detail("movie", 500)
    assert detail.result.source_id == 500
    assert "Other" not in detail.result.title and "Row" not in detail.result.title
    assert detail.result.poster_ref is None and detail.result.year is None
    lookup = next(c for c in http.calls if c.path.endswith("/lookup"))
    assert lookup.url.endswith("term=tmdb%3A500")
