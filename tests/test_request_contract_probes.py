"""Failure output from CI probes must not disclose temporary app credentials."""
import os
import secrets
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("probe", ["read", "webhook"])
def test_probe_failures_hide_credential_arguments(tmp_path, probe):
    token = secrets.token_hex(32)
    source = '''
import os
from types import SimpleNamespace
from integration import request_contracts as probes

def test_probe_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(probes.Http, "request", lambda *a, **kw: SimpleNamespace(status=500))
    monkeypatch.setattr(probes, "run", lambda *a, **kw: SimpleNamespace(returncode=1))
    if os.environ["PROBE_KIND"] == "read":
        probes.read_contracts("sonarr", 1, os.environ["PROBE_TEST_KEY"])
    else:
        probes.authenticated_webhooks({}, {"apikey:sonarr": os.environ["PROBE_TEST_KEY"]}, tmp_path)
'''
    test = tmp_path / "test_failure.py"
    test.write_text(source)
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(test)],
        env={**os.environ, "PYTHONPATH": str(Path(__file__).parent.resolve()),
             "PROBE_TEST_KEY": token, "PROBE_KIND": probe},
        capture_output=True, text=True, timeout=30, check=False)
    output = result.stdout + result.stderr
    assert result.returncode == 1
    assert "1 failed" in output
    if token in output:
        pytest.fail("probe failure disclosed credentials (output withheld)")


def test_movie_score_probe_ignores_tv_releases(monkeypatch):
    from integration import request_contracts as probes

    strict = {"id": 1, "name": "Orama", "minFormatScore": 0}
    item = {"id": 7, "qualityProfileId": 1}

    def call(port, key, path, *, method="GET", body=None, expected=200):
        if path.startswith("release?"):
            return [
                {"title": "Breaking.Bad.S01E01.1080p.WEB-DL.DTS.x264",
                 "customFormatScore": -10000, "rejected": True,
                 "rejections": ["Custom format score", "Wrong title"]},
                {"title": "Inception.2010.1080p.WEB-DL.DTS.x264",
                 "customFormatScore": -10000, "rejected": item["qualityProfileId"] == 1,
                 "rejections": ["Custom format score"] if item["qualityProfileId"] == 1 else []},
            ]
        if path == "qualityprofile" and method == "POST":
            return {**body, "id": 2}
        if path == "qualityprofile/1":
            return strict
        if path == "movie/7":
            if method == "PUT":
                item.update(body)
            return dict(item)
        assert path == "qualityprofile/2" and method == "DELETE"

    monkeypatch.setattr(probes, "call", call)
    probes.fallback_contract(1, secrets.token_hex(16), "movie", 7, strict, 7)
    assert item["qualityProfileId"] == 1
