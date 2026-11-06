"""Settings: change the running setup (``setup.json``) one section at a time.

Settings edits ``setup.json`` directly, and only while no ``setup.draft.json`` exists: an
unfinished wizard change is never overwritten (the page offers to continue or throw it away).
A save runs under ``setup.lock``, then starts an apply job exactly as the wizard's Install does.
Wizard sections are rendered by their own step templates, so the fields stay the same.
"""

from __future__ import annotations

import json
from typing import Any

from flask import (
    Blueprint,
    Flask,
    abort,
    redirect,
    render_template,
    request,
)

from orama import state
from orama.secrets import SecretStore
from orama.setup_model import MAX_EXTRA_HOST_NAMES, DesiredSetup
from orama.web import auth, catalogue, forms
from orama.web.jobs import TEST_BUSY, apply_operation
from orama.web.wizard import STEPS
from orama.wiring import prowlarr, sabnzbd

SETUP_FILE = "setup.json"
DRAFT_FILE = "setup.draft.json"
HARDWARE_FILE = "hardware.json"

# Sections that reuse the wizard's field pages: key, title, what it is for.
WIZARD_SECTIONS = (
    ("media", "Media servers"),
    ("folders", "Folders"),
    ("downloads", "Downloads and VPN"),
    ("playback", "Playback"),
    ("subtitles", "Subtitles"),
)
_STEP = {s.key: s for s in STEPS}

APP_NAMES = {"plex": "Plex", "jellyfin": "Jellyfin", "usenet": "SABnzbd", "torrent": "qBittorrent"}
# Typed secrets the confirm page asks for again instead of carrying them in the page.
RETYPE = {
    "media": (("jellyfin_password", "Jellyfin administrator password"),),
    "downloads": (("vpn_key", "WireGuard private key"),),
}

PASSWORD_MISMATCH = "The two new passwords do not match."
WRONG_PASSWORD = "That is not your current password."
HOST_NAME_BAD = ("That is not a name Orama can use. Use lower-case letters, digits, dots and "
                 "hyphens, like media.example.")


class _Back(Exception):
    """Go back to the Settings page without saving (a draft exists, or nothing differs)."""


class _NeedsConfirm(Exception):
    def __init__(self, apps: list[str]) -> None:
        super().__init__("confirm")
        self.apps = apps


class _DryStore:
    """Reads the secret store, writes nothing: to check a form before the owner confirms."""

    def __init__(self, store: SecretStore) -> None:
        self._store = store

    def get(self, name: str):
        return self._store.get(name)

    def set(self, name: str, value: str) -> None:
        pass

    def delete(self, name: str) -> None:
        pass


def register_settings(app: Flask) -> None:
    app.register_blueprint(_pages())


def _load(st, name: str) -> DesiredSetup | None:
    try:
        return DesiredSetup.load(st.state_dir / name)
    except FileNotFoundError:
        return None


