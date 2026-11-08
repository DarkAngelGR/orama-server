"""Data models and validation for media requests."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

RequestKind = Literal["movie", "episode", "season", "series"]
VALID_KINDS: frozenset[str] = frozenset({"movie", "episode", "season", "series"})
VALID_STAGES: frozenset[str] = frozenset(
    {"requested", "searching", "downloading", "importing", "ready", "unavailable"}
)

_UNSET: Any = object()


class ValidationError(ValueError):
    """Raised when request coordinates or updates fail domain validation."""


@dataclass(frozen=True)
class RequestSpec:
    """Immutable specification identifying a requested media item."""

    kind: RequestKind
    source_id: int
    season: int | None = None
    episode: int | None = None

    def __post_init__(self) -> None:
        if self.kind not in VALID_KINDS:
            raise ValidationError(f"Invalid request kind: {self.kind!r}")
        if not isinstance(self.source_id, int) or isinstance(self.source_id, bool) or self.source_id <= 0:
            raise ValidationError(f"source_id must be a positive integer, got {self.source_id!r}")

        if self.kind == "movie":
            if self.season is not None or self.episode is not None:
                raise ValidationError("Movie requests must not specify season or episode")
        elif self.kind == "series":
            if self.season is not None or self.episode is not None:
                raise ValidationError("Series requests must not specify season or episode")
        elif self.kind == "season":
            if self.season is None or not isinstance(self.season, int) or isinstance(self.season, bool) or self.season < 0:
                raise ValidationError(f"Season requests require a non-negative season integer, got {self.season!r}")
            if self.episode is not None:
                raise ValidationError("Season requests must not specify episode")
        elif self.kind == "episode":
            if self.season is None or not isinstance(self.season, int) or isinstance(self.season, bool) or self.season < 0:
                raise ValidationError(f"Episode requests require a non-negative season integer, got {self.season!r}")
            if self.episode is None or not isinstance(self.episode, int) or isinstance(self.episode, bool) or self.episode < 1:
                raise ValidationError(f"Episode requests require an episode integer >= 1, got {self.episode!r}")

    @property
    def target_key(self) -> str:
        """Deterministic target key for deduplication."""
        s = f"s{self.season}" if self.season is not None else ""
        e = f"e{self.episode}" if self.episode is not None else ""
        coords = f":{s}{e}" if (s or e) else ""
        return f"{self.kind}:{self.source_id}{coords}"


@dataclass(frozen=True)
class RequestRecord:
    """Durable state record of a media request."""

    id: str
    spec: RequestSpec
    created_at: float
    updated_at: float
    revision: int
    stage: str = "requested"
    waiting_reason: str | None = None
    progress: int | None = None
    playback_note: str | None = None
    subtitle_note: str | None = None
    next_check_at: float | None = None
    claim_owner: str | None = None
    claim_expires_at: float | None = None
    remote_id: int | None = None
    wait_mode: str | None = None
    fallback_deadline: float | None = None
    fallback_applied: bool = False
    title: str | None = None
    year: int | None = None


@dataclass(frozen=True)
class EpisodeTarget:
    """Shared child episode status for TV requests."""

    series_id: int
    season: int
    episode: int
    title: str = ""
    stage: str = "searching"
    has_file: bool = False


@dataclass(frozen=True, init=False)
class RequestUpdate:
    """Changes applied atomically to an existing RequestRecord.

    Distinguishes omitted fields (left unchanged in SQLite) from fields
    explicitly passed as ``None`` (cleared to NULL in SQLite).
    """

    stage: str | None
    waiting_reason: str | None
    progress: int | None
    playback_note: str | None
    subtitle_note: str | None
    next_check_at: float | None
    remote_id: int | None
    wait_mode: str | None
    fallback_deadline: float | None
    fallback_applied: bool | None
    provided_fields: frozenset[str] = field(repr=False, compare=False)

    def __init__(
        self,
        *,
        stage: str | None = _UNSET,
        waiting_reason: str | None = _UNSET,
        progress: int | None = _UNSET,
        playback_note: str | None = _UNSET,
        subtitle_note: str | None = _UNSET,
        next_check_at: float | None = _UNSET,
        remote_id: int | None = _UNSET,
        wait_mode: str | None = _UNSET,
        fallback_deadline: float | None = _UNSET,
        fallback_applied: bool | None = _UNSET,
    ) -> None:
        provided: set[str] = set()
        raw = {
            "stage": stage,
            "waiting_reason": waiting_reason,
            "progress": progress,
            "playback_note": playback_note,
            "subtitle_note": subtitle_note,
            "next_check_at": next_check_at,
            "remote_id": remote_id,
            "wait_mode": wait_mode,
            "fallback_deadline": fallback_deadline,
            "fallback_applied": fallback_applied,
        }
        for name, val in raw.items():
            if val is _UNSET:
                object.__setattr__(self, name, None)
            else:
                provided.add(name)
                object.__setattr__(self, name, val)
        object.__setattr__(self, "provided_fields", frozenset(provided))
