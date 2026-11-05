"""Apply jobs: one child process, a live checklist, a record that survives a restart."""

from __future__ import annotations

import json
import stat
import sys
import threading
from pathlib import Path

import pytest

from orama.cli import APPLY_BUSY
from orama.web.jobs import MAX_LINE_CHARS, MAX_LINES, JobRunner

from .conftest import csrf_from, signin


def script_argv(code: str):
    def argv_for(setup_path: Path) -> list[str]:
        return [sys.executable, "-c", code, str(setup_path)]

    return argv_for


def wait(runner: JobRunner, timeout: float = 10.0):
    job = runner.current()
    assert job is not None
    assert runner.wait(timeout)
    return runner.current()


def test_job_streams_lines(tmp_path: Path):
    code = "print('✓ one', flush=True)\nprint('✓ two', flush=True)"
    runner = JobRunner(tmp_path, script_argv(code))
    runner.start(tmp_path / "setup.json")
    job = wait(runner)
    assert job.status == "done"
    assert job.lines == ["✓ one", "✓ two"]
    assert job.finished is not None
    saved = json.loads((tmp_path / "last-apply.json").read_text())
    assert saved["status"] == "done"
    assert saved["lines"] == ["✓ one", "✓ two"]
    assert stat.S_IMODE((tmp_path / "last-apply.json").stat().st_mode) == 0o600


def test_second_start_returns_running_job(tmp_path: Path):
    gate = tmp_path / "go"
    code = (
        "import sys,time,pathlib\n"
        "print('start',flush=True)\n"
        "g=pathlib.Path(sys.argv[1]).parent/'go'\n"
        "while not g.exists(): time.sleep(0.02)\n"
    )
    runner = JobRunner(tmp_path, script_argv(code))
    first = runner.start(tmp_path / "setup.json")
    second = runner.start(tmp_path / "setup.json")
    assert second.started == first.started
    assert second.status == "running"
    gate.write_text("x")
    assert wait(runner).status == "done"


def test_failed_exit_marks_failed(tmp_path: Path):
    runner = JobRunner(tmp_path, script_argv("print('✗ broke')\nraise SystemExit(1)"))
    runner.start(tmp_path / "setup.json")
    job = wait(runner)
    assert job.status == "failed"
    assert job.lines == ["✗ broke"]


def test_after_callback_gets_exit_code(tmp_path: Path):
    seen: list[int] = []
    called = threading.Event()

    def after(code: int) -> None:
        seen.append(code)
        called.set()

    runner = JobRunner(tmp_path, script_argv("raise SystemExit(3)"))
    runner.start(tmp_path / "setup.json", after=after)
    wait(runner)
    # The child can be idle before its callback finishes on the reader thread.
    assert called.wait(10)
    assert seen == [3]


def test_interrupted_after_restart(tmp_path: Path):
    (tmp_path / "last-apply.json").write_text(
        json.dumps({"status": "running", "started": 1.0, "finished": None, "lines": ["✓ a"]})
    )
    runner = JobRunner(tmp_path, script_argv("pass"))
    job = runner.current()
    assert job is not None and job.status == "interrupted"
    assert job.lines == ["✓ a"]
    assert json.loads((tmp_path / "last-apply.json").read_text())["status"] == "interrupted"
    # and a new job can start
    runner.start(tmp_path / "setup.json")
    assert wait(runner).status == "done"


def test_lines_capped(tmp_path: Path):
    code = f"for i in range({MAX_LINES + 50}): print(str(i))\nprint('x'*{MAX_LINE_CHARS + 100})"
    runner = JobRunner(tmp_path, script_argv(code))
    runner.start(tmp_path / "setup.json")
    job = wait(runner)
    assert len(job.lines) == MAX_LINES
    assert all(len(line) <= MAX_LINE_CHARS for line in job.lines)
    assert job.lines[-1] == "x" * MAX_LINE_CHARS


def test_cli_apply_busy_shows_plain_message(tmp_path: Path):
    code = f"print({APPLY_BUSY!r})\nraise SystemExit(1)"
    runner = JobRunner(tmp_path, script_argv(code))
    runner.start(tmp_path / "setup.json")
    job = wait(runner)
    assert job.status == "failed"
    assert APPLY_BUSY in job.lines[-1]


def test_lines_are_redacted(tmp_path: Path):
    runner = JobRunner(tmp_path, script_argv("print('hello SECRETVALUE')"),
                       redact=lambda t: t.replace("SECRETVALUE", "***"))
    runner.start(tmp_path / "setup.json")
    assert wait(runner).lines == ["hello ***"]


def test_missing_program_marks_failed(tmp_path: Path):
    runner = JobRunner(tmp_path, lambda p: ["/nonexistent/prog-" + "x"])
    runner.start(tmp_path / "setup.json")
    job = runner.current()
    assert job.status == "failed"
    assert "could not be started" in job.lines[-1]


# ------------------------------------------------------------------ routes


@pytest.fixture
def signed(app, client, password):
    resp = signin(client, password)
    assert resp.status_code == 303
    return client


def swap_runner(app, runner):
    app.extensions["orama"].jobs = runner


def test_current_requires_sign_in(client):
    assert client.get("/jobs/current").status_code == 303


