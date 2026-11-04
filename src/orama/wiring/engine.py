"""Step engine: runs wiring steps in order, with dependencies and plain results."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from orama.wiring.http import Unreachable

APP_NAMES = {
    "sabnzbd": "SABnzbd",
    "qbittorrent": "qBittorrent",
    "sonarr": "Sonarr",
    "radarr": "Radarr",
    "prowlarr": "Prowlarr",
}


def display_name(app: str) -> str:
    return APP_NAMES.get(app, app)


class StepError(Exception):
    """A step failed; the message is plain language and safe to show."""


@dataclass
class StepResult:
    name: str
    app: str
    status: Literal["changed", "unchanged", "failed", "skipped"]
    message: str


@dataclass
class Step:
    name: str
    app: str
    run: Callable[[], str | None]
    needs: tuple[str, ...] = ()


def run_steps(steps: list[Step]) -> list[StepResult]:
    results: list[StepResult] = []
    bad: set[str] = set()  # names of failed or skipped steps
    for step in steps:
        blocker = next((n for n in step.needs if n in bad), None)
        if blocker is not None:
            blocking_app = next((s.app for s in steps if s.name == blocker), step.app)
            results.append(
                StepResult(step.name, step.app, "skipped", f"waiting for {display_name(blocking_app)}")
            )
            bad.add(step.name)
            continue
        try:
            message = step.run()
        except StepError as err:
            results.append(StepResult(step.name, step.app, "failed", str(err)))
            bad.add(step.name)
        except Exception as err:  # noqa: BLE001  never include err text: may carry secrets
            results.append(
                StepResult(
                    step.name,
                    step.app,
                    "failed",
                    f"unexpected error in {display_name(step.app)} ({type(err).__name__})",
                )
            )
            bad.add(step.name)
        else:
            if message is None:
                results.append(StepResult(step.name, step.app, "unchanged", ""))
            else:
                results.append(StepResult(step.name, step.app, "changed", message))
    return results


def wait_until(
    check: Callable[[], bool],
    *,
    app: str,
    timeout: float,
    interval: float = 3.0,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    deadline = clock() + timeout
    while True:
        try:
            if check():
                return
        except Unreachable:
            pass
        if clock() >= deadline:
            raise StepError(
                f"{display_name(app)} is still starting — run apply again in a few minutes."
            )
        sleep(interval)


def format_checklist(results: list[StepResult]) -> list[str]:
    lines: list[str] = []
    for r in results:
        name = display_name(r.app)
        if r.status == "changed":
            lines.append(f"✓ {name}: {r.message}")
        elif r.status == "failed":
            lines.append(f"✗ {name}: {r.message}")
        elif r.status == "skipped":
            lines.append(f"… {name}: skipped, {r.message}")
    if not any(r.status in ("changed", "failed") for r in results):
        return ["✓ Nothing to change"]
    return lines
