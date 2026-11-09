"""Authoritative status reconciliation against app snapshots."""
from __future__ import annotations

from typing import Any

from orama.requests.client import AppSnapshot
from orama.requests.models import RequestRecord, RequestUpdate


def _extract_tv_identifiers(
    entry: dict[str, Any],
    ep_coords: dict[int, tuple[int | None, int | None]],
) -> tuple[bool, int | None, set[int] | None, int | None, int | None]:
    """Extract and validate TV event identifiers: (valid, ep_id, ep_ids, season, episode)."""
    raw_ep_obj = entry.get("episode")
    if "episode" in entry and not isinstance(raw_ep_obj, dict):
        return False, None, None, None, None
    ep_obj = raw_ep_obj if isinstance(raw_ep_obj, dict) else None

    top_ep_id = entry.get("episodeId")
    if "episodeId" in entry and (type(top_ep_id) is not int or top_ep_id <= 0):
        return False, None, None, None, None
    nested_ep_id = ep_obj.get("id") if ep_obj is not None and "id" in ep_obj else None
    if ep_obj is not None and "id" in ep_obj and (type(nested_ep_id) is not int or nested_ep_id <= 0):
        return False, None, None, None, None
    if top_ep_id is not None and nested_ep_id is not None and top_ep_id != nested_ep_id:
        return False, None, None, None, None
    ep_id = top_ep_id if top_ep_id is not None else nested_ep_id

    entry_ep_ids: set[int] | None = None
    if "episodeIds" in entry:
        raw_ep_ids = entry.get("episodeIds")
        if (
            not isinstance(raw_ep_ids, list)
            or not raw_ep_ids
            or any(type(eid) is not int or eid <= 0 for eid in raw_ep_ids)
        ):
            return False, None, None, None, None
        entry_ep_ids = set(raw_ep_ids)
        if ep_id is not None and ep_id not in entry_ep_ids:
            return False, None, None, None, None

    top_season = entry.get("seasonNumber")
    if "seasonNumber" in entry and type(top_season) is not int:
        return False, None, None, None, None
    nested_season = (
        ep_obj.get("seasonNumber") if ep_obj is not None and "seasonNumber" in ep_obj else None
    )
    if ep_obj is not None and "seasonNumber" in ep_obj and type(nested_season) is not int:
        return False, None, None, None, None
    if top_season is not None and nested_season is not None and top_season != nested_season:
        return False, None, None, None, None
    season_num = top_season if top_season is not None else nested_season

    top_episode = entry.get("episodeNumber")
    if "episodeNumber" in entry and (type(top_episode) is not int or top_episode <= 0):
        return False, None, None, None, None
    nested_episode = (
        ep_obj.get("episodeNumber") if ep_obj is not None and "episodeNumber" in ep_obj else None
    )
    if (
        ep_obj is not None
        and "episodeNumber" in ep_obj
        and (type(nested_episode) is not int or nested_episode <= 0)
    ):
        return False, None, None, None, None
    if top_episode is not None and nested_episode is not None and top_episode != nested_episode:
        return False, None, None, None, None
    episode_num = top_episode if top_episode is not None else nested_episode

    if episode_num is not None and season_num is None and ep_id is None and entry_ep_ids is None:
        return False, None, None, None, None
    if ep_obj is not None and ep_id is None and season_num is None and episode_num is None:
        return False, None, None, None, None

    if ep_id is not None and ep_id in ep_coords:
        known_s, known_e = ep_coords[ep_id]
        if season_num is not None and known_s is not None and season_num != known_s:
            return False, None, None, None, None
        if episode_num is not None and known_e is not None and episode_num != known_e:
            return False, None, None, None, None

    if entry_ep_ids is not None:
        for eid in entry_ep_ids:
            if eid in ep_coords:
                known_s, known_e = ep_coords[eid]
                if season_num is not None and known_s is not None and season_num != known_s:
                    return False, None, None, None, None
                if (
                    episode_num is not None
                    and len(entry_ep_ids) == 1
                    and known_e is not None
                    and episode_num != known_e
                ):
                    return False, None, None, None, None

    return True, ep_id, entry_ep_ids, season_num, episode_num


def _matches_tv_event(
    entry: dict[str, Any],
    record: RequestRecord,
    target_ep_ids: set[int],
    ep_coords: dict[int, tuple[int | None, int | None]] | None = None,
) -> bool:
    if record.spec.kind == "movie":
        return True

    valid, ep_id, entry_ep_ids, season_num, episode_num = _extract_tv_identifiers(
        entry, ep_coords or {}
    )
    if not valid:
        return False

    if record.spec.kind == "episode":
        if season_num is not None and season_num != record.spec.season:
            return False
        if episode_num is not None and episode_num != record.spec.episode:
            return False
        if target_ep_ids:
            if ep_id is not None:
                return ep_id in target_ep_ids
            if entry_ep_ids is not None:
                return bool(entry_ep_ids & target_ep_ids)
        return (
            season_num is not None
            and episode_num is not None
            and season_num == record.spec.season
            and episode_num == record.spec.episode
        )

    if record.spec.kind == "season":
        if season_num is not None and season_num != record.spec.season:
            return False
        if target_ep_ids:
            if ep_id is not None:
                return ep_id in target_ep_ids
            if entry_ep_ids is not None:
                return bool(entry_ep_ids & target_ep_ids)
        return season_num is not None and season_num == record.spec.season

    if season_num is not None and season_num <= 0:
        return False
    if target_ep_ids:
        if ep_id is not None:
            return ep_id in target_ep_ids
        if entry_ep_ids is not None:
            return bool(entry_ep_ids & target_ep_ids)
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
    ep_coords: dict[int, tuple[int | None, int | None]] = {
        ep["id"]: (
            ep.get("seasonNumber") if type(ep.get("seasonNumber")) is int else None,
            ep.get("episodeNumber") if type(ep.get("episodeNumber")) is int else None,
        )
        for ep in series_episodes
        if type(ep.get("id")) is int
    }
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
            if q_id == remote_id and _matches_tv_event(q, record, target_ep_ids, ep_coords):
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
            if h_id == remote_id and _matches_tv_event(h, record, target_ep_ids, ep_coords):
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

