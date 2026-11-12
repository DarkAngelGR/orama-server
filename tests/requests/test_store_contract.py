"""Store behaviour the plan promises: shared targets, clears, leases, processes, versions."""
import multiprocessing
import sqlite3

from orama.requests.models import RequestSpec, RequestUpdate
from orama.requests.store import SCHEMA_VERSION, RequestStore


def test_overlapping_requests_share_one_episode_target(tmp_path):
    store = RequestStore(tmp_path / "requests.db")
    season, _ = store.create(RequestSpec(kind="season", source_id=9, season=2), 1000.0)
    episode, _ = store.create(
        RequestSpec(kind="episode", source_id=9, season=2, episode=4), 1000.0
    )
    store.upsert_episode_target(9, 2, 4, title="Four", stage="downloading")
    store.upsert_episode_target(9, 2, 4, stage="ready", has_file=True)
    seen_by_season = store.list_episode_targets(9, 2)
    assert len(seen_by_season) == 1
    assert (seen_by_season[0].title, seen_by_season[0].stage, seen_by_season[0].has_file) == (
        "Four",
        "ready",
        True,
    )
    assert season.id != episode.id


def test_omitted_field_is_kept_and_explicit_none_clears(tmp_path):
    store = RequestStore(tmp_path / "requests.db")
    rec, _ = store.create(RequestSpec(kind="movie", source_id=1), 1000.0)
    assert store.update(
        rec.id, 1, RequestUpdate(waiting_reason="Waiting for Radarr", progress=30)
    )
    assert store.update(rec.id, 2, RequestUpdate(stage="downloading"))
    kept = store.get(rec.id)
    assert kept.waiting_reason == "Waiting for Radarr" and kept.progress == 30
    # A stale revision cannot clear anything.
    assert not store.update(rec.id, 2, RequestUpdate(waiting_reason=None))
    assert store.get(rec.id).waiting_reason == "Waiting for Radarr"
    assert store.update(rec.id, 3, RequestUpdate(waiting_reason=None))
    cleared = store.get(rec.id)
    assert cleared.waiting_reason is None and cleared.progress == 30


def test_release_by_another_owner_changes_nothing(tmp_path):
    store = RequestStore(tmp_path / "requests.db")
    rec, _ = store.create(RequestSpec(kind="movie", source_id=1), 1000.0)
    claimed = store.claim_due("a", 1000.0)
    assert claimed is not None
    store.release(rec.id, "b", 5000.0)
    held = store.get(rec.id)
    assert held.claim_owner == "a" and held.next_check_at == 1000.0
    assert store.claim_due("b", 1001.0) is None  # still leased to a
    assert store.claim_due("b", 1000.0 + 61.0) is not None  # lease expired
    store.release(rec.id, "b", 5000.0)
    assert store.get(rec.id).claim_owner is None


def _create_many(path, offset):
    store = RequestStore(path)
    created = 0
    for i in range(1, 21):
        _, was_created = store.create(RequestSpec(kind="movie", source_id=i), 1000.0 + offset)
        created += int(was_created)
    return created


def test_duplicate_create_across_processes_makes_one_record_each(tmp_path):
    path = tmp_path / "requests.db"
    RequestStore(path)
    ctx = multiprocessing.get_context("spawn")
    with ctx.Pool(4) as pool:
        counts = pool.starmap(_create_many, [(path, n) for n in range(4)])
    assert sum(counts) == 20
    assert len(RequestStore(path).list_recent(100)) == 20


def test_old_layout_is_upgraded_and_the_version_is_stored(tmp_path):
    path = tmp_path / "requests.db"
    RequestStore(path)
    conn = sqlite3.connect(path)
    conn.execute("ALTER TABLE requests DROP COLUMN refresh_hint_at")
    conn.execute("UPDATE schema_version SET version = 0")
    conn.commit()
    conn.close()
    store = RequestStore(path)
    rec, _ = store.create(RequestSpec(kind="movie", source_id=1), 1000.0)
    store.enqueue_refresh("radarr", 1, 1001.0)
    assert store.get(rec.id) is not None
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == SCHEMA_VERSION
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    conn.close()
