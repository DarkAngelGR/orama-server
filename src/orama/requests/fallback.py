"""Playable-release waiting and per-request quality fallback."""
from __future__ import annotations

import copy
from typing import Any

from orama.profiles.compile import UNPLAYABLE_SCORE
from orama.requests.client import MediaClient, RequestError
from orama.requests.models import RequestRecord, RequestUpdate
from orama.requests.store import RequestStore

FALLBACK_PROFILE_NAME = "Orama fallback"


def compute_fallback_deadline(created_at: float, setup: Any) -> tuple[str, float | None]:
    """Compute (wait_mode, fallback_deadline) for a request created at ``created_at``."""
    mode = getattr(setup, "request_wait_mode", "timed") if setup is not None else "timed"
    hours = getattr(setup, "request_wait_hours", 24) if setup is not None else 24
    if mode == "always":
        # Always wait for a directly playable release -> never fall back
        return "always", None
    if mode == "never":
        # Never wait -> fall back immediately
        return "never", float(created_at)
    return "timed", float(created_at) + float(hours) * 3600.0


def fallback_due(record: RequestRecord, setup: Any, now: float) -> bool:
    """Return True if a request has waited long enough to fall back to broader releases."""
    # A finished item, or one whose file is gone, waits for the owner to ask again.
    if record.stage in ("ready", "unavailable") or record.fallback_applied:
        return False

    # Use snapshotted policy from creation/retry when present
    if record.wait_mode is not None:
        if record.wait_mode == "always":
            return False
        if record.wait_mode == "never":
            return True
        if record.fallback_deadline is not None:
            return now >= record.fallback_deadline

    mode, deadline = compute_fallback_deadline(record.created_at, setup)
    if mode == "always" or deadline is None:
        return False
    return now >= deadline


def ensure_fallback_profile(app_name: str, client: MediaClient, setup: Any = None) -> int:
    """Ensure the 'Orama fallback' quality profile exists in the target app.

    Copies the strict 'Orama' profile's quality constraints and lowers only the
    minimum custom-format score threshold to allow releases blocked by Orama's
    device-compatibility score floor (-10000), leaving the strict profile untouched.
    """
    _ = setup
    profiles = client._request(app_name, "qualityprofile")
    strict_profile: dict[str, Any] | None = None
    existing_fallback: dict[str, Any] | None = None

    if isinstance(profiles, list):
        for p in profiles:
            if not isinstance(p, dict):
                continue
            if p.get("name") == "Orama":
                strict_profile = p
            elif p.get("name") == FALLBACK_PROFILE_NAME:
                existing_fallback = p

    if strict_profile is None or type(strict_profile.get("id")) is not int:
        raise RequestError(
            f"Orama quality profile is not configured in {app_name.capitalize()} yet."
        )

    if existing_fallback is not None and "items" not in strict_profile:
        if type(existing_fallback.get("id")) is not int:
            raise RequestError(
                f"Invalid {FALLBACK_PROFILE_NAME} profile in {app_name.capitalize()}."
            )
        return existing_fallback["id"]

    template = copy.deepcopy(strict_profile)
    strict_min = int(template.get("minFormatScore", 0))
    fallback_doc: dict[str, Any] = {
        **template,
        "name": FALLBACK_PROFILE_NAME,
        "minFormatScore": min(strict_min, UNPLAYABLE_SCORE),
    }

    if existing_fallback is not None:
        if type(existing_fallback.get("id")) is not int:
            raise RequestError(
                f"Invalid {FALLBACK_PROFILE_NAME} profile in {app_name.capitalize()}."
            )
        fb_id = existing_fallback["id"]
        fallback_doc["id"] = fb_id
        if (
            existing_fallback.get("items") != fallback_doc.get("items")
            or existing_fallback.get("cutoff") != fallback_doc.get("cutoff")
            or existing_fallback.get("minFormatScore") != fallback_doc.get("minFormatScore")
        ):
            client._request(
                app_name, f"qualityprofile/{fb_id}", method="PUT", json_body=fallback_doc
            )
        return fb_id

    fallback_doc.pop("id", None)
    created = client._request(app_name, "qualityprofile", method="POST", json_body=fallback_doc)
    if not isinstance(created, dict) or type(created.get("id")) is not int:
        raise RequestError(
            f"Could not create {FALLBACK_PROFILE_NAME} profile in {app_name.capitalize()}."
        )
    return created["id"]


