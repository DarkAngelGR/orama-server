"""recyclarr-sync: the Agent renders Recyclarr's YAML itself from validated choices.

Orama sends only template names and custom-format scores per app. Everything else in the
file is fixed here: the base URLs, the quality profile each template stands for, the
profile's name in the app, and the API keys, which the Agent reads from its own env files
(never from the request). The YAML is Recyclarr 8.8.1's config schema (v8):

- ``quality_profiles: [{trash_id, name, reset_unmatched_scores}]`` makes a guide-backed
  profile (qualities and TRaSH scores from the guide). It is always named ``Orama``, one
  per app, whatever the template: a quality change updates the same profile instead of
  leaving an old one behind. Resetting unmatched scores is what TRaSH's own templates do;
  it also clears a score Orama no longer sends (device or quality switch).
- ``custom_format_groups.add`` lists the groups the TRaSH template lists.
- ``custom_formats: [{trash_ids, assign_scores_to: [{name, score}]}]`` sets Orama's device
  scores in Orama's profiles only; one entry per distinct score.
- ``delete_old_custom_formats: false``: custom formats the owner made are never deleted.
- ``reset_unmatched_scores`` resets only the scores inside the ``Orama`` profile: a custom
  format score the owner set in that profile by hand is reset too, while the owner's own
  profiles and custom formats are left alone.

Recyclarr's guide data is pinned. The Agent also writes Recyclarr's ``settings.yml`` (fixed
content, no secrets) whose ``resource_providers`` fetch the TRaSH guides and the config
templates at the commits below, replacing Recyclarr's unpinned default copies (Recyclarr
8.8.1: ``GitResourceProvider.reference`` is passed to ``git fetch --depth 1``, which accepts a
commit). Every id in ``TEMPLATES`` and in ``orama/profiles/trash.yaml`` was checked against
these commits. Updating them is a code change, and it changes the digest, so installs sync again.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

import yaml

from orama.catalog import CATALOG
from orama_agent.validate import Refused

CONFIG_FILE = "recyclarr.yml"  # in <config>/config/recyclarr, deleted after every run
STATE_FILE = "recyclarr.state"  # in app-env: digest of the last config that synced
CONTAINER_CONFIG = f"/config/{CONFIG_FILE}"
SETTINGS_FILE = "settings.yml"  # Recyclarr's settings, in the same folder; deleted after
SETTINGS_ALTERNATE = "settings.yaml"  # Recyclarr reads this spelling too (and refuses both)
TRASH_GUIDES_COMMIT = "d0f1a6cb29bee8b53bd1d7f8c73333649972f21c"  # TRaSH-Guides/Guides master
CONFIG_TEMPLATES_COMMIT = "89eebadd429952069182a22930af4d768cacda36"  # config-templates master
MAX_SCORES = 200
PROFILE_NAME = "Orama"  # the one quality profile Orama manages in each app
MIN_SCORE, MAX_SCORE = -10000, 10000
_TEMPLATE_NAME = re.compile(r"[a-z0-9-]{1,80}")
_TRASH_ID = re.compile(r"[0-9a-f]{32}")


@dataclass(frozen=True)
class Template:
    """One TRaSH config template as Recyclarr's config-templates repository defines it."""

    trash_id: str  # the guide quality profile
    groups: tuple[str, ...]  # custom format groups the template adds


@dataclass(frozen=True)
class _App:
    base_url: str
    key_env: str
    quality_definition: str


APPS = {
    "radarr": _App("http://radarr:7878", "RADARR__AUTH__APIKEY", "movie"),
    "sonarr": _App("http://sonarr:8989", "SONARR__AUTH__APIKEY", "series"),
}

# Copied from recyclarr/config-templates (master, schema v8), templates/<name>.yml: the
# quality profile trash_id and the uncommented `custom_format_groups.add` entries. These are
# the Plan 2b notes' quality candidates. Public TRaSH ids, not secrets.
TEMPLATES: dict[str, dict[str, Template]] = {
    "radarr": {
        "hd-bluray-web": Template(
            "d1d67249d3890e49bc12e275d989a7e9",
            ("f8bf8eab4617f12dfdbd16303d8da245", "a3ac6af01d78e4f21fcb75f601ac96df"),
        ),
        "uhd-bluray-web": Template(
            "64fb5f9858489bdac2af690e27c8f42f",
            ("ff204bbcecdd487d1cefcefdbf0c278d", "a3ac6af01d78e4f21fcb75f601ac96df"),
        ),
        "remux-web-2160p": Template(
            "fd161a61e3ab826d3a22d53f935696dd",
            ("ff204bbcecdd487d1cefcefdbf0c278d", "a3ac6af01d78e4f21fcb75f601ac96df"),
        ),
    },
    "sonarr": {
        "web-1080p": Template(
            "72dae194fc92bf828f32cde7744e51a1",
            ("158188097a58d7687dee647e04af0da3", "74aff4168620ed49dcc67e92b2c2a5b4",
             "85fae4a2294965b75710ef2989c850eb", "59c3af66780d08332fdc64e68297098f"),
        ),
        "web-2160p": Template(
            "d1498e7d189fbe6c7110ceaabb7473e6",
            ("e3f37512790f00d0e89e54fe5e790d1c", "74aff4168620ed49dcc67e92b2c2a5b4",
             "85fae4a2294965b75710ef2989c850eb", "59c3af66780d08332fdc64e68297098f"),
        ),
        "remux-web-2160p": Template(
            "76a5053bdb2d1e4a8f16a69a37d46c12",
            ("e3f37512790f00d0e89e54fe5e790d1c", "74aff4168620ed49dcc67e92b2c2a5b4",
             "85fae4a2294965b75710ef2989c850eb", "59c3af66780d08332fdc64e68297098f"),
        ),
    },
}

