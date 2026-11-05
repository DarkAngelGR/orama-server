"""Apply jobs: run ``orama apply`` as a child process and show its checklist live.

One job runs at a time. A reader thread collects the child's output lines (already redacted by the
CLI's own logging filter, and redacted again here); the page polls :func:`current` snapshots. The
last job is kept in ``last-apply.json`` so a restart shows "interrupted" instead of spinning.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from flask import Blueprint, Flask, redirect, render_template, request, url_for

from orama.web import auth

LAST_APPLY_FILE = "last-apply.json"
MAX_LINES = 500
MAX_LINE_CHARS = 500
SETUP_FILE = "setup.json"

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
            try:
                proc = subprocess.Popen(
                    self._argv_for(Path(setup_path)),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    errors="replace",
                    bufsize=1,
                    env=dict(os.environ),
                )
            except OSError:
                self._lines.append("✗ Orama could not be started to apply the setup.")
                self._status = "failed"
                self._finished = self._clock()
                self._save_locked()
                self._idle.set()
                return self._snapshot()  # type: ignore[return-value]
            self._save_locked()
            snapshot = self._snapshot()
        threading.Thread(
            target=self._read, args=(proc, after), name="orama-apply-reader", daemon=True
        ).start()
        return snapshot  # type: ignore[return-value]

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
            if proc.stdout is not None:
                proc.stdout.close()
            with self._lock:
                self._status = "done" if code == 0 else "failed"
                self._finished = self._clock()
                self._save_locked()
                self._idle.set()
        if after is not None:
            try:
                after(code)
            except Exception:  # noqa: BLE001
                log.error("the after-apply step failed")

    def _load(self) -> None:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            status = data["status"]
            if status not in _STATUSES:
                return
            lines = [str(x)[:MAX_LINE_CHARS] for x in data.get("lines", [])]
            started = float(data.get("started") or 0.0)
            finished = data.get("finished")
            finished = None if finished is None else float(finished)
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
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


def store_redactor(store) -> Callable[[str], str]:
    """A line redactor using the secret store's values and the key/value pattern."""
    from orama.secrets import RedactingFilter

    flt = RedactingFilter(store)

    def redact(text: str) -> str:
        try:
            literals = flt._literals()
        except Exception:  # noqa: BLE001
            literals = []
        return flt._redact(text, literals)

    return redact


def register_jobs(app: Flask) -> None:
    app.register_blueprint(_job_pages())


def _job_pages() -> Blueprint:
    pages = Blueprint("jobs", __name__)

    def render(job: Job | None):
        htmx = request.headers.get("HX-Request") == "true"
        template = "_job_poll.html" if htmx else "_job.html"
        return render_template(template, job=job, running=bool(job and job.status == "running"))

    @pages.get("/jobs/current")
    @auth.requires("setup")
    def current():
        return render(auth.web_state().jobs.current())

    @pages.post("/jobs/apply")
    @auth.requires("setup")
    def apply():
        st = auth.web_state()
        job = st.jobs.start(st.state_dir / SETUP_FILE)
        if request.headers.get("HX-Request") == "true":
            return render(job)
        return redirect(url_for("jobs.current"), 303)

    return pages
