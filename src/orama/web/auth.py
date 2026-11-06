"""Who is signed in, and what they may do.

- :class:`PasswordFile`: the one admin password, argon2id-hashed in ``auth.json`` (0600).
- :class:`Sessions`: server-side sessions in memory (a restart signs the owner out).
- :class:`RateLimiter`: wrong passwords and wrong first-run codes, per address and overall.
- :class:`Principal` and :func:`requires`: "who is signed in" kept apart from "what they
  can do".
"""

from __future__ import annotations

import errno
import functools
import hmac
import json
import os
import secrets
import stat
import threading
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from flask import abort, current_app, g, make_response, redirect, request, url_for

from orama import state

MIN_PASSWORD = 12
MAX_PASSWORD = 256
PASSWORD_LENGTH_MESSAGE = "Use a password between 12 and 256 characters long."

COOKIE = "orama_session"
IDLE_TIMEOUT = 7 * 24 * 60 * 60
ABSOLUTE_TIMEOUT = 30 * 24 * 60 * 60
# Pre-sign-in sessions anyone can create by opening the sign-in page: keep only so many.
MAX_ANONYMOUS_SESSIONS = 1000

WINDOW = 15 * 60
PER_ADDRESS_FAILURES = 5
GLOBAL_FAILURES = 30
RATE_MESSAGE = "Too many wrong tries. Wait 15 minutes, then try again."

_MAX_FILE = 4096
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


@dataclass(frozen=True)
class Principal:
    name: str
    permissions: frozenset[str]


ADMIN = Principal("admin", frozenset({"setup", "settings", "apps"}))


def password_length_ok(password: str) -> bool:
    return MIN_PASSWORD <= len(password) <= MAX_PASSWORD


def read_private_file(path: Path) -> bytes | None:
    """The contents of a small regular file, never through a link; None if it is missing,
    is not a regular file, or is too large."""
    try:
        fd = os.open(path, _READ_FLAGS)
    except OSError as e:
        if e.errno in (errno.ENOENT, errno.ELOOP, errno.ENXIO, errno.EISDIR):
            return None
        raise
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > _MAX_FILE:
            return None
        return os.read(fd, _MAX_FILE + 1)
    finally:
        os.close(fd)


def _write_private(path: Path, data: bytes, *, exclusive: bool) -> None:
    """Write ``data`` atomically, 0600 from creation (O_EXCL temp file, then link/rename).

    With ``exclusive`` the write fails with FileExistsError if ``path`` exists (even as a
    link).
    """
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if exclusive:
            os.link(tmp, path)  # atomic; fails if anything is named ``path``
            os.unlink(tmp)
        else:
            os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


class PasswordFile:
    """The admin password hash in ``auth.json`` (``{"hash": "<argon2id PHC string>"}``).

    Writes happen under ``setup.lock`` in the same folder, the lock ``bootstrap`` holds
    while it decides whether to write a first-run code.
    """

    def __init__(self, path: Path, hasher: PasswordHasher | None = None) -> None:
        self.path = Path(path)
        self._hasher = hasher or PasswordHasher()
        self._lock = state.setup_lock(self.path.parent)

    def exists(self) -> bool:
        """True when anything at all is named ``auth.json`` (a link too: no first run)."""
        return os.path.lexists(self.path)

    def create(self, password: str) -> None:
        """Save the first password. Raises FileExistsError once one exists, ValueError
        for a password of the wrong length."""
        if not password_length_ok(password):
            raise ValueError(PASSWORD_LENGTH_MESSAGE)
        data = self._encode(self._hasher.hash(password))
        with state.locked(self._lock):
            if self.exists():
                raise FileExistsError(errno.EEXIST, "a password already exists")
            _write_private(self.path, data, exclusive=True)

    def verify(self, password: str) -> bool:
        if not password or len(password) > MAX_PASSWORD:
            return False
        stored = self._hash()
        if stored is None:
            return False
        try:
            self._hasher.verify(stored, password)
        except (VerificationError, InvalidHashError):
            return False
        if self._hasher.check_needs_rehash(stored):
            self._replace(stored, self._hasher.hash(password))
        return True

    def change(self, old: str, new: str) -> bool:
        """Replace the password when ``old`` is right. ValueError for a bad ``new``."""
        if not password_length_ok(new):
            raise ValueError(PASSWORD_LENGTH_MESSAGE)
        if not self.verify(old):
            return False
        stored = self._hash()
        return stored is not None and self._replace(stored, self._hasher.hash(new))

    def _replace(self, expected: str, new_hash: str) -> bool:
        """Write ``new_hash`` if the file still holds ``expected`` (check-then-act, locked)."""
        with state.locked(self._lock):
            if self._hash() != expected:
                return False
            _write_private(self.path, self._encode(new_hash), exclusive=False)
        return True

    def _hash(self) -> str | None:
        raw = read_private_file(self.path)
        if raw is None:
            return None
        try:
            value = json.loads(raw).get("hash")
        except (ValueError, AttributeError):
            return None
        return value if isinstance(value, str) else None

    @staticmethod
    def _encode(hash_: str) -> bytes:
        return (json.dumps({"hash": hash_}) + "\n").encode()


