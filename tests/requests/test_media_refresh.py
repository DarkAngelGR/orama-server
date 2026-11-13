"""The worker refreshes the media server: debounced, after the lease, never fatal."""
from __future__ import annotations

import logging
import os

from orama.requests.client import AppSnapshot, MediaClient
from orama.requests.models import RequestSpec, RequestUpdate
from orama.requests.store import RequestStore
from orama.requests.worker import RequestWorker
from orama.secrets import JELLYFIN_KEY_NAME, SecretStore
from orama.setup_model import DesiredSetup
from orama.wiring.http import FakeHttp, Response


def _setup(tmp_path):
    return DesiredSetup(
        media_servers=frozenset({"jellyfin"}),
        download_methods=frozenset({"torrent"}),
        data_root=tmp_path / "data",
        config_root=tmp_path / "config",
        puid=1000,
        pgid=1000,
    )


class _Env:
    def __init__(self, tmp_path):
        self.store = RequestStore(tmp_path / "requests.db")
        self.secrets = SecretStore(tmp_path / "secrets.bin")
        self.key = "jf" + os.urandom(8).hex()
        self.secrets.set(JELLYFIN_KEY_NAME, self.key)
        self.http = FakeHttp()
        self.http.route("POST", "/Library/Refresh", lambda c: Response(204, {}, b""))
        self.setup = _setup(tmp_path)
        env = self

        class Client(MediaClient):
            def __init__(self):
                super().__init__(env.setup, env.secrets, env.http)

            def snapshot(self, app_name, remote_ids):
                return AppSnapshot(
                    complete=True,
                    items={7: {"id": 7, "tmdbId": 27205, "hasFile": True}},
                    queue=[],
                    history=[],
                    files=[{"id": 1, "movieId": 7, "size": 1000}],
                )

            def file_media_info(self, *a, **k):
                return None

            def subtitle_languages(self, *a, **k):
                return None

        self.now = 1000.0
        self.worker = RequestWorker(store=self.store, client_factory=Client, clock=lambda: self.now)

    def downloading_request(self, source_id=27205):
        rec, _ = self.store.create(RequestSpec(kind="movie", source_id=source_id), self.now)
        self.store.update(
            rec.id, rec.revision, RequestUpdate(stage="downloading", remote_id=7, next_check_at=self.now)
        )
        return rec

    def refresh_calls(self):
        return [c for c in self.http.calls if c.path == "/Library/Refresh"]


def test_refresh_happens_when_an_item_becomes_ready_without_any_webhook(tmp_path):
    env = _Env(tmp_path)
    rec = env.downloading_request()
    assert env.worker.tick(env.now) == 1
    assert env.store.get(rec.id).stage == "ready"
    assert len(env.refresh_calls()) == 1
    assert env.key not in env.refresh_calls()[0].url


def test_refresh_runs_after_the_lease_is_released(tmp_path):
    env = _Env(tmp_path)
    rec = env.downloading_request()
    seen = []

    def handler(call):
        seen.append(env.store.get(rec.id).claim_owner)
        return Response(204, {}, b"")

    env.http.route("POST", "/Library/Refresh", handler)
    env.worker.tick(env.now)
    assert seen == [None]


def test_many_events_in_a_window_make_at_most_one_call(tmp_path):
    env = _Env(tmp_path)
    for _ in range(20):
        env.store.mark_media_changed(env.now)
    for step in range(10):
        env.now += 2.0
        env.worker.tick(env.now)
        env.store.mark_media_changed(env.now)  # more events keep arriving
    assert len(env.refresh_calls()) == 1
    # Once the window has passed, the pending hint is refreshed once more.
    env.now += 61.0
    env.worker.tick(env.now)
    env.worker.tick(env.now + 1)
    assert len(env.refresh_calls()) == 2


def test_two_items_ready_in_one_window_refresh_once(tmp_path):
    env = _Env(tmp_path)
    env.downloading_request(1)
    env.downloading_request(2)
    env.worker.tick(env.now)
    env.worker.tick(env.now + 1)
    assert len(env.refresh_calls()) == 1


def test_no_change_means_no_call(tmp_path):
    env = _Env(tmp_path)
    env.worker.tick(env.now)
    assert env.refresh_calls() == []


def test_a_failing_jellyfin_never_aborts_a_tick_or_the_release(tmp_path, caplog):
    env = _Env(tmp_path)
    rec = env.downloading_request()

    def boom(call):
        raise RuntimeError("secret-" + env.key)

    env.http.route("POST", "/Library/Refresh", boom)
    with caplog.at_level(logging.DEBUG):
        assert env.worker.tick(env.now) == 1
    got = env.store.get(rec.id)
    assert got.stage == "ready" and got.claim_owner is None
    assert env.key not in caplog.text
    assert [r for r in caplog.records if r.levelno >= logging.INFO and "refresh" in r.getMessage()]


def test_an_unreachable_jellyfin_never_aborts_a_tick(tmp_path):
    env = _Env(tmp_path)
    env.downloading_request()
    env.http.unreachable = True
    assert env.worker.tick(env.now) == 1


def _restarted_worker(env):
    """A new worker on the same database, as after Orama restarts."""
    return RequestWorker(store=env.store, client_factory=env.worker.client_factory,
                         clock=lambda: env.now)


def test_a_change_held_back_by_the_window_survives_a_restart(tmp_path):
    env = _Env(tmp_path)
    env.store.mark_media_changed(env.now)
    env.worker.tick(env.now)
    assert len(env.refresh_calls()) == 1
    env.now += 10.0
    env.store.mark_media_changed(env.now)  # inside the 60 s window: held back
    env.worker.tick(env.now)
    assert len(env.refresh_calls()) == 1
    env.worker = _restarted_worker(env)  # Orama restarts before the window ends
    env.now += 61.0
    env.worker.tick(env.now)
    assert len(env.refresh_calls()) == 2


def test_an_item_ready_inside_the_window_survives_a_restart(tmp_path):
    env = _Env(tmp_path)
    env.store.mark_media_changed(env.now)
    env.worker.tick(env.now)
    assert len(env.refresh_calls()) == 1
    env.now += 5.0
    env.downloading_request()
    env.worker.tick(env.now)  # becomes Ready inside the window: refresh held back
    assert len(env.refresh_calls()) == 1
    env.worker = _restarted_worker(env)
    env.now += 61.0
    env.worker.tick(env.now)
    assert len(env.refresh_calls()) == 2


def test_the_client_refreshes_with_the_setup_it_was_given(tmp_path):
    env = _Env(tmp_path)
    assert MediaClient(env.setup, env.secrets, env.http).refresh_media_server() is True
    assert len(env.refresh_calls()) == 1
