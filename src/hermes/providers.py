"""Read-only external media adapters with fixed URLs and bounded responses."""
from __future__ import annotations
import os
import xml.etree.ElementTree as ET
from urllib.parse import urlparse
import httpx

MAX_RESPONSE_BYTES = 4 * 1024 * 1024
TIMEOUT = httpx.Timeout(10.0)


def private_server_url(value: str) -> str:
    """A configured LAN/localhost address only; prevent accidental public-server access."""
    import ipaddress
    parsed = urlparse(value)
    host = parsed.hostname or ""
    if parsed.scheme not in ("http", "https") or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/"):
        raise ValueError("Media server URL must be a base http(s) URL")
    try:
        ip = ipaddress.ip_address(host)
        ok = ip.is_private or ip.is_loopback
    except ValueError:
        ok = host in ("localhost", "ollama") or host.endswith(".local")
    if not ok:
        raise ValueError("Media server must use a private address or .local hostname")
    return value.rstrip("/")


async def get_bytes(client: httpx.AsyncClient, url: str, **kwargs) -> bytes:
    async with client.stream("GET", url, follow_redirects=False, timeout=TIMEOUT, **kwargs) as res:
        res.raise_for_status()
        if "location" in res.headers and 300 <= res.status_code < 400:
            raise ValueError("Redirect refused")
        data = bytearray()
        async for part in res.aiter_bytes():
            data.extend(part)
            if len(data) > MAX_RESPONSE_BYTES:
                raise ValueError("Upstream response too large")
        return bytes(data)


def plex_tmdb_id(node: ET.Element) -> int | None:
    for g in node.findall("Guid"):
        raw = g.attrib.get("id", "")
        if raw.startswith("tmdb://") and raw[7:].isdigit():
            return int(raw[7:])
    raw = node.attrib.get("guid", "")
    if raw.startswith("tmdb://") and raw[7:].isdigit():
        return int(raw[7:])
    return None


def plex_records(xml: bytes) -> tuple[list[dict], set[tuple[str, int]]]:
    root = ET.fromstring(xml)
    watched, library = [], set()
    for n in root.iter():
        if n.tag not in ("Video", "Directory"):
            continue
        kind = {"movie": "movie", "show": "series"}.get(n.attrib.get("type"))
        sid = plex_tmdb_id(n)
        if not kind or not sid:
            continue
        k = kind, sid
        library.add(k)
        views = int(n.attrib.get("viewCount", "0") or "0")
        rating = n.attrib.get("userRating")
        genres = [g.attrib.get("tag", "") for g in n.findall("Genre")][:25]
        if views > 0 or rating:
            watched.append({"kind": kind, "source_id": sid, "genres": genres,
                            "rating": float(rating) if rating else None, "completed": views > 0,
                            "liked": bool(rating and float(rating) >= 8)})
    return watched, library


async def plex_data(client: httpx.AsyncClient, base: str, token: str):
    base = private_server_url(base)
    headers = {"X-Plex-Token": token, "Accept": "application/xml"}
    sections = ET.fromstring(await get_bytes(client, base + "/library/sections", headers=headers))
    watched, library = [], set()
    for section in sections.findall("Directory"):
        if section.attrib.get("type") not in ("movie", "show"):
            continue
        section_id = section.attrib.get("key", "")
        if not section_id.isdigit():
            continue
        # Pagination avoids giant Plex libraries silently exceeding response caps.
        for start in range(0, 5000, 200):
            xml = await get_bytes(client, base + f"/library/sections/{section_id}/all",
                 headers={**headers, "X-Plex-Container-Start": str(start),
                          "X-Plex-Container-Size": "200"},
                 params={"includeGuids": "1", "includeUserState": "1"})
            root = ET.fromstring(xml)
            w, ids = plex_records(xml)
            watched.extend(w)
            library.update(ids)
            if len([n for n in root if n.tag in ("Video", "Directory")]) < 200:
                break
    return watched, library


async def jellyfin_data(client: httpx.AsyncClient, base: str, api_key: str, user_id: str):
    base = private_server_url(base)
    if not user_id or any(c not in "0123456789abcdefABCDEF-" for c in user_id):
        raise ValueError("Invalid Jellyfin user id")
    watched, library = [], set()
    for start in range(0, 5000, 200):
        body = await get_bytes(client, base + f"/Users/{user_id}/Items",
            headers={"X-Emby-Token": api_key}, params={"Recursive": "true", "StartIndex": start,
            "Limit": 200, "IncludeItemTypes": "Movie,Series", "Fields": "Genres,ProviderIds,UserData"})
        import json
        doc = json.loads(body)
        rows = doc.get("Items", [])
        for n in rows:
            sid = n.get("ProviderIds", {}).get("Tmdb")
            if not isinstance(sid, str) or not sid.isdigit():
                continue
            kind = {"Movie": "movie", "Series": "series"}.get(n.get("Type"))
            if kind is None:
                continue
            sid = int(sid)
            library.add((kind, sid))
            info = n.get("UserData") or {}
            rating = n.get("CommunityRating")
            played = info.get("Played") is True or int(info.get("PlayCount") or 0) > 0
            if played or info.get("IsFavorite") is True:
                watched.append({"kind": kind, "source_id": sid,
                                "genres": (n.get("Genres") or [])[:25], "completed": played,
                                "liked": info.get("IsFavorite") is True, "rating": None})
        if len(rows) < 200:
            break
    return watched, library


async def tmdb_candidates(client: httpx.AsyncClient, key: str, watched: list[dict]):
    # TMDB is fixed, not derived from any client/model input.
    ids = list(dict.fromkeys((w["kind"], w["source_id"]) for w in reversed(watched)))[:15]
    candidates = []
    for kind, sid in ids:
        endpoint = "movie" if kind == "movie" else "tv"
        for variant in ("recommendations", "similar"):
            raw = await get_bytes(client,
                f"https://api.themoviedb.org/3/{endpoint}/{sid}/{variant}",
                params={"api_key": key, "language": "en-US", "page": 1})
            import json
            for item in json.loads(raw).get("results", [])[:20]:
                name = item.get("title") if kind == "movie" else item.get("name")
                date = item.get("release_date") if kind == "movie" else item.get("first_air_date")
                year = int(date[:4]) if isinstance(date, str) and len(date) >= 4 and date[:4].isdigit() else None
                # Genre IDs are converted by the optional genre lists fetched below.
                if isinstance(item.get("id"), int) and isinstance(name, str):
                    candidates.append({"kind": kind, "source_id": item["id"], "title": name,
                                       "year": year, "genre_ids": item.get("genre_ids", []),
                                       "poster_path": item.get("poster_path"),
                                       "overview": item.get("overview", "")})
    genre_map = {}
    for kind in ("movie", "tv"):
        raw = await get_bytes(client, f"https://api.themoviedb.org/3/genre/{kind}/list",
                              params={"api_key": key, "language": "en-US"})
        import json
        genre_map[kind] = {g["id"]: g["name"] for g in json.loads(raw).get("genres", [])}
    for item in candidates:
        genre_kind = "movie" if item["kind"] == "movie" else "tv"
        ids_ = item.pop("genre_ids", [])
        item["genres"] = [genre_map[genre_kind][g] for g in ids_ if g in genre_map[genre_kind]][:25]
    return candidates
