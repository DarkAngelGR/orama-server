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


def test_reconcile_unrelated_episode_file_does_not_mark_episode_or_season_ready():
    now = 1000.0
    # S01E01 has a file, so series statistics.episodeFileCount == 1,
    # but user requested S01E02 (and Season 2).
    snapshot = AppSnapshot(
        complete=True,
        items={
            77: {
                "id": 77,
                "tvdbId": 81189,
                "statistics": {"episodeFileCount": 1, "episodeCount": 14},
                "seasons": [
                    {"seasonNumber": 1, "statistics": {"episodeFileCount": 1, "episodeCount": 7}},
                    {"seasonNumber": 2, "statistics": {"episodeFileCount": 0, "episodeCount": 7}},
                ],
            }
        },
        queue=[],
        history=[],
        files=[
            {
                "id": 1001,
                "seriesId": 77,
                "seasonNumber": 1,
                "episodeNumber": 1,
                "monitored": True,
                "hasFile": True,
            },
            {
                "id": 1002,
                "seriesId": 77,
                "seasonNumber": 1,
                "episodeNumber": 2,
                "monitored": True,
                "hasFile": False,
            },
        ],
    )

    ep2_rec = RequestRecord(
        id="req-ep2",
        spec=RequestSpec(kind="episode", source_id=81189, season=1, episode=2),
        created_at=now - 50,
        updated_at=now - 50,
        revision=1,
        stage="downloading",
        remote_id=77,
    )
    ep2_update = reconcile(ep2_rec, snapshot, now, remote_id=77)
    assert ep2_update.stage != "ready"

    s1_rec = RequestRecord(
        id="req-s1",
        spec=RequestSpec(kind="season", source_id=81189, season=1),
        created_at=now - 50,
        updated_at=now - 50,
        revision=1,
        stage="downloading",
        remote_id=77,
    )
    s1_update = reconcile(s1_rec, snapshot, now, remote_id=77)
    assert s1_update.stage != "ready"

    s2_rec = RequestRecord(
        id="req-s2",
        spec=RequestSpec(kind="season", source_id=81189, season=2),
        created_at=now - 50,
        updated_at=now - 50,
        revision=1,
        stage="downloading",
        remote_id=77,
    )
    s2_update = reconcile(s2_rec, snapshot, now, remote_id=77)
    assert s2_update.stage != "ready"

    # Meanwhile, a request for S01E01 IS marked ready
    ep1_rec = RequestRecord(
        id="req-ep1",
        spec=RequestSpec(kind="episode", source_id=81189, season=1, episode=1),
        created_at=now - 50,
        updated_at=now - 50,
        revision=1,
        stage="downloading",
        remote_id=77,
    )
    ep1_update = reconcile(ep1_rec, snapshot, now, remote_id=77)
    assert ep1_update.stage == "ready"

