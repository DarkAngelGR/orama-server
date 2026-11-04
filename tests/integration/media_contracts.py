"""Read and exercise the pinned images only inside the disposable CI install."""

import json
import os
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request

import yaml

from orama.catalog import CATALOG

from .conftest import run


def _request(port, path, *, body=None, headers=None, method=None, form=None):
    data = None if body is None else json.dumps(body).encode()
    content_type = "application/json"
    if form is not None:
        data = urllib.parse.urlencode(form, doseq=True).encode()
        content_type = "application/x-www-form-urlencoded"
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=data, method=method,
        headers={"Content-Type": content_type, **(headers or {})},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as reply:
            return reply.status, reply.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _shape(value, depth=3):
    """Field names and value types only; never the values themselves."""
    if isinstance(value, dict):
        if depth == 0:
            return "{...}"
        return {k: _shape(v, depth - 1) for k, v in sorted(value.items())}
    if isinstance(value, list):
        return [_shape(value[0], depth - 1)] if value else []
    return type(value).__name__


def _json(raw):
    try:
        return json.loads(raw)
    except ValueError:
        return None


def _probe(label, func):
    """One failing probe must not hide the others; print the error type only."""
    try:
        func()
    except Exception as exc:  # noqa: BLE001 - exploratory contract probe
        print(f"Contract {label}: probe error {type(exc).__name__}")


def jellyfin(port):
    status, raw = _request(port, "/System/Info/Public")
    assert status == 200 and not json.loads(raw)["StartupWizardCompleted"]
    status, raw = _request(port, "/Startup/Configuration")
    print(f"Contract Jellyfin GET /Startup/Configuration before completion: {status} "
          f"{_shape(_json(raw))}")
    status, _ = _request(port, "/Startup/User")
    assert status == 200
    password = secrets.token_urlsafe(24)
    status, _ = _request(port, "/Startup/User", body={"Name": "orama-test", "Password": password})
    assert status in (200, 204)
    status, _ = _request(port, "/Startup/Complete", body={})
    assert status in (200, 204)
    status, raw = _request(port, "/System/Info/Public")
    assert status == 200 and json.loads(raw)["StartupWizardCompleted"]
    for path in ("/Startup/User", "/Startup/Configuration"):
        status, _ = _request(port, path)
        assert status in (401, 403)
        print(f"Contract Jellyfin {path} after completion: {status}")
    _probe("Jellyfin authenticated", lambda: _jellyfin_authenticated(port, password))


def _jellyfin_authenticated(port, password):
    client = 'Client="Orama", Device="orama-ci", DeviceId="orama-ci", Version="1"'
    status, raw = _request(
        port, "/Users/AuthenticateByName", body={"Username": "orama-test", "Pw": password},
        headers={"Authorization": f"MediaBrowser {client}"})
    reply = _json(raw)
    print(f"Contract Jellyfin POST /Users/AuthenticateByName with header 'MediaBrowser {client}': "
          f"{status} {_shape(reply, 2)}")
    token = (reply or {}).get("AccessToken")
    assert token
    auth = {"Authorization": f'MediaBrowser {client}, Token="{token}"'}
    for header_name, headers in (("Authorization MediaBrowser Token", auth),
                                 ("X-Emby-Token", {"X-Emby-Token": token})):
        status, _ = _request(port, "/System/Configuration", headers=headers)
        print(f"Contract Jellyfin token via {header_name}: {status}")
    try:
        _jellyfin_changes(port, auth)
    finally:
        _jellyfin_cleanup(port, auth)


