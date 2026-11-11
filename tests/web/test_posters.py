"""Tests for the same-origin poster proxy and cache."""

from __future__ import annotations

import json
from pathlib import Path

from orama.secrets import SecretStore
from orama.setup_model import DesiredSetup
from orama.web import SETUP_FILE, create_app
from orama.wiring.http import FakeHttp, Response, UnsafeRedirect

PASSWORD = "correct horse battery"


class FakeClock:
    def __init__(self, now: float = 1_700_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class FakeAgent:
    def status(self) -> dict[str, str]:
        return {"plex": "running", "radarr": "running", "sonarr": "running"}


def _setup(**overrides) -> DesiredSetup:
    data: dict = {
        "media_servers": ["plex"],
        "download_methods": ["usenet"],
        "data_root": "/srv/orama",
        "config_root": "/etc/orama",
        "puid": 1000,
        "pgid": 1000,
        "usenet_servers": [{"slug": "p", "name": "p", "host": "news.example.com", "username": "u"}],
        "english_only": True,
    }
    data.update(overrides)
    return DesiredSetup(**data)


from web.conftest import signin


def _signed_in_client(tmp_path: Path, http: FakeHttp):
    tmp_path.mkdir(parents=True, exist_ok=True)
    clock = FakeClock()
    (tmp_path / SETUP_FILE).write_text(
        json.dumps(_setup().model_dump(mode="json")), encoding="utf-8"
    )
    store = SecretStore(tmp_path / "secrets.bin")
    store.set("apikey:radarr", "radarr-secret-token")
    store.set("apikey:sonarr", "sonarr-secret-token")

    app = create_app(tmp_path, agent=FakeAgent(), http=http, clock=clock)
    client = app.test_client()
    st = app.extensions["orama"]
    st.passwords.create(PASSWORD)
    assert signin(client, PASSWORD).status_code == 303
    return client, app


def test_poster_valid_https_trusted_origin_serves_image_and_caches(tmp_path: Path):
    http = FakeHttp()
    jpeg_bytes = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9"
    http.route(
        "GET",
        "/api/v3/movie/lookup",
        lambda call: Response(
            200,
            {"Content-Type": "application/json"},
            json.dumps([
                {
                    "tmdbId": 550,
                    "title": "Fight Club",
                    "year": 1999,
                    "images": [
                        {
                            "coverType": "poster",
                            "remoteUrl": "https://image.tmdb.org/t/p/w500/fightclub.jpg",
                        }
                    ],
                }
            ]).encode(),
        ),
    )
    http.route(
        "GET",
        "/t/p/w500/fightclub.jpg",
        lambda call: Response(200, {"Content-Type": "image/jpeg"}, jpeg_bytes),
    )
    client, _ = _signed_in_client(tmp_path, http)

    resp1 = client.get("/posters/movie/550")
    assert resp1.status_code == 200
    assert resp1.headers["Content-Type"] == "image/jpeg"
    assert resp1.data == jpeg_bytes

    calls_after_first = len(http.calls)
    resp2 = client.get("/posters/movie/550")
    assert resp2.status_code == 200
    assert resp2.data == jpeg_bytes
    assert len(http.calls) == calls_after_first


def test_poster_rejects_untrusted_private_or_http_origins_with_placeholder(tmp_path: Path):
    for bad_url in (
        "http://image.tmdb.org/t/p/w500/insecure.jpg",
        "https://evil.example.invalid/poster.jpg",
        "https://localhost/poster.jpg",
        "https://[::1]/poster.jpg",
    ):
        http = FakeHttp()
        http.route(
            "GET",
            "/api/v3/movie/lookup",
            lambda call, u=bad_url: Response(
                200,
                {"Content-Type": "application/json"},
                json.dumps([
                    {
                        "tmdbId": 600,
                        "title": "Bad Origin",
                        "images": [{"coverType": "poster", "remoteUrl": u}],
                    }
                ]).encode(),
            ),
        )
        client, _ = _signed_in_client(tmp_path / str( abs(hash(bad_url)) ), http)
        resp = client.get("/posters/movie/600")
        assert resp.status_code == 200
        assert "image/svg+xml" in resp.headers["Content-Type"]
        assert len(http.calls) == 1


def test_poster_rejects_unknown_mime_oversize_and_redirect(tmp_path: Path):
    # 1. Unknown MIME (text/html)
    http_mime = FakeHttp()
    http_mime.route(
        "GET",
        "/api/v3/movie/lookup",
        lambda call: Response(
            200,
            {},
            json.dumps([
                {
                    "tmdbId": 701,
                    "title": "HTML Poster",
                    "images": [
                        {"coverType": "poster", "remoteUrl": "https://image.tmdb.org/t/p/w500/p.jpg"}
                    ],
                }
            ]).encode(),
        ),
    )
    http_mime.route(
        "GET",
        "/t/p/w500/p.jpg",
        lambda call: Response(200, {"Content-Type": "text/html"}, b"<script>alert(1)</script>"),
    )
    client_mime, _ = _signed_in_client(tmp_path / "mime", http_mime)
    resp_mime = client_mime.get("/posters/movie/701")
    assert resp_mime.status_code == 200
    assert "image/svg+xml" in resp_mime.headers["Content-Type"]
    assert b"<script>" not in resp_mime.data

    # 2. Oversize poster (> 2 MiB)
    http_big = FakeHttp()
    http_big.route(
        "GET",
        "/api/v3/movie/lookup",
        lambda call: Response(
            200,
            {},
            json.dumps([
                {
                    "tmdbId": 702,
                    "title": "Big Poster",
                    "images": [
                        {"coverType": "poster", "remoteUrl": "https://image.tmdb.org/t/p/w500/big.jpg"}
                    ],
                }
            ]).encode(),
        ),
    )
    http_big.route(
        "GET",
        "/t/p/w500/big.jpg",
        lambda call: Response(200, {"Content-Type": "image/jpeg"}, b"x" * (2 * 1024 * 1024 + 10)),
    )
    client_big, _ = _signed_in_client(tmp_path / "big", http_big)
    resp_big = client_big.get("/posters/movie/702")
    assert resp_big.status_code == 200
    assert "image/svg+xml" in resp_big.headers["Content-Type"]

    # 3. Redirect refused
    http_redir = FakeHttp()
    http_redir.route(
        "GET",
        "/api/v3/movie/lookup",
        lambda call: Response(
            200,
            {},
            json.dumps([
                {
                    "tmdbId": 703,
                    "title": "Redirect Poster",
                    "images": [
                        {"coverType": "poster", "remoteUrl": "https://image.tmdb.org/t/p/w500/r.jpg"}
                    ],
                }
            ]).encode(),
        ),
    )

    def _raise_redirect(call):
        raise UnsafeRedirect("https://image.tmdb.org")

    http_redir.route("GET", "/t/p/w500/r.jpg", _raise_redirect)
    client_redir, _ = _signed_in_client(tmp_path / "redir", http_redir)
    resp_redir = client_redir.get("/posters/movie/703")
    assert resp_redir.status_code == 200
    assert "image/svg+xml" in resp_redir.headers["Content-Type"]


# ---------------------------------------------------------------- exact id match only

JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9"
OTHER_JPEG = JPEG + b"other"


def _lookup(items):
    return lambda call: Response(
        200, {"Content-Type": "application/json"}, json.dumps(items).encode())


def _poster_item(id_field: str, value: int, name: str, **extra) -> dict:
    return {
        id_field: value, "title": name, "year": 2000,
        "images": [{"coverType": "poster",
                    "remoteUrl": f"https://image.tmdb.org/t/p/w500/{name}.jpg"}],
        **extra,
    }


def _poster_http(lookup_path: str, items) -> FakeHttp:
    http = FakeHttp()
    http.route("GET", lookup_path, _lookup(items))
    http.route("GET", "/t/p/w500/wanted.jpg",
               lambda call: Response(200, {"Content-Type": "image/jpeg"}, JPEG))
    http.route("GET", "/t/p/w500/other.jpg",
               lambda call: Response(200, {"Content-Type": "image/jpeg"}, OTHER_JPEG))
    return http


def _fetched(http: FakeHttp) -> list[str]:
    return [c.path for c in http.calls if c.path.startswith("/t/p/")]


def test_poster_is_never_another_titles_when_the_first_result_has_a_different_id(tmp_path: Path):
    # The app answers with other titles only (as a text search for "550" would).
    http = _poster_http("/api/v3/movie/lookup", [
        _poster_item("tmdbId", 999, "other"),
        _poster_item("tmdbId", 5500, "other"),
    ])
    client, _ = _signed_in_client(tmp_path, http)
    resp = client.get("/posters/movie/550")
    assert resp.status_code == 200
    assert resp.headers["Content-Type"].startswith("image/svg+xml")  # the local placeholder
    assert _fetched(http) == []  # the other title's poster was never even downloaded


def test_poster_picks_the_exact_id_wherever_it_is_in_the_results(tmp_path: Path):
    http = _poster_http("/api/v3/movie/lookup", [
        _poster_item("tmdbId", 999, "other"),
        _poster_item("tmdbId", 550, "wanted"),
    ])
    client, _ = _signed_in_client(tmp_path, http)
    resp = client.get("/posters/movie/550")
    assert resp.headers["Content-Type"] == "image/jpeg" and resp.data == JPEG
    assert _fetched(http) == ["/t/p/w500/wanted.jpg"]


def test_poster_lookup_asks_by_id_not_by_text(tmp_path: Path):
    http = _poster_http("/api/v3/movie/lookup", [_poster_item("tmdbId", 550, "wanted")])
    http.route("GET", "/api/v3/series/lookup", _lookup([_poster_item("tvdbId", 81189, "wanted")]))
    client, _ = _signed_in_client(tmp_path, http)
    assert client.get("/posters/movie/550").data == JPEG
    assert client.get("/posters/series/81189").data == JPEG
    lookups = [c.url.split("?", 1)[1] for c in http.calls if c.path.endswith("/lookup")]
    assert lookups == ["term=tmdb%3A550", "term=tvdb%3A81189"]


def test_poster_matches_only_the_id_field_of_its_kind(tmp_path: Path):
    # The app's own row number ("id") and the other catalogue's id are different numbers
    # that can collide with the one asked for: neither may select a poster.
    http = _poster_http("/api/v3/movie/lookup", [
        _poster_item("id", 550, "other"),
        _poster_item("tvdbId", 550, "other"),
        _poster_item("tmdbId", "550", "other"),
    ])
    http.route("GET", "/api/v3/series/lookup", _lookup([
        _poster_item("tmdbId", 550, "other"),
        _poster_item("id", 550, "other", tvdbId=7),
    ]))
    client, _ = _signed_in_client(tmp_path, http)
    for path in ("/posters/movie/550", "/posters/series/550"):
        resp = client.get(path)
        assert resp.headers["Content-Type"].startswith("image/svg+xml"), path
    assert _fetched(http) == []
