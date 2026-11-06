"""Shared fixtures for the web app tests.

Addresses and secrets are built at run time: the repository is public.
"""

from __future__ import annotations

import base64
import re
import secrets as _secrets
from pathlib import Path

import pytest
from conftest import make_setup

from orama.web import create_app

FIFTEEN_MINUTES = 15 * 60
DAY = 24 * 60 * 60


def ip(*octets: int) -> str:
    return ".".join(str(o) for o in octets)


# A private LAN address and a host name, built at run time.
LAN = ip(192, 168, 7, 23)
NEW_LAN = ip(10, 1, 2, 3)
HOST_NAME = "nas-" + _secrets.token_hex(3)
EXTRA = "media-" + _secrets.token_hex(3) + ".tailnet.example"


class FakeClock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def write_setup(state_dir: Path, name: str = "setup.json", **overrides) -> None:
    values = {"lan_address": LAN, "host_name": HOST_NAME, "extra_host_names": (EXTRA,)}
    values.update(overrides)
    make_setup(**values).save(state_dir / name)


def wireguard_key() -> str:
    """A key shaped like a real WireGuard private key (32 random bytes in base64)."""
    return base64.b64encode(_secrets.token_bytes(32)).decode()


def new_password() -> str:
    return "pw-" + _secrets.token_urlsafe(12)


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    d = tmp_path / "orama"
    d.mkdir(mode=0o700)
    write_setup(d)
    return d


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def app(state_dir: Path, clock: FakeClock):
    app = create_app(state_dir, clock=clock)
    app.config["TESTING"] = True
    return app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def first_run_code(state_dir: Path) -> str:
    code = _secrets.token_urlsafe(16)
    path = state_dir / "first-run.code"
    path.write_text(code)
    path.chmod(0o600)
    return code


@pytest.fixture
def password(app) -> str:
    """A password already set (no first-run code left)."""
    pw = new_password()
    app.extensions["orama"].passwords.create(pw)
    return pw


_TOKEN = re.compile(r'name="csrf_token" value="([^"]+)"|"X-CSRF-Token": "([^"]+)"')


def csrf_from(response) -> str:
    """The page's CSRF token: from a form, else from the body's htmx headers."""
    match = _TOKEN.search(response.get_data(as_text=True))
    assert match, response.get_data(as_text=True)
    token = match.group(1) or match.group(2)
    assert token
    return token


def signin(client, password: str, **kwargs):
    token = csrf_from(client.get("/signin", **kwargs))
    return client.post(
        "/signin", data={"password": password, "csrf_token": token}, **kwargs
    )


def session_cookie(client) -> str | None:
    cookie = client.get_cookie("orama_session")
    return cookie.value if cookie else None


def signed_in(client, **kwargs) -> bool:
    response = client.get("/", **kwargs)
    return response.status_code == 200 and b"Welcome to Orama" in response.data
