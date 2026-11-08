"""Idempotent movie and TV submission to Sonarr and Radarr."""
from __future__ import annotations

from orama.requests.client import MediaClient
from orama.requests.models import RequestRecord, RequestUpdate
from orama.requests.store import RequestStore


def submit(
    record: RequestRecord, client: MediaClient, requests: RequestStore, now: float
) -> None:
    """Submit a media request to Radarr or Sonarr and track remote progress."""
    spec = record.spec

    if spec.kind == "movie":
        remote_id, has_file = client.ensure_movie(spec.source_id)
        if has_file:
            requests.update(
                record.id,
                record.revision,
                RequestUpdate(
                    stage="ready",
                    remote_id=remote_id,
                    waiting_reason=None,
                    next_check_at=now + 3600.0,
                ),
            )
        else:
            client.search_targets("radarr", remote_id)
            requests.update(
                record.id,
                record.revision,
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
        if spec.kind == "series":
            client.search_targets("sonarr", remote_id)
        else:
            target_episode_ids = client.monitor_episodes(remote_id, spec.season, spec.episode)
            if spec.kind == "episode" and spec.season is not None and spec.episode is not None:
                requests.upsert_episode_target(
                    spec.source_id,
                    spec.season,
                    spec.episode,
                    stage="downloading",
                    has_file=False,
                )
            client.search_targets(
                "sonarr",
                remote_id,
                season=spec.season,
                episode_ids=target_episode_ids if spec.kind == "episode" else None,
            )
        requests.update(
            record.id,
            record.revision,
            RequestUpdate(
                stage="downloading",
                remote_id=remote_id,
                waiting_reason=None,
                next_check_at=now + 5.0,
            ),
        )
