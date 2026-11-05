"""Real-browser check: Chromium creates the password, signs out and signs in again.

Marked ``integration`` and skipped when Node, Playwright or Chromium is missing, so the
unit run never needs a browser. It catches what the Flask test client cannot emulate,
such as the ``Origin`` header real browsers send on form posts.
"""

from __future__ import annotations

import os
import secrets
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

from orama.web import create_app

from .conftest import write_setup

SCRIPT = Path(__file__).with_name("browser_signin.js")
NO_BROWSER = 3

pytestmark = pytest.mark.integration


def _node_has_playwright() -> bool:
    node = shutil.which("node")
    if node is None:
        return False
    probe = subprocess.run([node, "-e", "require('playwright')"], check=False,
                           capture_output=True, timeout=30)
    return probe.returncode == 0


@pytest.fixture
def served_app(tmp_path):
    from waitress import create_server

    from orama.__main__ import SERVE_OPTIONS

    state_dir = tmp_path / "orama"
    state_dir.mkdir(mode=0o700)
    write_setup(state_dir)
    code = secrets.token_urlsafe(16)
    (state_dir / "first-run.code").write_text(code)
    os.chmod(state_dir / "first-run.code", 0o600)
    options = {**SERVE_OPTIONS, "host": "127.0.0.1", "port": 0, "threads": 4}
    server = create_server(create_app(state_dir), **options)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.effective_port}", code, state_dir
    finally:
        server.close()
        thread.join(5)


def test_chromium_first_run_sign_out_and_sign_in(served_app):
    if not _node_has_playwright():
        pytest.skip("Node with Playwright is not available")
    base, code, state_dir = served_app
    password = "pw-" + secrets.token_urlsafe(12)
    env = {**os.environ, "ORAMA_TEST_CODE": code, "ORAMA_TEST_PASSWORD": password}
    result = subprocess.run(
        ["node", str(SCRIPT), base], check=False,
        capture_output=True, text=True, timeout=120, cwd=SCRIPT.parent, env=env,
    )
    if result.returncode == NO_BROWSER:
        pytest.skip("Chromium is not available: " + result.stderr.strip())
    returncode, stderr = result.returncode, result.stderr
    assert returncode == 0, stderr
    assert result.stdout.strip() == "ok"
    assert (state_dir / "auth.json").exists()
    assert not (state_dir / "first-run.code").exists()
    assert password not in result.stdout + result.stderr