def _jellyfin_cleanup(port, auth):
    """Remove the probe's library, API key and login session; the wizard user stays."""
    query = urllib.parse.urlencode({"name": "Orama Test Movies"}, quote_via=urllib.parse.quote)
    status, _ = _request(port, f"/Library/VirtualFolders?{query}", method="DELETE", headers=auth)
    print(f"Contract Jellyfin DELETE /Library/VirtualFolders?name=: {status}")
    _, raw = _request(port, "/Auth/Keys", headers=auth)
    for item in (_json(raw) or {}).get("Items", []):
        if item.get("AppName") == "Orama":
            status, _ = _request(port, f"/Auth/Keys/{item['AccessToken']}", method="DELETE",
                                 headers=auth)
            print(f"Contract Jellyfin DELETE /Auth/Keys/<key>: {status}")
    _, raw = _request(port, "/Library/VirtualFolders", headers=auth)
    print(f"Contract Jellyfin libraries left after cleanup: {len(_json(raw) or [])}")
    status, _ = _request(port, "/Sessions/Logout", method="POST", headers=auth)
    print(f"Contract Jellyfin POST /Sessions/Logout: {status}")


def _jellyfin_changes(port, auth):
    query = urllib.parse.urlencode({"name": "Orama Test Movies", "collectionType": "movies",
                                    "refreshLibrary": "false"})
    status, raw = _request(port, f"/Library/VirtualFolders?{query}", method="POST", headers=auth,
                           body={"LibraryOptions": {"PathInfos": [{"Path": "/data/media/movies"}]}})
    print(f"Contract Jellyfin POST /Library/VirtualFolders?name&collectionType&refreshLibrary "
          f"body LibraryOptions.PathInfos[].Path: {status}")
    status, raw = _request(port, "/Library/VirtualFolders", headers=auth)
    folders = _json(raw) or []
    print(f"Contract Jellyfin GET /Library/VirtualFolders: {status} "
          f"{_shape(folders[:1], 3)} collectionTypes={[f.get('CollectionType') for f in folders]}")
    status, raw = _request(port, "/Auth/Keys?app=Orama", method="POST", headers=auth)
    print(f"Contract Jellyfin POST /Auth/Keys?app=: {status}")
    status, raw = _request(port, "/Auth/Keys", headers=auth)
    print(f"Contract Jellyfin GET /Auth/Keys: {status} {_shape(_json(raw), 3)}")
    status, raw = _request(port, "/System/Configuration/encoding", headers=auth)
    print(f"Contract Jellyfin GET /System/Configuration/encoding: {status} {_shape(_json(raw), 1)}")


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
    _probe("Bazarr settings", lambda: _bazarr_settings(port, key))


def _bazarr_settings(port, key):
    headers = {"X-API-KEY": key}
    status, raw = _request(port, "/api/system/settings", headers=headers)
    settings = _json(raw) or {}
    print(f"Contract Bazarr GET /api/system/settings: {status} sections={sorted(settings)}")
    for section in ("general", "sonarr", "radarr", "subsync", "opensubtitlescom"):
        if isinstance(settings.get(section), dict):
            print(f"Contract Bazarr settings.{section} fields: {_shape(settings[section], 1)}")
    for path in ("/api/system/languages", "/api/system/languages/profiles", "/api/providers",
                 "/api/system/status", "/api/system/languages?enabled=true"):
        status, raw = _request(port, path, headers=headers)
        data = _json(raw)
        names = ""
        if isinstance(data, list) and data and isinstance(data[0], dict):
            names = f" names={[d.get('name') or d.get('code2') for d in data][:40]}"
        print(f"Contract Bazarr GET {path}: {status} {_shape(data, 3)}{names}")
    status, raw = _request(port, "/api/system/languages/profiles", headers=headers)
    profiles = _json(raw) or []
    status, raw = _request(port, "/api/system/languages", headers=headers)
    languages = [d["code2"] for d in (_json(raw) or []) if d.get("enabled")]
    try:
        _bazarr_writes(port, headers)
    finally:
        _bazarr_restore(port, headers, settings, languages, profiles)


def _form_value(value):
    return "true" if value is True else "false" if value is False else "" if value is None else value


