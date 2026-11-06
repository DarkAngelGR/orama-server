"""Safe lookup and app read client for Sonarr and Radarr."""
from __future__ import annotations

import html
import re
import urllib.parse
from dataclasses import dataclass
from typing import Any

from orama.requests.models import RequestKind
from orama.secrets import SecretStore
from orama.wiring.http import Http, Unreachable

SAFE_URL_PATTERN = re.compile(r"^https?://[a-zA-Z0-9_.~:/?#\[\]@!$&'()*+,;=-]+$")


class RequestError(Exception):
    """Domain error for media lookup and read operations."""


@dataclass(frozen=True)
class SearchResult:
    """Normalized search result DTO."""

    kind: RequestKind
    source_id: int
    title: str
    year: int | None
    in_library: bool
    poster_ref: str | None


@dataclass(frozen=True)
class SeasonDetail:
    """Summary of a TV series season."""

    season_number: int
    episode_count: int
    monitored: bool


@dataclass(frozen=True)
class EpisodeDetail:
    """Summary of a specific TV episode."""

    season_number: int
    episode_number: int
    title: str
    has_file: bool
    monitored: bool


@dataclass(frozen=True)
class MediaDetail:
    """Normalized media detail view."""

    result: SearchResult
    remote_id: int | None
    seasons: list[SeasonDetail]
    episodes: list[EpisodeDetail]


@dataclass(frozen=True)
class AppSnapshot:
    """Authoritative snapshot of an app's current media, queue, and history state."""

    complete: bool
    items: dict[int, Any]
    queue: list[dict[str, Any]]
    history: list[dict[str, Any]]
    files: list[dict[str, Any]]


def _sanitize_title(raw: str) -> str:
    cleaned = html.escape(raw.strip())
    # Strip any residual tag shapes
    return re.sub(r"<[^>]*>", "", cleaned).strip()


def _sanitize_poster_url(url: str | None) -> str | None:
    if not url or not isinstance(url, str):
        return None
    url = url.strip()
    if not SAFE_URL_PATTERN.match(url):
        return None
    return url


