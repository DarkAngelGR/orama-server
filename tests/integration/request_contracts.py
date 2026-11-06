"""Assert request contracts on the pinned apps in the disposable CI install.

Unlike the older exploratory probes, failures here fail the integration job. Requests
and errors never print API keys, webhook tokens, or response bodies.
"""
import copy
import json
import secrets
import time
from pathlib import Path

import pytest

from orama.wiring.http import Http, Unreachable

from .conftest import run


def call(port, key, path, *, method="GET", body=None, expected=200):
    try:
        response = Http().request(method, f"http://127.0.0.1:{port}/api/v3/{path}",
                                  headers={"X-Api-Key": key}, json=body)
    except Unreachable:
        pytest.fail(f"request contract endpoint unavailable: {method} {path}")
    if response.status != expected:
        pytest.fail(f"request contract {method} {path}: HTTP {response.status}, expected {expected}")
    if not response.body:
        return None
    try:
        return response.json()
    except ValueError:
        pytest.fail(f"request contract {method} {path}: invalid JSON (body withheld)")


def read_contracts(app, port, key):
    status = call(port, key, "system/status")
    assert isinstance(status.get("version"), str)
    for resource in ("queue", "history"):
        page = call(port, key, f"{resource}?page=1&pageSize=1")
        assert page["page"] == page["pageSize"] == 1
        assert isinstance(page["totalRecords"], int) and page["totalRecords"] >= 0
        assert isinstance(page["records"], list) and len(page["records"]) <= 1
    assert isinstance(call(port, key, "command"), list)
    library = "series" if app == "sonarr" else "movie"
    assert isinstance(call(port, key, library), list)
    file_path = "episodefile?seriesId=2147483647" if app == "sonarr" else "moviefile?movieId=2147483647"
    assert call(port, key, file_path) == []
    profiles = call(port, key, "qualityprofile")
    mine = [p for p in profiles if p["name"] == "Orama"]
    assert len(mine) == 1
    for field in ("id", "minFormatScore", "cutoffFormatScore"):
        assert isinstance(mine[0][field], int)
    assert all(isinstance(i["format"], int) and isinstance(i["score"], int)
               for i in mine[0]["formatItems"])
    assert isinstance(mine[0]["items"], list)


def authenticated_webhooks(ports, keys, folder):
    token = secrets.token_hex(32)
    (folder / "token").write_text(token)
    (folder / "token").chmod(0o600)
    name = "orama-test-request-hooks-" + secrets.token_hex(4)
    script = Path(__file__).with_name("request_hook_receiver.py")
    created = []
    try:
        started = run(["docker", "run", "-d", "--name", name, "--network", "orama-net",
                       "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                       "-v", f"{folder}:/capture", "-v", f"{script}:/receiver.py:ro",
                       "python:3.12-slim", "python", "/receiver.py"], timeout=300)
        assert started.returncode == 0, "request webhook receiver did not start"
        deadline = time.monotonic() + 30
        while not (folder / "ready").exists():
            if time.monotonic() >= deadline:
                pytest.fail("request webhook receiver not ready")
            time.sleep(0.1)
        for app in ("sonarr", "radarr"):
            port, key = ports[app], keys[f"apikey:{app}"]
            schema = next(s for s in call(port, key, "notification/schema")
                          if s.get("implementation") == "Webhook")
            fields = {f["name"]: f for f in schema["fields"]}
            assert {"url", "method", "headers"} <= fields.keys()
            options = fields["method"]["selectOptions"]
            post = next(o["value"] for o in options if o["name"].upper() == "POST")
            payload = copy.deepcopy(schema)
            payload.update(name="Request contract (Orama)", onGrab=True, onDownload=True,
                           onUpgrade=True)
            values = {"url": f"http://{name}:8080/hooks/{app}", "method": post,
                      "headers": [{"key": "Authorization", "value": "Bearer " + token}]}
            for field in payload["fields"]:
                if field["name"] in values:
                    field["value"] = values[field["name"]]
            # The app sends its real Test webhook and exercises the custom auth header.
            call(port, key, "notification/test", method="POST", body=payload)
            saved = call(port, key, "notification", method="POST", body=payload, expected=201)
            created.append((port, key, saved["id"]))
            have = call(port, key, f"notification/{saved['id']}")
            assert have["implementation"] == "Webhook"
            assert have["onGrab"] is True and have["onDownload"] is True
        events = [json.loads(line) for line in (folder / "events").read_text().splitlines()]
        for app in ("sonarr", "radarr"):
            matched = [e for e in events if e["path"] == f"/hooks/{app}"]
            assert matched and all(e["authenticated"] for e in matched)
            assert any(e["eventType"] == "Test" for e in matched)
            assert all("eventType" in e["fields"] for e in matched)
    finally:
        try:
            for port, key, identifier in created:
                call(port, key, f"notification/{identifier}", method="DELETE")
        finally:
            run(["docker", "rm", "-f", name])
            (folder / "token").unlink(missing_ok=True)