def test_current_partial_polls_while_running(app, signed, tmp_path):
    gate = app.extensions["orama"].state_dir / "go"
    code = (
        "import sys,time,pathlib\nprint('✓ started',flush=True)\n"
        "g=pathlib.Path(sys.argv[1]).parent/'go'\n"
        "while not g.exists(): time.sleep(0.02)\n"
    )
    runner = JobRunner(app.extensions["orama"].state_dir, script_argv(code))
    swap_runner(app, runner)
    page = signed.get("/")
    token = csrf_from(page)
    resp = signed.post("/jobs/apply", data={"csrf_token": token})
    assert resp.status_code == 303
    htmx = signed.get("/jobs/current", headers={"HX-Request": "true"})
    body = htmx.get_data(as_text=True)
    assert 'hx-get="/jobs/current"' in body and 'hx-trigger="every 2s"' in body
    assert 'hx-target="#checklist-items"' in body and 'hx-swap="outerHTML"' in body
    assert 'id="checklist-items"' in body
    assert "aria-live" not in body
    full = signed.get("/jobs/current").get_data(as_text=True)
    assert "Applying your setup…" in full and 'aria-live="polite"' in full
    gate.write_text("x")
    assert runner.wait(10)
    done = signed.get("/jobs/current", headers={"HX-Request": "true"}).get_data(as_text=True)
    assert "every 2s" not in done and "✓ started" in done
    full = signed.get("/jobs/current").get_data(as_text=True)
    assert "All done" in full and "Apply again" in full


def test_failed_and_interrupted_words(app, signed):
    d = app.extensions["orama"].state_dir
    runner = JobRunner(d, script_argv("print('✗ no'); raise SystemExit(1)"))
    swap_runner(app, runner)
    runner.start(d / "setup.json")
    runner.wait(10)
    text = signed.get("/jobs/current").get_data(as_text=True)
    assert "Something needs attention — see the ✗ lines, fix them, then apply again." in text
    (d / "last-apply.json").write_text(
        json.dumps({"status": "running", "started": 1.0, "finished": None, "lines": []})
    )
    swap_runner(app, JobRunner(d, script_argv("pass")))
    text = signed.get("/jobs/current").get_data(as_text=True)
    assert "The last apply was interrupted. Apply again." in text


def test_apply_post_needs_csrf(app, signed):
    assert signed.post("/jobs/apply").status_code == 403


def test_apply_post_htmx_returns_partial(app, signed):
    d = app.extensions["orama"].state_dir
    runner = JobRunner(d, script_argv("print('✓ x')"))
    swap_runner(app, runner)
    token = csrf_from(signed.get("/"))
    resp = signed.post("/jobs/apply", headers={"HX-Request": "true", "X-CSRF-Token": token})
    assert resp.status_code == 200
    assert 'id="checklist-items"' in resp.get_data(as_text=True)
    runner.wait(10)


def test_no_job_yet(signed):
    text = signed.get("/jobs/current").get_data(as_text=True)
    assert "Apply" in text


# ------------------------------------------------------------------ fix round 1


def test_argv_error_fails_and_next_start_works(tmp_path: Path):
    calls = {"n": 0}
    seen: list[int] = []

    def argv_for(p):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ValueError("embedded null byte")
        return [sys.executable, "-c", "print('ok')"]

    runner = JobRunner(tmp_path, argv_for)
    job = runner.start(tmp_path / "setup.json", after=seen.append)
    assert job.status == "failed" and runner.wait(1)
    assert seen == [1]
    runner.start(tmp_path / "setup.json")
    assert wait(runner).status == "done"


def test_reader_failure_reaches_final_state(tmp_path: Path, monkeypatch):
    class Boom:
        def __iter__(self):
            raise RuntimeError("secret-ish detail")

        def close(self):
            raise OSError("close failed")

    class FakeProc:
        stdout = Boom()

        def kill(self):
            pass

        def wait(self):
            return 0

    monkeypatch.setattr("orama.web.jobs.subprocess.Popen", lambda *a, **k: FakeProc())
    runner = JobRunner(tmp_path, script_argv("pass"))
    runner.start(tmp_path / "setup.json")
    assert runner.wait(5)
    job = runner.current()
    assert job.status == "failed"
    assert "secret-ish" not in " ".join(job.lines)


def test_concurrent_starts_make_one_child(tmp_path: Path):
    count = tmp_path / "count"
    code = (
        "import sys,time\nopen(sys.argv[1]+'.n','a').write('x')\ntime.sleep(0.5)\n"
    )
    runner = JobRunner(tmp_path, lambda p: [sys.executable, "-c", code, str(tmp_path / "count")])
    threads = [threading.Thread(target=runner.start, args=(tmp_path / "setup.json",))
               for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert runner.wait(10)
    assert Path(str(count) + ".n").read_text() == "x"


@pytest.mark.parametrize(
    "content",
    [
        b"not json",
        b"[]",
        b'{"status": 5}',
        b'{"status": "done", "lines": "abc", "started": 1}',
        b'{"status": "done", "lines": {"a": 1}, "started": 1}',
        b'{"status": "done", "lines": [], "started": 1e999999}',
        b'{"status": "done", "lines": [], "started": ' + b"9" * 5000 + b"}",
        b"\xff\xfe",
        b'{"status": "done", "lines": ["' + b"a" * (2 * 1024 * 1024) + b'"]}',
    ],
)
def test_hostile_record_is_ignored(tmp_path: Path, content: bytes):
    (tmp_path / "last-apply.json").write_bytes(content)
    runner = JobRunner(tmp_path, script_argv("pass"))
    assert runner.current() is None