# ---------------------------------------------------------------- sessions


@dataclass
class Session:
    id: str
    principal: Principal | None
    csrf: str
    created: float
    seen: float = field(default=0.0)
    # The client address the session was started from (see Sessions).
    address: str = ""
    # Pre-sign-in only: the local path to open after signing in.
    next: str = "/"


class Sessions:
    """Server-side sessions in memory, safe across waitress threads.

    Each session is bound to the client address it started from. Browsers send a cookie
    to every port of a host, so the apps on this machine receive Orama's cookie too; an
    app container that replays it reaches Orama from its own orama-net address. A session
    used from any other address is ended at once and the request counts as signed out.
    """

    def __init__(self, clock: Callable[[], float]) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._signed_in: dict[str, Session] = {}
        self._anonymous: OrderedDict[str, Session] = OrderedDict()

    def new(self, principal: Principal | None, address: str) -> Session:
        now = self._clock()
        session = Session(secrets.token_urlsafe(32), principal, secrets.token_urlsafe(32),
                          now, now, address)
        with self._lock:
            if principal is None:
                self._anonymous[session.id] = session
                while len(self._anonymous) > MAX_ANONYMOUS_SESSIONS:
                    self._anonymous.popitem(last=False)
            else:
                self._prune(now)
                self._signed_in[session.id] = session
        return session

    def get(self, session_id: str | None, address: str) -> Session | None:
        """The live session, or None; touching it resets the idle timer.

        A session used from an address other than its own is ended (see the class).
        """
        if not session_id:
            return None
        now = self._clock()
        with self._lock:
            session = self._signed_in.get(session_id) or self._anonymous.get(session_id)
            if session is None:
                return None
            if self._expired(session, now) or not hmac.compare_digest(
                session.address.encode(), address.encode()
            ):
                self._remove(session_id)
                return None
            session.seen = now
            return session

    def end(self, session_id: str | None) -> None:
        if session_id:
            with self._lock:
                self._remove(session_id)

    def end_all(self) -> None:
        with self._lock:
            self._signed_in.clear()
            self._anonymous.clear()

    def _remove(self, session_id: str) -> None:
        self._signed_in.pop(session_id, None)
        self._anonymous.pop(session_id, None)

    @staticmethod
    def _expired(session: Session, now: float) -> bool:
        return now - session.seen >= IDLE_TIMEOUT or now - session.created >= ABSOLUTE_TIMEOUT

    def _prune(self, now: float) -> None:
        for sid in [sid for sid, s in self._signed_in.items() if self._expired(s, now)]:
            del self._signed_in[sid]


# ---------------------------------------------------------------- rate limits