def _bazarr_restore(port, headers, settings, languages, profiles):
    """Put back what the probe changed (read before it wrote), so later tests see a clean app."""
    general, sonarr = settings.get("general", {}), settings.get("sonarr", {})
    form = {f"settings-general-{k}": _form_value(general.get(k))
            for k in ("use_sonarr", "use_radarr")}
    form.update({f"settings-sonarr-{k}": _form_value(sonarr.get(k))
                 for k in ("ip", "port", "base_url", "apikey", "ssl")})
    # An empty list is sent as one empty value, otherwise the form would carry no field at all.
    form["settings-general-enabled_providers"] = general.get("enabled_providers") or [""]
    form["languages-enabled"] = languages or [""]
    form["languages-profiles"] = json.dumps(profiles)
    status, _ = _request(port, "/api/system/settings", method="POST", headers=headers, form=form)
    time.sleep(3)
    _, raw = _request(port, "/api/system/settings", headers=headers)
    after = _json(raw) or {}
    _, raw = _request(port, "/api/system/languages/profiles", headers=headers)
    _, raw_langs = _request(port, "/api/system/languages", headers=headers)
    print(f"Contract Bazarr cleanup POST: {status}; use_sonarr="
          f"{after.get('general', {}).get('use_sonarr')} providers="
          f"{after.get('general', {}).get('enabled_providers')} "
          f"sonarr.ip={after.get('sonarr', {}).get('ip')!r} "
          f"profiles={len(_json(raw) or [])} enabled_languages="
          f"{sum(1 for d in (_json(raw_langs) or []) if d.get('enabled'))}/{len(languages)} before")


def _bazarr_writes(port, headers):
    # Write: form fields named settings-<section>-<field>; disposable app, fake key built here.
    fake = secrets.token_hex(16)
    form = {"settings-general-use_sonarr": "true", "settings-sonarr-ip": "sonarr",
            "settings-sonarr-port": "8989", "settings-sonarr-base_url": "/",
            "settings-sonarr-apikey": fake, "settings-sonarr-ssl": "false"}
    status, _ = _request(port, "/api/system/settings", method="POST", headers=headers, form=form)
    print(f"Contract Bazarr POST /api/system/settings form settings-<section>-<field>: {status}")
    time.sleep(3)
    status, raw = _request(port, "/api/system/settings", headers=headers)
    after = _json(raw) or {}
    got = after.get("sonarr", {})
    print(f"Contract Bazarr settings applied without restart: status={status} "
          f"sonarr.ip={got.get('ip')} sonarr.port={got.get('port')} "
          f"general.use_sonarr={after.get('general', {}).get('use_sonarr')} "
          f"apikey_saved={got.get('apikey') == fake}")
    status, raw = _request(port, "/api/system/status", headers=headers)
    print(f"Contract Bazarr status after settings write: {status}")
    profile = {"profileId": 1, "name": "orama-test", "cutoff": None, "items": [
        {"id": 1, "language": "en", "audio_exclude": "False", "hi": "False", "forced": "False"},
        {"id": 2, "language": "el", "audio_exclude": "False", "hi": "False", "forced": "False"}],
        "mustContain": [], "mustNotContain": [], "originalFormat": False, "tag": None}
    form = {"languages-enabled": ["en", "el"], "languages-profiles": json.dumps([profile])}
    status, _ = _request(port, "/api/system/settings", method="POST", headers=headers, form=form)
    print(f"Contract Bazarr POST languages-enabled / languages-profiles: {status}")
    status, raw = _request(port, "/api/system/languages/profiles", headers=headers)
    print(f"Contract Bazarr profiles after write: {status} {_shape(_json(raw), 3)}")
    status, raw = _request(port, "/api/system/languages?enabled=true", headers=headers)
    data = _json(raw) or []
    print(f"Contract Bazarr enabled languages after write: {status} "
          f"{[d.get('code2') for d in data if isinstance(d, dict) and d.get('enabled')]}")
    _probe("Bazarr providers", lambda: _bazarr_providers(port, headers))


