"""Compile a (device, quality) choice into per-app Recyclarr templates and custom-format scores."""

from __future__ import annotations

from importlib import resources
from typing import TypedDict

import yaml

APPS = ("radarr", "sonarr")
UNPLAYABLE_SCORE = -10000
PREFERRED_SCORE = 1500


class AppRecyclarr(TypedDict):
    templates: list[str]
    scores: dict[str, int]


def _load(*parts: str) -> dict:
    node = resources.files("orama.profiles")
    for part in parts:
        node = node.joinpath(part)
    data = yaml.safe_load(node.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise TypeError(f"profile data {'/'.join(parts)} is not a mapping")
    return data


def device_names() -> list[str]:
    return sorted(
        p.name.removesuffix(".yaml")
        for p in resources.files("orama.profiles").joinpath("devices").iterdir()
        if p.name.endswith(".yaml")
    )


def quality_names() -> list[str]:
    return sorted(_load("trash.yaml")["templates"])


def compile_profile(device: str, quality: str) -> dict[str, AppRecyclarr]:
    if device not in device_names():
        raise ValueError(f"Unknown device '{device}'. Choose one of: {', '.join(device_names())}.")
    trash = _load("trash.yaml")
    if quality not in trash["templates"]:
        raise ValueError(
            f"Unknown quality '{quality}'. Choose one of: {', '.join(quality_names())}."
        )
    dev = _load("devices", f"{device}.yaml")
    out: dict[str, AppRecyclarr] = {}
    for app in APPS:
        names = trash[app]
        scores: dict[str, int] = {}
        # Preferred first, unplayable last: a format listed in both stays blocked.
        for symbols, value in (
            (dev["preferred"], PREFERRED_SCORE),
            (dev["unplayable"], UNPLAYABLE_SCORE),
        ):
            for symbol in symbols:
                if symbol not in names:
                    raise ValueError(f"Device '{device}' uses unknown format '{symbol}' for {app}.")
                for trash_id in names[symbol]:
                    scores[trash_id] = value
        out[app] = {
            "templates": [trash["templates"][quality][app]],
            "scores": dict(sorted(scores.items())),
        }
    return out
