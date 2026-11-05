"""The setup wizard: one page per step, editing ``setup.draft.json``.

The draft is the installer's ``setup.draft.json``; when only ``setup.json`` exists the first
save copies it into a draft. The saved setup is never changed here. The step list is data:
a step is "ready" (linked) once a page for ``/setup/<key>`` is registered, so later tasks
add their steps by adding the page.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlencode

from flask import Blueprint, Flask, current_app, redirect, render_template, request

from orama import state
from orama.setup_model import DesiredSetup
from orama.web import auth, forms

SETUP_FILE = "setup.json"
DRAFT_FILE = "setup.draft.json"


@dataclass(frozen=True)
class Step:
    key: str
    label: str


# Spec order. A step without a page yet (indexers and review) is shown but not linked.
STEPS: tuple[Step, ...] = (
    Step("password", "Password"),
    Step("media", "Media servers"),
    Step("folders", "Folders"),
    Step("downloads", "Downloads and VPN"),
    Step("indexers", "Indexers"),
    Step("playback", "Playback"),
    Step("subtitles", "Subtitles"),
    Step("review", "Review"),
)

# What each section page says above its form.
_SECTION_STEPS = ("media", "folders", "downloads", "playback", "subtitles")


def _is_ready(key: str) -> bool:
    if key == "password":
        return True
    path = f"/setup/{key}"
    return any(rule.rule == path for rule in current_app.url_map.iter_rules())


def step_list(current: str) -> list[dict[str, Any]]:
    """Every step with its state: done, current or next, and whether it has a page."""
    at = [s.key for s in STEPS].index(current)
    rows = []
    for i, s in enumerate(STEPS):
        ready = _is_ready(s.key)
        rows.append({
            "key": s.key,
            "label": s.label,
            "ready": ready,
            "state": "done" if i < at else "current" if i == at else "next",
            "url": f"/setup/{s.key}" if ready and s.key != "password" else None,
        })
    return rows


def _neighbour(key: str, step: int) -> str | None:
    """The nearest ready step before (-1) or after (+1) ``key``, as a path."""
    keys = [s.key for s in STEPS]
    i = keys.index(key) + step
    while 0 <= i < len(keys):
        if keys[i] != "password" and _is_ready(keys[i]):
            return f"/setup/{keys[i]}"
        i += step
    return None


def _working_setup(st) -> DesiredSetup | None:
    """The draft, else the saved setup (to be copied into a draft on the first save)."""
    for name in (DRAFT_FILE, SETUP_FILE):
        try:
            return DesiredSetup.load(st.state_dir / name)
        except FileNotFoundError:
            continue
    return None


def register_wizard(app: Flask) -> None:
    app.register_blueprint(_wizard_pages())


def _wizard_pages() -> Blueprint:
    pages = Blueprint("wizard", __name__)

    @pages.get("/setup")
    def index():
        st = auth.web_state()
        if st.passwords.exists():
            # Once a password exists the code means nothing; the wizard asks to sign in.
            return redirect("/setup/media", 303)
        code = request.args.get("code", "")
        # The code is neither checked nor shown here; the password page checks it.
        target = "/setup/password" + ("?" + urlencode({"code": code}, quote_via=quote) if code else "")
        return redirect(target, 303)

    def render_step(key: str, values: dict[str, Any], errors: dict[str, str], status: int):
        st = auth.web_state()
        step = next(s for s in STEPS if s.key == key)
        keys = [s.key for s in STEPS]
        applied = (st.state_dir / SETUP_FILE).exists()
        first_time = key == "downloads" and _first_apply_due(st)
        return render_template(
            f"wizard/{key}.html",
            step=step, steps=step_list(key), position=keys.index(key) + 1,
            values=values, errors=errors, saved=request.args.get("saved") == "1",
            back_url=_neighbour(key, -1), applied=applied, first_time=first_time,
        ), status

    def make_view(key: str):
        read, echo, apply = forms.SECTIONS[key]

        @auth.requires("setup")
        def view():
            st = auth.web_state()
            setup = _working_setup(st)
            if setup is None:
                return render_template(
                    "rejected.html",
                    message="Orama has no setup to change yet. Run the installer again, "
                            "then open the link it prints.",
                ), 409
            if request.method == "GET":
                return render_step(key, read(setup, st.store), {}, 200)
            try:
                with state.locked(state.setup_lock(st.state_dir)):
                    # Read again under the lock so a save made meanwhile is kept.
                    current = _working_setup(st) or setup
                    updated = apply(current, request.form, st.store)
                    updated.save(st.state_dir / DRAFT_FILE)
            except forms.Invalid as e:
                return render_step(key, echo(setup, request.form, st.store), e.errors, 400)
            if key == "downloads" and _first_apply_due(st):
                st.jobs.start(st.state_dir / DRAFT_FILE)
                return redirect("/setup/starting", 303)
            return redirect(_after(key), 303)

        view.__name__ = f"step_{key}"
        return view

    for key in _SECTION_STEPS:
        pages.add_url_rule(f"/setup/{key}", endpoint=f"step_{key}",
                           view_func=make_view(key), methods=["GET", "POST"])

    @pages.get("/setup/starting")
    @auth.requires("setup")
    def starting():
        st = auth.web_state()
        job = st.jobs.current()
        return render_template(
            "wizard/starting.html", job=job, running=bool(job and job.status == "running"),
            next_url=_neighbour("downloads", 1),
        )

    return pages


def _first_apply_due(st) -> bool:
    """True before the first apply: nothing saved as the real setup and no job ever run."""
    return not (st.state_dir / SETUP_FILE).exists() and st.jobs.current() is None


def _after(key: str) -> str:
    following = _neighbour(key, 1)
    return following if following is not None else f"/setup/{key}?saved=1"
