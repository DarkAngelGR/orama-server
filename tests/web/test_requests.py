"""Tests for search, media detail, request submission, live detail status, and Home."""

from __future__ import annotations

import json
from pathlib import Path

from orama.requests.models import RequestSpec, RequestUpdate
from orama.secrets import SecretStore
from orama.setup_model import DesiredSetup
from orama.web import SETUP_FILE, auth, create_app
from orama.wiring.http import FakeHttp, Response

PASSWORD = "correct horse battery"


class FakeClock:
    def __init__(self, now: float = 1_700_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class FakeAgent:
    def status(self) -> dict[str, str]:
        return {
            "plex": "running",
            "radarr": "running",
            "sonarr": "running",
            "bazarr": "running",
            "prowlarr": "running",
            "nzbget": "running",
        }


class SpyWorker:
    def __init__(self) -> None:
        self.wakes = 0

    def wake(self) -> None:
        self.wakes += 1


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


from web.conftest import csrf_from, signin


def _make_env(tmp_path: Path, **setup_overrides):
    clock = FakeClock()
    http = FakeHttp()
    worker = SpyWorker()
    setup = _setup(**setup_overrides)
    (tmp_path / SETUP_FILE).write_text(
        json.dumps(setup.model_dump(mode="json")), encoding="utf-8"
    )
    store = SecretStore(tmp_path / "secrets.bin")
    store.set("apikey:radarr", "radarr-secret-token")
    store.set("apikey:sonarr", "sonarr-secret-token")

    app = create_app(
        tmp_path,
        agent=FakeAgent(),
        http=http,
        clock=clock,
        request_worker=worker,
    )
    client = app.test_client()
    st = app.extensions["orama"]
    st.passwords.create(PASSWORD)
    assert signin(client, PASSWORD).status_code == 303
    csrf = csrf_from(client.get("/"))
    return client, app, st, http, worker, clock, csrf


def test_all_four_request_types_and_duplicate_post_idempotence(tmp_path: Path):
    client, _app, st, _http, worker, _clock, csrf = _make_env(tmp_path)

    # 1. Movie request
    r_movie = client.post(
        "/requests",
        data={
            "kind": "movie",
            "source_id": "101",
            "title": "Arrival",
            "year": "2016",
            "csrf_token": csrf,
        },
    )
    assert r_movie.status_code == 303
    movie_loc = r_movie.headers["Location"]
    assert movie_loc.startswith("/requests/")
    assert worker.wakes == 1

    # Duplicate POST -> same detail URL, no duplicate record
    r_movie_dup = client.post(
        "/requests",
        data={
            "kind": "movie",
            "source_id": "101",
            "title": "Arrival",
            "year": "2016",
            "csrf_token": csrf,
        },
    )
    assert r_movie_dup.status_code == 303
    assert r_movie_dup.headers["Location"] == movie_loc
    assert len(st.requests.list_recent()) == 1

    # 2. Single episode request
    r_ep = client.post(
        "/requests",
        data={
            "kind": "episode",
            "source_id": "202",
            "season": "1",
            "episode": "3",
            "title": "Andor",
            "csrf_token": csrf,
        },
    )
    assert r_ep.status_code == 303
    assert r_ep.headers["Location"].startswith("/requests/")

    # 3. Full season request
    r_season = client.post(
        "/requests",
        data={
            "kind": "season",
            "source_id": "202",
            "season": "1",
            "title": "Andor",
            "csrf_token": csrf,
        },
    )
    assert r_season.status_code == 303

    # 4. Ongoing series request
    r_series = client.post(
        "/requests",
        data={
            "kind": "series",
            "source_id": "202",
            "title": "Andor",
            "csrf_token": csrf,
        },
    )
    assert r_series.status_code == 303
    assert len(st.requests.list_recent()) == 4


def test_tampered_ids_and_coordinates_rejected(tmp_path: Path):
    client, _app, st, _http, _worker, _clock, csrf = _make_env(tmp_path)

    bad_payloads = [
        {"kind": "movie", "source_id": "-5", "csrf_token": csrf},
        {"kind": "movie", "source_id": "abc", "csrf_token": csrf},
        {"kind": "movie", "source_id": "100", "season": "1", "csrf_token": csrf},
        {"kind": "episode", "source_id": "100", "season": "1", "csrf_token": csrf},
        {"kind": "episode", "source_id": "100", "season": "1", "episode": "0", "csrf_token": csrf},
        {"kind": "season", "source_id": "100", "season": "-1", "csrf_token": csrf},
        {"kind": "unknown", "source_id": "100", "csrf_token": csrf},
    ]
    for payload in bad_payloads:
        resp = client.post("/requests", data=payload)
        assert resp.status_code == 400
    assert len(st.requests.list_recent()) == 0

    assert client.get("/media/unknown/123").status_code == 404
    assert client.get("/media/movie/0").status_code == 400
    assert client.get("/requests/not-a-uuid").status_code == 404


def test_xss_escaping_and_no_credentials_or_upstream_urls_in_html(tmp_path: Path):
    client, _app, _st, http, _worker, _clock, _csrf = _make_env(tmp_path)
    http.route(
        "GET",
        "/api/v3/movie/lookup",
        lambda call: Response(
            200,
            {},
            json.dumps([
                {
                    "tmdbId": 909,
                    "title": "<img src=x onerror=alert(1)>Alien",
                    "year": 1979,
                    "hasFile": False,
                    "images": [
                        {
                            "coverType": "poster",
                            "remoteUrl": "https://image.tmdb.org/t/p/w500/alien.jpg",
                        }
                    ],
                }
            ]).encode(),
        ),
    )
    resp = client.get("/search?q=Alien&type=movie")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "<img src=x onerror=alert(1)>" not in body
    assert "radarr-secret-token" not in body
    assert "https://image.tmdb.org" not in body
    assert "/posters/movie/909" in body


def test_unauthorized_and_csrf_enforced(tmp_path: Path):
    client, app, st, _http, _worker, _clock, csrf = _make_env(tmp_path)

    # Missing CSRF token rejected with 403
    resp_no_csrf = client.post(
        "/requests",
        data={"kind": "movie", "source_id": "101", "title": "Arrival"},
    )
    assert resp_no_csrf.status_code == 403

    # Principal without requests:write rejected with 403
    sid = next(iter(st.sessions._signed_in))
    st.sessions._signed_in[sid].principal = auth.Principal("viewer", frozenset({"requests:read"}))
    resp_no_perm = client.post(
        "/requests",
        data={"kind": "movie", "source_id": "101", "title": "Arrival", "csrf_token": csrf},
    )
    assert resp_no_perm.status_code == 403

    # Unauthenticated client redirected to /signin
    anon = app.test_client()
    assert anon.get("/search?q=test").status_code == 303
    assert anon.get("/media/movie/101").status_code == 303


def test_in_library_marking_and_disabled_app_handling(tmp_path: Path):
    client, _app, _st, http, _worker, _clock, _csrf = _make_env(tmp_path)
    http.route(
        "GET",
        "/api/v3/movie/lookup",
        lambda call: Response(
            200,
            {},
            json.dumps([
                {
                    "id": 12,
                    "tmdbId": 303,
                    "title": "The Matrix",
                    "year": 1999,
                    "hasFile": True,
                    "images": [],
                }
            ]).encode(),
        ),
    )
    http.route(
        "GET",
        "/api/v3/movie",
        lambda call: Response(
            200,
            {},
            json.dumps([{"id": 12, "tmdbId": 303, "title": "The Matrix", "hasFile": True}]).encode(),
        ),
    )

    detail_resp = client.get("/media/movie/303")
    assert detail_resp.status_code == 200
    html = detail_resp.get_data(as_text=True)
    assert "In library" in html
    assert 'name="kind" value="movie"' not in html

    # Disabled app shows plain message
    (tmp_path / SETUP_FILE).write_text(
        json.dumps(_setup(removed_apps=["radarr"]).model_dump(mode="json")),
        encoding="utf-8",
    )
    disabled_resp = client.get("/search?q=Matrix&type=movie")
    assert disabled_resp.status_code == 200
    assert "turned off in Settings" in disabled_resp.get_data(as_text=True)


def test_polling_get_triggers_no_work_and_renders_season_and_series_wording(tmp_path: Path):
    client, _app, st, http, worker, clock, csrf = _make_env(tmp_path)

    # Create a season request with partial child episode targets
    season_rec, _ = st.requests.create(
        RequestSpec(kind="season", source_id=500, season=1),
        clock(),
        title="Severance",
        year=2022,
    )
    st.requests.upsert_episode_target(500, 1, 1, title="Good News About Hell", stage="ready", has_file=True)
    st.requests.upsert_episode_target(500, 1, 2, title="Half Loop", stage="downloading", has_file=False)

    wakes_before = worker.wakes
    calls_before = len(http.calls)

    status_resp = client.get(f"/requests/{season_rec.id}/status")
    assert status_resp.status_code == 200
    status_html = status_resp.get_data(as_text=True)
    assert "1 of 2 episodes ready" in status_html
    assert "Good News About Hell" in status_html
    assert "Half Loop" in status_html
    assert worker.wakes == wakes_before
    assert len(http.calls) == calls_before

    # Create an ongoing series request and verify future-show subscription wording
    series_rec, _ = st.requests.create(
        RequestSpec(kind="series", source_id=600),
        clock(),
        title="Silo",
        year=2023,
    )
    st.requests.update(
        series_rec.id,
        series_rec.revision,
        RequestUpdate(
            stage="ready",
            playback_note="Ready · may transcode (TrueHD)",
            subtitle_note="Ready · EN subs, still looking for EL",
        ),
    )
    detail_resp = client.get(f"/requests/{series_rec.id}")
    assert detail_resp.status_code == 200
    detail_html = detail_resp.get_data(as_text=True)
    assert "Future episodes will download automatically as they air." in detail_html
    assert "Ready · may transcode (TrueHD)" in detail_html
    assert "Ready · EN subs, still looking for EL" in detail_html

    # Retry button wakes worker and resets stage to searching
    retry_resp = client.post(
        f"/requests/{series_rec.id}/retry",
        data={"csrf_token": csrf},
    )
    assert retry_resp.status_code == 303
    assert worker.wakes == wakes_before + 1
    updated = st.requests.get(series_rec.id)
    assert updated is not None
    assert updated.stage == "searching"

    # Retrying an unsubmitted request (remote_id is None) must not strand it:
    # RequestWorker.tick() must still submit it to the upstream app.
    from orama.requests.client import MediaClient
    from orama.requests.worker import RequestWorker

    unsub_rec, _ = st.requests.create(
        RequestSpec(kind="movie", source_id=777),
        clock(),
        title="Unsubmitted Movie",
    )
    st.requests.update(
        unsub_rec.id,
        unsub_rec.revision,
        RequestUpdate(waiting_reason="Waiting for Radarr", next_check_at=clock() + 60.0),
    )
    retry_unsub = client.post(
        f"/requests/{unsub_rec.id}/retry",
        data={"csrf_token": csrf},
    )
    assert retry_unsub.status_code == 303
    after_retry = st.requests.get(unsub_rec.id)
    assert after_retry is not None
    assert after_retry.stage == "requested"
    assert after_retry.waiting_reason is None

    http.route(
        "GET",
        "/api/v3/movie",
        lambda call: Response(
            200, {}, json.dumps([{"id": 44, "tmdbId": 777, "hasFile": False}]).encode()
        ),
    )
    http.route(
        "POST",
        "/api/v3/command",
        lambda call: Response(201, {}, json.dumps({"id": 1, "name": "MoviesSearch"}).encode()),
    )
    real_worker = RequestWorker(
        store=st.requests,
        client_factory=lambda: MediaClient(setup=st.setup.current(), store=st.store, http=http),
        clock=clock,
    )
    # Lock season_rec and series_rec forward so unsub_rec is claimed first
    for prior_id in (season_rec.id, series_rec.id):
        prior = st.requests.get(prior_id)
        assert prior is not None
        st.requests.update(
            prior.id,
            prior.revision,
            RequestUpdate(next_check_at=clock() + 100.0),
        )
    assert real_worker.tick(clock()) == 1
    submitted_rec = st.requests.get(unsub_rec.id)
    assert submitted_rec is not None
    assert submitted_rec.remote_id == 44
    assert submitted_rec.stage == "downloading"



def test_home_disk_warning_below_equal_above_threshold_and_stat_error(tmp_path: Path):
    from orama.agent_client import AgentError

    client, _app, st, _http, _worker, _clock, _csrf = _make_env(
        tmp_path, disk_warning_percent=5
    )

    class SpaceAgent(FakeAgent):
        def __init__(self, total: int, free: int, err: Exception | None = None) -> None:
            self.total = total
            self.free = free
            self.err = err

        def data_space(self) -> dict[str, int]:
            if self.err:
                raise self.err
            return {"total_bytes": self.total, "free_bytes": self.free}

    # 1. Below 5% threshold (4% free) -> warning banner shown on Home
    st.agent = SpaceAgent(total=1000, free=40)
    html_below = client.get("/").get_data(as_text=True)
    assert "Storage space is running low" in html_below
    assert "4%" in html_below

    # 2. Equal to 5% threshold (5% free) -> no low-disk warning banner
    st.agent = SpaceAgent(total=1000, free=50)
    html_equal = client.get("/").get_data(as_text=True)
    assert "Storage space is running low" not in html_equal

    # 3. Above 5% threshold (20% free) -> no low-disk warning banner
    st.agent = SpaceAgent(total=1000, free=200)
    html_above = client.get("/").get_data(as_text=True)
    assert "Storage space is running low" not in html_above

    # 4. Stat error / AgentError -> no false "0% free" warning
    st.agent = SpaceAgent(total=0, free=0, err=AgentError("cannot stat"))
    html_err = client.get("/").get_data(as_text=True)
    assert "Storage space is running low" not in html_err
    assert "0%" not in html_err



def test_damaged_request_database_shows_plain_unavailable_page(tmp_path: Path):
    client, _app, _st, _http, _worker, _clock, csrf = _make_env(tmp_path)
    (tmp_path / "requests.sqlite3").write_bytes(b"not a database" * 100)

    home = client.get("/")
    assert home.status_code == 200

    posted = client.post(
        "/requests", data={"kind": "movie", "source_id": "5", "title": "X", "csrf_token": csrf}
    )
    assert posted.status_code == 503
    text = posted.get_data(as_text=True).lower()
    assert "unavailable" in text
    assert "503" not in text and "sqlite" not in text


def test_requesting_a_ready_movie_again_makes_it_active_again(tmp_path: Path):
    client, _app, st, _http, worker, _clock, csrf = _make_env(tmp_path)
    form = {"kind": "movie", "source_id": "101", "title": "Arrival", "csrf_token": csrf}
    location = client.post("/requests", data=form).headers["Location"]
    rec = st.requests.list_recent()[0]
    st.requests.update(rec.id, rec.revision, RequestUpdate(stage="ready", remote_id=9))

    again = client.post("/requests", data=form)
    assert again.headers["Location"] == location
    after = st.requests.get(rec.id)
    assert after.stage == "requested" and after.remote_id == 9
    assert worker.wakes == 2


def test_oversized_ids_are_a_400_not_a_500(tmp_path: Path):
    client, _app, _st, _http, _worker, _clock, csrf = _make_env(tmp_path)
    for data in (
        {"kind": "movie", "source_id": "9223372036854775808"},
        {"kind": "movie", "source_id": "999999999"},
        {"kind": "episode", "source_id": "5", "season": "1", "episode": "9" * 30},
    ):
        response = client.post("/requests", data={**data, "title": "x", "csrf_token": csrf})
        assert response.status_code == 400
