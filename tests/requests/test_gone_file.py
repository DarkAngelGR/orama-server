"""A finished item whose file is gone waits for the owner; notes survive failed re-checks."""
from types import SimpleNamespace

import pytest

from orama.requests.client import AppSnapshot, MediaClient
from orama.requests.models import RequestSpec, RequestUpdate
from orama.requests.store import RequestStore
from orama.requests.worker import RequestWorker

GOOD_MEDIA = {"videoCodec": "h264", "audioCodec": "aac", "audioChannels": 2, "container": "mkv"}
# Wait policy "never": the fallback deadline is already over.
SETUP = SimpleNamespace(
    request_wait_mode="never",
    request_wait_hours=24,
    device="safest",
    subtitle_languages=("el", "en"),
    download_methods=("usenet",),
    vpn=None,
)
GOOD_PB = "Ready · direct play on Plex"
GOOD_SB = "Ready · EL and EN subs"


def _ready(store, kind="movie", **spec_extra):
    spec = RequestSpec(kind=kind, source_id=111, **spec_extra)
    rec, _ = store.create(spec, 1000.0, setup=SETUP)
    store.update(
        rec.id,
        rec.revision,
        RequestUpdate(
            stage="ready",
            remote_id=99,
            progress=100,
            playback_note=GOOD_PB,
            subtitle_note=GOOD_SB,
            next_check_at=1000.0,
        ),
    )
    return rec


def _client(items, complete=True, media=GOOD_MEDIA, subs=None, files=()):
    class Client(MediaClient):
        store = object()
        setup = SETUP

        def __init__(self):
            pass

        def snapshot(self, app_name, remote_ids):
            return AppSnapshot(
                complete=complete, items=items, queue=[], history=[], files=list(files)
            )

        def file_media_info(self, *a, **k):
            return media

        def subtitle_languages(self, *a, **k):
            return subs

    return Client


def test_file_gone_stays_unavailable_without_fallback(tmp_path, monkeypatch):
    store = RequestStore(tmp_path / "r.db")
    rec = _ready(store)
    calls = []
    monkeypatch.setattr(
        "orama.requests.worker.apply_fallback", lambda *a, **k: calls.append(1)
    )
    items = {99: {"id": 99, "tmdbId": 111, "hasFile": False}}
    worker = RequestWorker(store=store, client_factory=_client(items), clock=lambda: 0.0)
    assert worker.tick(2000.0) == 1
    first = store.get(rec.id)
    assert first.stage == "unavailable"
    assert first.next_check_at - 2000.0 >= 3600.0
    # Even when it is polled again later, it stays and never falls back.
    assert worker.tick(first.next_check_at) == 1
    again = store.get(rec.id)
    assert again.stage == "unavailable"
    assert not again.fallback_applied
    assert again.next_check_at - first.next_check_at >= 3600.0
    assert calls == []


def test_item_removed_from_the_app_becomes_unavailable(tmp_path):
    store = RequestStore(tmp_path / "r.db")
    rec = _ready(store)
    worker = RequestWorker(store=store, client_factory=_client({}), clock=lambda: 0.0)
    assert worker.tick(2000.0) == 1
    got = store.get(rec.id)
    assert got.stage == "unavailable"
    assert got.playback_note == GOOD_PB


def test_removed_episode_is_not_marked_as_having_a_file(tmp_path):
    store = RequestStore(tmp_path / "r.db")
    _ready(store, kind="episode", season=1, episode=2)
    worker = RequestWorker(store=store, client_factory=_client({}), clock=lambda: 0.0)
    assert worker.tick(2000.0) == 1
    targets = store.list_episode_targets(111, 1)
    assert not any(t.has_file for t in targets)


@pytest.mark.parametrize("complete", [True, False])
def test_failed_checks_keep_the_stored_notes(tmp_path, complete):
    store = RequestStore(tmp_path / "r.db")
    rec = _ready(store)
    items = {99: {"id": 99, "tmdbId": 111, "hasFile": True}}
    client = _client(items, complete=complete, media=None, subs=None)
    worker = RequestWorker(store=store, client_factory=client, clock=lambda: 0.0)
    assert worker.tick(2000.0) == 1
    got = store.get(rec.id)
    assert got.stage == "ready"
    assert got.playback_note == GOOD_PB
    assert got.subtitle_note == GOOD_SB
    assert got.next_check_at <= 2000.0 + 300.0 + 1


def test_incomplete_snapshot_without_the_item_changes_nothing(tmp_path):
    store = RequestStore(tmp_path / "r.db")
    rec = _ready(store)
    worker = RequestWorker(
        store=store, client_factory=_client({}, complete=False), clock=lambda: 0.0
    )
    assert worker.tick(2000.0) == 1
    got = store.get(rec.id)
    assert got.stage == "ready"
    assert got.playback_note == GOOD_PB and got.subtitle_note == GOOD_SB
