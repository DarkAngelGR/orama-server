"""Tests for RequestWorker background reconciliation worker."""
from orama.requests.client import AppSnapshot, MediaClient
from orama.requests.models import RequestSpec, RequestUpdate
from orama.requests.store import RequestStore
from orama.requests.worker import RequestWorker


def test_worker_tick_processes_due_records(tmp_path):
    store = RequestStore(tmp_path / "requests.db")
    now = 1000.0
    rec, _ = store.create(RequestSpec(kind="movie", source_id=111), now)
    # Set stage to downloading so it's claimed and processed
    store.update(rec.id, rec.revision, RequestUpdate(stage="downloading", next_check_at=now))

    class MockClient(MediaClient):
        def __init__(self):
            pass

        def snapshot(self, app_name, remote_ids):
            return AppSnapshot(
                complete=True,
                items={99: {"id": 99, "tmdbId": 111, "hasFile": True}},
                queue=[],
                history=[],
                files=[],
            )

    worker = RequestWorker(
        store=store,
        client_factory=lambda: MockClient(),
        clock=lambda: now,
    )

    processed = worker.tick(now)
    assert processed >= 1

    updated = store.get(rec.id)
    assert updated.stage == "ready"


def test_worker_lifecycle():
    class DummyWorker(RequestWorker):
        def __init__(self):
            self.running = False

        def start(self):
            self.running = True

        def stop(self, timeout=5):
            self.running = False

    w = DummyWorker()
    w.start()
    assert w.running is True
    w.stop()
    assert w.running is False


def test_ongoing_series_continues_checking_after_ready_and_discovers_new_episodes(tmp_path):
    store = RequestStore(tmp_path / "requests.db")
    now = 1000.0
    rec, _ = store.create(RequestSpec(kind="series", source_id=888), now)
    store.update(
        rec.id,
        rec.revision,
        RequestUpdate(stage="downloading", remote_id=12, next_check_at=now),
    )

    episodes_state = [
        {
            "id": 1,
            "seriesId": 12,
            "seasonNumber": 1,
            "episodeNumber": 1,
            "title": "Pilot",
            "monitored": True,
            "hasFile": True,
        }
    ]

    class SeriesClient(MediaClient):
        def __init__(self):
            pass

        def snapshot(self, app_name, remote_ids):
            return AppSnapshot(
                complete=True,
                items={12: {"id": 12, "tvdbId": 888}},
                queue=[],
                history=[],
                files=list(episodes_state),
            )

    worker = RequestWorker(
        store=store,
        client_factory=SeriesClient,
        clock=lambda: now,
    )

    # First tick marks the series ready because all known episodes (S01E01) have files
    assert worker.tick(now) == 1
    ready_rec = store.get(rec.id)
    assert ready_rec is not None
    assert ready_rec.stage == "ready"
    assert ready_rec.id in [r.id for r in store.list_active()]

    # Episode 2 airs later without a file; ongoing series is still claimed at next_check_at!
    episodes_state.append(
        {
            "id": 2,
            "seriesId": 12,
            "seasonNumber": 1,
            "episodeNumber": 2,
            "title": "Episode Two",
            "monitored": True,
            "hasFile": False,
        }
    )
    later = ready_rec.next_check_at
    assert worker.tick(later) == 1
    after_new_ep = store.get(rec.id)
    assert after_new_ep is not None
    assert after_new_ep.stage == "downloading"

    targets = store.list_episode_targets(888)
    assert [(t.season, t.episode, t.title, t.has_file) for t in targets] == [
        (1, 1, "Pilot", True),
        (1, 2, "Episode Two", False),
    ]