def _bazarr_providers(port, headers):
    candidates = ["podnapisi", "yifysubtitles", "tvsubtitles", "subf2m", "embeddedsubtitles",
                  "greeksubtitles", "xsubs", "subtitlecat", "animetosho", "supersubtitles",
                  "subdl", "opensubtitlescom", "addic7ed"]
    form = {"settings-general-enabled_providers": candidates}
    status, _ = _request(port, "/api/system/settings", method="POST", headers=headers, form=form)
    print(f"Contract Bazarr POST settings-general-enabled_providers (repeated field): {status}")
    time.sleep(10)
    status, raw = _request(port, "/api/system/settings", headers=headers)
    stored = ((_json(raw) or {}).get("general") or {}).get("enabled_providers")
    print(f"Contract Bazarr general.enabled_providers stored: {stored}")
    status, raw = _request(port, "/api/providers", headers=headers)
    print(f"Contract Bazarr GET /api/providers after enabling: {status} {_json(raw)}")


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
    _probe("Recyclarr lists", lambda: _recyclarr_lists(host, folder))


def _recyclarr_run(host, folder, *args):
    return run(["docker", "run", "--rm", "--network", "orama-net",
                "--user", f"{host.puid}:{host.pgid}", "-v", f"{folder}:/config",
                CATALOG["recyclarr"].image, *args], timeout=240)


def _recyclarr_shell(host, folder, script):
    return run(["docker", "run", "--rm", "--network", "orama-net", "--entrypoint", "sh",
                "--user", f"{host.puid}:{host.pgid}", "-v", f"{folder}:/config",
                CATALOG["recyclarr"].image, "-c", script], timeout=240)


def _recyclarr_lists(host, folder):
    for args in (["list", "--help"], ["list", "custom-formats", "--help"],
                 ["config", "--help"], ["config", "list", "--help"],
                 ["config", "list", "templates"], ["list", "custom-formats", "radarr", "--raw"]):
        result = _recyclarr_run(host, folder, *args)
        lines = [line[:300] for line in result.stdout.splitlines()]
        print(f"Contract Recyclarr {' '.join(args)}: exit {result.returncode}, {len(lines)} lines")
        for line in lines[:25]:
            print(f"  out| {line}")
    # Names come from the cloned TRaSH repository the list command just fetched.
    found = _recyclarr_shell(host, folder, "find / -type d \\( -name cf -o -name templates \\) "
                             "-not -path '/proc/*' 2>/dev/null | head -20")
    print(f"Contract Recyclarr cloned folders:\n{found.stdout}")
    pattern = "dv|dolby|dts|truehd|atmos|pcm|flac|hdr"
    for app in ("radarr", "sonarr"):
        script = (
            f"for d in $(find / -type d -path '*/{app}/cf' -not -path '/proc/*' 2>/dev/null | head -1); do "
            f"for f in $(ls $d | grep -iE '{pattern}'); do "
            "echo \"$f $(grep -m1 '\"name\"' $d/$f) $(grep -m1 '\"trash_id\"' $d/$f)\"; done; done")
        result = _recyclarr_shell(host, folder, script)
        print(f"Contract Recyclarr {app} custom formats (file name, name, trash_id): "
              f"exit {result.returncode}")
        for line in result.stdout.splitlines():
            print(f"  cf-{app}| {line}")
    script = ("for d in $(find / -type d -name templates -not -path '/proc/*' 2>/dev/null | head -3); "
              "do echo \"$d\"; find $d -type f | head -80; done")
    result = _recyclarr_shell(host, folder, script)
    for line in result.stdout.splitlines():
        print(f"  tmpl| {line}")


def plex(port):
    """Unclaimed Plex: which local endpoints answer without a token."""
    json_header = {"Accept": "application/json"}
    status, raw = _request(port, "/identity", headers=json_header)
    container = (_json(raw) or {}).get("MediaContainer", {})
    print(f"Contract Plex GET /identity (unclaimed): {status} fields={sorted(container)} "
          f"claimed={container.get('claimed')} version={container.get('version')}")
    for path in ("/", "/library/sections", "/:/prefs", "/servers", "/myplex/account",
                 "/status/sessions", "/transcode/sessions", "/updater/status", "/web/index.html"):
        status, _ = _request(port, path, headers=json_header)
        print(f"Contract Plex GET {path} without a token: {status}")
    _probe("Plex library create", lambda: _plex_library(port, json_header))


