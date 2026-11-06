"""Orama's web app: sign-in, the first-run password and the shared security checks.

Later pages are blueprints that use :func:`orama.web.auth.requires` and, in their forms,
``{{ csrf_token() }}``. Every request first passes the Host allowlist, then (for
POST/PUT/PATCH/DELETE) the CSRF check, before any page code runs.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from flask import Blueprint, Flask, current_app, g, redirect, render_template, request, url_for
from flask.globals import request_ctx
from werkzeug.exceptions import HTTPException

from orama.agent_client import AgentClient
from orama.secrets import SecretStore
from orama.setup_model import DesiredSetup
from orama.web import auth, security
from orama.web.apps import register_apps, running_summary
from orama.web.assets import configure_assets
from orama.web.jobs import JobRunner, register_jobs, store_redactor
from orama.web.plex import register_plex
from orama.web.settings import register_settings
from orama.web.wizard import register_wizard
from orama.wiring.http import Http

SETUP_FILE = "setup.json"
DRAFT_FILE = "setup.draft.json"
AUTH_FILE = "auth.json"
FIRST_RUN_CODE_FILE = "first-run.code"
SECRETS_FILE = "secrets.bin"
MAX_REQUEST_BYTES = 1024 * 1024

# Plain words for each refusal; never an HTTP code, a path or anything from the request.
MESSAGES = {
    400: "Orama could not read that request. Go back and try again.",
    403: "This page is not open to you.",
    404: "This page does not exist. Go back, or start again from the home page.",
    405: "This page cannot be used that way. Go back and try again.",
    413: "That was too much to send at once. Go back and try again with less.",
    429: auth.RATE_MESSAGE,
}
GENERIC_MESSAGE = "Something went wrong in Orama. Wait a minute, then try again."


class SetupReader:
    """The current setup (``setup.json``, else the draft), re-read when the file changes."""

    def __init__(self, state_dir: Path) -> None:
        self._state_dir = state_dir
        self._lock = threading.Lock()
        self._key: tuple | None = None
        self._setup: DesiredSetup | None = None

    def current(self) -> DesiredSetup | None:
        with self._lock:  # the file check and the cache update happen together
            for name in (SETUP_FILE, DRAFT_FILE):
                path = self._state_dir / name
                try:
                    st = os.stat(path)
                except OSError:
                    continue
                key = (name, st.st_ino, st.st_mtime_ns, st.st_size)
                if key != self._key:
                    try:
                        setup = DesiredSetup.load(path)
                    except Exception:  # unreadable: local names only  # noqa: BLE001
                        # Not cached: a file caught mid-write is read again next time.
                        self._key, self._setup = None, None
                        return None
                    self._key, self._setup = key, setup
                return self._setup
            return None


@dataclass
class WebState:
    state_dir: Path
    agent: AgentClient
    http: Http
    clock: Callable[[], float]
    passwords: auth.PasswordFile
    sessions: auth.Sessions
    limiter: auth.RateLimiter
    store: SecretStore
    setup: SetupReader
    jobs: JobRunner
    apply_gate: threading.Lock = field(default_factory=threading.Lock)

    @property
    def first_run_code(self) -> Path:
        return self.state_dir / FIRST_RUN_CODE_FILE


def create_app(
    state_dir: Path,
    *,
    agent: AgentClient | None = None,
    http: Http | None = None,
    clock: Callable[[], float] = time.time,
    request_worker: Any = None,
) -> Flask:
    state_dir = Path(state_dir)
    app = Flask(__name__)
    app.config.update(
        MAX_CONTENT_LENGTH=MAX_REQUEST_BYTES,
        PROVIDE_AUTOMATIC_OPTIONS=False,
        # Flask's own signed-cookie session is never used; keep its name off ours.
        SESSION_COOKIE_NAME="orama_flask_unused",
    )
    store = SecretStore(state_dir / SECRETS_FILE)
    app.extensions["orama"] = WebState(
        state_dir=state_dir,
        agent=agent or AgentClient(),
        http=http or Http(),
        clock=clock,
        passwords=auth.PasswordFile(state_dir / AUTH_FILE),
        sessions=auth.Sessions(clock),
        limiter=auth.RateLimiter(clock),
        store=store,
        setup=SetupReader(state_dir),
        jobs=JobRunner(state_dir, redact=store_redactor(store), clock=clock),
    )
    app.extensions["request_worker"] = request_worker
    app.jinja_env.globals["csrf_token"] = security.csrf_token

    configure_assets(app)
    app.before_request(_check_request)
    app.after_request(_finish_response)
    app.register_error_handler(HTTPException, _http_error)
    app.register_error_handler(Exception, _server_error)
    app.register_blueprint(_auth_pages())
    register_jobs(app)
    register_wizard(app)
    register_plex(app)
    register_settings(app)
    register_apps(app)
    return app


# ---------------------------------------------------------------- request hooks


def rejected(message: str, status: int):
    """A plain refusal page: the reason in words, nothing from the request."""
    if request_ctx.url_adapter is None:
        # Werkzeug could not read the Host header, so Flask has no URL builder; page links
        # are relative paths, which need no host.
        request_ctx.url_adapter = current_app.url_map.bind("localhost")
    return render_template("rejected.html", message=message), status


def _check_request():
    st = auth.web_state()
    g.principal = None
    g.session = None
    # 1. Host allowlist, for every request (also /healthz and static files).
    if not security.host_allowed(request.environ, st.setup.current()):
        return rejected(security.HOST_REJECTED, 400)
    # 2. No OPTIONS (and so no CORS preflight) anywhere.
    if request.method == "OPTIONS":
        return rejected(MESSAGES[405], 405)
    # 3. The session, from the cookie.
    session = st.sessions.get(request.cookies.get(auth.COOKIE), request.remote_addr or "")
    if session is not None:
        g.session = session
        g.principal = session.principal
    # 4. CSRF on every state-changing request.
    if request.method in security.UNSAFE_METHODS and not security.csrf_ok():
        return rejected(security.CSRF_REJECTED, 403)
    return None


def _finish_response(response):
    https = request.scheme == "https"
    if g.get("set_cookie"):
        response.set_cookie(
            auth.COOKIE, g.set_cookie, max_age=auth.ABSOLUTE_TIMEOUT, path="/",
            secure=https, httponly=True, samesite="Strict",
        )
    elif g.get("clear_cookie"):
        response.delete_cookie(auth.COOKIE, path="/", secure=https, httponly=True,
                               samesite="Strict")
    return security.add_security_headers(response)


def _http_error(error: HTTPException):
    if error.code is None or error.code < 400:
        return error
    return rejected(MESSAGES.get(error.code, GENERIC_MESSAGE), error.code)


def _server_error(error: Exception):
    if isinstance(error, HTTPException):
        return _http_error(error)
    logging.getLogger("orama.web").exception("page failed")
    return rejected(GENERIC_MESSAGE, 500)


# ---------------------------------------------------------------- pages


def _auth_pages() -> Blueprint:
    pages = Blueprint("auth", __name__)

    @pages.get("/healthz")
    def healthz():
        return "ok", 200, {"Content-Type": "text/plain; charset=utf-8"}

    @pages.get("/")
    def home():
        st = auth.web_state()
        if not st.passwords.exists():
            return redirect(url_for("auth.setup_password"), 303)
        if g.principal is None:
            return redirect(url_for("auth.signin", next=_next_for_home()), 303)
        if not (st.state_dir / SETUP_FILE).exists():
            return redirect("/setup/media", 303)
        setup = st.setup.current()
        return render_template("home.html", summary=running_summary(setup) if setup else None)

    @pages.route("/signin", methods=["GET", "POST"])
    def signin():
        st = auth.web_state()
        if not st.passwords.exists():
            return redirect(url_for("auth.setup_password"), 303)
        if request.method == "GET":
            nxt = auth.safe_next(request.args.get("next"))
            if g.principal is not None:
                return redirect(nxt, 303)
            # Where to go after signing in is kept with the pre-sign-in session.
            g.may_start_session = True
            auth.current_session(create=True).next = nxt
            return render_template("signin.html")
        nxt = g.session.next  # the CSRF check guarantees a session
        address = request.remote_addr or ""
        if not st.limiter.try_begin(address):
            return _signin_page(auth.RATE_MESSAGE, 429)
        ok = False
        try:
            ok = st.passwords.verify(request.form.get("password", ""))
        finally:
            st.limiter.finish(address, failed=not ok)
        if not ok:
            return _signin_page("That password is not right.", 401)
        auth.start_session(auth.ADMIN)
        return redirect(nxt, 303)

    @pages.post("/signout")
    def signout():
        auth.end_session()
        return redirect(url_for("auth.signin"), 303)

    @pages.route("/setup/password", methods=["GET", "POST"])
    def setup_password():
        st = auth.web_state()
        if st.passwords.exists():
            return redirect(url_for("auth.signin"), 303)
        g.may_start_session = True
        code = request.values.get("code", "")
        if request.method == "GET" and not code:
            return render_template("first_run.html")
        # A code is checked (and counted when wrong) before the password form is shown,
        # and again when the form is sent.
        address = request.remote_addr or ""
        if not st.limiter.try_begin(address):
            return render_template("first_run.html", error=auth.RATE_MESSAGE), 429
        ok = False
        try:
            ok = auth.code_matches(st.first_run_code, code)
        finally:
            st.limiter.finish(address, failed=not ok)
        if not ok:
            return render_template(
                "first_run.html",
                error="This setup link is not valid. Open the link the installer printed.",
            ), 403
        if request.method == "GET":
            return render_template("first_run.html", code=code)
        password = request.form.get("password", "")
        if not auth.password_length_ok(password):
            return render_template(
                "first_run.html", code=code, error=auth.PASSWORD_LENGTH_MESSAGE
            ), 400
        try:
            st.passwords.create(password)
        except FileExistsError:
            return redirect(url_for("auth.signin"), 303)
        st.first_run_code.unlink(missing_ok=True)
        auth.start_session(auth.ADMIN)
        return redirect("/", 303)

    return pages


def _signin_page(error: str, status: int):
    g.may_start_session = True
    return render_template("signin.html", error=error), status


def _next_for_home() -> str | None:
    if not request.query_string:
        return None
    return auth.safe_next(request.full_path)
