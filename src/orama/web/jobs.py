"""Apply jobs: run ``orama apply`` as a child process and show its checklist live.

One job runs at a time. A reader thread collects the child's output lines (already redacted by the
CLI's own logging filter, and redacted again here); the page polls :func:`current` snapshots. The
last job is kept in ``last-apply.json`` so a restart shows "interrupted" instead of spinning.
"""

from __future__ import annotations

import json
import logging
import math
import os
import secrets
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from flask import Blueprint, Flask, redirect, render_template, request, url_for

from orama import state
from orama.cli import APPLY_BUSY
from orama.secrets import RedactingFilter, SecretStore
from orama.web import auth

LAST_APPLY_FILE = "last-apply.json"
MAX_LINES = 500
MAX_LINE_CHARS = 500
MAX_RECORD_BYTES = 1024 * 1024
SETUP_FILE = "setup.json"
TEST_BUSY = "Orama is busy applying the setup. Try again in a moment."

Status = Literal["running", "done", "failed", "interrupted"]
_STATUSES = ("running", "done", "failed", "interrupted")

log = logging.getLogger("orama.web.jobs")


@dataclass(frozen=True)
class Job:
    status: Status
    lines: list[str] = field(default_factory=list)
    started: float = 0.0
    finished: float | None = None


def default_argv(setup_path: Path) -> list[str]:
    return [sys.executable, "-m", "orama.cli", "apply", "--setup", str(setup_path)]


class JobRunner:
    def __init__(
        self,
        state_dir: Path,
        argv_for: Callable[[Path], list[str]] = default_argv,
        *,
        redact: Callable[[str], str] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._path = Path(state_dir) / LAST_APPLY_FILE
        self._argv_for = argv_for
        self._redact = redact
        self._clock = clock
        self._lock = threading.Lock()
        self._idle = threading.Event()
        self._idle.set()
        self._status: Status | None = None
        self._lines: deque[str] = deque(maxlen=MAX_LINES)
        self._started = 0.0
        self._finished: float | None = None
        self._load()

    # ---------------------------------------------------------------- public

    def current(self) -> Job | None:
        with self._lock:
            return self._snapshot()

    def start(self, setup_path: Path, after: Callable[[int], None] | None = None) -> Job:
        """Start an apply, or return the one already running."""
        with self._lock:
            if self._status == "running":
                return self._snapshot()  # type: ignore[return-value]
            self._status = "running"
            self._lines = deque(maxlen=MAX_LINES)
            self._started = self._clock()
            self._finished = None
            self._idle.clear()
            proc = None
            try:
                proc = subprocess.Popen(
                    self._argv_for(Path(setup_path)),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                )
                self._save_locked()
                threading.Thread(
                    target=self._read, args=(proc, after), name="orama-apply-reader", daemon=True
                ).start()
            except Exception:  # noqa: BLE001  (any failure: never stay "running")
                if proc is not None:
                    try:
                        proc.kill()
                    except Exception:  # noqa: BLE001, S110
                        pass
                self._lines.append("✗ Orama could not be started to apply the setup.")
                self._status = "failed"
                self._finished = self._clock()
                self._save_locked()
                self._idle.set()
                failed = self._snapshot()
            else:
                return self._snapshot()  # type: ignore[return-value]
        self._run_after(after, 1)
        return failed  # type: ignore[return-value]

    def wait(self, timeout: float | None = None) -> bool:
        """Block until no job is running (for tests and shutdown)."""
        return self._idle.wait(timeout)

    # ---------------------------------------------------------------- internals

    def _snapshot(self) -> Job | None:
        if self._status is None:
            return None
        return Job(self._status, list(self._lines), self._started, self._finished)

    def _clean(self, raw: str) -> str:
        line = raw.rstrip("\r\n")
        if self._redact is not None:
            try:
                line = self._redact(line)
            except Exception:  # noqa: BLE001  (never show a line we could not redact)
                line = "***"
        return line[:MAX_LINE_CHARS]

    def _read(self, proc: subprocess.Popen, after: Callable[[int], None] | None) -> None:
        code = 1
        try:
            assert proc.stdout is not None
            for raw in proc.stdout:
                line = self._clean(raw)
                with self._lock:
                    self._lines.append(line)
            code = proc.wait()
        except Exception:  # noqa: BLE001  (message only: never raw child output)
            log.error("reading the apply output failed")
            with self._lock:
                self._lines.append("✗ Orama lost track of the apply. Apply again.")
            try:
                proc.kill()
                proc.wait()
            except OSError:
                pass
        finally:
            with self._lock:
                self._status = "done" if code == 0 else "failed"
                self._finished = self._clock()
                self._save_locked()
                self._idle.set()
            try:
                if proc.stdout is not None:
                    proc.stdout.close()
            except Exception:  # noqa: BLE001, S110
                pass
        self._run_after(after, code)

    @staticmethod
    def _run_after(after: Callable[[int], None] | None, code: int) -> None:
        if after is not None:
            try:
                after(code)
            except Exception:  # noqa: BLE001
                log.error("the after-apply step failed")

    def _load(self) -> None:
        try:
            with open(self._path, "rb") as f:
                raw = f.read(MAX_RECORD_BYTES + 1)
            if len(raw) > MAX_RECORD_BYTES:
                return
            data = json.loads(raw.decode("utf-8"))
            status = data["status"]
            if status not in _STATUSES:
                return
            if not isinstance(data.get("lines", []), list):
                return
            lines = [str(x)[:MAX_LINE_CHARS] for x in data.get("lines", [])]
            started = float(data.get("started") or 0.0)
            finished = data.get("finished")
            finished = None if finished is None else float(finished)
            if not math.isfinite(started) or (finished is not None and not math.isfinite(finished)):
                return
        except Exception:  # noqa: BLE001  (garbled record: start as if none)
            return
        self._lines = deque(lines[-MAX_LINES:], maxlen=MAX_LINES)
        self._started, self._finished = started, finished
        self._status = status
        if status == "running":  # the process that ran it is gone
            self._status = "interrupted"
            try:
                self._save_locked()
            except OSError:
                log.error("could not save the apply record")

    def _save_locked(self) -> None:
        data = json.dumps(
            {
                "status": self._status,
                "started": self._started,
                "finished": self._finished,
                "lines": list(self._lines),
            }
        ).encode()
        tmp = self._path.with_name(f".{self._path.name}.{secrets.token_hex(8)}.tmp")
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._path)
        except OSError:
            tmp.unlink(missing_ok=True)
            log.error("could not save the apply record")


