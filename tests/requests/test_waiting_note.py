"""The 'Waiting for <app>' note goes away as soon as the app answers again."""
from orama.requests.client import AppSnapshot, MediaClient
from orama.requests.models import RequestSpec, RequestUpdate
from orama.requests.store import RequestStore
from orama.requests.worker import RequestWorker


class Agent:
    def __init__(self):
        self.states = {"radarr": "exited"}

    def status(self):
        return dict(self.states)


class Client(MediaClient):
    def __init__(self):
        pass

    def snapshot(self, app_name, remote_ids):
        return AppSnapshot(
            complete=True,
            items={99: {"id": 99, "tmdbId": 111, "hasFile": False}},
            queue=[
                {"movieId": 99, "status": "downloading", "size": 100, "sizeleft": 55}
            ],
            history=[],
            files=[],
        )


def test_waiting_for_app_clears_when_the_app_is_back(tmp_path):
    store = RequestStore(tmp_path / "requests.db")
    rec, _ = store.create(RequestSpec(kind="movie", source_id=111), 1000.0)
    store.update(
        rec.id, rec.revision, RequestUpdate(stage="downloading", remote_id=99, next_check_at=1000.0)
    )
    agent = Agent()
    worker = RequestWorker(
        store=store, client_factory=Client, clock=lambda: 0.0, agent=agent
    )

    assert worker.tick(1000.0) == 1
    assert store.get(rec.id).waiting_reason == "Waiting for Radarr"

    agent.states = {"radarr": "running"}
    assert worker.tick(1020.0) == 1
    after = store.get(rec.id)
    assert after.stage == "downloading"
    assert after.progress == 45
    assert after.waiting_reason is None
