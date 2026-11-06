"""Authoritative status reconciliation against app snapshots."""
from __future__ import annotations

from orama.requests.client import AppSnapshot
from orama.requests.models import RequestRecord, RequestUpdate


def reconcile(
    record: RequestRecord,
    snapshot: AppSnapshot,
    now: float,
    remote_id: int | None = None,
) -> RequestUpdate:
    """Derive next status stage and progress from snapshot data."""
    # Check library item state first
    item = None
    if remote_id is not None and remote_id in snapshot.items:
        item = snapshot.items[remote_id]
    else:
        # Match by external ID
        for it in snapshot.items.values():
            s_id = it.get("tmdbId" if record.spec.kind == "movie" else "tvdbId")
            if s_id == record.spec.source_id:
                item = it
                remote_id = it.get("id")
                break

    if item is not None:
        if record.spec.kind == "movie":
            has_file = bool(item.get("hasFile"))
        else:
            stats = item.get("statistics", {})
            has_file = bool(stats.get("episodeFileCount", 0) > 0)

        if has_file:
            return RequestUpdate(
                stage="ready",
                progress=100,
                next_check_at=now + 3600.0,
            )

    # Check active download / import queue
    if remote_id is not None:
        for q in snapshot.queue:
            q_id = q.get("movieId" if record.spec.kind == "movie" else "seriesId")
            if q_id == remote_id:
                status = str(q.get("status", "")).lower()
                if status in ("importpending", "importing"):
                    return RequestUpdate(
                        stage="importing",
                        progress=100,
                        next_check_at=now + 5.0,
                    )
                size = q.get("size", 0)
                sizeleft = q.get("sizeleft", 0)
                progress = None
                if isinstance(size, (int, float)) and size > 0 and isinstance(sizeleft, (int, float)):
                    progress = max(0, min(100, int((size - sizeleft) / size * 100)))

                return RequestUpdate(
                    stage="downloading",
                    progress=progress,
                    next_check_at=now + 5.0,
                )

    # Check history for failures or imports
    if remote_id is not None:
        for h in snapshot.history:
            h_id = h.get("movieId" if record.spec.kind == "movie" else "seriesId")
            if h_id == remote_id:
                event_type = str(h.get("eventType", "")).lower()
                if "failed" in event_type or "failure" in event_type:
                    return RequestUpdate(
                        stage="downloading",
                        waiting_reason="Download failed; searching for release",
                        next_check_at=now + 30.0,
                    )

    # Standard poll backoff
    return RequestUpdate(
        stage=record.stage,
        next_check_at=now + 30.0,
    )
