"""Idempotent movie and TV submission to Sonarr and Radarr."""
from __future__ import annotations

from orama.requests.client import MediaClient, RequestError, _sanitize_title
from orama.requests.models import RequestRecord, RequestUpdate
from orama.requests.store import RequestStore


def submit(
    record: RequestRecord, client: MediaClient, requests: RequestStore, now: float
) -> None:
    """Submit a media request to Radarr or Sonarr and track remote progress."""
    current = requests.get(record.id) or record
    rev = current.revision
    spec = current.spec

    if spec.kind == "movie":
        remote_id, has_file = client.ensure_movie(spec.source_id)
        if current.remote_id != remote_id and requests.update(
            current.id, rev, RequestUpdate(remote_id=remote_id)
        ):
            rev += 1

        if has_file:
            requests.update(
                current.id,
                rev,
                RequestUpdate(
                    stage="ready",
                    progress=100,
                    remote_id=remote_id,
                    waiting_reason=None,
                    next_check_at=now + 3600.0,
                ),
            )
        else:
            has_check = hasattr(client, "has_active_search_or_download")
            if not (
                has_check
                and client.has_active_search_or_download(
                    "radarr", remote_id, include_completed=True
                )
            ):
                client.search_targets("radarr", remote_id)
            requests.update(
                current.id,
                rev,
                RequestUpdate(
                    stage="downloading",
                    remote_id=remote_id,
                    waiting_reason=None,
                    next_check_at=now + 5.0,
                ),
            )
    else:
        # TV: series, season, or episode
        remote_id, _has_file = client.ensure_series(
            spec.source_id,
            monitor="all" if spec.kind == "series" else "none",
        )
        if current.remote_id != remote_id and requests.update(
            current.id, rev, RequestUpdate(remote_id=remote_id)
        ):
            rev += 1

        has_check = hasattr(client, "has_active_search_or_download")
        if spec.kind == "series":
            series_eps: list[dict] = []
            if hasattr(client, "_has_route") and client._has_route("GET", "episode"):
                try:
                    raw_eps = client._request("sonarr", "episode", params={"seriesId": remote_id})
                    if isinstance(raw_eps, list):
                        series_eps = [
                            ep
                            for ep in raw_eps
                            if isinstance(ep, dict) and int(ep.get("seasonNumber", 0)) > 0
                        ]
                except RequestError:
                    series_eps = []

            for ep in series_eps:
                ep_has_file = bool(ep.get("hasFile"))
                requests.upsert_episode_target(
                    spec.source_id,
                    int(ep.get("seasonNumber", 0)),
                    int(ep.get("episodeNumber", 1)),
                    title=_sanitize_title(str(ep.get("title", ""))),
                    stage="ready" if ep_has_file else "downloading",
                    has_file=ep_has_file,
                )

            if series_eps and all(bool(ep.get("hasFile")) for ep in series_eps):
                requests.update(
                    current.id,
                    rev,
                    RequestUpdate(
                        stage="ready",
                        progress=100,
                        remote_id=remote_id,
                        waiting_reason=None,
                        next_check_at=now + 300.0,
                    ),
                )
                return

            if not (
                has_check
                and client.has_active_search_or_download(
                    "sonarr", remote_id, include_completed=True
                )
            ):
                client.search_targets("sonarr", remote_id)
        else:
            target_episode_ids = client.monitor_episodes(remote_id, spec.season, spec.episode)
            matched_eps = getattr(client, "_last_monitored_episodes", None) or []

            if not target_episode_ids:
                requests.update(
                    current.id,
                    rev,
                    RequestUpdate(
                        stage="searching",
                        remote_id=remote_id,
                        waiting_reason="Waiting for episode metadata",
                        next_check_at=now + 15.0,
                    ),
                )
                return

            if matched_eps:
                for ep in matched_eps:
                    s_num = int(ep.get("seasonNumber", spec.season or 0))
                    e_num = int(ep.get("episodeNumber", spec.episode or 1))
                    ep_has_file = bool(ep.get("hasFile"))
                    requests.upsert_episode_target(
                        spec.source_id,
                        s_num,
                        e_num,
                        title=_sanitize_title(str(ep.get("title", ""))),
                        stage="ready" if ep_has_file else "downloading",
                        has_file=ep_has_file,
                    )
            elif spec.kind == "episode" and spec.season is not None and spec.episode is not None:
                requests.upsert_episode_target(
                    spec.source_id,
                    spec.season,
                    spec.episode,
                    stage="downloading",
                    has_file=False,
                )

            if matched_eps and all(bool(ep.get("hasFile")) for ep in matched_eps):
                requests.update(
                    current.id,
                    rev,
                    RequestUpdate(
                        stage="ready",
                        progress=100,
                        remote_id=remote_id,
                        waiting_reason=None,
                        next_check_at=now + 3600.0,
                    ),
                )
                return

            ep_ids_arg = target_episode_ids if spec.kind == "episode" else None
            season_arg = spec.season if spec.kind == "season" else None
            if not (
                has_check
                and client.has_active_search_or_download(
                    "sonarr",
                    remote_id,
                    season=season_arg,
                    episode_ids=ep_ids_arg,
                    include_completed=True,
                )
            ):
                client.search_targets(
                    "sonarr",
                    remote_id,
                    season=spec.season,
                    episode_ids=ep_ids_arg,
                )

        requests.update(
            current.id,
            rev,
            RequestUpdate(
                stage="downloading",
                remote_id=remote_id,
                waiting_reason=None,
                next_check_at=now + 5.0,
            ),
        )

