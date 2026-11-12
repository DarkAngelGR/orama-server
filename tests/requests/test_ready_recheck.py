"""Finished (Ready) requests are looked at again; eligibility never reads the notes."""
from orama.requests.client import AppSnapshot, MediaClient
from orama.requests.models import RequestSpec, RequestUpdate
from orama.requests.store import RequestStore
from orama.requests.worker import RequestWorker

GOOD_MEDIA = {
    "videoCodec": "h264",
    "audioCodec": "aac",
    "audioChannels": 2,
    "container": "mkv",
}


def _ready_movie(store, now, **extra):
    rec, _ = store.create(RequestSpec(kind="movie", source_id=111), now)
    store.update(
        rec.id,
        rec.revision,
        RequestUpdate(
            stage="ready",
            remote_id=99,
            playback_note="Ready · direct play on Plex",
            subtitle_note="Ready · EN subs, still looking for EL",
            next_check_at=now + 300.0,
            **extra,
        ),
    )
    return rec


def test_ready_row_with_filled_notes_is_claimed_when_due(tmp_path):
    store = RequestStore(tmp_path / "requests.db")
    rec = _ready_movie(store, 1000.0)
    assert store.claim_due("w", 1299.0) is None
    claimed = store.claim_due("w", 1301.0)
    assert claimed is not None and claimed.id == rec.id


def test_still_looking_note_clears_when_the_subtitle_arrives(tmp_path):
    store = RequestStore(tmp_path / "requests.db")
    rec = _ready_movie(store, 1000.0)
    subs = [{"code2": "en"}]

    class Client(MediaClient):
        store = object()

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

        def file_media_info(self, *a, **k):
            return dict(GOOD_MEDIA)

        def subtitle_languages(self, *a, **k):
            return list(subs)

    worker = RequestWorker(store=store, client_factory=Client, clock=lambda: 0.0)
    assert worker.tick(1301.0) == 1
    still = store.get(rec.id)
    assert "still looking for el" in still.subtitle_note.lower()
    assert still.next_check_at <= 1301.0 + 300.0 + 1  # keeps checking every 5 minutes

    subs.append({"code2": "el"})
    assert worker.tick(still.next_check_at) == 1
    done = store.get(rec.id)
    assert "still looking" not in done.subtitle_note.lower()
    assert done.stage == "ready"
    # Everything is final now: a long interval, not 5 minutes.
    assert done.next_check_at - still.next_check_at >= 3600.0


def test_deleted_file_is_noticed_after_the_app_confirms_it(tmp_path):
    store = RequestStore(tmp_path / "requests.db")
    rec = _ready_movie(store, 1000.0)

    class Client(MediaClient):
        def __init__(self):
            pass

        def snapshot(self, app_name, remote_ids):
            return AppSnapshot(
                complete=True,
                items={99: {"id": 99, "tmdbId": 111, "hasFile": False}},
                queue=[],
                history=[],
                files=[],
            )

    worker = RequestWorker(store=store, client_factory=Client, clock=lambda: 0.0)
    assert worker.tick(1301.0) == 1
    assert store.get(rec.id).stage == "unavailable"
