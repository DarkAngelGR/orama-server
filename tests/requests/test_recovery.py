"""Tests for reboot/drift recovery, startup checks, and app/VPN availability backoff."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

from orama import state
from orama.requests.client import AppSnapshot, MediaClient
from orama.requests.models import RequestSpec, RequestUpdate
from orama.requests.store import RequestStore
from orama.requests.worker import RequestWorker, schedule_startup_check
from orama.setup_model import DesiredSetup, VpnConfig
from orama.web import SETUP_FILE
from orama.web.jobs import JobRunner


def _make_setup(**overrides) -> DesiredSetup:
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


class FakeAgent:
    def __init__(self, states: dict[str, str] | None = None, error: Exception | None = None) -> None:
        self.states = states if states is not None else {"plex": "running", "radarr": "running", "sonarr": "running", "bazarr": "running", "prowlarr": "running", "sabnzbd": "running"}
        self.error = error

    def status(self) -> dict[str, str]:
        if self.error:
            raise self.error
        return dict(self.states)


def test_process_restart_recovers_active_and_unsubmitted_requests(tmp_path: Path):
    db_path = tmp_path / "requests.sqlite3"
    store1 = RequestStore(db_path)
    now = 1_700_000_000.0

    # 1. Request created right before crash (still in 'requested' stage)
    unsubmitted, _ = store1.create(RequestSpec(kind="movie", source_id=101), now)

    # 2. Request claimed by a previous worker that crashed (lease expired)
    active, _ = store1.create(RequestSpec(kind="movie", source_id=202), now)
    store1.update(
        active.id,
        active.revision,
        RequestUpdate(stage="downloading", remote_id=55, next_check_at=now - 100.0),
    )
    claimed = store1.claim_due("dead-worker", now - 100.0, lease_seconds=60.0)
    assert claimed is not None and claimed.id == active.id

    submitted_movies: list[int] = []

    class RecoveryClient(MediaClient):
        def __init__(self) -> None:
            self.setup = _make_setup()

        def ensure_movie(self, tmdb_id: int) -> tuple[int, bool]:
            submitted_movies.append(tmdb_id)
            return 77, False

        def search_targets(self, app_name: str, remote_id: int, season=None, episode_ids=None) -> int:
            return 1

        def snapshot(self, app_name: str, remote_ids) -> AppSnapshot:
            return AppSnapshot(
                complete=True,
                items={
                    55: {"id": 55, "tmdbId": 202, "hasFile": True},
                    77: {"id": 77, "tmdbId": 101, "hasFile": False},
                },
                queue=[],
                history=[],
                files=[],
            )

    # New store & worker instance after restart
    store2 = RequestStore(db_path)
    worker = RequestWorker(
        store=store2,
        client_factory=RecoveryClient,
        clock=lambda: now,
        worker_id="new-worker",
        agent=FakeAgent(),
    )

    assert worker.tick(now) == 1
    assert worker.tick(now) == 1

    rec_active = store2.get(active.id)
    assert rec_active is not None and rec_active.stage == "ready"

    rec_unsub = store2.get(unsubmitted.id)
    assert rec_unsub is not None and rec_unsub.stage == "downloading"
    assert submitted_movies == [101]


def test_startup_check_coalesces_with_busy_apply_and_avoids_failed_loop(tmp_path: Path):
    setup_path = tmp_path / SETUP_FILE
    setup = _make_setup()
    setup_path.write_text(json.dumps(setup.model_dump(mode="json")), encoding="utf-8")

    started_runs: list[Path] = []

    def fake_argv(p: Path) -> list[str]:
        started_runs.append(p)
        return [sys.executable, "-c", "print('✓ All set')"]

    jobs = JobRunner(tmp_path, argv_for=fake_argv)

    # 1. When all enabled apps are running and last job succeeded, no apply needed
    (tmp_path / "last-apply.json").write_text(
        json.dumps({"status": "done", "started": 10.0, "finished": 12.0, "lines": []}),
        encoding="utf-8",
    )
    jobs = JobRunner(tmp_path, argv_for=fake_argv)
    agent_ok = FakeAgent()
    assert schedule_startup_check(tmp_path, agent_ok, jobs) is False
    assert started_runs == []

    # 2. When last job failed permanently, do NOT enter a repeated apply loop
    (tmp_path / "last-apply.json").write_text(
        json.dumps({"status": "failed", "started": 10.0, "finished": 12.0, "lines": ["✗ failed"]}),
        encoding="utf-8",
    )
    jobs_failed = JobRunner(tmp_path, argv_for=fake_argv)
    agent_drifted = FakeAgent({"plex": "exited"})
    assert schedule_startup_check(tmp_path, agent_drifted, jobs_failed) is False
    assert started_runs == []

    # 3. When apply.lock is busy, coalesce and return False
    (tmp_path / "last-apply.json").unlink(missing_ok=True)
    jobs_clean = JobRunner(tmp_path, argv_for=fake_argv)
    with state.locked(state.apply_lock(tmp_path), blocking=False):
        assert schedule_startup_check(tmp_path, agent_drifted, jobs_clean) is False
    assert started_runs == []

    # 4. When drifted and lock is free, startup check triggers apply
    assert schedule_startup_check(tmp_path, agent_drifted, jobs_clean) is True
    jobs_clean.wait(timeout=5.0)
    assert len(started_runs) == 1


def test_worker_waits_for_down_app_and_continues_usenet_when_torrent_vpn_down(tmp_path: Path):
    store = RequestStore(tmp_path / "requests.sqlite3")
    now = 1_700_000_000.0
    rec, _ = store.create(RequestSpec(kind="movie", source_id=303), now)
    store.update(rec.id, rec.revision, RequestUpdate(stage="downloading", remote_id=10, next_check_at=now))

    http_calls = 0

    class ClientWithSetup(MediaClient):
        def __init__(self, setup_obj) -> None:
            self.setup = setup_obj

        def snapshot(self, app_name: str, remote_ids) -> AppSnapshot:
            nonlocal http_calls
            http_calls += 1
            return AppSnapshot(
                complete=True,
                items={10: {"id": 10, "tmdbId": 303, "hasFile": True}},
                queue=[],
                history=[],
                files=[],
            )

    # 1. Radarr is restarting/exited -> sets "Waiting for Radarr" without calling Radarr HTTP
    agent_radarr_down = FakeAgent({"radarr": "restarting", "plex": "running"})
    worker_down = RequestWorker(
        store=store,
        client_factory=lambda: ClientWithSetup(_make_setup()),
        clock=lambda: now,
        agent=agent_radarr_down,
    )
    assert worker_down.tick(now) == 1
    after_down = store.get(rec.id)
    assert after_down is not None
    assert after_down.waiting_reason == "Waiting for Radarr"
    assert http_calls == 0

    # 2. Torrent-only with VPN and gluetun is stopped -> sets "Waiting for VPN"
    now += 60.0
    vpn_cfg = VpnConfig(provider="protonvpn", private_key_secret="vpn:private_key")
    torrent_only_setup = SimpleNamespace(
        download_methods=("torrent",),
        vpn=vpn_cfg,
    )
    agent_vpn_down = FakeAgent({"radarr": "running", "gluetun": "exited", "qbittorrent": "running"})
    worker_vpn = RequestWorker(
        store=store,
        client_factory=lambda: ClientWithSetup(torrent_only_setup),
        clock=lambda: now,
        agent=agent_vpn_down,
    )
    assert worker_vpn.tick(now) == 1
    after_vpn = store.get(rec.id)
    assert after_vpn is not None
    assert after_vpn.waiting_reason == "Waiting for VPN"
    assert http_calls == 0

    # 3. Usenet + Torrent with VPN and gluetun is stopped -> normal Usenet work continues!
    now += 60.0
    dual_setup = SimpleNamespace(
        download_methods=("usenet", "torrent"),
        vpn=vpn_cfg,
    )
    worker_dual = RequestWorker(
        store=store,
        client_factory=lambda: ClientWithSetup(dual_setup),
        clock=lambda: now,
        agent=agent_vpn_down,
    )
    assert worker_dual.tick(now) == 1
    after_dual = store.get(rec.id)
    assert after_dual is not None
    assert after_dual.stage == "ready"
    assert http_calls == 1