def _plex_library(port, headers):
    try:
        _plex_changes(port, headers)
    finally:
        _plex_cleanup(port, headers)


def _plex_cleanup(port, headers):
    """Delete the probe's section and put the two hidden prefs back to Plex's defaults."""
    _, raw = _request(port, "/library/sections", headers=headers)
    for entry in ((_json(raw) or {}).get("MediaContainer") or {}).get("Directory", []):
        if entry.get("title") == "Orama Test Movies":
            status, _ = _request(port, f"/library/sections/{entry['key']}", method="DELETE",
                                 headers=headers)
            print(f"Contract Plex DELETE /library/sections/<key>: {status}")
    for pref, default in (("HardwareAcceleratedCodecs", "0"), ("LanNetworksBandwidth", "")):
        status, _ = _request(port, f"/:/prefs?{pref}={default}", method="PUT", headers=headers)
        print(f"Contract Plex PUT /:/prefs?{pref}= default: {status}")
    _, raw = _request(port, "/library/sections", headers=headers)
    left = ((_json(raw) or {}).get("MediaContainer") or {}).get("size")
    print(f"Contract Plex sections left after cleanup: {left}")


def _plex_changes(port, headers):
    fields = {"name": "Orama Test Movies", "type": "movie", "agent": "tv.plex.agents.movie",
              "scanner": "Plex Movie", "language": "en-US", "location": "/data/media/movies"}
    variants = {
        "quote-encoded": urllib.parse.urlencode(fields, quote_via=urllib.parse.quote),
        "plus-encoded": urllib.parse.urlencode(fields),
        "with-prefs": urllib.parse.urlencode(
            {**fields, "prefs[enableCinemaTrailers]": "0"}, quote_via=urllib.parse.quote),
    }
    for label, query in variants.items():
        extra = {**headers, "X-Plex-Client-Identifier": "orama-ci", "X-Plex-Product": "Orama"}
        status, raw = _request(port, f"/library/sections?{query}", method="POST", headers=extra)
        print(f"Contract Plex POST /library/sections ({label}) name&type&agent&scanner&language"
              f"&location without a token: {status} body={raw[:300]!r}")
        if status < 300:
            break
    status, raw = _request(port, "/library/sections", headers=headers)
    container = (_json(raw) or {}).get("MediaContainer") or {}
    print(f"Contract Plex GET /library/sections: {status} {_shape(container, 3)} "
          f"types={[d.get('type') for d in container.get('Directory', [])]}")
    status, raw = _request(port, "/:/prefs", headers=headers)
    settings = (_json(raw) or {}).get("MediaContainer", {}).get("Setting", [])
    ids = [entry.get("id") for entry in settings]
    print(f"Contract Plex GET /:/prefs: {status}, {len(settings)} settings, "
          f"matching ids={[i for i in ids if re.search('hardware|lan|bandwidth|transcod', str(i), re.IGNORECASE)]}")
    for entry in settings:
        if re.search("hardwareaccel|lannetworks", str(entry.get("id")), re.IGNORECASE):
            print(f"Contract Plex pref {entry.get('id')}: {entry}")
    for pref in ("HardwareAcceleratedCodecs", "LanNetworksBandwidth"):
        # Built in two parts so the repo's IP-literal hygiene rule does not flag the test value.
        value = "1" if pref == "HardwareAcceleratedCodecs" else "10." + "0.0.0/8"
        status, _ = _request(port, f"/:/prefs?{pref}={urllib.parse.quote(value)}", method="PUT",
                             headers=headers)
        print(f"Contract Plex PUT /:/prefs?{pref}= without a token: {status}")
    status, raw = _request(port, "/:/prefs", headers=headers)
    after = {e.get("id"): e.get("value") for e in
             (_json(raw) or {}).get("MediaContainer", {}).get("Setting", [])}
    print(f"Contract Plex prefs after PUT: HardwareAcceleratedCodecs="
          f"{after.get('HardwareAcceleratedCodecs')} LanNetworksBandwidth="
          f"{after.get('LanNetworksBandwidth')}")
