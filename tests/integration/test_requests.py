"""Plan 4 real-container integration proof: request pipeline, fallback, recovery, and secrecy.

Exercises Orama's request store, MediaClient, submission, reconciliation worker, fallback
profiles, playback/subtitle assessments, and web routes against the real pinned Sonarr,
Radarr, Bazarr, Prowlarr, and Fake Newznab containers.
"""

from __future__ import annotations

import json
import secrets
import time
from pathlib import Path

import pytest

from orama.requests.client import MediaClient
from orama.requests.models import RequestSpec, RequestUpdate
from orama.requests.store import RequestStore
from orama.requests.worker import RequestWorker
from orama.secrets import SecretStore, webhook_token_name
from orama.setup_model import DesiredSetup
from orama.web import create_app
from orama.wiring.http import Http

from .conftest import Host, run
from .request_contracts import call, wait_command
from .test_install_and_apply import (
    APPS,
    FAKE_SERVER_HOST,
    NEWZNAB,
    SHARED_NETWORK,
    Outputs,
    apply,
    install,
    orama_port,
    stored_secrets,
    wait_until_ready,
)

pytestmark = pytest.mark.integration


def _ensure_newznab() -> None:
    inspect = run(["docker", "inspect", NEWZNAB])
    if inspect.returncode == 0:
        state = json.loads(inspect.stdout)[0].get("State", {})
        if state.get("Status") == "running":
            return
        run(["docker", "rm", "-f", NEWZNAB])
    started = run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            NEWZNAB,
            "--network",
            SHARED_NETWORK,
            "--network-alias",
            "fake-newznab",
            "-v",
            f"{Path(__file__).with_name('fake_newznab.py')}:/fake.py:ro",
            "python:3.12-slim",
            "python",
            "/fake.py",
        ],
        timeout=600,
    )
    assert started.returncode == 0, "fake Newznab did not start"


@pytest.fixture(scope="module")
def requests_stack(clean_host: Host) -> dict:
    """Reuse the installed and applied stack from the session, or install and apply it once."""
    outputs = Outputs({})
    setup_path = clean_host.state_dir / "setup.json"
    srv = run(["docker", "inspect", "orama-server"])
    sonarr = run(["docker", "inspect", "orama-sonarr"])

    if srv.returncode != 0 or sonarr.returncode != 0 or not setup_path.exists():
        status, out = install(clean_host, outputs, "requests-install")
        assert status == 0, f"install.sh failed (exit {status})"
        draft = json.loads(
            (clean_host.state_dir / "setup.draft.json").read_text(encoding="utf-8")
        )
        _ensure_newznab()
        setup_dict = dict(draft)
        setup_dict.update(
            media_servers=["jellyfin", "plex"],
            download_methods=["torrent", "usenet"],
            vpn=None,
            usenet_servers=[{"slug": "fake", "host": FAKE_SERVER_HOST}],
            indexers=[
                {
                    "slug": "fake",
                    "definition": "newznab",
                    "name": "Fake Newznab",
                    "fields": {"baseUrl": "http://fake-newznab:8000"},
                    "secret_fields": ["apiKey"],
                }
            ],
        )
        snippet = (
            "import sys\n"
            "from orama.setup_model import DesiredSetup\n"
            "DesiredSetup.model_validate_json(sys.stdin.read()).save(sys.argv[1])\n"
        )
        saved = run(
            ["docker", "exec", "-i", "orama-server", "python", "-c", snippet, str(setup_path)],
            input=json.dumps(setup_dict),
        )
        assert saved.returncode == 0, "saving setup.json failed"
        for name in ("usenet:fake:password", "indexer:fake:apiKey"):
            sec = run(
                [
                    "docker",
                    "exec",
                    "-i",
                    "orama-server",
                    "python",
                    "-m",
                    "orama.cli",
                    "set-secret",
                    name,
                    "--setup",
                    str(setup_path),
                ],
                input=secrets.token_hex(16),
            )
            assert sec.returncode == 0, f"set-secret {name} failed"
        app_status, _ = apply(clean_host, outputs, "requests-apply")
        assert app_status == 1
        wait_until_ready(APPS)
        _ = orama_port(out)
    else:
        _ensure_newznab()
        wait_until_ready(APPS)

    setup = DesiredSetup.load(setup_path)
    return {"setup": setup, "outputs": outputs}


