"""The ordered list of wiring steps for the apps a setup turns on."""

from __future__ import annotations

from typing import Any

from orama.setup_model import DesiredSetup, enabled_apps
from orama.wiring.arr import arr_steps
from orama.wiring.engine import Step
from orama.wiring.http import Http
from orama.wiring.prowlarr import prowlarr_steps
from orama.wiring.qbittorrent import qbittorrent_steps
from orama.wiring.sabnzbd import sabnzbd_steps


def build_steps(setup: DesiredSetup, store: Any, http: Http) -> list[Step]:
    """Steps for the enabled apps only: SABnzbd, qBittorrent, Sonarr, Radarr, Prowlarr."""
    apps = enabled_apps(setup)
    steps: list[Step] = []
    if "sabnzbd" in apps:
        steps += sabnzbd_steps(setup, store, http)
    if "qbittorrent" in apps:
        steps += qbittorrent_steps(setup, store, http)
    if "sonarr" in apps:
        steps += arr_steps("sonarr", setup, store, http)
    if "radarr" in apps:
        steps += arr_steps("radarr", setup, store, http)
    if "prowlarr" in apps:
        steps += prowlarr_steps(setup, store, http)
    return steps
