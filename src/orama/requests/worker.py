"""Background reconciliation worker and startup drift check for media requests."""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from orama import state
from orama.agent_client import AgentError
from orama.requests.client import MediaClient, RequestError, _sanitize_title
from orama.requests.fallback import apply_fallback, fallback_due
from orama.requests.models import RequestUpdate
from orama.requests.playback import assess_playback
from orama.requests.reconcile import reconcile
from orama.requests.store import RequestStore, StoreError
from orama.requests.submit import submit
from orama.requests.subtitles import assess_episode_subtitles, assess_subtitles
from orama.setup_model import DesiredSetup, enabled_apps

_LOG = logging.getLogger(__name__)
SETUP_FILE = "setup.json"


def schedule_startup_check(
    state_dir: Path,
    agent: Any,
    jobs: Any,
    apply_gate: threading.Lock | None = None,
) -> bool:
    """Run a startup drift check and start an apply job if apps drifted.

    Coalesces with any running apply and avoids repeated apply loops after a permanent failure.
    """
    state_dir = Path(state_dir)
    setup_path = state_dir / SETUP_FILE
    if not setup_path.exists():
        return False

    current = jobs.current()
    if current is not None and current.status in ("running", "failed"):
        return False

    try:
        setup = DesiredSetup.load(setup_path)
        states = agent.status()
    except (AgentError, OSError, ValueError):
        return False

    if not isinstance(states, dict):
        return False

    expected = enabled_apps(setup)
    drifted = (
        current is None
        or current.status == "interrupted"
        or any(states.get(app) != "running" for app in expected)
    )
    if not drifted:
        return False

    gate = apply_gate or threading.Lock()
    if not gate.acquire(blocking=False):
        return False
    try:
        current_again = jobs.current()
        if current_again is not None and current_again.status in ("running", "failed"):
            return False
        try:
            with state.locked(state.apply_lock(state_dir), blocking=False):
                pass
        except state.Busy:
            return False
        jobs.start(setup_path)
        return True
    finally:
        gate.release()


def _unwrap_setup(raw_setup: Any) -> Any:
    if raw_setup is not None and hasattr(raw_setup, "current") and callable(raw_setup.current):
        return raw_setup.current()
    return raw_setup


