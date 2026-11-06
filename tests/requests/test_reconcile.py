"""Tests for status reconciliation logic."""
from orama.requests.client import AppSnapshot
from orama.requests.models import RequestRecord, RequestSpec
from orama.requests.reconcile import reconcile


def test_reconcile_file_present_marks_ready():
    now = 1000.0
    rec = RequestRecord(
        id="req-1",
        spec=RequestSpec(kind="movie", source_id=10),
        created_at=now - 50,
        updated_at=now - 50,
        revision=1,
        stage="downloading",
    )
    # Remote movie has file
    snapshot = AppSnapshot(
        complete=True,
        items={42: {"id": 42, "tmdbId": 10, "hasFile": True}},
        queue=[],
        history=[],
        files=[],
    )
    update = reconcile(rec, snapshot, now, remote_id=42)
    assert update.stage == "ready"
    assert update.progress == 100
    assert update.next_check_at == now + 3600.0


def test_reconcile_queue_downloading_computes_progress():
    now = 1000.0
    rec = RequestRecord(
        id="req-2",
        spec=RequestSpec(kind="movie", source_id=20),
        created_at=now - 50,
        updated_at=now - 50,
        revision=1,
        stage="downloading",
    )
    snapshot = AppSnapshot(
        complete=True,
        items={43: {"id": 43, "tmdbId": 20, "hasFile": False}},
        queue=[{"id": 1, "movieId": 43, "status": "downloading", "size": 1000, "sizeleft": 250}],
        history=[],
        files=[],
    )
    update = reconcile(rec, snapshot, now, remote_id=43)
    assert update.stage == "downloading"
    assert update.progress == 75  # (1000 - 250) / 1000 * 100
    assert update.next_check_at == now + 5.0


def test_reconcile_queue_importing():
    now = 1000.0
    rec = RequestRecord(
        id="req-3",
        spec=RequestSpec(kind="movie", source_id=30),
        created_at=now - 50,
        updated_at=now - 50,
        revision=1,
        stage="downloading",
    )
    snapshot = AppSnapshot(
        complete=True,
        items={44: {"id": 44, "tmdbId": 30, "hasFile": False}},
        queue=[{"id": 2, "movieId": 44, "status": "importPending"}],
        history=[],
        files=[],
    )
    update = reconcile(rec, snapshot, now, remote_id=44)
    assert update.stage == "importing"
    assert update.next_check_at == now + 5.0


def test_reconcile_item_not_in_queue_or_library_backs_off():
    now = 1000.0
    rec = RequestRecord(
        id="req-4",
        spec=RequestSpec(kind="movie", source_id=40),
        created_at=now - 50,
        updated_at=now - 50,
        revision=1,
        stage="downloading",
    )
    snapshot = AppSnapshot(
        complete=True,
        items={},
        queue=[],
        history=[],
        files=[],
    )
    update = reconcile(rec, snapshot, now, remote_id=None)
    assert update.stage == "downloading"
    assert update.next_check_at == now + 30.0