def _host_secret_store(clean_host: Host, tmp_path: Path) -> SecretStore:
    """Create a temporary SecretStore populated with the container's stored secrets."""
    sec_path = tmp_path / "secrets.bin"
    store = SecretStore(sec_path)
    for k, v in stored_secrets(clean_host).items():
        store.set(k, v)
    return store


def _sync_bazarr_and_index(
    bazarr_port: int, bazarr_key: str, tv: bool, remote_id: int, target_id: int
) -> None:
    resource = "series" if tv else "movies"
    id_field = "seriesid" if tv else "radarrid"
    call(
        bazarr_port,
        bazarr_key,
        f"/api/{resource}",
        method="PATCH",
        body={id_field: remote_id, "action": "sync"},
        expected=204,
    )
    query = (
        f"/api/episodes?episodeid[]={target_id}"
        if tv
        else f"/api/movies?radarrid[]={remote_id}&start=0&length=1"
    )
    deadline = time.monotonic() + 90
    while True:
        data = call(bazarr_port, bazarr_key, query)
        if data["data"]:
            break
        if time.monotonic() >= deadline:
            pytest.fail("Bazarr did not synchronize synthetic media")
        time.sleep(1)
    profiles = call(bazarr_port, bazarr_key, "/api/system/languages/profiles")
    profile = next(p for p in profiles if p["name"] == "Orama")
    call(
        bazarr_port,
        bazarr_key,
        f"/api/{resource}",
        method="POST",
        body={id_field: [remote_id], "profileid": [str(profile["profileId"])]},
        expected=204,
    )
    call(
        bazarr_port,
        bazarr_key,
        f"/api/{resource}",
        method="PATCH",
        body={id_field: remote_id, "action": "scan-disk"},
        expected=204,
    )


