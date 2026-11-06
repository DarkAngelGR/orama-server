"""Data models and validation for media requests."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

RequestKind = Literal["movie", "episode", "season", "series"]
VALID_KINDS: frozenset[str] = frozenset({"movie", "episode", "season", "series"})


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


@dataclass(frozen=True)
class RequestUpdate:
    """Changes applied atomically to an existing RequestRecord."""

    stage: str | None = None
    waiting_reason: str | None = None
    progress: int | None = None
    playback_note: str | None = None
    subtitle_note: str | None = None
    next_check_at: float | None = None