class MediaClient:
    """Communicates safely with Sonarr and Radarr using strict allowlists."""

    def __init__(self, setup: Any, store: SecretStore, http: Http) -> None:
        self.setup = setup
        self.store = store
        self.http = http

    def _get_app_info(self, app_name: str) -> tuple[str, str]:
        ports = getattr(self.setup, "ports", {})
        port = ports.get(app_name, 7878 if app_name == "radarr" else 8989)
        key = self.store.get(app_name)
        if not key:
            raise RequestError(f"{app_name.capitalize()} is not configured or key missing")
        return f"http://127.0.0.1:{port}", key

    def _request(
        self,
        app_name: str,
        path: str,
        *,
        method: str = "GET",
        params: dict[str, Any] | None = None,
        json_body: Any = None,
    ) -> Any:
        base_url, key = self._get_app_info(app_name)
        query_str = f"?{urllib.parse.urlencode(params)}" if params else ""
        url = f"{base_url}/api/v3/{path.lstrip('/')}{query_str}"
        headers = {"X-Api-Key": key}

        try:
            resp = self.http.request(method, url, headers=headers, json=json_body)
        except Unreachable:
            raise RequestError(f"{app_name.capitalize()} is unreachable or timed out") from None

        if resp.status == 401 or resp.status == 403:
            raise RequestError(f"Authentication failed for {app_name.capitalize()}")
        if resp.status not in (200, 201, 202):
            raise RequestError(f"{app_name.capitalize()} returned HTTP {resp.status}")

        if not resp.body:
            return {}
        try:
            return resp.json()
        except ValueError:
            raise RequestError(f"{app_name.capitalize()} returned malformed JSON") from None

    def search(self, kind: RequestKind, query: str) -> list[SearchResult]:
        """Search media through Radarr (movie) or Sonarr (series)."""
        trimmed = query.strip()
        if not (1 <= len(trimmed) <= 200):
            raise RequestError("Query must be between 1 and 200 characters")

        app_name = "radarr" if kind == "movie" else "sonarr"
        path = "movie/lookup" if kind == "movie" else "series/lookup"
        data = self._request(app_name, path, params={"term": trimmed})

        if not isinstance(data, list):
            return []

        results: list[SearchResult] = []
        for item in data[:50]:
            if not isinstance(item, dict):
                continue
            if kind == "movie":
                source_id = item.get("tmdbId") or item.get("id")
                in_library = bool(item.get("hasFile") or item.get("id"))
            else:
                source_id = item.get("tvdbId") or item.get("id")
                stats = item.get("statistics", {})
                in_library = bool(stats.get("episodeFileCount", 0) > 0 or item.get("id"))

            if not source_id or not isinstance(source_id, int):
                continue

            title = _sanitize_title(str(item.get("title", "")))
            year = item.get("year")
            images = item.get("images", [])
            poster_url = None
            if isinstance(images, list):
                for img in images:
                    if isinstance(img, dict) and img.get("coverType") == "poster":
                        poster_url = _sanitize_poster_url(img.get("remoteUrl"))
                        break

            results.append(
                SearchResult(
                    kind=kind,
                    source_id=source_id,
                    title=title,
                    year=year if isinstance(year, int) else None,
                    in_library=in_library,
                    poster_ref=poster_url,
                )
            )

        return results

    def detail(self, kind: RequestKind, source_id: int) -> MediaDetail:
        """Fetch media detail by source ID."""
        results = self.search(kind, str(source_id))
        matched = next((r for r in results if r.source_id == source_id), None)
        if not matched and results:
            matched = results[0]
        if not matched:
            matched = SearchResult(
                kind=kind,
                source_id=source_id,
                title=f"Media #{source_id}",
                year=None,
                in_library=False,
                poster_ref=None,
            )

        app_name = "radarr" if kind == "movie" else "sonarr"
        endpoint = "movie" if kind == "movie" else "series"
        all_items = self._request(app_name, endpoint)
        remote_id = None
        seasons: list[SeasonDetail] = []
        episodes: list[EpisodeDetail] = []

        if isinstance(all_items, list):
            for item in all_items:
                s_id = item.get("tmdbId" if kind == "movie" else "tvdbId")
                if s_id == source_id:
                    remote_id = item.get("id")
                    if kind == "series" and isinstance(item.get("seasons"), list):
                        for s in item["seasons"]:
                            seasons.append(
                                SeasonDetail(
                                    season_number=s.get("seasonNumber", 0),
                                    episode_count=s.get("statistics", {}).get("totalEpisodeCount", 0),
                                    monitored=bool(s.get("monitored")),
                                )
                            )
                    break

        return MediaDetail(
            result=matched,
            remote_id=remote_id,
            seasons=seasons,
            episodes=episodes,
        )

    def snapshot(self, app_name: str, remote_ids: set[int] | list[int]) -> AppSnapshot:
        """Fetch bounded snapshot of app items, queue, and history."""
        target_ids = set(remote_ids)
        endpoint = "movie" if app_name == "radarr" else "series"
        raw_items = self._request(app_name, endpoint)
        items: dict[int, Any] = {}
        if isinstance(raw_items, list):
            for it in raw_items:
                r_id = it.get("id")
                if r_id in target_ids:
                    items[r_id] = it

        raw_queue = self._request(app_name, "queue", params={"pageSize": 100})
        queue: list[dict[str, Any]] = []
        complete = True
        if isinstance(raw_queue, dict):
            records = raw_queue.get("records", [])
            total = raw_queue.get("totalRecords", len(records))
            if total > 100:
                complete = False
            if isinstance(records, list):
                for q in records:
                    m_id = q.get("movieId" if app_name == "radarr" else "seriesId")
                    if m_id in target_ids:
                        queue.append(q)

        raw_history = self._request(app_name, "history", params={"pageSize": 100})
        history: list[dict[str, Any]] = []
        if isinstance(raw_history, dict):
            records = raw_history.get("records", [])
            if isinstance(records, list):
                for h in records:
                    m_id = h.get("movieId" if app_name == "radarr" else "seriesId")
                    if m_id in target_ids:
                        history.append(h)

        return AppSnapshot(
            complete=complete,
            items=items,
            queue=queue,
            history=history,
            files=[],
        )

    def _get_quality_profile_id(self, app_name: str) -> int:
        profiles = self._request(app_name, "qualityprofile")
        if isinstance(profiles, list):
            for p in profiles:
                if p.get("name") == "Orama":
                    return p["id"]
            if profiles:
                return profiles[0]["id"]
        return 1

    def _get_root_folder_path(self, app_name: str) -> str:
        folders = self._request(app_name, "rootfolder")
        if isinstance(folders, list) and folders:
            return folders[0].get("path", f"/data/media/{app_name}")
        return f"/data/media/{'movies' if app_name == 'radarr' else 'tv'}"

    def ensure_movie(self, tmdb_id: int) -> tuple[int, bool]:
        """Ensure movie is added in Radarr without auto-searching; returns (remote_id, has_file)."""
        existing_list = self._request("radarr", "movie")
        if isinstance(existing_list, list):
            for m in existing_list:
                if m.get("tmdbId") == tmdb_id:
                    return m["id"], bool(m.get("hasFile"))

        lookup_res = self._request("radarr", "movie/lookup", params={"term": f"tmdb:{tmdb_id}"})
        if not isinstance(lookup_res, list) or not lookup_res:
            lookup_res = self._request("radarr", "movie/lookup", params={"term": str(tmdb_id)})
        if not isinstance(lookup_res, list) or not lookup_res:
            raise RequestError(f"Could not locate movie metadata for TMDB #{tmdb_id}")

        movie_data = lookup_res[0]
        qp_id = self._get_quality_profile_id("radarr")
        root_path = self._get_root_folder_path("radarr")

        payload = {
            **movie_data,
            "qualityProfileId": qp_id,
            "rootFolderPath": root_path,
            "monitored": True,
            "addOptions": {"searchForMovie": False},
        }
        created = self._request("radarr", "movie", method="POST", json_body=payload)
        return created["id"], bool(created.get("hasFile"))

    def ensure_series(self, tvdb_id: int) -> tuple[int, bool]:
        """Ensure series is added in Sonarr without auto-searching; returns (remote_id, has_file)."""
        existing_list = self._request("sonarr", "series")
        if isinstance(existing_list, list):
            for s in existing_list:
                if s.get("tvdbId") == tvdb_id:
                    stats = s.get("statistics", {})
                    has_file = bool(stats.get("episodeFileCount", 0) > 0)
                    return s["id"], has_file

        lookup_res = self._request("sonarr", "series/lookup", params={"term": f"tvdb:{tvdb_id}"})
        if not isinstance(lookup_res, list) or not lookup_res:
            lookup_res = self._request("sonarr", "series/lookup", params={"term": str(tvdb_id)})
        if not isinstance(lookup_res, list) or not lookup_res:
            raise RequestError(f"Could not locate series metadata for TVDB #{tvdb_id}")

        series_data = lookup_res[0]
        qp_id = self._get_quality_profile_id("sonarr")
        root_path = self._get_root_folder_path("sonarr")

        payload = {
            **series_data,
            "qualityProfileId": qp_id,
            "rootFolderPath": root_path,
            "monitored": True,
            "addOptions": {"searchForMissingEpisodes": False},
        }
        created = self._request("sonarr", "series", method="POST", json_body=payload)
        stats = created.get("statistics", {})
        has_file = bool(stats.get("episodeFileCount", 0) > 0)
        return created["id"], has_file

    def monitor_episodes(
        self, series_id: int, season: int | None, episode: int | None
    ) -> list[int]:
        """Set monitoring on targeted episodes only."""
        episodes = self._request("sonarr", "episode", params={"seriesId": series_id})
        target_ids: list[int] = []
        if isinstance(episodes, list):
            for ep in episodes:
                if episode is not None:
                    if ep.get("seasonNumber") == season and ep.get("episodeNumber") == episode:
                        target_ids.append(ep["id"])
                elif season is not None:
                    if ep.get("seasonNumber") == season:
                        target_ids.append(ep["id"])
                else:
                    target_ids.append(ep["id"])

        if target_ids:
            self._request(
                "sonarr",
                "episode/monitor",
                method="PUT",
                json_body={"episodeIds": target_ids, "monitored": True},
            )
        return target_ids

    def search_targets(
        self,
        app_name: str,
        remote_id: int,
        season: int | None = None,
        episode_ids: list[int] | None = None,
    ) -> int:
        """Trigger search command in Radarr or Sonarr."""
        if app_name == "radarr":
            cmd = {"name": "MoviesSearch", "movieIds": [remote_id]}
        else:
            if episode_ids:
                cmd = {"name": "EpisodeSearch", "episodeIds": episode_ids}
            elif season is not None:
                cmd = {"name": "SeasonSearch", "seriesId": remote_id, "seasonNumber": season}
            else:
                cmd = {"name": "SeriesSearch", "seriesId": remote_id}

        res = self._request(app_name, "command", method="POST", json_body=cmd)
        return res.get("id", 0)