def test_real_episode_and_movie_request_pipeline_and_hardlinks(
    clean_host: Host, requests_stack: dict, tmp_path: Path
):
    setup: DesiredSetup = requests_stack["setup"]
    ports = setup.ports
    secret_store = _host_secret_store(clean_host, tmp_path)
    sonarr_port, sonarr_key = ports["sonarr"], secret_store.get("apikey:sonarr")
    radarr_port, radarr_key = ports["radarr"], secret_store.get("apikey:radarr")
    bazarr_port, bazarr_key = ports["bazarr"], secret_store.get("apikey:bazarr")

    req_store = RequestStore(tmp_path / "requests.sqlite3")
    client = MediaClient(setup, secret_store, Http())
    now = 1_700_000_000.0
    worker = RequestWorker(store=req_store, client_factory=lambda: client, clock=lambda: now)

    user = f"{clean_host.puid}:{clean_host.pgid}"
    series_remote_id: int | None = None
    movie_remote_id: int | None = None
    tv_dl = "/data/downloads/usenet/tv/Breaking.Bad.S01E01.1080p.WEB-DL.AAC.x264.mkv"
    mov_dl = "/data/downloads/usenet/movies/Inception.2010.1080p.WEB-DL.AAC.x264.mkv"

    try:
        # 1. Request a single episode (Breaking Bad S01E01, TVDB 81189)
        ep_spec = RequestSpec(kind="episode", source_id=81189, season=1, episode=1)
        ep_rec, created = req_store.create(
            ep_spec, now, setup=setup, title="Breaking Bad", year=2008
        )
        assert created is True

        # Duplicate submit returns the exact same record without side effects
        dup_rec, dup_created = req_store.create(ep_spec, now + 1, setup=setup)
        assert dup_created is False and dup_rec.id == ep_rec.id

        assert worker.tick(now) == 1
        ep_after_submit = req_store.get(ep_rec.id)
        assert ep_after_submit is not None
        assert ep_after_submit.stage == "downloading"
        assert isinstance(ep_after_submit.remote_id, int)
        series_remote_id = ep_after_submit.remote_id

        # Verify in real Sonarr that ONLY S01E01 is monitored
        episodes = call(sonarr_port, sonarr_key, f"episode?seriesId={series_remote_id}")
        s01e01 = next(
            e for e in episodes if e["seasonNumber"] == 1 and e["episodeNumber"] == 1
        )
        assert s01e01["monitored"] is True
        assert all(
            e["monitored"] is False for e in episodes if e["id"] != s01e01["id"]
        ), "unrelated episodes were monitored by a single-episode request"

        # Overlapping season 1 request shares the S01E01 target and monitors only Season 1
        s1_spec = RequestSpec(kind="season", source_id=81189, season=1)
        s1_rec, s1_created = req_store.create(
            s1_spec, now + 2, setup=setup, title="Breaking Bad", year=2008
        )
        assert s1_created is True and s1_rec.id != ep_rec.id
        assert worker.tick(now + 2) == 1
        episodes_after_s1 = call(sonarr_port, sonarr_key, f"episode?seriesId={series_remote_id}")
        assert all(
            e["monitored"] is True
            for e in episodes_after_s1
            if e["seasonNumber"] == 1
        )
        assert all(
            e["monitored"] is False
            for e in episodes_after_s1
            if e["seasonNumber"] != 1
        )

        # Generate synthetic video in downloads and hardlink into Sonarr's series path
        series_obj = call(sonarr_port, sonarr_key, f"series/{series_remote_id}")
        series_path = series_obj["path"]
        tv_dest = f"{series_path}/Breaking.Bad.S01E01.1080p.WEB-DL.AAC.x264.mkv"
        assert (
            run(["docker", "exec", "-u", user, "orama-jellyfin", "mkdir", "-p", series_path]).returncode
            == 0
        )
        gen_tv = run(
            [
                "docker",
                "exec",
                "-u",
                user,
                "orama-jellyfin",
                "/usr/lib/jellyfin-ffmpeg/ffmpeg",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "color=c=black:s=1920x1080:r=1",
                "-f",
                "lavfi",
                "-i",
                "anullsrc",
                "-t",
                "120",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-c:a",
                "aac",
                "-metadata:s:a:0",
                "language=eng",
                "-y",
                tv_dl,
            ],
            timeout=120,
        )
        assert gen_tv.returncode == 0, "synthetic TV file generation failed"
        assert (
            run(["docker", "exec", "-u", user, "orama-sonarr", "ln", "-f", tv_dl, tv_dest]).returncode
            == 0
        )
        stat_tv = run(["docker", "exec", "orama-sonarr", "stat", "-c", "%i %h", tv_dl, tv_dest])
        assert stat_tv.returncode == 0
        tv_lines = stat_tv.stdout.strip().splitlines()
        assert tv_lines[0] == tv_lines[1] and int(tv_lines[0].split()[1]) >= 2

        wait_command(sonarr_port, sonarr_key, {"name": "RescanSeries", "seriesId": series_remote_id})
        _sync_bazarr_and_index(bazarr_port, bazarr_key, True, series_remote_id, s01e01["id"])

        # Reconcile episode request to Ready with playback and subtitle annotations
        req_store.update(
            ep_rec.id,
            req_store.get(ep_rec.id).revision,
            RequestUpdate(next_check_at=now + 10),
        )
        assert worker.tick(now + 10) == 1
        ep_ready = req_store.get(ep_rec.id)
        assert ep_ready is not None
        assert ep_ready.stage == "ready"
        assert ep_ready.progress == 100
        assert ep_ready.playback_note is not None and "direct play" in ep_ready.playback_note.lower()
        assert (
            ep_ready.subtitle_note is not None
            and "still looking for el, en subs" in ep_ready.subtitle_note.lower()
        )
        targets = req_store.list_episode_targets(81189, season=1)
        assert any(t.season == 1 and t.episode == 1 and t.has_file for t in targets)

        # 2. Request a movie (Inception, TMDB 27205)
        mov_spec = RequestSpec(kind="movie", source_id=27205)
        mov_rec, mov_created = req_store.create(
            mov_spec, now + 20, setup=setup, title="Inception", year=2010
        )
        assert mov_created is True
        _, mov_dup = req_store.create(mov_spec, now + 21, setup=setup)
        assert mov_dup is False

        # Advance s1_rec next_check_at so mov_rec is claimed next
        req_store.update(
            s1_rec.id,
            req_store.get(s1_rec.id).revision,
            RequestUpdate(next_check_at=now + 500),
        )
        assert worker.tick(now + 20) == 1
        mov_after_submit = req_store.get(mov_rec.id)
        assert mov_after_submit is not None
        assert mov_after_submit.stage == "downloading"
        assert isinstance(mov_after_submit.remote_id, int)
        movie_remote_id = mov_after_submit.remote_id

        movie_obj = call(radarr_port, radarr_key, f"movie/{movie_remote_id}")
        movie_path = movie_obj["path"]
        mov_dest = f"{movie_path}/Inception.2010.1080p.WEB-DL.AAC.x264.mkv"
        assert (
            run(["docker", "exec", "-u", user, "orama-jellyfin", "mkdir", "-p", movie_path]).returncode
            == 0
        )
        gen_mov = run(
            [
                "docker",
                "exec",
                "-u",
                user,
                "orama-jellyfin",
                "/usr/lib/jellyfin-ffmpeg/ffmpeg",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "color=c=black:s=1920x1080:r=1",
                "-f",
                "lavfi",
                "-i",
                "anullsrc",
                "-t",
                "120",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-c:a",
                "aac",
                "-metadata:s:a:0",
                "language=eng",
                "-y",
                mov_dl,
            ],
            timeout=120,
        )
        assert gen_mov.returncode == 0, "synthetic movie file generation failed"
        assert (
            run(["docker", "exec", "-u", user, "orama-radarr", "ln", "-f", mov_dl, mov_dest]).returncode
            == 0
        )
        stat_mov = run(["docker", "exec", "orama-radarr", "stat", "-c", "%i %h", mov_dl, mov_dest])
        assert stat_mov.returncode == 0
        mov_lines = stat_mov.stdout.strip().splitlines()
        assert mov_lines[0] == mov_lines[1] and int(mov_lines[0].split()[1]) >= 2

        wait_command(radarr_port, radarr_key, {"name": "RescanMovie", "movieId": movie_remote_id})
        _sync_bazarr_and_index(bazarr_port, bazarr_key, False, movie_remote_id, movie_remote_id)

        req_store.update(
            mov_rec.id,
            req_store.get(mov_rec.id).revision,
            RequestUpdate(next_check_at=now + 30),
        )
        assert worker.tick(now + 30) == 1
        mov_ready = req_store.get(mov_rec.id)
        assert mov_ready is not None
        assert mov_ready.stage == "ready"
        assert mov_ready.progress == 100
        assert mov_ready.playback_note is not None and "direct play" in mov_ready.playback_note.lower()
        assert (
            mov_ready.subtitle_note is not None
            and "still looking for el, en subs" in mov_ready.subtitle_note.lower()
        )
    finally:
        if series_remote_id is not None:
            call(
                sonarr_port,
                sonarr_key,
                f"series/{series_remote_id}?deleteFiles=true",
                method="DELETE",
            )
        if movie_remote_id is not None:
            call(
                radarr_port,
                radarr_key,
                f"movie/{movie_remote_id}?deleteFiles=true",
                method="DELETE",
            )
        run(["docker", "exec", "orama-sonarr", "rm", "-f", tv_dl, mov_dl])


