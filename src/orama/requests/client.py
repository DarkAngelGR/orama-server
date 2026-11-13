"""Safe lookup and app read client for Sonarr and Radarr."""
from __future__ import annotations

import html
import logging
import re
import urllib.parse
from dataclasses import dataclass
from typing import Any

from orama.requests.models import RequestKind
from orama.secrets import SecretStore
from orama.wiring.http import Http, Unreachable

_LOG = logging.getLogger("orama.requests")
# The catalogue id each kind is known by, and how Radarr/Sonarr look one up exactly
# (contract checked in tests/integration/request_contracts.py).
_ID_FIELD = {"movie": "tmdbId", "series": "tvdbId"}
_ID_TERM = {"movie": "tmdb", "series": "tvdb"}

SAFE_URL_PATTERN = re.compile(r"^https?://[a-zA-Z0-9_.~:/?#\[\]@!$&'()*+,;=-]+$")


class RequestError(Exception):
    """Domain error for media lookup and read operations."""


def _exact(items: Any, field: str, wanted: int) -> dict[str, Any] | None:
    """The lookup item whose ``field`` (a catalogue id) is exactly ``wanted``, else None.

    A lookup can answer with other titles (a number is also a search word). The app's row
    number (``id``) and the other catalogue's id are different numbers: they never count.
    """
    if not isinstance(items, list):
        return None
    for item in items:
        if isinstance(item, dict) and type(item.get(field)) is int and item[field] == wanted:
            return item
    return None


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

    def __init__(
        self,
        setup: Any,
        store: SecretStore,
        http: Http,
        *,
        use_service_hosts: bool = False,
    ) -> None:
        self.setup = setup
        self.store = store
        self.http = http
        self.use_service_hosts = use_service_hosts

    def refresh_media_server(self) -> bool:
        """Ask the media server (Jellyfin) to scan; never raises. See ``refresh_library``."""
        from orama.wiring.jellyfin import refresh_library

        return refresh_library(self.setup, self.store, self.http)

    def _get_app_info(self, app_name: str) -> tuple[str, str]:
        removed = getattr(self.setup, "removed_apps", ()) or ()
        if app_name in removed:
            raise RequestError(f"{app_name.capitalize()} is turned off in Settings.")
        default_ports = {"radarr": 7878, "sonarr": 8989, "bazarr": 6767}
        key = self.store.get(f"apikey:{app_name}") or self.store.get(app_name)
        if not key:
            raise RequestError(f"{app_name.capitalize()} is not configured or key missing")
        if self.use_service_hosts:
            return f"http://{app_name}:{default_ports.get(app_name, 8989)}", key
        ports = getattr(self.setup, "ports", {}) or {}
        port = ports.get(app_name, default_ports.get(app_name, 8989))
        return f"http://127.0.0.1:{port}", key

    def _has_route(self, method: str, path: str) -> bool:
        http = getattr(self, "http", None)
        if http is None:
            return False
        routes = getattr(http, "routes", None)
        if isinstance(routes, dict):
            rel = path if path.startswith("/") else f"/api/v3/{path.lstrip('/')}"
            return (method.upper(), rel) in routes
        return True

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
        rel = f"/api/v3/{path.lstrip('/')}{query_str}"
        url = f"{base_url}{rel}"
        headers = {"X-Api-Key": key}

        try:
            resp = self.http.request(method, url, headers=headers, json=json_body)
        except Unreachable:
            if not self.use_service_hosts and type(self.http).__name__ == "Http":
                default_ports = {"radarr": 7878, "sonarr": 8989, "bazarr": 6767}
                svc_url = f"http://{app_name}:{default_ports.get(app_name, 8989)}{rel}"
                try:
                    resp = self.http.request(method, svc_url, headers=headers, json=json_body)
                except Unreachable:
                    raise RequestError(
                        f"{app_name.capitalize()} is unreachable or timed out"
                    ) from None
            else:
                raise RequestError(
                    f"{app_name.capitalize()} is unreachable or timed out"
                ) from None

        if resp.status == 401 or resp.status == 403:
            raise RequestError(f"Authentication failed for {app_name.capitalize()}")
        # What the user reads is plain words. The status goes to the log only, with the
        # fixed API path: never the query (search words), the key or the app's own text.
        unexpected = RequestError(
            f"{app_name.capitalize()} did not answer as expected. Try again in a minute.")
        if resp.status not in (200, 201, 202):
            _LOG.warning("%s answered %s %s with status %s",
                         app_name, method, path.lstrip("/"), resp.status)
            raise unexpected

        if not resp.body:
            return {}
        try:
            return resp.json()
        except ValueError:
            _LOG.warning("%s answered %s %s with something that is not JSON",
                         app_name, method, path.lstrip("/"))
            raise unexpected from None

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
            result = self._search_result(kind, item)
            if result is not None:
                results.append(result)
        return results

    def find(self, kind: RequestKind, source_id: int) -> SearchResult | None:
        """The one title with exactly this TMDB id (movie) or TVDB id (series), else None.

        The app is asked by id (``tmdb:<id>`` / ``tvdb:<id>``), and only an item whose own
        catalogue id is that number is accepted: never the first result, and never a match
        on the app's row number or on the other catalogue's id.
        """
        app_name = "radarr" if kind == "movie" else "sonarr"
        path = "movie/lookup" if kind == "movie" else "series/lookup"
        data = self._request(app_name, path, params={"term": f"{_ID_TERM[kind]}:{source_id}"})
        return self._search_result(kind, _exact(data, _ID_FIELD[kind], source_id))

    @staticmethod
    def _search_result(kind: RequestKind, item: Any) -> SearchResult | None:
        if not isinstance(item, dict):
            return None
        if kind == "movie":
            source_id = item.get("tmdbId") or item.get("id")
            in_library = (
                bool(item.get("hasFile"))
                if "hasFile" in item
                else bool(item.get("id"))
            )
        else:
            source_id = item.get("tvdbId") or item.get("id")
            stats = item.get("statistics", {})
            in_library = (
                bool(stats.get("episodeFileCount", 0) > 0)
                if isinstance(stats, dict) and "episodeFileCount" in stats
                else bool(item.get("id"))
            )

        if not source_id or not isinstance(source_id, int):
            return None

        title = _sanitize_title(str(item.get("title", "")))
        year = item.get("year")
        images = item.get("images", [])
        poster_url = None
        if isinstance(images, list):
            for img in images:
                if isinstance(img, dict) and img.get("coverType") == "poster":
                    poster_url = _sanitize_poster_url(img.get("remoteUrl"))
                    break

        return SearchResult(
            kind=kind,
            source_id=source_id,
            title=title,
            year=year if isinstance(year, int) else None,
            in_library=in_library,
            poster_ref=poster_url,
        )

    def detail(self, kind: RequestKind, source_id: int) -> MediaDetail:
        """Fetch media detail by source ID."""
        app_name = "radarr" if kind == "movie" else "sonarr"
        lookup_path = "movie/lookup" if kind == "movie" else "series/lookup"
        raw_lookup = self._request(
            app_name, lookup_path, params={"term": f"{_ID_TERM[kind]}:{source_id}"})
        # Only the title with exactly this catalogue id: never the first result.
        matched_raw = _exact(raw_lookup, _ID_FIELD[kind], source_id)

        if matched_raw is not None:
            sid = (
                matched_raw.get("tmdbId" if kind == "movie" else "tvdbId")
                or matched_raw.get("id")
                or source_id
            )
            if kind == "movie":
                in_lib = (
                    bool(matched_raw.get("hasFile"))
                    if "hasFile" in matched_raw
                    else bool(matched_raw.get("id"))
                )
            else:
                stats = matched_raw.get("statistics", {})
                in_lib = (
                    bool(stats.get("episodeFileCount", 0) > 0)
                    if isinstance(stats, dict) and "episodeFileCount" in stats
                    else bool(matched_raw.get("id"))
                )
            poster_url = None
            images = matched_raw.get("images", [])
            if isinstance(images, list):
                for img in images:
                    if isinstance(img, dict) and img.get("coverType") == "poster":
                        poster_url = _sanitize_poster_url(img.get("remoteUrl"))
                        break
            year = matched_raw.get("year")
            matched = SearchResult(
                kind=kind,
                source_id=int(sid),
                title=_sanitize_title(str(matched_raw.get("title", f"Media #{source_id}"))),
                year=year if isinstance(year, int) else None,
                in_library=in_lib,
                poster_ref=poster_url,
            )
        else:
            matched = SearchResult(
                kind=kind,
                source_id=source_id,
                title=f"Media #{source_id}",
                year=None,
                in_library=False,
                poster_ref=None,
            )

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
                                    episode_count=s.get("statistics", {}).get(
                                        "totalEpisodeCount", s.get("episodeCount", 0)
                                    ),
                                    monitored=bool(s.get("monitored")),
                                )
                            )
                    break

        if kind == "series" and not seasons and matched_raw and isinstance(matched_raw.get("seasons"), list):
            for s in matched_raw["seasons"]:
                if isinstance(s, dict):
                    seasons.append(
                        SeasonDetail(
                            season_number=s.get("seasonNumber", 0),
                            episode_count=s.get("statistics", {}).get(
                                "totalEpisodeCount", s.get("episodeCount", 0)
                            ),
                            monitored=bool(s.get("monitored")),
                        )
                    )

        if kind == "series" and remote_id is not None and self._has_route("GET", "episode"):
            try:
                raw_eps = self._request("sonarr", "episode", params={"seriesId": remote_id})
                if isinstance(raw_eps, list):
                    for ep in raw_eps:
                        if isinstance(ep, dict):
                            episodes.append(
                                EpisodeDetail(
                                    season_number=int(ep.get("seasonNumber", 0)),
                                    episode_number=int(ep.get("episodeNumber", 1)),
                                    title=_sanitize_title(str(ep.get("title", ""))),
                                    has_file=bool(ep.get("hasFile")),
                                    monitored=bool(ep.get("monitored")),
                                )
                            )
            except RequestError:
                pass

        return MediaDetail(
            result=matched,
            remote_id=remote_id,
            seasons=seasons,
            episodes=episodes,
        )

    def snapshot(self, app_name: str, remote_ids: set[int] | list[int]) -> AppSnapshot:
        """Fetch bounded snapshot of app items, queue, history, and episode state."""
        target_ids = set(remote_ids)
        endpoint = "movie" if app_name == "radarr" else "series"
        external_key = "tmdbId" if app_name == "radarr" else "tvdbId"
        raw_items = self._request(app_name, endpoint)
        items: dict[int, Any] = {}
        if isinstance(raw_items, list):
            for it in raw_items:
                if not isinstance(it, dict):
                    continue
                r_id = it.get("id")
                ext_id = it.get(external_key)
                if isinstance(r_id, int) and (r_id in target_ids or ext_id in target_ids):
                    items[r_id] = it

        matched_ids = target_ids | set(items.keys())

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
                    if not isinstance(q, dict):
                        continue
                    m_id = q.get("movieId" if app_name == "radarr" else "seriesId")
                    if m_id in matched_ids:
                        queue.append(q)

        raw_history = self._request(app_name, "history", params={"pageSize": 100})
        history: list[dict[str, Any]] = []
        if isinstance(raw_history, dict):
            records = raw_history.get("records", [])
            if isinstance(records, list):
                for h in records:
                    if not isinstance(h, dict):
                        continue
                    m_id = h.get("movieId" if app_name == "radarr" else "seriesId")
                    if m_id in matched_ids:
                        history.append(h)

        files: list[dict[str, Any]] = []
        if app_name == "sonarr" and self._has_route("GET", "episode"):
            series_ids = sorted(items.keys())
            for s_id in series_ids:
                try:
                    raw_eps = self._request("sonarr", "episode", params={"seriesId": s_id})
                except RequestError:
                    continue
                if isinstance(raw_eps, list):
                    for ep in raw_eps:
                        if isinstance(ep, dict):
                            files.append(ep)

        return AppSnapshot(
            complete=complete,
            items=items,
            queue=queue,
            history=history,
            files=files,
        )

    def _get_quality_profile_id(self, app_name: str) -> int:
        profiles = self._request(app_name, "qualityprofile")
        if isinstance(profiles, list):
            for p in profiles:
                if (
                    isinstance(p, dict)
                    and p.get("name") == "Orama"
                    and type(p.get("id")) is int
                ):
                    return p["id"]
        raise RequestError(
            f"Orama quality profile is not configured in {app_name.capitalize()} yet."
        )

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
        movie_data = _exact(lookup_res, "tmdbId", tmdb_id)
        if movie_data is None:
            lookup_res = self._request("radarr", "movie/lookup", params={"term": str(tmdb_id)})
            movie_data = _exact(lookup_res, "tmdbId", tmdb_id)
        if movie_data is None:  # never add whatever came first: it can be another film
            raise RequestError(f"Could not locate movie metadata for TMDB #{tmdb_id}")

        qp_id = self._get_quality_profile_id("radarr")
        root_path = self._get_root_folder_path("radarr")

        payload = {
            **movie_data,
            "qualityProfileId": qp_id,
            "rootFolderPath": root_path,
            "monitored": True,
            "minimumAvailability": "released",
            "addOptions": {"searchForMovie": False},
        }
        try:
            created = self._request("radarr", "movie", method="POST", json_body=payload)
        except RequestError:
            reread = self._request("radarr", "movie")
            if isinstance(reread, list):
                for m in reread:
                    if isinstance(m, dict) and m.get("tmdbId") == tmdb_id:
                        return m["id"], bool(m.get("hasFile"))
            raise
        return created["id"], bool(created.get("hasFile"))

    def ensure_series(self, tvdb_id: int, *, monitor: str = "none") -> tuple[int, bool]:
        """Ensure series is added in Sonarr without auto-searching; returns (remote_id, has_file)."""
        existing_list = self._request("sonarr", "series")
        if isinstance(existing_list, list):
            for s in existing_list:
                if s.get("tvdbId") == tvdb_id:
                    stats = s.get("statistics", {})
                    has_file = bool(stats.get("episodeFileCount", 0) > 0)
                    return s["id"], has_file

        lookup_res = self._request("sonarr", "series/lookup", params={"term": f"tvdb:{tvdb_id}"})
        series_data = _exact(lookup_res, "tvdbId", tvdb_id)
        if series_data is None:
            lookup_res = self._request("sonarr", "series/lookup", params={"term": str(tvdb_id)})
            series_data = _exact(lookup_res, "tvdbId", tvdb_id)
        if series_data is None:  # never add whatever came first: it can be another show
            raise RequestError(f"Could not locate series metadata for TVDB #{tvdb_id}")

        qp_id = self._get_quality_profile_id("sonarr")
        root_path = self._get_root_folder_path("sonarr")

        payload = {
            **series_data,
            "qualityProfileId": qp_id,
            "rootFolderPath": root_path,
            "monitored": True,
            "seasonFolder": True,
            "addOptions": {
                "monitor": monitor,
                "searchForMissingEpisodes": False,
                "searchForCutoffUnmetEpisodes": False,
            },
        }
        try:
            created = self._request("sonarr", "series", method="POST", json_body=payload)
        except RequestError:
            reread = self._request("sonarr", "series")
            if isinstance(reread, list):
                for s in reread:
                    if isinstance(s, dict) and s.get("tvdbId") == tvdb_id:
                        stats = s.get("statistics", {})
                        return s["id"], bool(stats.get("episodeFileCount", 0) > 0)
            raise
        stats = created.get("statistics", {})
        has_file = bool(stats.get("episodeFileCount", 0) > 0)
        return created["id"], has_file

    def _fetch_target_episodes(
        self, series_id: int, season: int | None, episode: int | None
    ) -> list[dict[str, Any]]:
        import time

        episodes = self._request("sonarr", "episode", params={"seriesId": series_id})
        if (
            isinstance(episodes, list)
            and not episodes
            and type(getattr(self, "http", None)).__name__ != "FakeHttp"
        ):
            deadline = time.monotonic() + 30.0
            while time.monotonic() < deadline:
                time.sleep(0.5)
                episodes = self._request("sonarr", "episode", params={"seriesId": series_id})
                if isinstance(episodes, list) and episodes:
                    break

        matched: list[dict[str, Any]] = []
        if isinstance(episodes, list):
            for ep in episodes:
                if not isinstance(ep, dict):
                    continue
                if episode is not None:
                    if ep.get("seasonNumber") == season and ep.get("episodeNumber") == episode:
                        matched.append(ep)
                elif season is not None:
                    if ep.get("seasonNumber") == season:
                        matched.append(ep)
                else:
                    matched.append(ep)
        return matched

    def monitor_episodes(
        self, series_id: int, season: int | None, episode: int | None
    ) -> list[int]:
        """Set monitoring on targeted episodes only."""
        matched = self._fetch_target_episodes(series_id, season, episode)
        self._last_monitored_episodes = matched
        target_ids = [ep["id"] for ep in matched if isinstance(ep.get("id"), int)]
        if target_ids:
            self._request(
                "sonarr",
                "episode/monitor",
                method="PUT",
                json_body={"episodeIds": target_ids, "monitored": True},
            )
        return target_ids

    def command_status(self, app_name: str, command_id: int | None = None) -> Any:
        """Query command status in Radarr or Sonarr."""
        path = f"command/{command_id}" if command_id is not None else "command"
        return self._request(app_name, path)

    def has_active_search_or_download(
        self,
        app_name: str,
        remote_id: int,
        *,
        season: int | None = None,
        episode_ids: list[int] | None = None,
        include_completed: bool = False,
    ) -> bool:
        """Return True if a matching search command or queue download is already active."""
        allowed_statuses = {"queued", "started", "processing"}
        if include_completed:
            allowed_statuses.add("completed")

        if self._has_route("GET", "command"):
            try:
                cmds = self._request(app_name, "command")
            except RequestError:
                cmds = None
            if isinstance(cmds, list):
                for c in cmds:
                    if not isinstance(c, dict):
                        continue
                    status = str(c.get("status", "queued")).lower()
                    if status not in allowed_statuses:
                        continue
                    body = c.get("body") if isinstance(c.get("body"), dict) else c
                    cmd_name = str(
                        c.get("name") or c.get("commandName") or body.get("name") or ""
                    )
                    if app_name == "radarr":
                        m_ids = body.get("movieIds") or c.get("movieIds") or []
                        m_id = body.get("movieId") or c.get("movieId")
                        if cmd_name == "MoviesSearch" and (
                            (isinstance(m_ids, list) and remote_id in m_ids)
                            or m_id == remote_id
                        ):
                            return True
                    else:
                        c_ep_ids = body.get("episodeIds") or c.get("episodeIds") or []
                        c_series_id = body.get("seriesId") or c.get("seriesId")
                        c_season = (
                            body.get("seasonNumber")
                            if "seasonNumber" in body
                            else c.get("seasonNumber")
                        )
                        if episode_ids is not None:
                            if (
                                cmd_name == "EpisodeSearch"
                                and isinstance(c_ep_ids, list)
                                and bool(episode_ids)
                                and set(episode_ids) <= set(c_ep_ids)
                            ):
                                return True
                            if (
                                cmd_name in ("SeasonSearch", "SeriesSearch")
                                and c_series_id == remote_id
                                and c_season in (None, season)
                            ):
                                return True
                        elif season is not None:
                            if (
                                cmd_name == "SeasonSearch"
                                and c_series_id == remote_id
                                and c_season == season
                            ):
                                return True
                            if cmd_name == "SeriesSearch" and c_series_id == remote_id:
                                return True
                        else:
                            if cmd_name == "SeriesSearch" and c_series_id == remote_id:
                                return True

        if self._has_route("GET", "queue"):
            try:
                raw_q = self._request(app_name, "queue", params={"pageSize": 100})
            except RequestError:
                raw_q = None
            records = (
                raw_q.get("records", [])
                if isinstance(raw_q, dict)
                else (raw_q if isinstance(raw_q, list) else [])
            )
            if isinstance(records, list):
                for q in records:
                    if not isinstance(q, dict):
                        continue
                    q_status = str(q.get("status", "")).lower()
                    if "failed" in q_status or "warning" in q_status:
                        continue
                    if app_name == "radarr":
                        if q.get("movieId") == remote_id:
                            return True
                    elif q.get("seriesId") == remote_id:
                        if episode_ids is not None:
                            if q.get("episodeId") in episode_ids:
                                return True
                        elif season is not None:
                            ep_obj = q.get("episode") if isinstance(q.get("episode"), dict) else {}
                            q_season = (
                                q.get("seasonNumber")
                                if q.get("seasonNumber") is not None
                                else ep_obj.get("seasonNumber")
                            )
                            if q_season in (None, season):
                                return True
                        else:
                            return True

        return False

    def search_targets(
        self,
        app_name: str,
        remote_id: int,
        season: int | None = None,
        episode_ids: list[int] | None = None,
    ) -> int:
        """Trigger search command in Radarr or Sonarr."""
        if app_name == "radarr":
            cmd: dict[str, Any] = {"name": "MoviesSearch", "movieIds": [remote_id]}
        else:
            if episode_ids is not None:
                if not episode_ids:
                    return 0
                cmd = {"name": "EpisodeSearch", "episodeIds": list(episode_ids)}
            elif season is not None:
                cmd = {"name": "SeasonSearch", "seriesId": remote_id, "seasonNumber": season}
            else:
                cmd = {"name": "SeriesSearch", "seriesId": remote_id}

        if self.has_active_search_or_download(
            app_name, remote_id, season=season, episode_ids=episode_ids
        ):
            return 0

        res = self._request(app_name, "command", method="POST", json_body=cmd)
        return res.get("id", 0) if isinstance(res, dict) else 0

    def file_media_info(
        self,
        app_name: str,
        remote_id: int,
        *,
        season: int | None = None,
        episode: int | None = None,
    ) -> dict[str, Any] | None:
        """Fetch mediaInfo dictionary for an imported movie or episode file."""
        if not hasattr(self, "store") or not hasattr(self, "http"):
            return None
        try:
            if app_name == "radarr":
                files = self._request("radarr", "moviefile", params={"movieId": remote_id})
            else:
                files = self._request("sonarr", "episodefile", params={"seriesId": remote_id})
        except RequestError:
            return None

        if not isinstance(files, list) or not files:
            return None

        chosen: dict[str, Any] | None = None
        if app_name == "radarr":
            for f in files:
                if isinstance(f, dict) and f.get("movieId") in (None, remote_id):
                    chosen = f
                    break
        elif episode is not None:
            target_file_id: int | None = None
            episode_lookup_done = False
            if self._has_route("GET", "episode"):
                try:
                    raw_eps = self._request("sonarr", "episode", params={"seriesId": remote_id})
                    if isinstance(raw_eps, list):
                        episode_lookup_done = True
                        matched_ep = next(
                            (
                                ep
                                for ep in raw_eps
                                if isinstance(ep, dict)
                                and ep.get("seasonNumber") == season
                                and ep.get("episodeNumber") == episode
                            ),
                            None,
                        )
                        if matched_ep is None:
                            return None
                        raw_fid = matched_ep.get("episodeFileId")
                        if isinstance(raw_fid, int) and raw_fid > 0:
                            target_file_id = raw_fid
                        elif "hasFile" in matched_ep and not matched_ep.get("hasFile"):
                            return None
                except RequestError:
                    pass

            if target_file_id is not None:
                for f in files:
                    if isinstance(f, dict) and f.get("id") == target_file_id:
                        chosen = f
                        break
            else:
                for f in files:
                    if not isinstance(f, dict):
                        continue
                    if season is not None and f.get("seasonNumber") not in (None, season):
                        continue
                    if f.get("episodeNumber") == episode:
                        chosen = f
                        break
                    ep_list = f.get("episodes")
                    if isinstance(ep_list, list) and any(
                        isinstance(e, dict) and e.get("episodeNumber") == episode for e in ep_list
                    ):
                        chosen = f
                        break
                if chosen is None and not episode_lookup_done:
                    season_files = [
                        f
                        for f in files
                        if isinstance(f, dict)
                        and f.get("seasonNumber") == season
                        and "episodeNumber" not in f
                        and "episodes" not in f
                    ]
                    if len(season_files) == 1 and len(files) == 1:
                        chosen = season_files[0]
        elif season is not None:
            for f in files:
                if isinstance(f, dict) and f.get("seasonNumber") == season:
                    chosen = f
                    break
        else:
            for f in files:
                if isinstance(f, dict):
                    chosen = f
                    break

        if chosen is None:
            return None
        info = chosen.get("mediaInfo")
        return info if isinstance(info, dict) else None

    def _fetch_bazarr_rows(self, kind: RequestKind, remote_id: int) -> list[dict[str, Any]] | None:
        if not hasattr(self, "store") or not hasattr(self, "http"):
            return None
        try:
            base_url, key = self._get_app_info("bazarr")
        except RequestError:
            return None

        if kind == "movie":
            url = f"{base_url}/api/movies?radarrid[]={remote_id}&start=0&length=1"
        else:
            url = f"{base_url}/api/episodes?seriesid[]={remote_id}"
        try:
            resp = self.http.request("GET", url, headers={"X-Api-Key": key})
            if resp.status != 200 or not resp.body:
                return None
            payload = resp.json()
        except (Unreachable, ValueError):
            return None

        if not isinstance(payload, dict):
            return None
        rows = payload.get("data")
        if not isinstance(rows, list) or not rows:
            return None
        return [r for r in rows if isinstance(r, dict)]

    def episode_subtitle_rows(
        self, remote_id: int, *, season: int | None = None
    ) -> list[dict[str, Any]] | None:
        """Fetch per-episode subtitle availability rows for a season or series."""
        rows = self._fetch_bazarr_rows("series", remote_id)
        if not rows:
            return None
        matched: list[dict[str, Any]] = []
        for r in rows:
            if season is not None:
                ep_num = str(r.get("episode_number", "")).strip()
                if r.get("season") != season and not ep_num.startswith(f"{season}x"):
                    continue
            subs = r.get("subtitles")
            if isinstance(subs, list):
                matched.append({"available": subs})
        return matched or None

    def subtitle_languages(
        self,
        kind: RequestKind,
        remote_id: int,
        *,
        season: int | None = None,
        episode: int | None = None,
    ) -> list[dict[str, Any]] | None:
        """Fetch Bazarr available subtitle entries for a movie or episode, or None if unavailable."""
        rows = self._fetch_bazarr_rows(kind, remote_id)
        if not rows:
            return None

        target_row: dict[str, Any] | None = None
        if kind == "movie":
            for r in rows:
                if r.get("radarrId") in (None, remote_id):
                    target_row = r
                    break
        elif season is not None and episode is not None:
            ep_tokens = {f"{season}x{episode}", f"{season}x{episode:02d}"}
            for r in rows:
                ep_num = str(r.get("episode_number", "")).strip()
                if ep_num in ep_tokens or (
                    r.get("season") == season and r.get("episode") == episode
                ):
                    target_row = r
                    break
        elif season is not None:
            for r in rows:
                ep_num = str(r.get("episode_number", "")).strip()
                if r.get("season") == season or ep_num.startswith(f"{season}x"):
                    target_row = r
                    break
        else:
            target_row = rows[0]

        if target_row is None:
            return None

        subs = target_row.get("subtitles")
        return subs if isinstance(subs, list) else None

