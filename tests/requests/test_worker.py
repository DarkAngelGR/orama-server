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