SETTINGS: dict[str, Any] = {
    "resource_providers": [
        {"name": "orama-trash-guides", "type": "trash-guides",
         "clone_url": "https://github.com/TRaSH-Guides/Guides.git",
         "reference": TRASH_GUIDES_COMMIT, "replace_default": True},
        {"name": "orama-config-templates", "type": "config-templates",
         "clone_url": "https://github.com/recyclarr/config-templates.git",
         "reference": CONFIG_TEMPLATES_COMMIT, "replace_default": True},
    ],
}

Apps = dict[str, dict[str, Any]]  # {app: {"templates": [str], "scores": {trash_id: int}}}


def settings_text() -> str:
    """Recyclarr's settings.yml: fixed, and holding no secret."""
    return yaml.safe_dump(SETTINGS, sort_keys=False)


def _expect(value: Any, keys: set[str], what: str) -> None:
    if not isinstance(value, dict) or set(value) != keys:
        raise Refused(f"{what} must be an object with exactly: {', '.join(sorted(keys))}.")


def check(args: Any) -> tuple[Apps, bool]:
    """The request's apps (validated copy) and whether to run even if nothing changed."""
    if not isinstance(args, dict) or not ({"apps"} <= set(args) <= {"apps", "force"}):
        raise Refused("This command takes apps (and optionally force).")
    force = args.get("force", False)
    if not isinstance(force, bool):
        raise Refused("force must be true or false.")
    apps = args["apps"]
    if not isinstance(apps, dict) or not apps:
        raise Refused("apps must name Sonarr, Radarr or both.")
    out: Apps = {}
    for app, spec in apps.items():
        if app not in APPS:
            raise Refused("Recyclarr only manages Sonarr and Radarr.")
        _expect(spec, {"templates", "scores"}, f"The {app} choice")
        out[app] = {
            "templates": _check_templates(app, spec["templates"]),
            "scores": _check_scores(spec["scores"]),
        }
    return out, force


def _check_templates(app: str, templates: Any) -> list[str]:
    if not isinstance(templates, list) or len(templates) != 1:
        raise Refused("Choose exactly one quality template per app.")
    for name in templates:
        if not isinstance(name, str) or not _TEMPLATE_NAME.fullmatch(name):
            raise Refused("A quality template name is not valid.")
        if name not in TEMPLATES[app]:
            raise Refused(f"The quality template {name!r} is not one Orama uses for {app}.")
    return list(templates)


def _check_scores(scores: Any) -> dict[str, int]:
    if not isinstance(scores, dict):
        raise Refused("Scores must be an object of custom format id to score.")
    if len(scores) > MAX_SCORES:
        raise Refused(f"At most {MAX_SCORES} scores per app.")
    for trash_id, score in scores.items():
        if not isinstance(trash_id, str) or not _TRASH_ID.fullmatch(trash_id):
            raise Refused("A custom format id is not valid.")
        if (not isinstance(score, int) or isinstance(score, bool)
                or not MIN_SCORE <= score <= MAX_SCORE):
            raise Refused(f"Scores must be whole numbers from {MIN_SCORE} to {MAX_SCORE}.")
    return dict(scores)


def render(apps: Apps, keys: dict[str, str]) -> dict[str, Any]:
    """The whole Recyclarr config for ``apps``; ``keys`` are the apps' API keys."""
    doc: dict[str, Any] = {}
    for app in sorted(apps):
        spec, fixed = apps[app], APPS[app]
        template = TEMPLATES[app][spec["templates"][0]]
        instance: dict[str, Any] = {
            "base_url": fixed.base_url,
            "api_key": keys[app],
            "delete_old_custom_formats": False,
            "quality_definition": {"type": fixed.quality_definition},
            "quality_profiles": [{
                "trash_id": template.trash_id,
                "name": PROFILE_NAME,
                "reset_unmatched_scores": {"enabled": True},
            }],
            "custom_format_groups": {"add": [{"trash_id": g} for g in template.groups]},
        }
        by_score: dict[int, list[str]] = {}
        for trash_id, score in spec["scores"].items():
            by_score.setdefault(score, []).append(trash_id)
        if by_score:
            instance["custom_formats"] = [
                {"trash_ids": sorted(by_score[score]),
                 "assign_scores_to": [{"name": PROFILE_NAME, "score": score}]}
                for score in sorted(by_score)
            ]
        doc[app] = {f"orama-{app}": instance}
    return doc


def digest(apps: Apps) -> str:
    """Identifies what a sync would do, without the keys: the rendered config with the
    keys left out, the pinned guide commits and the pinned Recyclarr image."""
    doc = render(apps, {app: "" for app in apps})
    text = json.dumps(
        {"image": CATALOG["recyclarr"].image, "config": doc, "settings": SETTINGS},
        sort_keys=True,
    )
    return hashlib.sha256(text.encode()).hexdigest()