class RateLimiter:
    """5 failures from one address in 15 minutes lock that address for 15 minutes; 30
    failures from all addresses in 15 minutes lock everyone until the window clears.

    A check is ``try_begin`` (refused while locked) and then ``finish``. Checks still
    running count against both limits, so parallel attempts cannot slip past them.
    """

    def __init__(self, clock: Callable[[], float]) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._failures: dict[str, deque[float]] = {}
        self._locked_until: dict[str, float] = {}
        self._all: deque[float] = deque()
        self._pending: dict[str, int] = {}
        self._pending_all = 0

    def blocked(self, address: str) -> bool:
        now = self._clock()
        with self._lock:
            self._expire(now)
            return self._blocked(address)

    def try_begin(self, address: str) -> bool:
        """Start a check for ``address``: False (nothing recorded) while it is locked."""
        now = self._clock()
        with self._lock:
            self._expire(now)
            if self._blocked(address):
                return False
            self._pending[address] = self._pending.get(address, 0) + 1
            self._pending_all += 1
            return True

    def finish(self, address: str, *, failed: bool) -> None:
        """End a check started with ``try_begin``; only a failed one is counted.

        The reservation is released and the failure recorded under one lock, so no
        parallel ``try_begin`` can slip in between.
        """
        now = self._clock()
        with self._lock:
            left = self._pending.get(address, 0) - 1
            if left > 0:
                self._pending[address] = left
            else:
                self._pending.pop(address, None)
            self._pending_all = max(0, self._pending_all - 1)
            if failed:
                self._record_failure(address, now)

    def fail(self, address: str) -> None:
        now = self._clock()
        with self._lock:
            self._record_failure(address, now)

    def _record_failure(self, address: str, now: float) -> None:
        """Count a failure; the caller holds the lock."""
        self._expire(now)
        self._all.append(now)
        times = self._failures.setdefault(address, deque())
        times.append(now)
        if len(times) >= PER_ADDRESS_FAILURES:
            self._locked_until[address] = now + WINDOW
            del self._failures[address]

    def _blocked(self, address: str) -> bool:
        if address in self._locked_until:
            return True
        mine = len(self._failures.get(address, ())) + self._pending.get(address, 0)
        return (mine >= PER_ADDRESS_FAILURES
                or len(self._all) + self._pending_all >= GLOBAL_FAILURES)

    def _expire(self, now: float) -> None:
        while self._all and now - self._all[0] >= WINDOW:
            self._all.popleft()
        for address in list(self._failures):
            times = self._failures[address]
            while times and now - times[0] >= WINDOW:
                times.popleft()
            if not times:
                del self._failures[address]
        for address in [a for a, t in self._locked_until.items() if now >= t]:
            del self._locked_until[address]


# ---------------------------------------------------------------- request helpers


def web_state():
    return current_app.extensions["orama"]


def current_session(*, create: bool = False) -> Session | None:
    """This request's session; with ``create`` a new pre-sign-in one when there is none."""
    session = g.get("session")
    if session is None and create:
        session = web_state().sessions.new(None, request.remote_addr or "")
        g.session = session
        g.set_cookie = session.id
    return session


def start_session(principal: Principal) -> Session:
    """Sign ``principal`` in with a fresh session id (the old session, if any, ends)."""
    sessions = web_state().sessions
    old = g.get("session")
    if old is not None:
        sessions.end(old.id)
    session = sessions.new(principal, request.remote_addr or "")
    g.session = session
    g.principal = principal
    g.set_cookie = session.id
    return session


def end_session() -> None:
    old = g.get("session")
    if old is not None:
        web_state().sessions.end(old.id)
    g.session = None
    g.principal = None
    g.clear_cookie = True


def change_password(old: str, new: str) -> bool:
    """Change the password; on success every session ends and this one starts afresh.

    ValueError for a ``new`` password of the wrong length.
    """
    principal = g.get("principal")
    if principal is None:
        return False
    st = web_state()
    if not st.passwords.change(old, new):
        return False
    st.sessions.end_all()
    g.session = None
    start_session(principal)
    return True


def safe_next(value: str | None) -> str:
    """``value`` when it is a path on this site, else ``/``."""
    if not value or not value.startswith("/") or value.startswith("//"):
        return "/"
    if "\\" in value or any(ord(c) < 0x20 or ord(c) == 0x7F for c in value):
        return "/"
    parts = urlsplit(value)
    if parts.scheme or parts.netloc:
        return "/"
    return value


def requires(permission: str) -> Callable:
    """Page decorator: signed out → sign-in page (coming back here); no permission → 403."""

    def decorate(view: Callable) -> Callable:
        @functools.wraps(view)
        def wrapper(*args, **kwargs):
            principal = g.get("principal")
            if principal is None:
                if request.headers.get("HX-Request") == "true":
                    # An htmx poll would swap the sign-in page into the part it updates:
                    # tell htmx to load the sign-in page as a whole page instead.
                    response = make_response("", 200)
                    response.headers["HX-Redirect"] = url_for("auth.signin")
                    return response
                path = request.full_path.rstrip("?") if request.query_string else request.path
                return redirect(url_for("auth.signin", next=safe_next(path)), 303)
            if permission not in principal.permissions:
                abort(403)
            return view(*args, **kwargs)

        return wrapper

    return decorate


def code_matches(path: Path, code: str) -> bool:
    """True when ``code`` equals the first-run code in ``path`` (a regular file, no link)."""
    raw = read_private_file(path)
    if raw is None:
        return False
    try:
        expected = raw.decode("ascii").strip()
    except UnicodeDecodeError:
        return False
    if not expected or not code:
        return False
    return hmac.compare_digest(code.encode(), expected.encode())