def _covered_by_record(ep: dict[str, Any], record: RequestRecord) -> bool:
    if record.spec.kind == "series":
        return True
    if record.spec.kind == "season":
        return ep.get("seasonNumber") == record.spec.season
    if record.spec.kind == "episode":
        return (
            ep.get("seasonNumber") == record.spec.season
            and ep.get("episodeNumber") == record.spec.episode
        )
    return False


def apply_fallback(
    record: RequestRecord,
    client: MediaClient,
    requests: RequestStore,
    setup: Any,
    now: float,
) -> RequestUpdate:
    """Attach 'Orama fallback' to the requested item when safe and trigger a search."""
    app_name = "radarr" if record.spec.kind == "movie" else "sonarr"
    resource = "movie" if app_name == "radarr" else "series"
    external_key = "tmdbId" if app_name == "radarr" else "tvdbId"

    items = client._request(app_name, resource)
    target_item: dict[str, Any] | None = None
    if isinstance(items, list):
        for it in items:
            if not isinstance(it, dict):
                continue
            if record.remote_id is not None and it.get("id") == record.remote_id:
                target_item = it
                break
            if it.get(external_key) == record.spec.source_id:
                target_item = it
                break

    if target_item is None:
        update = RequestUpdate(
            waiting_reason="Waiting for library entry before fallback",
            next_check_at=now + 30.0,
        )
        requests.update(record.id, record.revision, update)
        return update

    remote_id = int(target_item["id"])
    profiles = client._request(app_name, "qualityprofile")
    strict_id: int | None = None
    existing_fb_id: int | None = None
    if isinstance(profiles, list):
        for p in profiles:
            if isinstance(p, dict):
                if p.get("name") == "Orama" and type(p.get("id")) is int:
                    strict_id = p["id"]
                elif p.get("name") == FALLBACK_PROFILE_NAME and type(p.get("id")) is int:
                    existing_fb_id = p["id"]

    if strict_id is None:
        raise RequestError(
            f"Orama quality profile is not configured in {app_name.capitalize()} yet."
        )

    current_profile_id = target_item.get("qualityProfileId")
    if (
        current_profile_id is not None
        and current_profile_id != strict_id
        and (existing_fb_id is None or current_profile_id != existing_fb_id)
    ):
        # Preserve a user-assigned custom quality profile
        update = RequestUpdate(
            remote_id=remote_id,
            waiting_reason="Keeping custom quality profile assigned in app",
            next_check_at=now + 300.0,
        )
        requests.update(record.id, record.revision, update)
        return update

    target_episode_ids: list[int] = []
    if app_name == "sonarr":
        episodes = client._request("sonarr", "episode", params={"seriesId": remote_id})
        if isinstance(episodes, list):
            active_for_series = [
                r
                for r in requests.list_active()
                if r.spec.kind != "movie"
                and r.spec.source_id == record.spec.source_id
                and (r.id == record.id or fallback_due(r, setup, now))
            ]
            if not any(r.id == record.id for r in active_for_series):
                active_for_series.append(record)

            for ep in episodes:
                if not isinstance(ep, dict):
                    continue
                if _covered_by_record(ep, record) and isinstance(ep.get("id"), int):
                    target_episode_ids.append(ep["id"])
                if (
                    ep.get("monitored")
                    and not ep.get("hasFile", False)
                    and not any(_covered_by_record(ep, r) for r in active_for_series)
                ):
                    update = RequestUpdate(
                        remote_id=remote_id,
                        waiting_reason=(
                            "No playable release yet — keeping strict profile because "
                            "other episodes in this series are monitored"
                        ),
                        next_check_at=now + 60.0,
                    )
                    requests.update(record.id, record.revision, update)
                    return update

    if app_name == "sonarr" and record.spec.kind in ("episode", "season") and not target_episode_ids:
        update = RequestUpdate(
            remote_id=remote_id,
            waiting_reason="Waiting for episode metadata before fallback",
            next_check_at=now + 30.0,
        )
        requests.update(record.id, record.revision, update)
        return update

    fallback_id = ensure_fallback_profile(app_name, client, setup)
    if current_profile_id != fallback_id:
        updated_item = {**target_item, "qualityProfileId": fallback_id}
        client._request(
            app_name, f"{resource}/{remote_id}", method="PUT", json_body=updated_item
        )

    if app_name == "radarr":
        client.search_targets("radarr", remote_id)
    else:
        client.search_targets(
            "sonarr",
            remote_id,
            season=record.spec.season if record.spec.kind == "season" else None,
            episode_ids=target_episode_ids if record.spec.kind == "episode" else None,
        )

    update = RequestUpdate(
        remote_id=remote_id,
        fallback_applied=True,
        waiting_reason="No directly playable release found — searching broader releases",
        next_check_at=now + 5.0,
    )
    requests.update(record.id, record.revision, update)
    return update


