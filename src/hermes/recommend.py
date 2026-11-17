"""Deterministic ranking; model output may only reorder catalogue IDs."""
from __future__ import annotations
from collections import Counter
from typing import Any


def key_of(item: dict) -> tuple[str, int] | None:
    kind, sid = item.get("kind"), item.get("source_id")
    if kind not in ("movie", "series") or type(sid) is not int or not 0 < sid <= 100_000_000:
        return None
    return kind, sid


def rank(watched: list[dict], candidates: list[dict], library: set[tuple[str, int]], limit: int = 24) -> list[dict]:
    weights: Counter[str] = Counter()
    seen = set(library)
    for w in watched[:3000]:
        k = key_of(w)
        if k:
            seen.add(k)
        rating = w.get("rating")
        positive = w.get("liked") is True or (type(rating) in (int, float) and 7 <= rating <= 10)
        strength = 4 if positive else 1 if w.get("completed") is True else 0
        genres = w.get("genres", [])
        if isinstance(genres, list):
            for g in genres[:25]:
                if isinstance(g, str) and 0 < len(g) <= 60:
                    weights[g.casefold()] += strength
    out = []
    added = set()
    for c in candidates[:5000]:
        if not isinstance(c, dict):
            continue
        k = key_of(c)
        if not k or k in seen or k in added:
            continue
        title = c.get("title")
        genres = c.get("genres", [])
        if not isinstance(title, str) or not 0 < len(title.strip()) <= 200 or not isinstance(genres, list):
            continue
        if any(not isinstance(g, str) or len(g) > 60 for g in genres):
            continue
        added.add(k)
        matches = sorted(((weights[g.casefold()], g) for g in genres if weights[g.casefold()] > 0), reverse=True)
        item = {"kind": k[0], "source_id": k[1], "title": title.strip(),
                "year": c.get("year") if type(c.get("year")) is int else None,
                "poster_path": c.get("poster_path") if isinstance(c.get("poster_path"), str) else None,
                "overview": str(c.get("overview", ""))[:500],
                "score": sum(w for w, _ in matches),
                "reason": f"Matches your interest in {matches[0][1]}." if matches else "A new title to explore."}
        out.append(item)
    out.sort(key=lambda r: (-r["score"], r["kind"], r["source_id"]))
    return out[:max(0, min(50, limit))]


def reorder(known: list[dict], output: Any) -> list[dict]:
    if not isinstance(output, dict) or not isinstance(output.get("ids"), list):
        return known
    indexed = {(c["kind"], c["source_id"]): c for c in known}
    used = set()
    ordered = []
    for obj in output["ids"][:100]:
        if not isinstance(obj, dict):
            continue
        k = key_of(obj)
        if k in indexed and k not in used:
            ordered.append(indexed[k])
            used.add(k)
    ordered.extend(c for c in known if (c["kind"], c["source_id"]) not in used)
    return ordered
