"""Hints survive a lease, values are validated, updated_at follows every update."""
import math

import pytest

from orama.requests.models import RequestSpec, RequestUpdate, ValidationError
from orama.requests.store import RequestStore


def test_refresh_hint_during_a_lease_is_not_lost(tmp_path):
    store = RequestStore(tmp_path / "requests.db")
    rec, _ = store.create(RequestSpec(kind="movie", source_id=7), 1000.0)
    store.update(rec.id, rec.revision, RequestUpdate(stage="downloading", remote_id=7))
    claimed = store.claim_due("w", 2000.0)
    assert claimed is not None
    store.enqueue_refresh("radarr", 7, 2001.0)  # a Grab webhook, mid-tick
    store.release(rec.id, "w", 2030.0)
    after = store.get(rec.id)
    assert after.next_check_at <= 2001.0
    claimed_again = store.claim_due("w", 2002.0)
    assert claimed_again is not None
    # The hint was served: a normal release now uses the worker's own time.
    store.release(rec.id, "w", 2100.0)
    assert store.get(rec.id).next_check_at == 2100.0


def test_recheck_request_during_a_lease_is_not_lost_when_the_update_wins(tmp_path):
    # The Retry button's own update succeeds (the worker's is then refused as stale); the
    # hint it records must still beat the worker's later release time.
    store = RequestStore(tmp_path / "requests.db")
    rec, _ = store.create(RequestSpec(kind="movie", source_id=7), 1000.0)
    claimed = store.claim_due("w", 2000.0)
    assert claimed is not None
    assert store.update(rec.id, rec.revision, RequestUpdate(next_check_at=2001.0), now=2001.0)
    store.request_recheck(rec.id, 2001.0)
    assert not store.update(claimed.id, claimed.revision, RequestUpdate(stage="searching"))
    store.release(rec.id, "w", 2030.0)
    assert store.get(rec.id).next_check_at <= 2001.0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"kind": "movie", "source_id": 2**63},
        {"kind": "movie", "source_id": 100_000_001},
        {"kind": "season", "source_id": 1, "season": 10**30},
        {"kind": "episode", "source_id": 1, "season": 1, "episode": 10**30},
    ],
)
def test_oversized_coordinates_are_validation_errors(kwargs):
    with pytest.raises(ValidationError):
        RequestSpec(**kwargs)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"stage": "<b>bogus</b>"},
        {"stage": None},
        {"progress": 1000},
        {"progress": -1},
        {"progress": True},
        {"progress": "5"},
        {"next_check_at": None},
        {"next_check_at": math.nan},
        {"next_check_at": math.inf},
        {"next_check_at": "soon"},
        {"remote_id": "abc"},
        {"remote_id": 0},
        {"remote_id": True},
        {"remote_id": 2**63},
        {"fallback_applied": "yes"},
        {"fallback_applied": None},
        {"waiting_reason": 5},
        {"waiting_reason": "x" * 5000},
        {"wait_mode": "sometimes"},
        {"fallback_deadline": "tomorrow"},
    ],
)
def test_invalid_updates_are_validation_errors(kwargs):
    with pytest.raises(ValidationError):
        RequestUpdate(**kwargs)


def test_valid_updates_and_explicit_clears_are_accepted():
    update = RequestUpdate(
        stage="ready", progress=None, waiting_reason=None, remote_id=5,
        next_check_at=12, fallback_applied=True, wait_mode="timed", fallback_deadline=None,
    )
    assert "waiting_reason" in update.provided_fields
    assert "playback_note" not in update.provided_fields


def test_updated_at_changes_on_every_update(tmp_path):
    store = RequestStore(tmp_path / "requests.db")
    rec, _ = store.create(RequestSpec(kind="movie", source_id=1), 1000.0)
    assert store.update(rec.id, 1, RequestUpdate(stage="downloading"), now=5000.0)
    assert store.get(rec.id).updated_at == 5000.0
    assert not store.update(rec.id, 1, RequestUpdate(stage="ready"), now=9000.0)
    assert store.get(rec.id).updated_at == 5000.0  # a refused update changes nothing
    assert store.update(rec.id, 2, RequestUpdate(stage="ready"), now=9000.0)
    got = store.get(rec.id)
    assert got.updated_at == 9000.0 and got.created_at == 1000.0 and got.revision == 3


def test_asking_again_for_a_finished_item_during_a_lease_is_not_lost(tmp_path):
    store = RequestStore(tmp_path / "requests.db")
    rec, _ = store.create(RequestSpec(kind="movie", source_id=7), 1000.0)
    store.update(rec.id, rec.revision, RequestUpdate(stage="ready", remote_id=7))
    assert store.claim_due("w", 2000.0) is not None
    store.create(RequestSpec(kind="movie", source_id=7), 2001.0)  # asked again, mid-check
    store.release(rec.id, "w", 2000.0 + 6 * 3600.0)
    after = store.get(rec.id)
    assert after.stage == "requested"
    assert after.next_check_at <= 2001.0


def test_stored_rows_above_the_new_limits_do_not_break_reads(tmp_path):
    import sqlite3

    path = tmp_path / "requests.db"
    store = RequestStore(path)
    old, _ = store.create(RequestSpec(kind="movie", source_id=7), 1000.0)
    other, _ = store.create(RequestSpec(kind="movie", source_id=8), 1000.0)
    conn = sqlite3.connect(path)
    conn.execute("UPDATE requests SET source_id = 999999999 WHERE id = ?", (old.id,))
    conn.commit()
    conn.close()
    assert {r.id for r in store.list_recent()} == {old.id, other.id}
    assert {r.id for r in store.list_active()} == {old.id, other.id}
    claimed = {store.claim_due("w", 2000.0).id, store.claim_due("w", 2000.0).id}
    assert claimed == {old.id, other.id}
