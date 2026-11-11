"""Real-browser pass: Chromium drives the whole app, with fakes for everything outside it.

Marked ``integration`` and skipped when Node, Playwright or Chromium is missing, so the unit
run never needs a browser. The browser is what finds what Flask's test client cannot: the
``Origin`` header real browsers send, CSP violations, htmx swaps, focus and the layout at
phone width. No network is used: the Agent, Prowlarr, SABnzbd, Plex and the apply child are
fakes, and the secrets are made at run time.

The driver is ``browser_pass.cjs``; it fails on any console error, script error, failed or
refused request, sideways scrolling, unlabelled control or secret echoed back in a page.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from orama.web import create_app
from orama.web.jobs import JobRunner, store_redactor
from orama.wiring import prowlarr, sabnzbd
from orama.wiring.http import FakeHttp, Response

from .conftest import write_setup
from .test_wizard_indexers import DEFINITION

SCRIPT = Path(__file__).with_name("browser_pass.cjs")
NO_BROWSER = 3
PORTS = {"plex": 32400, "jellyfin": 8096, "sonarr": 8989, "radarr": 7878, "prowlarr": 9696,
         "bazarr": 6767, "sabnzbd": 8080, "qbittorrent": 8081}

pytestmark = pytest.mark.integration

# The apply child: prints a short checklist slowly, so the page can be seen updating.
FAKE_APPLY = (
    "import sys, time\n"
    "for line in ('✓ Your apps are running', '✓ Prowlarr is connected', '✓ All set'):\n"
    "    print(line, flush=True)\n"
    "    time.sleep(1.1)\n"
)


def _node_has_playwright() -> bool:
    node = shutil.which("node")
    if node is None:
        return False
    probe = subprocess.run([node, "-e", "require('playwright')"], check=False,
                           capture_output=True, timeout=30)
    return probe.returncode == 0


class FakeAgent:
    def status(self):
        return {name: "running" for name in PORTS}

    def data_space(self):
        return {"total_bytes": 1000 * 1024**3, "free_bytes": 500 * 1024**3}

    def self_update_pull(self):
        return True

    def self_update(self):
        return True


class Served:
    def __init__(self, base: str, state_dir: Path, code: str, password: str) -> None:
        self.base, self.state_dir, self.code, self.password = base, state_dir, code, password


@pytest.fixture
def serve(tmp_path, monkeypatch):
    """Start the real app on a free port; ``serve(first_run=..., installed=...)``."""
    from waitress import create_server

    from orama.__main__ import SERVE_OPTIONS

    servers = []

    def start(*, first_run: bool, installed: bool = False) -> Served:
        state_dir = tmp_path / (f"orama-{len(servers)}" if servers else "orama")
        state_dir.mkdir(mode=0o700)
        # The installer leaves a draft; the first apply turns it into the real setup.
        write_setup(state_dir, "setup.json" if installed else "setup.draft.json", ports=PORTS)
        code = secrets.token_urlsafe(16)
        password = "pw-" + secrets.token_urlsafe(12)
        http = FakeHttp()
        pin_checks = []

        def pin_status(_call):
            pin_checks.append(1)
            body = {"authToken": secrets.token_urlsafe(20)} if len(pin_checks) > 2 else {}
            return Response(200, {}, json.dumps(body).encode())

        http.route("POST", "/api/v2/pins", lambda _: Response(
            201, {}, json.dumps({"id": 51, "code": "ABCD", "expiresIn": 900}).encode()))
        http.route("GET", "/api/v2/pins/51", pin_status)
        http.route("GET", "/api/v3/movie/lookup", lambda call: Response(
            200, {}, json.dumps([] if "UnknownTitle" in call.url else [{
                "tmdbId": 12345,
                "title": "Dune: Part Two",
                "year": 2024,
                "hasFile": False,
                "images": [],
            }]).encode()))
        http.route("GET", "/api/v3/movie", lambda _: Response(200, {}, b"[]"))
        app = create_app(state_dir, agent=FakeAgent(), http=http)
        st = app.extensions["orama"]
        st.store.set("apikey:radarr", secrets.token_hex(16))
        st.store.set("apikey:sonarr", secrets.token_hex(16))
        st.jobs = JobRunner(state_dir, lambda _path: [sys.executable, "-c", FAKE_APPLY],
                            redact=store_redactor(st.store))
        if first_run:
            (state_dir / "first-run.code").write_text(code)
            os.chmod(state_dir / "first-run.code", 0o600)
        else:
            st.passwords.create(password)
        options = {**SERVE_OPTIONS, "host": "127.0.0.1", "port": 0, "threads": 6}
        server = create_server(app, **options)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        servers.append((server, thread))
        return Served(f"http://127.0.0.1:{server.effective_port}", state_dir, code, password)

    # What Prowlarr and SABnzbd would answer: the page tests pass and records it.
    monkeypatch.setattr("orama.web.catalogue.load", lambda *args: [DEFINITION])

    def passing(kind):
        def run(setup, store, http, slug):
            store.set(f"test-ok:{kind}:{slug}", "1")
        return run

    monkeypatch.setattr(prowlarr, "test_indexer", passing("indexer"))
    monkeypatch.setattr(sabnzbd, "test_usenet_server", passing("usenet"))
    try:
        yield start
    finally:
        for server, thread in servers:
            server.close()
            thread.join(5)


def run_browser(scenario: str, served: Served, *, width: int | None = None,
                scheme: str | None = None, timeout: int = 240) -> None:
    """Run one scenario. ``ORAMA_TEST_SCHEME``, ``_WIDTH``, ``_SHOTS_DIR`` and ``_AXE`` in the
    environment pick the colour scheme and width, save screenshots and add axe-core's rules."""
    if not _node_has_playwright():
        pytest.skip("Node with Playwright is not available")
    env = {**os.environ, "ORAMA_TEST_CODE": served.code, "ORAMA_TEST_PASSWORD": served.password,
           "ORAMA_TEST_WIDTH": str(width or os.environ.get("ORAMA_TEST_WIDTH", "1280"))}
    if scheme is not None:
        env["ORAMA_TEST_SCHEME"] = scheme
    result = subprocess.run(
        ["node", str(SCRIPT), scenario, served.base], check=False, capture_output=True,
        text=True, timeout=timeout, cwd=SCRIPT.parent, env=env,
    )
    if result.returncode == NO_BROWSER:
        pytest.skip("Chromium is not available: " + result.stderr.strip())
    assert result.returncode == 0, result.stderr + result.stdout
    print(result.stdout)  # the driver's notes (small links, axe results) show with -s
    assert result.stdout.strip().splitlines()[-1] == "ok"
    assert served.password not in result.stdout + result.stderr


