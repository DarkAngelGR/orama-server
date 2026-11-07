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
    __tracebackhide__ = True
    endpoint = path if path.startswith("/") else "/api/v3/" + path
    try:
        response = Http().request(method, f"http://127.0.0.1:{port}{endpoint}",
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
    __tracebackhide__ = True
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
    if app == "sonarr":
        call(port, key, file_path, expected=404)
    else:
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
    __tracebackhide__ = True
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
        for app in ("sonarr", "radarr"):
            output = run(["docker", "logs", f"orama-{app}"])
            assert output.returncode == 0, "app logs unavailable for webhook credential check"
            if token in output.stdout:
                pytest.fail("webhook credential present in app logs (output withheld)")
    finally:
        try:
            cleanup_failed = False
            for port, key, identifier in created:
                try:
                    call(port, key, f"notification/{identifier}", method="DELETE")
                except pytest.fail.Exception:
                    cleanup_failed = True
            if cleanup_failed:
                pytest.fail("request webhook notification cleanup failed")
        finally:
            run(["docker", "rm", "-f", name])
            (folder / "token").unlink(missing_ok=True)


def wait_command(port, key, body):
    __tracebackhide__ = True
    command = call(port, key, "command", method="POST", body=body, expected=201)
    assert isinstance(command["id"], int)
    deadline = time.monotonic() + 180
    while True:
        current = call(port, key, f"command/{command['id']}")
        assert current["body"]["name"].lower() == body["name"].lower()
        if current["status"] == "completed":
            return current
        if current["status"] in ("failed", "aborted", "cancelled"):
            pytest.fail(f"request contract command {body['name']} failed (details withheld)")
        if time.monotonic() >= deadline:
            pytest.fail(f"request contract command {body['name']} timed out")
        time.sleep(1)


def title_contracts(app, port, key, host, bazarr_port, bazarr_key):
    """Real metadata/add/monitor/search/file contracts; synthetic media, no downloads.

    Adding a title always contacts Skyhook in these pins. Catalogue unavailability
    deliberately fails this required contract instead of silently skipping coverage.
    """
    __tracebackhide__ = True
    tv = app == "sonarr"
    resource, external, source = ("series", "tvdbId", 81189) if tv else ("movie", "tmdbId", 27205)
    term = "tvdb:81189" if tv else "tmdb:27205"
    results = call(port, key, f"{resource}/lookup?term={term}")
    item = next((r for r in results if r.get(external) == source), None)
    assert item is not None, "required catalogue fixture unavailable"
    assert isinstance(item["title"], str) and isinstance(item["year"], int)
    strict = next(p for p in call(port, key, "qualityprofile") if p["name"] == "Orama")
    root = "/data/media/tv" if tv else "/data/media/movies"
    path = root + "/Orama Request Contract"
    payload = {**item, "qualityProfileId": strict["id"], "path": path,
               "rootFolderPath": root, "monitored": True}
    if tv:
        payload.update(seasonFolder=True, addOptions={"monitor": "none",
                       "searchForMissingEpisodes": False, "searchForCutoffUnmetEpisodes": False})
    else:
        payload.update(minimumAvailability="released", addOptions={"searchForMovie": False})
    observation = {"lookup": {k: item[k] for k in (external, "title", "year")}}
    identifier = None
    try:
        added = call(port, key, resource, method="POST", body=payload, expected=201)
        identifier = added["id"]
        assert added[external] == source and isinstance(identifier, int)
        observation["added"] = {k: added[k] for k in ("id", external, "monitored", "qualityProfileId")}
        have = call(port, key, f"{resource}?{external}={source}")
        assert [r["id"] for r in have] == [identifier]
        if tv:
            wait_command(port, key, {"name": "RefreshSeries", "seriesId": identifier})
            episodes = call(port, key, f"episode?seriesId={identifier}")
            target = next(e for e in episodes if e["seasonNumber"] == e["episodeNumber"] == 1)
            observation["episode"] = {k: target[k] for k in (
                "id", "seriesId", "seasonNumber", "episodeNumber", "monitored", "hasFile")}
            assert len(episodes) > 1 and all(e["monitored"] is False for e in episodes)
            # An existing unrelated selection must survive an episode-only request.
            other = next(e for e in episodes if e["id"] != target["id"])
            call(port, key, "episode/monitor", method="PUT",
                 body={"episodeIds": [other["id"]], "monitored": True}, expected=202)
            before = {e["id"]: e["monitored"] for e in episodes}
            before[other["id"]] = True
            call(port, key, "episode/monitor", method="PUT",
                 body={"episodeIds": [target["id"]], "monitored": True}, expected=202)
            after = call(port, key, f"episode?seriesId={identifier}")
            assert {e["id"]: e["monitored"] for e in after} == {**before, target["id"]: True}
            fallback_contract(port, key, resource, identifier, strict, target["id"])
            command = wait_command(port, key, {"name": "EpisodeSearch", "episodeIds": [target["id"]]})
            assert command["body"]["episodeIds"] == [target["id"]]
        else:
            fallback_contract(port, key, resource, identifier, strict, identifier)
            wait_command(port, key, {"name": "MoviesSearch", "movieIds": [identifier]})
        filename = ("Breaking.Bad.S01E01.1080p.WEB-DL.AAC.x264.mkv" if tv else
                    "Inception.2010.1080p.WEB-DL.AAC.x264.mkv")
        user = f"{host.puid}:{host.pgid}"
        made = run(["docker", "exec", "-u", user, "orama-jellyfin", "mkdir", "-p", path])
        assert made.returncode == 0, "synthetic media folder creation failed"
        made = run(["docker", "exec", "-u", user, "orama-jellyfin",
                    "/usr/lib/jellyfin-ffmpeg/ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                    "color=c=black:s=1920x1080:r=1", "-f", "lavfi", "-i", "anullsrc",
                    "-t", "120", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
                    "-metadata:s:a:0", "language=eng", "-y", path + "/" + filename], timeout=120)
        assert made.returncode == 0, "synthetic media generation failed (output withheld)"
        scan = {"name": "RescanSeries", "seriesId": identifier} if tv else {
            "name": "RescanMovie", "movieId": identifier}
        wait_command(port, key, scan)
        files = call(port, key, f"episodefile?seriesId={identifier}" if tv else
                     f"moviefile?movieId={identifier}")
        assert len(files) == 1, "synthetic file was not discovered"
        file = files[0]
        assert isinstance(file["id"], int) and file["size"] > 0
        info = file["mediaInfo"]
        assert info["videoCodec"] and info["audioCodec"]
        assert info["resolution"] == "1920x1080"
        assert info["videoBitDepth"] == 8
        assert isinstance(info["audioLanguages"], str)
        assert isinstance(info.get("subtitles", ""), str)
        observation["mediaInfo"] = {k: info.get(k) for k in (
            "videoCodec", "audioCodec", "resolution", "videoBitDepth", "audioLanguages",
            "videoDynamicRange", "videoDynamicRangeType", "subtitles")}
        observation["subtitles"] = subtitle_contract(
            bazarr_port, bazarr_key, tv, identifier, target["id"] if tv else identifier)
        return observation
    finally:
        if identifier is not None:
            call(port, key, f"{resource}/{identifier}?deleteFiles=true", method="DELETE")


def subtitle_contract(port, key, tv, identifier, file_target):
    __tracebackhide__ = True
    resource = "series" if tv else "movies"
    id_field = "seriesid" if tv else "radarrid"
    call(port, key, f"/api/{resource}", method="PATCH",
         body={id_field: identifier, "action": "sync"}, expected=204)
    query = (f"/api/episodes?episodeid[]={file_target}" if tv else
             f"/api/movies?radarrid[]={identifier}&start=0&length=1")
    deadline = time.monotonic() + 90
    while True:
        data = call(port, key, query)
        if data["data"]:
            break
        if time.monotonic() >= deadline:
            pytest.fail("Bazarr did not synchronize synthetic media")
        time.sleep(1)
    # Initial synchronization may expose the row before subtitle indexing finishes.
    # Assign the test profile explicitly and index the synthetic file synchronously.
    profiles = call(port, key, "/api/system/languages/profiles")
    profile = next(p for p in profiles if p["name"] == "Orama")
    call(port, key, f"/api/{resource}", method="POST",
         body={id_field: [identifier], "profileid": [str(profile["profileId"])]}, expected=204)
    call(port, key, f"/api/{resource}", method="PATCH",
         body={id_field: identifier, "action": "scan-disk"}, expected=204)
    data = call(port, key, query)
    assert len(data["data"]) == 1
    item = data["data"][0]
    assert item["sonarrEpisodeId" if tv else "radarrId"] == file_target
    assert isinstance(item["missing_subtitles"], list)
    assert isinstance(item["subtitles"], list)
    assert {s["code2"] for s in item["missing_subtitles"]} == {"el", "en"}
    return {"missing_subtitles": item["missing_subtitles"], "subtitles": item["subtitles"]}


def fallback_contract(port, key, resource, identifier, strict, target):
    __tracebackhide__ = True
    query = f"release?{'episodeId' if resource == 'series' else 'movieId'}={target}"

    def release():
        __tracebackhide__ = True
        releases = call(port, key, query)
        prefix = "Breaking.Bad.S01E01." if resource == "series" else "Inception.2010."
        matches = [r for r in releases if r["title"].startswith(prefix)
                   and ".DTS.x264" in r["title"]]
        assert matches, "synthetic DTS release missing from indexer results"
        return matches[0]

    blocked = release()
    if resource == "movie":
        assert any(lang.get("name") == "English" for lang in blocked["languages"]), (
            "synthetic movie language must be explicit to isolate the codec score")
    score = blocked["customFormatScore"]
    assert score < strict["minFormatScore"]
    assert blocked["rejected"] is True
    assert any("score" in reason.lower() for reason in blocked["rejections"])
    alternate = {**strict, "name": "Request fallback contract", "minFormatScore": score}
    alternate.pop("id", None)
    saved = call(port, key, "qualityprofile", method="POST", body=alternate, expected=201)
    item = None
    try:
        item = call(port, key, f"{resource}/{identifier}")
        call(port, key, f"{resource}/{identifier}", method="PUT",
             body={**item, "qualityProfileId": saved["id"]}, expected=202)
        allowed = release()
        assert allowed["customFormatScore"] == score
        if allowed["rejected"]:
            reasons = [str(r).lower() for r in allowed.get("rejections", [])]
            categories = [c for c in ("size", "language", "quality", "score", "available", "age",
                                      "retention", "delay", "title", "already", "release", "indexer",
                                      "wanted", "unknown", "parse", "download client")
                          if any(c in r for r in reasons)]
            pytest.fail("alternate score floor still rejected synthetic DTS release; "
                        f"reason categories: {categories} (raw reasons withheld)")
        unchanged = call(port, key, f"qualityprofile/{strict['id']}")
        assert unchanged == strict, "shared strict quality profile changed"
    finally:
        try:
            if item is not None:
                call(port, key, f"{resource}/{identifier}", method="PUT", body=item, expected=202)
        finally:
            call(port, key, f"qualityprofile/{saved['id']}", method="DELETE")