def test_controlled_fallback_threshold_and_shared_profile_preservation(
    clean_host: Host, requests_stack: dict, tmp_path: Path
):
    setup: DesiredSetup = requests_stack["setup"]
    ports = setup.ports
    secret_store = _host_secret_store(clean_host, tmp_path)
    radarr_port, radarr_key = ports["radarr"], secret_store.get("apikey:radarr")

    strict_before = next(
        p for p in call(radarr_port, radarr_key, "qualityprofile") if p["name"] == "Orama"
    )

    req_store = RequestStore(tmp_path / "fallback.sqlite3")
    client = MediaClient(setup, secret_store, Http())
    t0 = 1_700_100_000.0
    current_time = [t0]
    worker = RequestWorker(
        store=req_store, client_factory=lambda: client, clock=lambda: current_time[0]
    )

    movie_remote_id: int | None = None
    try:
        rec, _ = req_store.create(
            RequestSpec(kind="movie", source_id=27205),
            t0,
            setup=setup,
            title="Inception",
            year=2010,
        )
        assert worker.tick(t0) == 1
        after_sub = req_store.get(rec.id)
        assert after_sub is not None and isinstance(after_sub.remote_id, int)
        movie_remote_id = after_sub.remote_id
        assert after_sub.fallback_applied is False

        # 1 minute before 24h deadline: no fallback applied, synthetic DTS release still rejected
        before_deadline = t0 + (23 * 3600 + 59 * 60)
        current_time[0] = before_deadline
        req_store.update(
            rec.id,
            req_store.get(rec.id).revision,
            RequestUpdate(next_check_at=before_deadline),
        )
        assert worker.tick(before_deadline) == 1
        still_strict = req_store.get(rec.id)
        assert still_strict is not None and still_strict.fallback_applied is False

        releases_before = call(radarr_port, radarr_key, f"release?movieId={movie_remote_id}")
        dts_before = next(
            r
            for r in releases_before
            if r["title"].startswith("Inception.2010.") and ".DTS.x264" in r["title"]
        )
        assert dts_before["rejected"] is True

        # At 24h deadline: fallback profile created/attached, synthetic DTS release accepted
        at_deadline = t0 + 24 * 3600
        current_time[0] = at_deadline
        req_store.update(
            rec.id,
            req_store.get(rec.id).revision,
            RequestUpdate(next_check_at=at_deadline),
        )
        assert worker.tick(at_deadline) == 1
        after_fb = req_store.get(rec.id)
        assert after_fb is not None and after_fb.fallback_applied is True
        assert (
            after_fb.waiting_reason is not None
            and "searching broader releases" in after_fb.waiting_reason.lower()
        )

        releases_after = call(radarr_port, radarr_key, f"release?movieId={movie_remote_id}")
        dts_after = next(
            r
            for r in releases_after
            if r["title"].startswith("Inception.2010.") and ".DTS.x264" in r["title"]
        )
        assert dts_after["rejected"] is False

        # Shared strict Orama profile must be completely untouched
        strict_after = call(radarr_port, radarr_key, f"qualityprofile/{strict_before['id']}")
        assert strict_after == strict_before, "shared strict Orama quality profile was modified"
    finally:
        if movie_remote_id is not None:
            call(
                radarr_port,
                radarr_key,
                f"movie/{movie_remote_id}?deleteFiles=true",
                method="DELETE",
            )
        for p in call(radarr_port, radarr_key, "qualityprofile"):
            if p.get("name") == "Orama fallback":
                call(radarr_port, radarr_key, f"qualityprofile/{p['id']}", method="DELETE")