# ---------------------------------------------------------------- web


def store_redactor(store: SecretStore) -> Callable[[str], str]:
    """A line redactor using the secret store's values and the key/value pattern.

    The store is read per line on purpose: it picks up secrets created in the middle of an apply.
    """
    return RedactingFilter(store).redact


def poll_view(job: Job | None) -> tuple[dict, dict]:
    """Template context and headers for an htmx poll of the checklist.

    The poller says how many lines the page already shows and which job they belong to.
    When they match, only the new lines are appended (and nothing at all when there are
    none), so a screen reader hears each line once instead of the whole list every 2 s.
    Anything else (a new job, a bad count, a POST) sends the whole list.
    """
    seen = request.args.get("seen", type=int)
    since = request.args.get("since", type=int)
    # With nothing shown yet the page holds the "Waiting to start." placeholder: replace it.
    if (request.method != "GET" or job is None or not seen or since != job_stamp(job)
            or not 0 < seen <= len(job.lines)):
        return {"new_lines": None}, {}
    return {"new_lines": job.lines[seen:]}, {"HX-Reswap": "beforeend"}


def job_stamp(job: Job) -> int:
    return int(job.started * 1000)


def register_jobs(app: Flask) -> None:
    app.register_blueprint(_job_pages())


@contextmanager
def apply_operation(st):
    """Serialize web operations through the gap before the CLI takes apply.lock."""
    if not st.apply_gate.acquire(blocking=False):
        raise state.Busy
    try:
        yield
    finally:
        st.apply_gate.release()


def _job_pages() -> Blueprint:
    pages = Blueprint("jobs", __name__)

    def render(job: Job | None, **extra):
        htmx = request.headers.get("HX-Request") == "true"
        template = "_job_poll.html" if htmx else "_job.html"
        context, headers = poll_view(job) if htmx else ({}, {})
        return render_template(template, job=job, running=bool(job and job.status == "running"),
                               **context, **extra), 200, headers

    @pages.get("/jobs/current")
    @auth.requires("setup")
    def current():
        return render(auth.web_state().jobs.current())

    @pages.post("/jobs/apply")
    @auth.requires("setup")
    def apply():
        st = auth.web_state()
        # The wizard's starting page polls itself (it shows Continue when the job ends).
        extra = ({"starting": True, "poll_url": "/setup/starting"}
                 if request.form.get("return") == "starting" else {})
        try:
            with apply_operation(st):
                current = st.jobs.current()
                if current and current.status == "running":
                    job = current
                else:
                    with state.locked(state.apply_lock(st.state_dir), blocking=False):
                        pass
                    job = st.jobs.start(st.state_dir / SETUP_FILE)
        except state.Busy:
            if request.headers.get("HX-Request") == "true":
                # htmx does not swap a 4xx answer, so the notice comes as a 200 partial.
                return render(st.jobs.current(), notice_message=APPLY_BUSY, **extra)
            return render_template("rejected.html", message=TEST_BUSY), 409
        if request.headers.get("HX-Request") == "true":
            return render(job, **extra)
        return redirect(url_for("jobs.current"), 303)

    return pages
