"""The setup wizard: one page per step, editing ``setup.draft.json``.

The draft is the installer's ``setup.draft.json``; when only ``setup.json`` exists the first
save copies it into a draft. The draft becomes ``setup.json`` (see :func:`_promote`) only when
an apply starts from the wizard: the first one (after Downloads) and Install. The step list is data:
a step is "ready" (linked) once a page for ``/setup/<key>`` is registered, so later tasks
add their steps by adding the page.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlencode

from flask import Blueprint, Flask, abort, current_app, redirect, render_template, request, url_for

from orama import state
from orama.secrets import JELLYFIN_USER_NAME, PLEX_TOKEN_NAME, VPN_KEY_NAME
from orama.setup_model import DesiredSetup
from orama.web import auth, catalogue, forms
from orama.web.jobs import TEST_BUSY, apply_operation
from orama.wiring import jellyfin, prowlarr, sabnzbd

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


def _promote(st, setup: DesiredSetup) -> None:
    """Make ``setup`` the real setup and drop the draft. The caller holds ``setup.lock``."""
    setup.save(st.state_dir / SETUP_FILE)  # atomic: temp file and rename
    (st.state_dir / DRAFT_FILE).unlink(missing_ok=True)


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
                try:
                    with apply_operation(st):
                        if _first_apply_due(st):
                            # Promote the draft, so "Apply again" has a setup to apply
                            # even when this first apply fails.
                            with state.locked(state.setup_lock(st.state_dir)):
                                draft = _working_setup(st)
                                if draft is not None:
                                    _promote(st, draft)
                            st.jobs.start(st.state_dir / SETUP_FILE)
                except state.Busy:
                    return render_template("rejected.html", message=TEST_BUSY), 409
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
        # The page polls itself, so the Continue button appears when the job ends.
        return render_template(
            "_job_poll.html" if request.headers.get("HX-Request") == "true"
            else "wizard/starting.html",
            job=job, running=bool(job and job.status == "running"),
            next_url=_neighbour("downloads", 1), starting=True, poll_url="/setup/starting",
        )

    def render_indexers(setup, message="", problem=False, status=200):
        st = auth.web_state()
        query = request.args.get("q", "")[:100]
        definition = request.values.get("definition", "")
        edit_slug = request.args.get("edit") or request.form.get("edit")
        editing = next((item for item in setup.indexers if item.slug == edit_slug), None)
        if editing:
            definition = editing.definition
        server_slug = request.args.get("edit-usenet") or request.form.get("edit-usenet")
        editing_server = next((item for item in setup.usenet_servers if item.slug == server_slug), None)
        try:
            job = st.jobs.current()
            if job and job.status == "running":
                raise catalogue.Unavailable
            definitions = catalogue.load(st.store, st.http, setup.download_methods)
            waiting = False
        except catalogue.Unavailable:
            definitions = []
            waiting = True
        selected = next(
            (row for row in definitions if row.definition.casefold() == definition.casefold()), None,
        )
        matches = [row for row in definitions if query.casefold() in row.name.casefold()][:50]
        fragment = request.method == "GET" and request.headers.get("HX-Request") == "true"
        return render_template(
            "wizard/_indexer_form.html" if fragment else "wizard/indexers.html",
            setup=setup, steps=step_list("indexers"), definitions=matches, selected=selected,
            step=next(s for s in STEPS if s.key == "indexers"),
            position=next(i for i, s in enumerate(STEPS, 1) if s.key == "indexers"),
            query=query, definition=definition, waiting=waiting, message=message,
            editing=editing, editing_server=editing_server,
            saved_fields={field for field in editing.secret_fields
                          if st.store.get(f"indexer:{editing.slug}:{field}") is not None}
            if editing else set(),
            server_password_saved=bool(editing_server and
                                       st.store.get(f"usenet:{editing_server.slug}:password")),
            problem=problem, back_url=_neighbour("indexers", -1),
            next_url=_neighbour("indexers", 1),
            tested_indexers={i.slug for i in setup.indexers
                             if st.store.get(f"test-ok:indexer:{i.slug}") == "1"},
            tested_servers={s.slug for s in setup.usenet_servers
                            if st.store.get(f"test-ok:usenet:{s.slug}") == "1"},
        ), status

    def change_entry(kind: str, action: str, slug: str = ""):
        st = auth.web_state()
        setup = _working_setup(st)
        if setup is None:
            abort(409)
        definitions = None
        try:
            if kind == "indexer" and action == "add":
                # Up to a few seconds of HTTP: read it before taking any lock.
                definitions = catalogue.load(st.store, st.http, setup.download_methods)
            with (apply_operation(st),
                  state.locked(state.apply_lock(st.state_dir), blocking=False)):
                job = st.jobs.current()
                if job and job.status == "running":
                    raise state.Busy
                with state.locked(state.setup_lock(st.state_dir)):
                    setup = _working_setup(st)
                    if setup is None:
                        abort(409)
                    if kind == "indexer":
                        items = setup.indexers
                        remove = forms.remove_indexer
                        test = prowlarr.test_indexer
                    else:
                        items = setup.usenet_servers
                        remove = forms.remove_usenet_server
                        test = sabnzbd.test_usenet_server
                    if action != "add" and not any(item.slug == slug for item in items):
                        abort(404)
                    if action == "add":
                        edit_key = "edit" if kind == "indexer" else "edit-usenet"
                        edit_slug = request.form.get(edit_key)
                        existing = next((item for item in items if item.slug == edit_slug), None)
                        if edit_slug and existing is None:
                            abort(404)
                        if kind == "indexer":
                            choice = existing.definition if existing else request.form.get("definition")
                            definition = next(
                                (d for d in definitions or () if d.definition.casefold() ==
                                 (choice or "").casefold()), None,
                            )
                            if definition is None:
                                raise forms.Invalid({"definition": "Choose an indexer from the list."})
                            setup, slug = forms.add_indexer(
                                setup, definition, request.form, st.store, existing=existing,
                            )
                        else:
                            setup, slug = forms.add_usenet_server(
                                setup, request.form, st.store, existing=existing,
                            )
                    elif action == "remove":
                        try:
                            saved = DesiredSetup.load(st.state_dir / SETUP_FILE)
                        except FileNotFoundError:
                            saved = None
                        active = (() if saved is None else
                                  saved.indexers if kind == "indexer" else saved.usenet_servers)
                        setup = remove(setup, slug, st.store, keep_credentials=any(
                            item.slug == slug for item in active
                        ))
                    if action != "test":
                        setup.save(st.state_dir / DRAFT_FILE)
                if action == "remove":
                    message, problem = "Removed.", False
                else:
                    error = test(setup, st.store, st.http, slug)
                    entries = setup.indexers if kind == "indexer" else setup.usenet_servers
                    entry = next(item for item in entries if item.slug == slug)
                    label = entry.name if kind == "indexer" else entry.host
                    message, problem = error or f"{label} works.", bool(error)
        except state.Busy:
            return render_indexers(setup, TEST_BUSY, True, 409)
        except catalogue.Unavailable:
            return render_indexers(setup, "Prowlarr is still starting. Try again shortly.", True, 503)
        except forms.Invalid as error:
            return render_indexers(setup, next(iter(error.errors.values())), True, 400)
        return render_indexers(setup, message, problem)

    @pages.route("/setup/indexers", methods=["GET", "POST"])
    @auth.requires("setup")
    def indexers():
        if request.method == "POST":
            return change_entry("indexer", "add")
        setup = _working_setup(auth.web_state())
        if setup is None:
            abort(409)
        return render_indexers(setup)

    @pages.post("/setup/usenet")
    @auth.requires("setup")
    def usenet():
        return change_entry("usenet", "add")

    @pages.post("/setup/indexers/<slug>/<action>")
    @auth.requires("setup")
    def indexer_action(slug, action):
        if action not in ("test", "remove"):
            abort(404)
        return change_entry("indexer", action, slug)

    @pages.post("/setup/usenet/<slug>/<action>")
    @auth.requires("setup")
    def usenet_action(slug, action):
        if action not in ("test", "remove"):
            abort(404)
        return change_entry("usenet", action, slug)

    def render_review(setup, message="", status=200):
        st = auth.web_state()
        subtitles = forms.read_subtitles(setup, st.store)
        language_labels = dict(subtitles["options"])
        return render_template(
            "wizard/review.html", setup=setup, message=message, steps=step_list("review"),
            step=STEPS[-1], position=len(STEPS), back_url=_neighbour("review", -1),
            device=dict(forms.DEVICES)[setup.device], quality=dict(forms.QUALITIES)[setup.quality],
            languages=[language_labels.get(code, code) for code in setup.subtitle_languages],
            vpn_key_saved=bool(st.store.get(VPN_KEY_NAME)),
            subtitles_account_saved=subtitles["account_saved"],
        ), status

    @pages.get("/setup/review")
    @auth.requires("setup")
    def review():
        setup = _working_setup(auth.web_state())
        if setup is None:
            abort(409)
        return render_review(setup)

    @pages.post("/setup/install")
    @auth.requires("setup")
    def install():
        st = auth.web_state()
        try:
            with apply_operation(st):
                job = st.jobs.current()
                if job and job.status == "running":
                    return redirect(url_for("wizard.finish"), 303)
                with (state.locked(state.apply_lock(st.state_dir), blocking=False),
                      state.locked(state.setup_lock(st.state_dir))):
                    setup = _working_setup(st)
                    if setup is None:
                        abort(409)
                    if not any(st.store.get(f"test-ok:indexer:{i.slug}") == "1"
                               for i in setup.indexers):
                        return render_review(setup, "Test at least one indexer before installing.", 400)
                    if "usenet" in setup.download_methods and not any(
                        st.store.get(f"test-ok:usenet:{s.slug}") == "1" for s in setup.usenet_servers
                    ):
                        return render_review(
                            setup, "Test at least one Usenet server before installing.", 400,
                        )
                    _promote(st, setup)
                # The child acquires the same apply lock. Release it before starting the child.
                st.jobs.start(st.state_dir / SETUP_FILE)
        except state.Busy:
            setup = _working_setup(st)
            if setup is None:
                abort(409)
            return render_review(setup, TEST_BUSY, 409)
        return redirect(url_for("wizard.finish"), 303)

    @pages.get("/setup/finish")
    @auth.requires("setup")
    def finish():
        st = auth.web_state()
        try:
            setup = DesiredSetup.load(st.state_dir / SETUP_FILE)
        except FileNotFoundError:
            return redirect(url_for("wizard.review"), 303)
        job = st.jobs.current()
        return render_template(
            "wizard/_finish_actions.html" if request.headers.get("HX-Request") == "true"
            else "wizard/finish.html",
            job=job, setup=setup,
            running=bool(job and job.status == "running"),
            plex_connected=bool(st.store.get(PLEX_TOKEN_NAME)),
            jellyfin_user=st.store.get(JELLYFIN_USER_NAME) or jellyfin.ADMIN_USER,
        )

    return pages


def _first_apply_due(st) -> bool:
    """True before the first apply: nothing saved as the real setup and no job ever run."""
    return not (st.state_dir / SETUP_FILE).exists() and st.jobs.current() is None


def _after(key: str) -> str:
    following = _neighbour(key, 1)
    return following if following is not None else f"/setup/{key}?saved=1"