class RequestWorker:
    """Manages periodic status reconciliation loops and webhook hints."""

    def __init__(
        self,
        store: RequestStore,
        client_factory: Callable[[], MediaClient],
        clock: Callable[[], float] = time.time,
        worker_id: str = "worker-1",
        agent: Any = None,
    ) -> None:
        self.store = store
        self.client_factory = client_factory
        self.clock = clock
        self.worker_id = worker_id
        self.agent = agent
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
        refreshes = self.store.drain_refreshes()
        _ = refreshes

        record = self.store.claim_due(owner=self.worker_id, now=now, lease_seconds=60.0)
        if record is None:
            return 0

        try:
            client = self.client_factory()
            app_name = "radarr" if record.spec.kind == "movie" else "sonarr"
            setup_obj = _unwrap_setup(getattr(client, "setup", None))

            if self.agent is not None and hasattr(self.agent, "status"):
                try:
                    states = self.agent.status()
                except (AgentError, OSError):
                    states = None

                if isinstance(states, dict) and states:
                    if states.get(app_name) != "running":
                        self.store.update(
                            record.id,
                            record.revision,
                            RequestUpdate(
                                waiting_reason=f"Waiting for {app_name.capitalize()}",
                                next_check_at=now + 15.0,
                            ),
                        )
                        self.store.release(record.id, self.worker_id, now + 15.0)
                        return 1

                    if setup_obj is not None:
                        methods = getattr(setup_obj, "download_methods", ()) or ()
                        vpn = getattr(setup_obj, "vpn", None)
                        if (
                            vpn is not None
                            and "torrent" in methods
                            and "usenet" not in methods
                            and states.get("gluetun") != "running"
                        ):
                            self.store.update(
                                record.id,
                                record.revision,
                                RequestUpdate(
                                    waiting_reason="Waiting for VPN",
                                    next_check_at=now + 15.0,
                                ),
                            )
                            self.store.release(record.id, self.worker_id, now + 15.0)
                            return 1

            needs_submit = (
                record.stage == "requested"
                or (record.remote_id is None and record.stage in ("searching", "unavailable"))
                or record.waiting_reason == "Waiting for episode metadata"
            )
            if needs_submit and hasattr(client, "ensure_movie"):
                submit(record, client, self.store, now)
                updated_after_submit = self.store.get(record.id)
                next_after = (
                    updated_after_submit.next_check_at
                    if updated_after_submit and updated_after_submit.next_check_at is not None
                    else now + 5.0
                )
                self.store.release(record.id, self.worker_id, next_after)
                return 1

            remote_ids = {record.spec.source_id}
            if record.remote_id is not None:
                remote_ids.add(record.remote_id)
            snapshot = client.snapshot(app_name, remote_ids=remote_ids)

            resolved_remote_id = record.remote_id
            if resolved_remote_id is None:
                for it in snapshot.items.values():
                    s_id = it.get("tmdbId" if record.spec.kind == "movie" else "tvdbId")
                    if s_id == record.spec.source_id and isinstance(it.get("id"), int):
                        resolved_remote_id = it["id"]
                        break

            if record.spec.kind != "movie" and snapshot.files:
                for ep in snapshot.files:
                    if not isinstance(ep, dict):
                        continue
                    if (
                        resolved_remote_id is not None
                        and ep.get("seriesId") not in (None, resolved_remote_id)
                    ):
                        continue
                    s_num = int(ep.get("seasonNumber", 0))
                    e_num = int(ep.get("episodeNumber", 1))
                    if record.spec.kind == "episode" and (
                        s_num != record.spec.season or e_num != record.spec.episode
                    ):
                        continue
                    if record.spec.kind == "season" and s_num != record.spec.season:
                        continue
                    if (
                        record.spec.kind == "series"
                        and s_num <= 0
                        and not ep.get("monitored")
                    ):
                        continue
                    ep_has_file = bool(ep.get("hasFile"))
                    ep_stage = (
                        "ready"
                        if ep_has_file
                        else ("downloading" if ep.get("monitored") else "searching")
                    )
                    self.store.upsert_episode_target(
                        record.spec.source_id,
                        s_num,
                        e_num,
                        title=_sanitize_title(str(ep.get("title", ""))),
                        stage=ep_stage,
                        has_file=ep_has_file,
                    )

            update = reconcile(record, snapshot, now, remote_id=resolved_remote_id)

            if (
                update.stage != "ready"
                and setup_obj is not None
                and hasattr(client, "store")
                and fallback_due(record, setup_obj, now)
            ):
                fb_update = apply_fallback(record, client, self.store, setup_obj, now)
                next_fb = (
                    fb_update.next_check_at if fb_update.next_check_at is not None else now + 5.0
                )
                self.store.release(record.id, self.worker_id, next_fb)
                return 1

            if update.stage == "ready" and hasattr(client, "store") and resolved_remote_id is not None:
                media_info = client.file_media_info(
                    app_name,
                    resolved_remote_id,
                    season=record.spec.season,
                    episode=record.spec.episode,
                )
                device = getattr(setup_obj, "device", "safest") or "safest"
                pb = assess_playback(media_info, device=device)
                if pb.needs_conversion:
                    for code in pb.reason_codes:
                        self.store.record_followup(record.id, "conversion", code, now)

                wanted_langs = (
                    getattr(setup_obj, "subtitle_languages", ("el", "en")) or ("el", "en")
                )
                if record.spec.kind in ("season", "series") and hasattr(
                    client, "episode_subtitle_rows"
                ):
                    ep_rows = client.episode_subtitle_rows(
                        resolved_remote_id,
                        season=record.spec.season if record.spec.kind == "season" else None,
                    )
                    sb = (
                        assess_episode_subtitles(ep_rows, wanted_langs)
                        if ep_rows
                        else assess_subtitles(None, wanted_langs)
                    )
                else:
                    avail_subs = client.subtitle_languages(
                        record.spec.kind,
                        resolved_remote_id,
                        season=record.spec.season,
                        episode=record.spec.episode,
                    )
                    sb = assess_subtitles(avail_subs, wanted_langs)

                if sb.needs_translation:
                    for code in sb.reason_codes:
                        self.store.record_followup(record.id, "translation", code, now)

                if (
                    record.spec.kind == "episode"
                    and record.spec.season is not None
                    and record.spec.episode is not None
                ):
                    self.store.upsert_episode_target(
                        record.spec.source_id,
                        record.spec.season,
                        record.spec.episode,
                        stage="ready",
                        has_file=True,
                    )

                needs_recheck = (
                    record.spec.kind == "series"
                    or pb.status == "unknown"
                    or sb.status in ("unknown", "partial", "missing")
                )
                next_ready_check = (
                    now + 300.0
                    if needs_recheck
                    else (update.next_check_at if update.next_check_at is not None else now + 3600.0)
                )

                update = RequestUpdate(
                    stage="ready",
                    progress=100,
                    remote_id=resolved_remote_id,
                    waiting_reason=None,
                    playback_note=pb.note,
                    subtitle_note=sb.note,
                    next_check_at=next_ready_check,
                )

            self.store.update(record.id, record.revision, update)
            next_check = update.next_check_at if update.next_check_at is not None else now + 30.0
            self.store.release(record.id, self.worker_id, next_check)
            return 1
        except Exception:
            self.store.release(record.id, self.worker_id, now + 15.0)
            raise

