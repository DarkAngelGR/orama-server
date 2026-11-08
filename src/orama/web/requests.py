"""Browser routes for media search, media detail, request submission, and live request status."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Any

from flask import Blueprint, Flask, abort, current_app, redirect, render_template, request, url_for

from orama.requests.client import MediaClient, RequestError
from orama.requests.models import (
    EpisodeTarget,
    RequestRecord,
    RequestSpec,
    RequestUpdate,
    ValidationError,
)
from orama.requests.store import _snapshot_policy
from orama.web import auth

_STAGE_LABELS = {
    "requested": "Searching",
    "searching": "Searching",
    "downloading": "Downloading",
    "importing": "Importing",
    "ready": "Ready",
    "unavailable": "Unavailable",
}


@dataclass(frozen=True)
class RequestView:
    """View model for displaying a RequestRecord in templates."""

    id: str
    title: str
    year: int | None
    kind: str
    kind_label: str
    target_label: str
    stage: str
    stage_label: str
    progress: int | None
    waiting_reason: str | None
    playback_note: str | None
    subtitle_note: str | None
    subscription_note: str | None
    season_summary: str | None
    episodes: list[EpisodeTarget]
    can_retry: bool


def _clean_title(raw: str | None, fallback: str) -> str:
    if not raw or not isinstance(raw, str):
        return fallback
    cleaned = re.sub(r"<[^>]*>", "", raw).strip()
    return cleaned[:200] if cleaned else fallback


def stage_label(stage: str, progress: int | None = None) -> str:
    base = _STAGE_LABELS.get(stage, "Searching")
    if stage == "downloading" and progress is not None:
        return f"Downloading ({progress}%)"
    return base


def to_request_view(
    rec: RequestRecord, episodes: list[EpisodeTarget] | None = None
) -> RequestView:
    eps = list(episodes or [])
    fallback_title = f"{rec.spec.kind.capitalize()} #{rec.spec.source_id}"
    title = _clean_title(rec.title, fallback_title)

    if rec.spec.kind == "movie":
        kind_label = "Movie"
        target_label = "Movie"
    elif rec.spec.kind == "episode":
        kind_label = "Episode"
        target_label = f"Season {rec.spec.season}, Episode {rec.spec.episode}"
    elif rec.spec.kind == "season":
        kind_label = "Season"
        target_label = f"Season {rec.spec.season}"
    else:
        kind_label = "TV Series"
        target_label = "All seasons (including future episodes)"

    subscription_note = None
    if rec.spec.kind == "series":
        subscription_note = (
            "Active series subscription. Future episodes will download automatically as they air."
        )

    season_summary = None
    if eps and rec.spec.kind in ("season", "series"):
        ready_count = sum(1 for e in eps if e.has_file or e.stage == "ready")
        season_summary = f"{ready_count} of {len(eps)} episodes ready"

    return RequestView(
        id=rec.id,
        title=title,
        year=rec.year,
        kind=rec.spec.kind,
        kind_label=kind_label,
        target_label=target_label,
        stage=rec.stage,
        stage_label=stage_label(rec.stage, rec.progress),
        progress=rec.progress,
        waiting_reason=rec.waiting_reason,
        playback_note=rec.playback_note,
        subtitle_note=rec.subtitle_note,
        subscription_note=subscription_note,
        season_summary=season_summary,
        episodes=eps,
        can_retry=rec.stage != "ready" or bool(rec.waiting_reason),
    )


def _parse_int(val: str | None) -> int | None:
    if val is None or val == "":
        return None
    if not re.fullmatch(r"-?\d+", val.strip()):
        raise ValueError("not an integer")
    return int(val.strip())


def _wake_worker() -> None:
    worker: Any = current_app.extensions.get("request_worker")
    if worker is not None and hasattr(worker, "wake"):
        worker.wake()


def register_requests(app: Flask) -> None:
    bp = Blueprint("requests", __name__)

    @bp.get("/search")
    @auth.requires("requests:read")
    def search():
        q = request.args.get("q", "").strip()
        kind = request.args.get("type", "movie").strip()
        if kind not in ("movie", "series"):
            abort(400)

        results = []
        error = None
        if q:
            st = auth.web_state()
            setup = st.setup.current()
            if setup is None:
                error = "Finish setting up Orama before searching."
            else:
                client = MediaClient(setup=setup, store=st.store, http=st.http)
                try:
                    results = client.search(kind, q)  # type: ignore[arg-type]
                except RequestError as exc:
                    error = str(exc)

        return render_template(
            "requests/search.html",
            query=q,
            kind=kind,
            results=results,
            error=error,
        )

    @bp.get("/media/<kind>/<source_id>")
    @auth.requires("requests:read")
    def media(kind: str, source_id: str):
        if kind not in ("movie", "series"):
            abort(404)
        try:
            sid = _parse_int(source_id)
        except ValueError:
            abort(400)
        if sid is None or sid <= 0 or sid > 100_000_000:
            abort(400)

        st = auth.web_state()
        setup = st.setup.current()
        if setup is None:
            abort(400)

        client = MediaClient(setup=setup, store=st.store, http=st.http)
        error = None
        detail = None
        try:
            detail = client.detail(kind, sid)  # type: ignore[arg-type]
        except RequestError as exc:
            error = str(exc)

        return render_template(
            "requests/media.html",
            kind=kind,
            source_id=sid,
            detail=detail,
            error=error,
        )

    @bp.post("/requests")
    @auth.requires("requests:write")
    def create_request():
        kind = request.form.get("kind", "").strip()
        try:
            source_id = _parse_int(request.form.get("source_id"))
            season = _parse_int(request.form.get("season"))
            episode = _parse_int(request.form.get("episode"))
            year = _parse_int(request.form.get("year"))
        except ValueError:
            abort(400)

        if source_id is None:
            abort(400)

        try:
            spec = RequestSpec(
                kind=kind,  # type: ignore[arg-type]
                source_id=source_id,
                season=season,
                episode=episode,
            )
        except ValidationError:
            abort(400)

        raw_title = request.form.get("title", "").strip()
        title = _clean_title(raw_title, f"{kind.capitalize()} #{source_id}")

        st = auth.web_state()
        setup = st.setup.current()
        rec, _ = st.requests.create(
            spec,
            st.clock(),
            setup=setup,
            title=title,
            year=year if (year is not None and 1880 <= year <= 2100) else None,
        )
        _wake_worker()
        return redirect(url_for("requests.detail", request_id=rec.id), 303)

    def _load_detail_view(request_id: str) -> RequestView:
        try:
            uuid_obj = uuid.UUID(request_id)
        except ValueError:
            abort(404)
        canonical_id = str(uuid_obj)
        st = auth.web_state()
        rec = st.requests.get(canonical_id)
        if rec is None:
            abort(404)
        eps: list[EpisodeTarget] = []
        if rec.spec.kind != "movie":
            season_filter = rec.spec.season if rec.spec.kind in ("season", "episode") else None
            eps = st.requests.list_episode_targets(rec.spec.source_id, season_filter)
            if rec.spec.kind == "episode" and rec.spec.episode is not None:
                eps = [e for e in eps if e.episode == rec.spec.episode]
        return to_request_view(rec, eps)

    @bp.get("/requests/<request_id>")
    @auth.requires("requests:read")
    def detail(request_id: str):
        view = _load_detail_view(request_id)
        return render_template(
            "requests/detail.html",
            req=view,
            live=(view.stage != "ready" or view.kind == "series"),
        )

    @bp.get("/requests/<request_id>/status")
    @auth.requires("requests:read")
    def status(request_id: str):
        view = _load_detail_view(request_id)
        return render_template("requests/_status.html", req=view)

    @bp.post("/requests/<request_id>/retry")
    @auth.requires("requests:write")
    def retry(request_id: str):
        try:
            canonical_id = str(uuid.UUID(request_id))
        except ValueError:
            abort(404)
        st = auth.web_state()
        rec = st.requests.get(canonical_id)
        if rec is None:
            abort(404)
        now = st.clock()
        setup = st.setup.current()
        wait_mode, fallback_deadline = _snapshot_policy(rec.created_at, setup)
        next_stage = "requested" if rec.stage == "requested" else "searching"
        st.requests.update(
            rec.id,
            rec.revision,
            RequestUpdate(
                stage=next_stage,
                waiting_reason=None,
                next_check_at=now,
                wait_mode=wait_mode,
                fallback_deadline=fallback_deadline,
            ),
        )
        _wake_worker()
        return redirect(url_for("requests.detail", request_id=rec.id), 303)

    app.register_blueprint(bp)