def retry_request(
    request_id: str,
    requests: RequestStore,
    setup: Any,
    now: float,
) -> RequestRecord | None:
    """Explicitly retry a request using the current setup policy without resetting elapsed wait."""
    record = requests.get(request_id)
    if record is None:
        return None
    wait_mode, deadline = compute_fallback_deadline(record.created_at, setup)
    next_stage = (
        "requested" if record.stage == "requested" else "searching"
    )
    update = RequestUpdate(
        stage=next_stage,
        waiting_reason=None,
        wait_mode=wait_mode,
        fallback_deadline=deadline,
        next_check_at=now,
    )
    requests.update(record.id, record.revision, update)
    return requests.get(request_id)



def restore_strict_profile(record: RequestRecord, client: MediaClient, requests: RequestStore) -> bool:
    """Put the strict 'Orama' profile back on a finished fallback request's movie or show.

    Fallback is per request: once the request is Ready, the title waits for playable releases
    again (a followed show would otherwise take every later episode without the wait). Left
    alone while another unfinished request for the same title still needs the fallback, when
    the item already has the strict profile, and when the owner chose another profile by hand.
    True if the profile was changed.
    """
    if not record.fallback_applied or record.stage != "ready" or record.remote_id is None:
        return False
    is_movie = record.spec.kind == "movie"
    if any(
        r.id != record.id and r.fallback_applied and r.stage != "ready"
        and (r.spec.kind == "movie") == is_movie and r.spec.source_id == record.spec.source_id
        for r in requests.list_active()
    ):
        return False
    app_name = "radarr" if is_movie else "sonarr"
    resource = "movie" if is_movie else "series"
    strict_id = fallback_id = None
    profiles = client._request(app_name, "qualityprofile")
    for p in profiles if isinstance(profiles, list) else []:
        if isinstance(p, dict) and type(p.get("id")) is int:
            if p.get("name") == "Orama":
                strict_id = p["id"]
            elif p.get("name") == FALLBACK_PROFILE_NAME:
                fallback_id = p["id"]
    if strict_id is None or fallback_id is None:
        return False
    item = client._request(app_name, f"{resource}/{record.remote_id}")
    if not isinstance(item, dict) or item.get("qualityProfileId") != fallback_id:
        return False
    client._request(app_name, f"{resource}/{record.remote_id}", method="PUT",
                    json_body={**item, "qualityProfileId": strict_id})
    return True
