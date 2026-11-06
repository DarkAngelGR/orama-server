"""Tests for the durable SQLite RequestStore and models."""
import os
import sqlite3
import stat
import threading

import pytest

from orama.requests.models import (
    RequestSpec,
    RequestUpdate,
    ValidationError,
)
from orama.requests.store import CorruptStoreError, RequestStore


def test_episode_coordinates_validate():
    # Valid movie
    spec = RequestSpec(kind="movie", source_id=123)
    assert spec.kind == "movie"
    assert spec.source_id == 123
    assert spec.season is None
    assert spec.episode is None

    # Movie rejects season or episode
    with pytest.raises(ValidationError):
        RequestSpec(kind="movie", source_id=123, season=1)
    with pytest.raises(ValidationError):
        RequestSpec(kind="movie", source_id=123, episode=1)

    # Valid series
    spec_series = RequestSpec(kind="series", source_id=456)
    assert spec_series.kind == "series"
    with pytest.raises(ValidationError):
        RequestSpec(kind="series", source_id=456, season=1)

    # Valid season (season 0 is allowed for specials)
    spec_season = RequestSpec(kind="season", source_id=456, season=0)
    assert spec_season.season == 0
    with pytest.raises(ValidationError):
        RequestSpec(kind="season", source_id=456, season=-1)
    with pytest.raises(ValidationError):
        RequestSpec(kind="season", source_id=456, season=1, episode=1)

    # Valid episode (episode must be >= 1)
    spec_ep = RequestSpec(kind="episode", source_id=456, season=1, episode=1)
    assert spec_ep.episode == 1
    with pytest.raises(ValidationError):
        RequestSpec(kind="episode", source_id=456, season=1, episode=0)
    with pytest.raises(ValidationError):
        RequestSpec(kind="episode", source_id=456, season=1, episode=-2)

    # Negative source_id refused
    with pytest.raises(ValidationError):
        RequestSpec(kind="movie", source_id=0)
    with pytest.raises(ValidationError):
        RequestSpec(kind="movie", source_id=-10)


def test_duplicate_target_has_one_record(tmp_path):
    db_path = tmp_path / "requests.db"
    store = RequestStore(db_path)
    now = 1000.0

    spec = RequestSpec(kind="movie", source_id=603)
    record1, created1 = store.create(spec, now)
    assert created1 is True
    assert record1.spec == spec
    assert record1.revision == 1

    # Second creation with identical spec returns existing record with created=False
    record2, created2 = store.create(spec, now + 10)
    assert created2 is False
    assert record2.id == record1.id
    assert record2.created_at == record1.created_at

    # Concurrent creation threads
    results = []

    def run_create():
        s = RequestStore(db_path)
        r, c = s.create(RequestSpec(kind="movie", source_id=999), 2000.0)
        results.append((r.id, c))

    threads = [threading.Thread(target=run_create) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # All threads got the exact same ID, exactly one reported created=True
    created_count = sum(1 for _, c in results if c)
    assert created_count == 1
    ids = {r_id for r_id, _ in results}
    assert len(ids) == 1


def test_overlapping_requests_share_episode_target(tmp_path):
    store = RequestStore(tmp_path / "requests.db")
    now = 1000.0

    # Whole series request
    series_rec, _ = store.create(RequestSpec(kind="series", source_id=100), now)
    # Specific episode request within the same series
    ep_rec, _ = store.create(RequestSpec(kind="episode", source_id=100, season=1, episode=1), now + 1)

    assert series_rec.id != ep_rec.id

    active = store.list_active()
    ids = [r.id for r in active]
    assert series_rec.id in ids
    assert ep_rec.id in ids


def test_restart_retains_intent_and_refresh(tmp_path):
    db_path = tmp_path / "requests.db"
    store1 = RequestStore(db_path)
    now = 1000.0

    rec, _ = store1.create(RequestSpec(kind="movie", source_id=77), now)
    store1.enqueue_refresh("radarr", 555, now)

    # Re-open with new store instance
    store2 = RequestStore(db_path)
    fetched = store2.get(rec.id)
    assert fetched is not None
    assert fetched.id == rec.id
    assert fetched.spec.source_id == 77

    # Refresh queue is retained
    refreshes = store2.drain_refreshes()
    assert ("radarr", 555) in refreshes


def test_expired_claim_can_be_recovered(tmp_path):
    store = RequestStore(tmp_path / "requests.db")
    now = 1000.0

    rec, _ = store.create(RequestSpec(kind="movie", source_id=88), now)

    # Claim due
    claimed = store.claim_due(owner="worker-1", now=now, lease_seconds=30.0)
    assert claimed is not None
    assert claimed.id == rec.id

    # Another worker cannot claim before lease expires
    assert store.claim_due(owner="worker-2", now=now + 10.0, lease_seconds=30.0) is None

    # After lease expires at now + 35, worker-2 can recover the claim
    recovered = store.claim_due(owner="worker-2", now=now + 35.0, lease_seconds=30.0)
    assert recovered is not None
    assert recovered.id == rec.id

    # Release updates next check time
    store.release(rec.id, owner="worker-2", next_check_at=now + 100.0)
    assert store.claim_due(owner="worker-1", now=now + 50.0) is None
    assert store.claim_due(owner="worker-1", now=now + 101.0) is not None


def test_stale_revision_cannot_overwrite_newer_state(tmp_path):
    store = RequestStore(tmp_path / "requests.db")
    now = 1000.0

    rec, _ = store.create(RequestSpec(kind="movie", source_id=99), now)
    assert rec.revision == 1

    # First update succeeds, revision increments to 2
    update1 = RequestUpdate(stage="downloading", progress=50)
    ok1 = store.update(rec.id, expected_revision=1, changes=update1)
    assert ok1 is True

    updated_rec = store.get(rec.id)
    assert updated_rec.revision == 2
    assert updated_rec.stage == "downloading"
    assert updated_rec.progress == 50

    # Stale update with revision 1 fails
    stale_update = RequestUpdate(stage="importing")
    ok2 = store.update(rec.id, expected_revision=1, changes=stale_update)
    assert ok2 is False

    # State was unchanged by stale update
    rec_after = store.get(rec.id)
    assert rec_after.revision == 2
    assert rec_after.stage == "downloading"


def test_private_database_and_corrupt_schema_refusal(tmp_path):
    db_path = tmp_path / "requests.db"
    store = RequestStore(db_path)
    store.create(RequestSpec(kind="movie", source_id=1), 1000.0)

    # Permissions on db file: owner read/write only (mode 0o600)
    mode = stat.S_IMODE(os.stat(db_path).st_mode)
    assert mode & 0o077 == 0  # no group or other access

    # Unknown future schema version raises CorruptStoreError
    corrupt_db = tmp_path / "future.db"
    conn = sqlite3.connect(corrupt_db)
    conn.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY)")
    conn.execute("INSERT INTO schema_version VALUES (999)")
    conn.commit()
    conn.close()

    with pytest.raises(CorruptStoreError):
        RequestStore(corrupt_db)
