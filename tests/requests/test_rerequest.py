"""Asking again for a finished item starts the same record again."""
import pytest

from orama.requests.models import RequestSpec, RequestUpdate
from orama.requests.store import RequestStore


def _finish(store, rec, stage, **extra):
    assert store.update(
        rec.id,
        rec.revision,
        RequestUpdate(
            stage=stage,
            remote_id=77,
            progress=100,
            playback_note="Ready · direct play on Plex",
            subtitle_note="Ready · EN subs",
            next_check_at=9_999_999.0,
            **extra,
        ),
    )
    return store.get(rec.id)


@pytest.mark.parametrize("stage", ["ready", "unavailable"])
@pytest.mark.parametrize(
    "spec",
    [
        RequestSpec(kind="movie", source_id=5),
        RequestSpec(kind="season", source_id=6, season=3),
        RequestSpec(kind="episode", source_id=6, season=3, episode=2),
    ],
)
def test_asking_again_reactivates_a_finished_request(tmp_path, stage, spec):
    store = RequestStore(tmp_path / "requests.db")
    rec, created = store.create(spec, 1000.0)
    assert created
    done = _finish(store, rec, stage, waiting_reason="Media file is no longer in library")

    again, created_again = store.create(spec, 5000.0)
    assert not created_again
    assert again.id == rec.id
    assert again.stage == "requested"
    assert again.waiting_reason is None
    assert again.next_check_at == 5000.0
    assert again.remote_id == 77  # no duplicate add
    assert again.revision > done.revision
    assert store.get(rec.id).stage == "requested"
    assert rec.id in [r.id for r in store.list_active()]
    claimed = store.claim_due("w", 5000.0)
    assert claimed is not None and claimed.id == rec.id


def test_asking_again_leaves_active_requests_and_series_alone(tmp_path):
    store = RequestStore(tmp_path / "requests.db")
    movie, _ = store.create(RequestSpec(kind="movie", source_id=1), 1000.0)
    store.update(movie.id, movie.revision, RequestUpdate(stage="downloading", progress=40))
    before = store.get(movie.id)
    again, _ = store.create(RequestSpec(kind="movie", source_id=1), 2000.0)
    assert again.revision == before.revision and again.progress == 40

    series, _ = store.create(RequestSpec(kind="series", source_id=2), 1000.0)
    ready = _finish(store, series, "ready")
    again, _ = store.create(RequestSpec(kind="series", source_id=2), 2000.0)
    assert again.revision == ready.revision and again.stage == "ready"
