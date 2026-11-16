"""Jarvis recommendation ranking: deterministic, offline and safe by default.

Input candidates come from trusted catalogue adapters, never from the language model.
The model can only reorder validated catalogue identifiers.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Candidate:
    source_id: int
    kind: str
    title: str
    genres: tuple[str, ...] = ()
    overview: str = ""
    year: int | None = None


@dataclass(frozen=True)
class Recommendation:
    candidate: Candidate
    score: float
    reason: str


def _candidate(raw: Any) -> Candidate | None:
    if not isinstance(raw, dict):
        return None
    sid, kind, title = raw.get("source_id"), raw.get("kind"), raw.get("title")
    if type(sid) is not int or not 0 < sid <= 100_000_000:
        return None
    if kind not in ("movie", "series") or not isinstance(title, str):
        return None
    title = title.strip()
    if not title or len(title) > 200:
        return None
    genres = raw.get("genres", [])
    if not isinstance(genres, list) or len(genres) > 30:
        return None
    if any(not isinstance(g, str) or len(g) > 60 for g in genres):
        return None
    overview = raw.get("overview", "")
    if not isinstance(overview, str):
        return None
    year = raw.get("year")
    if year is not None and (type(year) is not int or not 1880 <= year <= 2100):
        return None
    return Candidate(sid, kind, title, tuple(genres), overview[:500], year)


def rank(
    watched: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    *,
    excluded: set[tuple[str, int]] | None = None,
    limit: int = 12,
) -> list[Recommendation]:
    """Return stable recommendations without external network access.

    Watched entries carry 'genres', 'rating' (0..10), 'completed', and 'liked'.
    Continue-watching is *not* treated as an implicit positive vote.
    """
    weights: Counter[str] = Counter()
    seen: set[tuple[str, int]] = set(excluded or ())
    for item in watched[:3000]:
        if not isinstance(item, dict):
            continue
        sid, kind = item.get("source_id"), item.get("kind")
        if type(sid) is int and kind in ("movie", "series"):
            seen.add((kind, sid))
        raw_genres = item.get("genres", [])
        if not isinstance(raw_genres, list):
            continue
        rating = item.get("rating")
        explicit = bool(item.get("liked"))
        completed = bool(item.get("completed"))
        positive = explicit or (type(rating) in (int, float) and 7 <= rating <= 10)
        if not positive and not completed:
            continue
        strength = 4 if positive else 1
        for genre in raw_genres[:30]:
            if isinstance(genre, str) and 0 < len(genre) <= 60:
                weights[genre.casefold()] += strength

    results: list[Recommendation] = []
    unique: set[tuple[str, int]] = set()
    for raw in candidates[:5000]:
        item = _candidate(raw)
        if item is None:
            continue
        key = (item.kind, item.source_id)
        if key in seen or key in unique:
            continue
        unique.add(key)
        matching = sorted(
            ((weights[g.casefold()], g) for g in item.genres if weights[g.casefold()] > 0),
            reverse=True,
        )
        score = float(sum(weight for weight, _ in matching))
        reason = (
            f"Matches your interest in {matching[0][1][:60]}."
            if matching else "Something new to discover."
        )
        results.append(Recommendation(item, score, reason))
    results.sort(key=lambda r: (-r.score, r.candidate.kind, r.candidate.source_id))
    return results[: max(0, min(limit, 50))]


def apply_model_ranking(
    ranked: list[Recommendation], model_output: Any
) -> list[Recommendation]:
    """Accept only known IDs from the LLM; never accept invented titles or HTML.

    Model output: {"ids": [{"kind": "movie", "source_id": 123}, ...]}.
    Unknown, duplicate or malformed entries are ignored; missing IDs retain
    their deterministic order. Model-generated prose is deliberately discarded.
    """
    if not isinstance(model_output, dict) or not isinstance(model_output.get("ids"), list):
        return ranked
    by_id = {(r.candidate.kind, r.candidate.source_id): r for r in ranked}
    ordered: list[Recommendation] = []
    used: set[tuple[str, int]] = set()
    for raw in model_output["ids"][:100]:
        if not isinstance(raw, dict):
            continue
        kind, sid = raw.get("kind"), raw.get("source_id")
        if type(sid) is not int:
            continue
        key = (kind, sid)
        if key in by_id and key not in used:
            ordered.append(by_id[key])
            used.add(key)
    ordered.extend(r for r in ranked if (r.candidate.kind, r.candidate.source_id) not in used)
    return ordered
