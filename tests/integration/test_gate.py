"""The integration gate itself (a fast unit test): opted in means fail, never skip."""

import pytest

from . import conftest as it


@pytest.fixture
def capable(monkeypatch):
    """A host with everything the integration test needs; tests take one thing away."""
    monkeypatch.setenv("ORAMA_INTEGRATION", "1")
    monkeypatch.setenv("ORAMA_TEST_PUID", "1000")
    monkeypatch.setenv("ORAMA_TEST_PGID", "1000")
    monkeypatch.setattr(it.os, "geteuid", lambda: 0)
    monkeypatch.setattr(it, "_docker_works", lambda: True)
    return monkeypatch


def test_capable_host_passes(capable):
    assert it.require_host() == (1000, 1000)


def test_not_opted_in_skips(capable):
    capable.delenv("ORAMA_INTEGRATION")
    with pytest.raises(pytest.skip.Exception):
        it.require_host()


@pytest.mark.parametrize("missing", ["root", "docker", "ids"])
def test_opted_in_but_missing_prerequisite_fails(capable, missing):
    if missing == "root":
        capable.setattr(it.os, "geteuid", lambda: 1000)
    elif missing == "docker":
        capable.setattr(it, "_docker_works", lambda: False)
    else:
        capable.setenv("ORAMA_TEST_PUID", "0")
    with pytest.raises(pytest.fail.Exception):
        it.require_host()