def test_wizard_end_to_end_with_fakes(serve):
    served = serve(first_run=True)
    run_browser("wizard", served)
    assert (served.state_dir / "auth.json").exists()
    assert not (served.state_dir / "first-run.code").exists()
    assert (served.state_dir / "setup.json").exists()
    assert not (served.state_dir / "setup.draft.json").exists()
    # The wizard's answers reached the saved setup; Settings then turned the VPN off.
    saved = json.loads((served.state_dir / "setup.json").read_text())
    assert sorted(saved["media_servers"]) == ["jellyfin", "plex"]
    assert saved["device"] == "apple_tv" and saved["vpn"] is None
    assert [s["host"] for s in saved["usenet_servers"]] == ["news.example"]
    assert len(saved["indexers"]) == 1


def test_keyboard_only_wizard(serve):
    run_browser("keyboard", serve(first_run=False))


def test_no_horizontal_scroll_at_360px(serve):
    # The whole wizard, Settings and Apps at phone width: every page is checked on load.
    run_browser("wizard", serve(first_run=True), width=360)


def test_apps_focus_survives_refresh(serve):
    run_browser("apps-focus", serve(first_run=False, installed=True))


def test_requests_browser_flow(serve):
    run_browser("requests", serve(first_run=False, installed=True))
    run_browser("requests", serve(first_run=False, installed=True), width=360, scheme="dark")


def test_settings_password_strength_browser(serve):
    run_browser("password-strength", serve(first_run=False, installed=True))


