"""The ordered list of wiring steps for the apps a setup turns on."""

from __future__ import annotations

from typing import Any

from orama.setup_model import DesiredSetup, enabled_apps
from orama.wiring.arr import arr_steps
from orama.wiring.bazarr import bazarr_steps
from orama.wiring.engine import Step
from orama.wiring.http import Http
from orama.wiring.jellyfin import jellyfin_steps
from orama.wiring.plex import plex_steps
from orama.wiring.prowlarr import prowlarr_steps
from orama.wiring.qbittorrent import qbittorrent_steps
from orama.wiring.recyclarr import recyclarr_steps
from orama.wiring.sabnzbd import sabnzbd_steps


def build_steps(setup: DesiredSetup, store: Any, http: Http, agent: Any) -> list[Step]:
    """Steps for the enabled apps only, in dependency order.

    SABnzbd, qBittorrent, Sonarr, Radarr, Prowlarr, Bazarr, Recyclarr, Plex, Jellyfin, then
    the Sonarr/Radarr notifications that tell a media server to refresh (they need the media
    servers' steps). A Plex still waiting for the owner's sign-in skips only the steps that
    need Plex.
    """
    apps = enabled_apps(setup)
    steps: list[Step] = []
    if "sabnzbd" in apps:
        steps += sabnzbd_steps(setup, store, http)
    if "qbittorrent" in apps:
        steps += qbittorrent_steps(setup, store, http)
    notify: list[Step] = []
    for app in ("sonarr", "radarr"):
        if app in apps:
            own = arr_steps(app, setup, store, http)
            steps += [s for s in own if ":notify:" not in s.name]
            notify += [s for s in own if ":notify:" in s.name]
    if "prowlarr" in apps:
        steps += prowlarr_steps(setup, store, http)
    if "bazarr" in apps:
        steps += bazarr_steps(setup, store, http)
    steps += recyclarr_steps(setup, store, http, agent)
    if "plex" in apps:
        steps += plex_steps(setup, store, http)
    if "jellyfin" in apps:
        steps += jellyfin_steps(setup, store, http)
    return steps + notify
