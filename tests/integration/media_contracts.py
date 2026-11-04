"""Read and exercise the pinned images only inside the disposable CI install."""

import json
import os
import secrets
import urllib.error
import urllib.request

import yaml

from orama.catalog import CATALOG

from .conftest import run


def _request(port, path, *, body=None, headers=None):
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=None if body is None else json.dumps(body).encode(),
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as reply:
            return reply.status, reply.read()
    except urllib.error.HTTPError as exc:
        return exc.code, b""


def jellyfin(port):
    status, raw = _request(port, "/System/Info/Public")
    assert status == 200 and not json.loads(raw)["StartupWizardCompleted"]
    status, _ = _request(port, "/Startup/User")
    assert status == 200
    status, _ = _request(port, "/Startup/User", body={
        "Name": "orama-test", "Password": secrets.token_urlsafe(24),
    })
    assert status in (200, 204)
    status, _ = _request(port, "/Startup/Complete", body={})
    assert status in (200, 204)
    status, raw = _request(port, "/System/Info/Public")
    assert status == 200 and json.loads(raw)["StartupWizardCompleted"]
    for path in ("/Startup/User", "/Startup/Configuration"):
        status, _ = _request(port, path)
        assert status in (401, 403)
        print(f"Contract Jellyfin {path} after completion: {status}")


def bazarr(port):
    # Read only inside the disposable app. Keep its full config out of tracebacks.
    response = run(["docker", "exec", "orama-bazarr", "cat", "/config/config/config.yaml"])
    if response.returncode:
        raise AssertionError("Bazarr config was unavailable")
    key = yaml.safe_load(response.stdout)["auth"]["apikey"]
    status, _ = _request(port, "/api/system/status", headers={"X-API-KEY": key})
    assert status == 200
    status, _ = _request(port, "/api/system/status")
    assert status == 401
    print("Contract Bazarr auth.apikey accepted via X-API-KEY; anonymous status 401")


def recyclarr(host, folder, key):
    config = folder / "recyclarr.yml"
    config.write_text(yaml.safe_dump({"radarr": {"movies": {
        "base_url": "http://radarr:7878", "api_key": key,
        "quality_definition": {"type": "movie"},
    }}}))
    os.chmod(config, 0o600)
    os.chown(config, host.puid, host.pgid)
    os.chown(folder, host.puid, host.pgid)
    result = run(["docker", "run", "--rm", "--network", "orama-net",
                  "--user", f"{host.puid}:{host.pgid}", "-v", f"{folder}:/config",
                  CATALOG["recyclarr"].image, "sync", "radarr", "--preview",
                  "--config", "/config/recyclarr.yml"], timeout=240)
    if result.returncode:
        raise AssertionError("Pinned Recyclarr preview failed (output withheld)")
    print("Contract Recyclarr sync radarr --preview --config accepted; exited successfully")