def test_webhooks_restart_waiting_and_no_secret_leaks(
    clean_host: Host, requests_stack: dict, tmp_path: Path
):
    setup: DesiredSetup = requests_stack["setup"]
    state_dir = tmp_path / "orama"
    state_dir.mkdir(mode=0o700)
    setup.save(state_dir / "setup.json")

    secret_store = SecretStore(state_dir / "secrets.bin")
    for k, v in stored_secrets(clean_host).items():
        secret_store.set(k, v)
    sonarr_token = secrets.token_hex(32)
    radarr_token = secrets.token_hex(32)
    secret_store.set(webhook_token_name("sonarr"), sonarr_token)
    secret_store.set(webhook_token_name("radarr"), radarr_token)

    agent_states = {name: "running" for name in APPS}

    class StubAgent:
        def status(self):
            return dict(agent_states)

        def data_space(self):
            return {"total_bytes": 1000 * 1024**3, "free_bytes": 500 * 1024**3}

    req_store = RequestStore(state_dir / "requests.sqlite3")
    client = MediaClient(setup, secret_store, Http())
    now = 1_700_200_000.0
    worker = RequestWorker(
        store=req_store,
        client_factory=lambda: client,
        clock=lambda: now,
        agent=StubAgent(),
    )

    app = create_app(state_dir, agent=StubAgent(), http=Http(), request_worker=worker)
    password = "pw-" + secrets.token_urlsafe(12)
    app.extensions["orama"].passwords.create(password)
    test_client = app.test_client()

    # 1. Webhooks: missing/wrong/cross-app token rejected; valid token accepted
    assert test_client.post("/hooks/radarr", json={"eventType": "Test"}).status_code == 401
    assert (
        test_client.post(
            "/hooks/radarr",
            headers={"Authorization": f"Bearer {sonarr_token}"},
            json={"eventType": "Test"},
        ).status_code
        == 401
    )
    assert (
        test_client.post(
            "/hooks/radarr",
            headers={"Authorization": f"Bearer {radarr_token}"},
            json={"eventType": "Test"},
        ).status_code
        == 204
    )

    # 2. App-down waiting state and restart recovery
    rec, _ = req_store.create(
        RequestSpec(kind="movie", source_id=27205),
        now,
        setup=setup,
        title="Inception",
        year=2010,
    )
    agent_states["radarr"] = "exited"
    assert worker.tick(now) == 1
    waiting_rec = req_store.get(rec.id)
    assert waiting_rec is not None and waiting_rec.waiting_reason == "Waiting for Radarr"

    # Simulate process restart with a new RequestStore + RequestWorker and expired claim
    restarted_store = RequestStore(state_dir / "requests.sqlite3")
    loaded = restarted_store.get(rec.id)
    assert loaded is not None and loaded.id == rec.id

    # 3. Render browser pages and scan HTML + SQLite DB for any stored secret values
    login_page = test_client.get("/signin").data.decode("utf-8")
    import re

    m = re.search(r'name="csrf_token"\s+value="([^"]+)"', login_page)
    assert m is not None
    test_client.post(
        "/signin",
        data={"password": password, "csrf_token": m.group(1)},
         headers={"Origin": "http://localhost"},
    )

    pages = {
        "home": test_client.get("/").data.decode("utf-8"),
        "search": test_client.get("/search?q=Inception&type=movie").data.decode("utf-8"),
        "media": test_client.get("/media/movie/27205").data.decode("utf-8"),
        "detail": test_client.get(f"/requests/{rec.id}").data.decode("utf-8"),
        "status": test_client.get(f"/requests/{rec.id}/status").data.decode("utf-8"),
        "sqlite_raw": (state_dir / "requests.sqlite3").read_bytes().decode("latin-1"),
    }
    secret_values = [
        v for v in (*stored_secrets(clean_host).values(), sonarr_token, radarr_token, password)
        if len(v) >= 8
    ]
    leaks = sorted(
        where for where, text in pages.items() if any(sec in text for sec in secret_values)
    )
    assert leaks == [], f"secret values leaked in: {leaks}"
