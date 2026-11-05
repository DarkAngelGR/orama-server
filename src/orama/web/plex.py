"""Plex sign-in from the wizard. PIN ids and tokens stay on the server."""

from __future__ import annotations

from threading import Lock

from flask import Blueprint, Flask, abort, redirect, render_template, request, url_for

from orama import state
from orama.setup_model import DesiredSetup
from orama.web import auth
from orama.web.jobs import apply_operation
from orama.wiring.plex import SignIn, SignInError, begin_sign_in, poll_sign_in


def register_plex(app: Flask) -> None:
    pages = Blueprint("plex", __name__)
    gate = Lock()
    pending: tuple[str, SignIn] | None = None
    next_check = 0.0

    def view(pin=None, error=""):
        fragment = request.headers.get("HX-Request") == "true"
        return render_template("wizard/_plex_status.html" if fragment else "wizard/plex.html",
                               pin=pin, error=error)

    def current_pin():
        session = auth.current_session()
        if pending and session and pending[0] == session.id:
            return pending[1]
        return None

    @pages.post("/plex/sign-in")
    @auth.requires("setup")
    def start():
        nonlocal pending, next_check
        st = auth.web_state()
        path = st.state_dir / "setup.json"
        try:
            setup = DesiredSetup.load(path)
        except FileNotFoundError:
            abort(409)
        if "plex" not in setup.media_servers:
            abort(409)
        with gate:
            pin = current_pin()
            if pin is None or pin.expires_at <= st.clock():
                try:
                    pin = begin_sign_in(st.store, st.http, clock=st.clock)
                except SignInError:
                    return view(error="Plex did not answer. Try signing in again."), 503
                session = auth.current_session()
                if session is None:
                    abort(403)
                pending = (session.id, pin)
                next_check = 0.0
        return redirect(url_for("plex.status"), 303)

    @pages.route("/plex/sign-in/status", methods=["GET", "POST"])
    @auth.requires("setup")
    def status():
        nonlocal pending, next_check
        st = auth.web_state()
        with gate:
            pin = current_pin()
            if pin is None:
                return redirect(url_for("wizard.finish"), 303)
            if pin.expires_at <= st.clock():
                return view(error="The Plex code expired. Start sign-in again.")
            # Reading status is safe. Checking the upstream PIN may store a token,
            # so only a POST that passed Orama's CSRF check can do it.
            if request.method != "POST" or st.clock() < next_check:
                return view(pin)
            next_check = st.clock() + 2
            try:
                with apply_operation(st):
                    with state.locked(state.apply_lock(st.state_dir), blocking=False):
                        job = st.jobs.current()
                        if job and job.status == "running":
                            return view(pin)
                        result = poll_sign_in(st.store, st.http, pin.pin_id)
                    if result == "waiting":
                        return view(pin)
                    pending = None
                    if result == "expired":
                        return view(error="The Plex code expired. Start sign-in again.")
                    st.jobs.start(st.state_dir / "setup.json")
            except state.Busy:
                return view(pin)
        if request.headers.get("HX-Request") == "true":
            return "", 200, {"HX-Redirect": url_for("wizard.finish")}
        return redirect(url_for("wizard.finish"), 303)

    app.register_blueprint(pages)
