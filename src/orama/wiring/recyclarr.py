"""The recyclarr:sync step: the device and quality choice, synced by the Agent, then checked.

Orama compiles the setup's device and quality into templates and custom-format scores
(``orama.profiles``) and hands them to the Agent's ``recyclarr-sync``; the Agent renders
Recyclarr's config and runs it. Orama then reads each app's custom formats and quality
profiles and reports a change only once every score it sent is set in the app's quality
profile named "Orama". Orama only reads here: Recyclarr does
every write, so custom formats and profiles the owner made are never touched by Orama.
Scores Orama stopped sending are cleared by Recyclarr (``reset_unmatched_scores``); they
are not checked here, since a leftover block cannot be told apart from TRaSH's own score.
That reset covers every score inside the "Orama" profile, so a score the owner changed by
hand in that profile goes back to the guide's; the owner's other profiles are untouched.
The guide data Recyclarr uses is pinned to fixed commits by the Agent.
"""

from __future__ import annotations

from typing import Any

from orama.agent_client import AgentError
from orama.profiles.compile import compile_profile, load_trash
from orama.setup_model import DesiredSetup, enabled_apps
from orama.wiring.engine import Step, StepError, display_name
from orama.wiring.http import Http, Unreachable

PORTS = {"sonarr": 8989, "radarr": 7878}  # also the order apps are named in messages
PROFILE_NAME = "Orama"  # the one quality profile the Agent's Recyclarr config manages

# TRaSH custom-format names for each symbolic name in profiles/trash.yaml, in the same
# order as its ids (Plan 2b notes, observed in CI). Recyclarr creates the formats under
# these names, so they find the formats in the apps.
FORMAT_NAMES: dict[str, tuple[str, ...]] = {
    "dv": ("DV (Disk)", "DV Boost"),
    "dv_no_fallback": ("DV (w/o HDR fallback)",),
    "dts": ("DTS",),
    "dts_hd_ma": ("DTS-HD MA",),
    "dts_x": ("DTS X",),
    "dts_hd_hra": ("DTS-HD HRA",),
    "dts_es": ("DTS-ES",),
    "truehd": ("TrueHD",),
    "truehd_atmos": ("TrueHD ATMOS",),
    "pcm_multichannel": ("PCM",),
    "flac": ("FLAC",),
    "hdr10": ("HDR",),
    "hdr10_plus": ("HDR10+ Boost",),
}


def format_names(app: str) -> dict[str, str]:
    """trash_id -> custom-format name, for every format Orama scores in ``app``."""
    names: dict[str, str] = {}
    for symbol, ids in load_trash()[app].items():
        labels = FORMAT_NAMES[symbol]
        if len(labels) != len(ids):
            raise ValueError(f"profiles/trash.yaml {app}.{symbol} and FORMAT_NAMES differ")
        names.update(zip(ids, labels, strict=True))
    return names


def recyclarr_steps(setup: DesiredSetup, store: Any, http: Http, agent: Any) -> list[Step]:
    present = [app for app in PORTS if app in enabled_apps(setup)]
    if not present:
        return []

    def read(app: str, path: str) -> Any:
        name = display_name(app)
        key = store.get(f"apikey:{app}")
        if key is None:
            raise StepError(f"The {name} API key is missing — run apply again.")
        url = f"http://{app}:{PORTS[app]}/api/v3/{path}"
        try:
            resp = http.request("GET", url, headers={"X-Api-Key": key})
        except Unreachable:
            raise StepError(f"{name} could not be reached — run apply again.") from None
        if resp.status in (401, 403):
            raise StepError(f"{name} did not accept Orama's API key — run apply again.")
        if resp.status >= 300:
            raise StepError(f"{name} refused a request (HTTP {resp.status}) — run apply again.")
        try:
            data = resp.json()
        except ValueError:
            raise StepError(f"{name} gave an answer Orama could not read.") from None
        if not isinstance(data, list):
            raise StepError(f"{name} gave an answer Orama could not read.")
        return data

    def in_place(app: str, spec: dict[str, Any]) -> bool:
        """Every score Orama sent is set in the app's Orama profile."""
        names = format_names(app)
        ids = {f.get("name"): f.get("id") for f in read(app, "customformat")
               if isinstance(f, dict)}
        profile = next((p for p in read(app, "qualityprofile")
                        if isinstance(p, dict) and p.get("name") == PROFILE_NAME), None)
        if profile is None:
            return False
        scores = {i.get("format"): i.get("score") for i in profile.get("formatItems") or []
                  if isinstance(i, dict)}
        for trash_id, score in spec["scores"].items():
            name = names.get(trash_id)
            if name not in ids or scores.get(ids[name]) != score:
                return False
        return True

    def sync() -> str | None:
        profile = compile_profile(setup.device, setup.quality)
        wanted = {app: profile[app] for app in present}
        missing = [app for app in present if not in_place(app, wanted[app])]
        try:
            ran = agent.recyclarr_sync(wanted, force=bool(missing))
        except AgentError as err:  # the Agent's messages are plain and safe to show
            raise StepError(err.message) from None
        still = [app for app in present if not in_place(app, wanted[app])]
        if still:
            apps = " and ".join(display_name(a) for a in still)
            verb = "do" if len(still) > 1 else "does"
            raise StepError(f"{apps} {verb} not have Orama's quality scores yet — run apply again.")
        if not (ran or missing):
            return None
        return "set the quality profiles for your playback device"

    return [Step("recyclarr:sync", "recyclarr", sync,
                 needs=tuple(f"wait:{app}" for app in present))]
