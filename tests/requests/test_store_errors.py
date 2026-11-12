"""sqlite failures surface as StoreError and never as raw sqlite3 errors."""
import sqlite3
import threading
import time

import pytest

from orama.requests.models import RequestSpec
from orama.requests.store import CorruptStoreError, RequestStore, StoreBusyError, StoreError
from orama.requests.worker import RequestWorker


def test_garbage_file_is_a_corrupt_store_and_left_untouched(tmp_path):
    path = tmp_path / "requests.db"
    path.write_bytes(b"this is not a database at all" * 50)
    before = path.read_bytes()
    with pytest.raises(CorruptStoreError):
        RequestStore(path)
    assert path.read_bytes() == before


def test_truncated_database_is_a_corrupt_store(tmp_path):
    path = tmp_path / "requests.db"
    store = RequestStore(path)
    for i in range(1, 60):
        store.create(RequestSpec(kind="movie", source_id=i), 1000.0, title="x" * 200)
    data = path.read_bytes()
    path.write_bytes(data[: len(data) // 2])
    with pytest.raises(StoreError) as info:
        RequestStore(path)
    assert not isinstance(info.value, sqlite3.Error)


def test_version_table_without_requests_table_is_corrupt(tmp_path):
    path = tmp_path / "requests.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY)")
    conn.execute("INSERT INTO schema_version VALUES (1)")
    conn.commit()
    conn.close()
    with pytest.raises(CorruptStoreError):
        RequestStore(path)


def test_foreign_requests_table_is_corrupt(tmp_path):
    path = tmp_path / "requests.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE requests (a INTEGER)")
    conn.commit()
    conn.close()
    with pytest.raises(CorruptStoreError):
        RequestStore(path)


def test_locked_database_is_a_retryable_store_error(tmp_path):
    path = tmp_path / "requests.db"
    store = RequestStore(path, busy_timeout_ms=50)
    blocker = sqlite3.connect(path, isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(StoreBusyError):
            store.create(RequestSpec(kind="movie", source_id=1), 1000.0)
        with pytest.raises(StoreBusyError):
            store.drain_refreshes()
        with pytest.raises(StoreError):
            store.claim_due("w", 1000.0)
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()
    rec, created = store.create(RequestSpec(kind="movie", source_id=1), 1000.0)
    assert created and store.get(rec.id) is not None


def test_worker_thread_survives_database_errors(tmp_path, caplog):
    store = RequestStore(tmp_path / "requests.db")
    calls = []
    done = threading.Event()

    def broken_drain():
        calls.append(1)
        if len(calls) >= 3:
            done.set()
        raise sqlite3.OperationalError("database or disk is full: secret-title")

    store.drain_refreshes = broken_drain  # type: ignore[method-assign]
    worker = RequestWorker(store=store, client_factory=lambda: None, clock=lambda: 1000.0)
    worker.poll_interval = 0.01
    with caplog.at_level("WARNING"):
        worker.start()
        assert done.wait(5.0)
        time.sleep(0.05)
        assert worker._thread is not None and worker._thread.is_alive()
        worker.stop()
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert warnings
    assert all("secret-title" not in r.getMessage() for r in caplog.records)