def _hardware(st) -> tuple[int | None, bool] | None:
    """What the installer found, or None when the file is missing or not understood."""
    try:
        data = json.loads((st.state_dir / HARDWARE_FILE).read_text(encoding="utf-8"))
        gid, nvidia = data["render_gid"], data["nvidia"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if (gid is not None and (type(gid) is not int or not 0 <= gid <= 65535)) or type(nvidia) is not bool:
        return None
    return gid, nvidia


def _graphics_text(gid: int | None, nvidia: bool) -> str:
    parts = [f"a graphics device for video (render group {gid})" if gid is not None
             else "no graphics device for video"]
    parts.append("an NVIDIA graphics card" if nvidia else "no NVIDIA graphics card")
    return " and ".join(parts)


def _stopping(section: str, setup: DesiredSetup, form) -> list[str]:
    """The apps this form would turn off."""
    if section == "media":
        return [APP_NAMES[n] for n in ("plex", "jellyfin")
                if n in setup.media_servers and not forms._on(form, f"server_{n}")]
    if section == "downloads":
        return [APP_NAMES[n] for n in ("usenet", "torrent")
                if n in setup.download_methods and not forms._on(form, n)]
    return []


def _pages() -> Blueprint:
    pages = Blueprint("settings", __name__)

    # ------------------------------------------------------------ the page

    def render_page(status: int = 200, overrides: dict[str, dict] | None = None,
                    message: str = "", problem: bool = False):
        st = auth.web_state()
        overrides = overrides or {}
        setup = _load(st, SETUP_FILE)
        draft = (st.state_dir / DRAFT_FILE).exists()
        sections: list[dict[str, Any]] = []
        extra: dict[str, Any] = {}
        if setup is not None and not draft:
            for key, title in WIZARD_SECTIONS:
                read = forms.SECTIONS[key][0]
                given = overrides.get(key, {})
                html = render_template(
                    f"wizard/{key}.html", layout="settings/_bare.html", embedded=True,
                    form_action=f"/settings/{key}", step=_STEP[key],
                    values=given.get("values") or read(setup, st.store),
                    errors=given.get("errors", {}), applied=True, first_time=False,
                    saved=False, back_url=None,
                )
                sections.append({"key": key, "title": title, "html": html})
            hardware = _hardware(st)
            if hardware is not None and hardware != (setup.render_gid, setup.nvidia):
                extra["graphics_found"] = _graphics_text(*hardware)
                extra["graphics_now"] = _graphics_text(setup.render_gid, setup.nvidia)
        job = st.jobs.current()
        show_job = bool(job and (job.status == "running" or request.args.get("applied")))
        saved = request.args.get("saved", "")
        if not message and saved == "addresses":
            message = "Saved. The new name works from the next page you open."
        elif not message and saved == "password":
            message = "Your password is changed. Other sessions were signed out."
        return render_template(
            "settings/index.html", setup=setup, draft=draft, sections=sections, job=job,
            show_job=show_job, running=bool(job and job.status == "running"),
            message=message, problem=problem, address_errors=overrides.get("addresses", {}).get("errors", {}),
            password_errors=overrides.get("password", {}).get("errors", {}),
            indexer_count=len(setup.indexers) if setup else 0,
            usenet_count=len(setup.usenet_servers) if setup else 0,
            max_names=MAX_EXTRA_HOST_NAMES, **extra,
        ), status

    @pages.get("/settings")
    @auth.requires("settings")
    def index():
        return render_page()

    # ------------------------------------------------------------ saving

    def commit(section: str, change, *, start: bool, echo=None):
        """Run ``change(setup) -> setup`` on setup.json under the lock; maybe start an apply."""
        st = auth.web_state()
        path = st.state_dir / SETUP_FILE
        if not path.exists():
            return render_template(
                "rejected.html",
                message="Orama has no setup to change yet. Finish the setup first.",
            ), 409

        def guarded(setup: DesiredSetup) -> DesiredSetup:
            if (st.state_dir / DRAFT_FILE).exists():
                raise _Back
            return change(setup)

        try:
            if start:
                with apply_operation(st):
                    job = st.jobs.current()
                    if job and job.status == "running":
                        raise state.Busy
                    with state.locked(state.apply_lock(st.state_dir), blocking=False):
                        state.update_setup(path, guarded)
                    # The child takes apply.lock itself: start it after releasing ours.
                    st.jobs.start(path)
            else:
                state.update_setup(path, guarded)
        except _Back:
            return redirect("/settings", 303)
        except state.Busy:
            return render_page(409, message=TEST_BUSY, problem=True,
                               overrides=echo(None) if echo else None)
        except forms.Invalid as e:
            return render_page(400, overrides=echo(e.errors) if echo else None)
        except _NeedsConfirm as e:
            return confirm_page(section, e.apps)
        return redirect("/settings?applied=1" if start else f"/settings?saved={section}", 303)

    def confirm_page(section: str, apps: list[str]):
        hidden = [(k, v) for k, v in request.form.items(multi=True)
                  if k not in ("csrf_token", "confirm")
                  and k not in {name for name, _ in RETYPE.get(section, ())}]
        retype = [(name, label) for name, label in RETYPE.get(section, ())
                  if request.form.get(name)]
        return render_template(
            "settings/confirm.html", section=section, apps=apps, hidden=hidden, retype=retype,
            title=dict(WIZARD_SECTIONS)[section],
        )

    def make_section(section: str):
        _read, echo, apply_fn = forms.SECTIONS[section]

        @auth.requires("settings")
        def save():
            st = auth.web_state()
            form = request.form
            confirmed = form.get("confirm") == "1"

            def change(setup: DesiredSetup) -> DesiredSetup:
                off = _stopping(section, setup, form)
                if off and not confirmed:
                    apply_fn(setup, form, _DryStore(st.store))  # mistakes come first
                    raise _NeedsConfirm(off)
                return apply_fn(setup, form, st.store)

            def shown(errors):
                setup = _load(st, SETUP_FILE)
                if setup is None:
                    return None
                return {section: {"values": echo(setup, form, st.store), "errors": errors or {}}}

            return commit(section, change, start=True, echo=shown)

        save.__name__ = f"save_{section}"
        return save

    for key, _title in WIZARD_SECTIONS:
        pages.add_url_rule(f"/settings/{key}", endpoint=f"save_{key}",
                           view_func=make_section(key), methods=["POST"])

    @pages.post("/settings/addresses")
    @auth.requires("settings")
    def addresses():
        form = request.form
        removing = form.get("remove", "").strip().lower()
        adding = form.get("add_name", "").strip().lower()

        def change(setup: DesiredSetup) -> DesiredSetup:
            names = list(setup.extra_host_names)
            if removing:
                names = [n for n in names if n != removing]
            elif adding:
                if adding in names:
                    raise forms.Invalid({"add_name": "That name is already in the list."})
                if len(names) >= MAX_EXTRA_HOST_NAMES:
                    raise forms.Invalid({
                        "add_name": f"You can add up to {MAX_EXTRA_HOST_NAMES} names. "
                                    "Remove one first."})
                names.append(adding)
            else:
                raise forms.Invalid({"add_name": "Type the name to add."})
            try:
                return forms._rebuild(setup, extra_host_names=names)
            except Exception:  # noqa: BLE001 - the model's own refusal, in plain words
                raise forms.Invalid({"add_name": HOST_NAME_BAD}) from None

        def shown(errors):
            return {"addresses": {"errors": errors or {}}}

        return commit("addresses", change, start=False, echo=shown)

    @pages.post("/settings/graphics")
    @auth.requires("settings")
    def graphics():
        st = auth.web_state()
        found = _hardware(st)
        if found is None:
            return redirect("/settings", 303)
        gid, nvidia = found

        def change(setup: DesiredSetup) -> DesiredSetup:
            if (setup.render_gid, setup.nvidia) == (gid, nvidia):
                raise _Back  # nothing differs: back to the page, no apply
            return forms._rebuild(setup, render_gid=gid, nvidia=nvidia)

        return commit("graphics", change, start=True)

    @pages.post("/settings/password")
    @auth.requires("settings")
    def password():
        st = auth.web_state()
        old = request.form.get("current_password", "")
        new = request.form.get("new_password", "")
        errors: dict[str, str] = {}

        def again(status: int):
            return render_page(status, overrides={"password": {"errors": errors}})

        if new != request.form.get("repeat_password", ""):
            errors["repeat_password"] = PASSWORD_MISMATCH
            return again(400)
        if not auth.password_length_ok(new):
            errors["new_password"] = auth.PASSWORD_LENGTH_MESSAGE
            return again(400)
        address = request.remote_addr or ""
        if not st.limiter.try_begin(address):
            errors["current_password"] = auth.RATE_MESSAGE
            return again(429)
        changed = False
        try:
            changed = auth.change_password(old, new)
        finally:
            st.limiter.finish(address, failed=not changed)
        if not changed:
            errors["current_password"] = WRONG_PASSWORD
            return again(400)
        return redirect("/settings?saved=password", 303)

    @pages.post("/settings/change-setup")
    @auth.requires("settings")
    def change_setup():
        st = auth.web_state()
        with state.locked(state.setup_lock(st.state_dir)):
            if not (st.state_dir / DRAFT_FILE).exists():
                live = _load(st, SETUP_FILE)
                if live is not None:
                    live.save(st.state_dir / DRAFT_FILE)
        return redirect("/setup/media", 303)

    @pages.post("/settings/discard-draft")
    @auth.requires("settings")
    def discard_draft():
        st = auth.web_state()
        with state.locked(state.setup_lock(st.state_dir)):
            (st.state_dir / DRAFT_FILE).unlink(missing_ok=True)
        return redirect("/settings", 303)

    # ------------------------------------------------------------ indexers and Usenet

    def render_indexers(setup: DesiredSetup, message: str = "", problem: bool = False,
                        status: int = 200):
        st = auth.web_state()
        query = request.args.get("q", "")[:100]
        definition = request.values.get("definition", "")
        edit_slug = request.args.get("edit") or request.form.get("edit")
        editing = next((i for i in setup.indexers if i.slug == edit_slug), None)
        if editing:
            definition = editing.definition
        server_slug = request.args.get("edit-usenet") or request.form.get("edit-usenet")
        editing_server = next((i for i in setup.usenet_servers if i.slug == server_slug), None)
        try:
            job = st.jobs.current()
            if job and job.status == "running":
                raise catalogue.Unavailable
            definitions = catalogue.load(st.store, st.http, setup.download_methods)
            waiting = False
        except catalogue.Unavailable:
            definitions, waiting = [], True
        selected = next((r for r in definitions
                         if r.definition.casefold() == definition.casefold()), None)
        matches = [r for r in definitions if query.casefold() in r.name.casefold()][:50]
        fragment = request.method == "GET" and request.headers.get("HX-Request") == "true"
        job = st.jobs.current()
        return render_template(
            "wizard/_indexer_form.html" if fragment else "settings/indexers.html",
            setup=setup, definitions=matches, selected=selected, query=query,
            definition=definition, waiting=waiting, message=message, problem=problem,
            editing=editing, editing_server=editing_server,
            indexers_endpoint="settings.indexers",
            saved_fields={f for f in editing.secret_fields
                          if st.store.get(f"indexer:{editing.slug}:{f}") is not None}
            if editing else set(),
            server_password_saved=bool(editing_server and
                                       st.store.get(f"usenet:{editing_server.slug}:password")),
            tested_indexers={i.slug for i in setup.indexers
                             if st.store.get(f"test-ok:indexer:{i.slug}") == "1"},
            tested_servers={s.slug for s in setup.usenet_servers
                            if st.store.get(f"test-ok:usenet:{s.slug}") == "1"},
            job=job, show_job=bool(job and request.method == "POST" and message),
            running=bool(job and job.status == "running"),
        ), status

    def change_entry(kind: str, action: str, slug: str = ""):
        st = auth.web_state()
        path = st.state_dir / SETUP_FILE
        setup = _load(st, SETUP_FILE)
        if setup is None:
            abort(409)
        if (st.state_dir / DRAFT_FILE).exists():
            return redirect("/settings", 303)
        definitions = None
        message, problem = "", False
        try:
            if kind == "indexer" and action == "add":
                # Up to a few seconds of HTTP: read it before taking any lock.
                definitions = catalogue.load(st.store, st.http, setup.download_methods)
            with apply_operation(st):
                job = st.jobs.current()
                if job and job.status == "running":
                    raise state.Busy
                with state.locked(state.apply_lock(st.state_dir), blocking=False):
                    with state.locked(state.setup_lock(st.state_dir)):
                        if (st.state_dir / DRAFT_FILE).exists():
                            return redirect("/settings", 303)
                        setup = DesiredSetup.load(path)
                        if kind == "indexer":
                            items, remove = setup.indexers, forms.remove_indexer
                            test = prowlarr.test_indexer
                        else:
                            items, remove = setup.usenet_servers, forms.remove_usenet_server
                            test = sabnzbd.test_usenet_server
                        if action != "add" and not any(i.slug == slug for i in items):
                            abort(404)
                        if action == "add":
                            edit_key = "edit" if kind == "indexer" else "edit-usenet"
                            edit_slug = request.form.get(edit_key)
                            existing = next((i for i in items if i.slug == edit_slug), None)
                            if edit_slug and existing is None:
                                abort(404)
                            if kind == "indexer":
                                choice = (existing.definition if existing
                                          else request.form.get("definition"))
                                definition = next(
                                    (d for d in definitions or ()
                                     if d.definition.casefold() == (choice or "").casefold()),
                                    None)
                                if definition is None:
                                    raise forms.Invalid(
                                        {"definition": "Choose an indexer from the list."})
                                setup, slug = forms.add_indexer(
                                    setup, definition, request.form, st.store, existing=existing)
                            else:
                                setup, slug = forms.add_usenet_server(
                                    setup, request.form, st.store, existing=existing)
                        elif action == "remove":
                            # A live entry: its removal step in the next apply deletes it.
                            setup = remove(setup, slug, st.store, keep_credentials=True)
                        if action != "test":
                            setup.save(path)
                    if action == "remove":
                        message = "Removed."
                    else:
                        error = test(setup, st.store, st.http, slug)
                        entries = setup.indexers if kind == "indexer" else setup.usenet_servers
                        entry = next(i for i in entries if i.slug == slug)
                        label = entry.name if kind == "indexer" else entry.host
                        message, problem = error or f"{label} works.", bool(error)
                if action != "test":
                    st.jobs.start(path)  # after apply.lock is released
        except state.Busy:
            return render_indexers(setup, TEST_BUSY, True, 409)
        except catalogue.Unavailable:
            return render_indexers(setup, "Prowlarr is still starting. Try again shortly.",
                                   True, 503)
        except forms.Invalid as error:
            return render_indexers(setup, next(iter(error.errors.values())), True, 400)
        return render_indexers(setup, message, problem)

    @pages.route("/settings/indexers", methods=["GET", "POST"])
    @auth.requires("settings")
    def indexers():
        if request.method == "POST":
            return change_entry("indexer", "add")
        setup = _load(auth.web_state(), SETUP_FILE)
        if setup is None:
            abort(409)
        return render_indexers(setup)

    @pages.post("/settings/usenet")
    @auth.requires("settings")
    def usenet():
        return change_entry("usenet", "add")

    @pages.post("/settings/indexers/<slug>/<action>")
    @auth.requires("settings")
    def indexer_action(slug, action):
        if action not in ("test", "remove"):
            abort(404)
        return change_entry("indexer", action, slug)

    @pages.post("/settings/usenet/<slug>/<action>")
    @auth.requires("settings")
    def usenet_action(slug, action):
        if action not in ("test", "remove"):
            abort(404)
        return change_entry("usenet", action, slug)

    return pages
