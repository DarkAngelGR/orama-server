"""Authoritative status reconciliation against app snapshots."""
from __future__ import annotations

from typing import Any

from orama.requests.client import AppSnapshot
from orama.requests.models import RequestRecord, RequestUpdate


def _matches_tv_event(
    entry: dict[str, Any], record: RequestRecord, target_ep_ids: set[int]
) -> bool:
    if record.spec.kind == "movie":
        return True
    ep_id = entry.get("episodeId")
    if target_ep_ids and isinstance(ep_id, int):
        return ep_id in target_ep_ids

    ep_obj = entry.get("episode") if isinstance(entry.get("episode"), dict) else entry
    season_num = (
        entry.get("seasonNumber")
        if entry.get("seasonNumber") is not None
        else ep_obj.get("seasonNumber")
    )
    episode_num = (
        entry.get("episodeNumber")
        if entry.get("episodeNumber") is not None
        else ep_obj.get("episodeNumber")
    )

    if record.spec.kind == "episode":
        if season_num is not None and season_num != record.spec.season:
            return False
        if episode_num is not None and episode_num != record.spec.episode:
            return False
    elif record.spec.kind == "season":
        if season_num is not None and season_num != record.spec.season:
            return False
    return True


def reconcile(
    record: RequestRecord,
    snapshot: AppSnapshot,
    now: float,
    remote_id: int | None = None,
) -> RequestUpdate:
    """Derive next status stage and progress from snapshot data."""
    item = None
    if remote_id is not None and remote_id in snapshot.items:
        item = snapshot.items[remote_id]
    else:
        for it in snapshot.items.values():
            s_id = it.get("tmdbId" if record.spec.kind == "movie" else "tvdbId")
            if s_id == record.spec.source_id:
                item = it
                remote_id = it.get("id")
                break

    series_episodes = [
        ep
        for ep in snapshot.files
        if isinstance(ep, dict)
        and (remote_id is None or ep.get("seriesId") in (None, remote_id))
    ]
    target_ep_ids: set[int] = set()
    has_file = False

    if record.spec.kind == "movie":
        has_file = bool(item is not None and item.get("hasFile"))
    elif record.spec.kind == "episode":
        matching_eps = [
            ep
            for ep in series_episodes
            if ep.get("seasonNumber") == record.spec.season
            and ep.get("episodeNumber") == record.spec.episode
        ]
        target_ep_ids = {ep["id"] for ep in matching_eps if isinstance(ep.get("id"), int)}
        has_file = bool(matching_eps) and all(bool(ep.get("hasFile")) for ep in matching_eps)
    elif record.spec.kind == "season":
        season_eps = [
            ep for ep in series_episodes if ep.get("seasonNumber") == record.spec.season
        ]
        target_ep_ids = {ep["id"] for ep in season_eps if isinstance(ep.get("id"), int)}
        if season_eps:
            monitored_eps = [ep for ep in season_eps if ep.get("monitored", True)]
            eval_eps = monitored_eps or season_eps
            has_file = bool(eval_eps) and all(bool(ep.get("hasFile")) for ep in eval_eps)
        elif item is not None and isinstance(item.get("seasons"), list):
            for s in item["seasons"]:
                if isinstance(s, dict) and s.get("seasonNumber") == record.spec.season:
                    s_stats = s.get("statistics") if isinstance(s.get("statistics"), dict) else {}
                    ep_files = int(s_stats.get("episodeFileCount", 0) or 0)
                    ep_total = int(
                        s_stats.get("episodeCount") or s_stats.get("totalEpisodeCount") or 0
                    )
                    has_file = bool(ep_total > 0 and ep_files >= ep_total)
                    break
    else:
        regular_eps = [ep for ep in series_episodes if int(ep.get("seasonNumber", 0)) > 0]
        candidate_eps = regular_eps or series_episodes
        target_ep_ids = {ep["id"] for ep in candidate_eps if isinstance(ep.get("id"), int)}
        if candidate_eps:
            monitored_eps = [ep for ep in candidate_eps if ep.get("monitored", True)]
            eval_eps = monitored_eps or candidate_eps
            has_file = bool(eval_eps) and all(bool(ep.get("hasFile")) for ep in eval_eps)
        elif item is not None:
            stats = item.get("statistics") if isinstance(item.get("statistics"), dict) else {}
            ep_files = int(stats.get("episodeFileCount", 0) or 0)
            if "episodeCount" in stats or "totalEpisodeCount" in stats:
                ep_total = int(
                    stats.get("episodeCount") or stats.get("totalEpisodeCount") or 0
                )
                has_file = bool(ep_total > 0 and ep_files >= ep_total)
            else:
                has_file = False

    if has_file:
        next_ready_check = now + (300.0 if record.spec.kind == "series" else 3600.0)
        return RequestUpdate(
            stage="ready",
            progress=100,
            next_check_at=next_ready_check,
        )

    # Check active download / import queue
    if remote_id is not None:
        for q in snapshot.queue:
            q_id = q.get("movieId" if record.spec.kind == "movie" else "seriesId")
            if q_id == remote_id and _matches_tv_event(q, record, target_ep_ids):
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
                if (
                    isinstance(size, (int, float))
                    and size > 0
                    and isinstance(sizeleft, (int, float))
                ):
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
            if h_id == remote_id and _matches_tv_event(h, record, target_ep_ids):
                event_type = str(h.get("eventType", "")).lower()
                if "failed" in event_type or "failure" in event_type:
                    return RequestUpdate(
                        stage="downloading",
                        waiting_reason="Download failed; searching for release",
                        next_check_at=now + 30.0,
                    )

    if record.stage == "ready":
        if record.spec.kind == "series":
            return RequestUpdate(
                stage="downloading",
                progress=None,
                waiting_reason=None,
                next_check_at=now + 30.0,
            )
        if snapshot.complete and item is not None:
            return RequestUpdate(
                stage="unavailable",
                progress=None,
                waiting_reason="Media file is no longer in library",
                next_check_at=now + 300.0,
            )

    # Standard poll backoff
    return RequestUpdate(
        stage=record.stage,
        next_check_at=now + 30.0,
    )

