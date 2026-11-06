"""Background reconciliation worker for media requests."""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

from orama.requests.client import MediaClient, RequestError
from orama.requests.reconcile import reconcile
from orama.requests.store import RequestStore, StoreError

_LOG = logging.getLogger(__name__)


class RequestWorker:
    """Manages periodic status reconciliation loops and webhook hints."""

    def __init__(
        self,
        store: RequestStore,
        client_factory: Callable[[], MediaClient],
        clock: Callable[[], float] = time.time,
        worker_id: str = "worker-1",
    ) -> None:
        self.store = store
        self.client_factory = client_factory
        self.clock = clock
        self.worker_id = worker_id
        self._running = False
        self._thread: threading.Thread | None = None
        self._wake_event = threading.Event()

    def start(self) -> None:
        """Start the background reconciliation loop."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True, name="orama-request-worker")
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Stop the background worker."""
        self._running = False
        self._wake_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        self._thread = None

    def wake(self) -> None:
        """Wake up the worker to process newly queued work immediately."""
        self._wake_event.set()

    def _run(self) -> None:
        while self._running:
            try:
                self.tick(self.clock())
            except (RequestError, StoreError, OSError) as e:
                _LOG.debug("request worker tick error: %s", e)
            self._wake_event.wait(timeout=2.0)
            self._wake_event.clear()

    def tick(self, now: float) -> int:
        """Process one due media request; returns number of processed records."""
        # Process any pending webhook notifications
        refreshes = self.store.drain_refreshes()
        _ = refreshes  # hints ingested

        record = self.store.claim_due(owner=self.worker_id, now=now, lease_seconds=60.0)
        if record is None:
            return 0

        try:
            client = self.client_factory()
            app_name = "radarr" if record.spec.kind == "movie" else "sonarr"
            snapshot = client.snapshot(app_name, remote_ids={record.spec.source_id})
            update = reconcile(record, snapshot, now)
            self.store.update(record.id, record.revision, update)
            next_check = update.next_check_at if update.next_check_at is not None else now + 30.0
            self.store.release(record.id, self.worker_id, next_check)
            return 1
        except Exception:
            # On error, release with short backoff
            self.store.release(record.id, self.worker_id, now + 15.0)
            raise
