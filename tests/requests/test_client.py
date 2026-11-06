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
