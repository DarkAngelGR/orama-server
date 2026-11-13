"""Plan 5 Task 3 end-to-end proof: clean machine install, wizard, real download, subtitle, Ready."""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from e2e.fake_torznab import RELEASES
from e2e.target import Target

pytestmark = [pytest.mark.integration, pytest.mark.e2e]

E2E_DIR = Path(__file__).resolve().parent
WIZARD_SCRIPT = E2E_DIR / "wizard.cjs"
FIXTURE_IMAGE = (
    "python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b"
)
INDEXER_CONTAINER = "orama-e2e-indexer"
SHARED_NETWORK = "orama-net"
DEVICE_LABEL = "Samsung S95F TV"
DEVICE_NOTE = "Samsung S95F"
QUALITY_LABEL = "1080p \u2014 smaller files"
SUBTITLE_LABELS = ["Greek", "English"]
EPISODE_PREFIX = "Breaking.Bad.S01E01"
MOVIE_PREFIX = "Inception.2010"
INSTALL_TIMEOUT = 1800
CLIPS_TIMEOUT = 900
WIZARD_TIMEOUT = 2400
JELLYFIN_TIMEOUT = 180

READY_LINE_RE = re.compile(
    r"Orama is ready\. Open (http://\S+:(\d+)/setup\?code=([A-Za-z0-9_-]{16,64})) in your browser\."
)
PORT_LINE_RE = re.compile(r"Orama is ready\. Open http://\S+:(\d+)(?:/setup\S*)? in your browser\.")
COMPLETED_TORRENT_STATES = frozenset(
    {"uploading", "stalledUP", "queuedUP", "pausedUP", "stoppedUP", "forcedUP", "checkingUP"}
)


def _write_secret_json(path: Path, payload: dict) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)


def _install_env(target: Target) -> dict[str, str]:
    return {
        "ORAMA_BUILD_LOCAL": "1",
        "ORAMA_SRC": target.repo_dir,
        "ORAMA_DATA_ROOT": target.data_root,
        "ORAMA_CONFIG_ROOT": target.config_root,
        "ORAMA_PUID": str(target.puid),
        "ORAMA_PGID": str(target.pgid),
    }


def _run_install(target: Target) -> subprocess.CompletedProcess[str]:
    return target.run(
        ["bash", f"{target.repo_dir}/install.sh"],
        env=_install_env(target),
        timeout=INSTALL_TIMEOUT,
    )


def _docker_inspect(target: Target, container: str) -> dict:
    res = target.run(["docker", "inspect", container], timeout=60)
    assert res.returncode == 0, f"docker inspect {container} failed:\n{target.scrub(res.stdout)}"
    return json.loads(res.stdout)[0]


def _container_ids(target: Target) -> dict[str, str]:
    res = target.run(
        ["docker", "ps", "-a", "--no-trunc", "--format", "{{.Names}} {{.ID}}"],
        timeout=60,
    )
    assert res.returncode == 0, f"docker ps failed:\n{target.scrub(res.stdout)}"
    pairs = (line.split() for line in res.stdout.splitlines() if line.strip())
    return {name: cid for name, cid in pairs if name.startswith("orama-")}


def _orama_python(target: Target, script: str, *args: str, stdin_data: str | None = None) -> str:
    res = target.run(
        ["docker", "exec", "-i", "orama-server", "python", "-c", script, *args],
        input=stdin_data,
        timeout=120,
    )
    assert res.returncode == 0, f"in-container query failed:\n{target.scrub(res.stdout)}"
    return res.stdout


def _read_remote_bytes(target: Target, path: str) -> bytes:
    script = (
        "import base64, sys; "
        "from pathlib import Path; "
        "print(base64.b64encode(Path(sys.argv[1]).read_bytes()).decode())"
    )
    res = target.run(["python3", "-c", script, path], timeout=60)
    assert res.returncode == 0, f"reading {path} failed:\n{target.scrub(res.stdout)}"
    import base64

    return base64.b64decode(res.stdout.strip())


def _qbittorrent_torrents(target: Target, config_root: str) -> list[dict]:
    snippet = """
import json, sys, urllib.parse, urllib.request
from http.cookiejar import CookieJar
from pathlib import Path
from orama.secrets import SecretStore

secrets_path = Path(sys.argv[1])
store = SecretStore(secrets_path)
password = store.get("password:qbittorrent")
assert password, "missing qBittorrent password"
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
form = urllib.parse.urlencode({"username": "orama", "password": password}).encode()
resp = opener.open("http://qbittorrent:8090/api/v2/auth/login", data=form, timeout=20)
assert resp.status in (200, 204), f"qBittorrent login HTTP {resp.status}"
torrents = json.loads(opener.open("http://qbittorrent:8090/api/v2/torrents/info", timeout=20).read())
print(json.dumps([
    {
        "hash": t.get("hash", ""),
        "name": t.get("name", ""),
        "state": t.get("state", ""),
        "progress": t.get("progress", 0),
        "amount_left": t.get("amount_left", -1),
        "completed": t.get("completed", 0),
        "size": t.get("size", 0),
    }
    for t in torrents
]))
"""
    raw = _orama_python(target, snippet, f"{config_root}/orama/secrets.bin")
    return json.loads(raw)


def _jellyfin_items(target: Target, config_root: str) -> list[dict]:
    snippet = """
import json, sys, urllib.request
from pathlib import Path
from orama.secrets import SecretStore

store = SecretStore(Path(sys.argv[1]))
key = store.get("apikey:jellyfin")
assert key, "missing Jellyfin API key"
auth = f'MediaBrowser Client="Orama", Device="Orama", DeviceId="orama", Version="1", Token="{key}"'
req = urllib.request.Request(
    "http://jellyfin:8096/Items?Recursive=true&IncludeItemTypes=Series,Episode,Movie&Fields=ProviderIds,Path",
    headers={"Accept": "application/json", "Authorization": auth},
)
with urllib.request.urlopen(req, timeout=20) as resp:
    data = json.loads(resp.read())
items = data.get("Items", data) if isinstance(data, dict) else data
print(json.dumps([
    {
        "Id": i.get("Id"),
        "Name": i.get("Name"),
        "Type": i.get("Type"),
        "SeriesId": i.get("SeriesId"),
        "SeriesName": i.get("SeriesName"),
        "ParentIndexNumber": i.get("ParentIndexNumber"),
        "IndexNumber": i.get("IndexNumber"),
        "ProviderIds": i.get("ProviderIds") or {},
        "Path": i.get("Path") or "",
    }
    for i in items
]))
"""
    raw = _orama_python(target, snippet, f"{config_root}/orama/secrets.bin")
    return json.loads(raw)


def _prepare_clips_and_indexer(
    target: Target, clips_remote: str, torznab_key: str
) -> dict[str, str]:
    make_res = target.run(
        ["bash", f"{target.repo_dir}/tests/e2e/make_clips.sh", clips_remote],
        timeout=CLIPS_TIMEOUT,
    )
    assert make_res.returncode == 0, f"clip generation failed:\n{target.scrub(make_res.stdout)}"

    # Compute expected torrent info-hashes from the actual clip files on disk.
    hash_script = """
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from fake_torznab import RELEASES, torrent_for

clips = Path(sys.argv[2])
base = sys.argv[3]
hashes = {}
for name, _ in RELEASES:
    _, infohash = torrent_for(clips / name, f"{base}/files/{name}")
    hashes[name] = infohash
print(json.dumps(hashes))
"""
    hash_res = target.run(
        [
            "python3",
            "-c",
            hash_script,
            f"{target.repo_dir}/tests/e2e",
            clips_remote,
            f"http://{INDEXER_CONTAINER}:8000",
        ],
        timeout=60,
    )
    assert hash_res.returncode == 0, f"torrent infohash failed:\n{target.scrub(hash_res.stdout)}"
    infohashes = json.loads(hash_res.stdout)

    # Start the hardened fake Torznab + web seed container on orama-net.
    # Pass TORZNAB_KEY only in environment, never on argv.
    started = target.run(
        [
            "docker",
            "run",
            "--detach",
            "--name",
            INDEXER_CONTAINER,
            "--network",
            SHARED_NETWORK,
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--read-only",
            "-e",
            "TORZNAB_KEY",
            "-v",
            f"{clips_remote}:/clips:ro",
            "-v",
            f"{target.repo_dir}/tests/e2e/fake_torznab.py:/indexer.py:ro",
            FIXTURE_IMAGE,
            "python",
            "/indexer.py",
            "/clips",
        ],
        env={"TORZNAB_KEY": torznab_key},
        timeout=300,
    )
    assert started.returncode == 0, (
        f"fake indexer failed to start:\n{target.scrub(started.stdout)}"
    )

    deadline = time.monotonic() + 60
    while True:
        probe = target.run(
            [
                "docker",
                "exec",
                INDEXER_CONTAINER,
                "python",
                "-c",
                "import socket; socket.create_connection(('127.0.0.1', 8000), 1).close()",
            ],
            timeout=15,
        )
        if probe.returncode == 0:
            break
        if time.monotonic() >= deadline:
            pytest.fail("fake Torznab server did not begin listening on port 8000")
        time.sleep(1)

    return infohashes


def _run_wizard(
    args: list[str], target: Target, captured: dict[str, str], timeout: float = WIZARD_TIMEOUT,
) -> dict:
    __tracebackhide__ = True
    assert WIZARD_SCRIPT.is_file(), f"missing wizard driver: {WIZARD_SCRIPT}"
    node = shutil.which("node") or "node"
    res = subprocess.run(
        [node, str(WIZARD_SCRIPT), *args],
        cwd=WIZARD_SCRIPT.parent,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    label = "wizard verification" if args[0] == "--verify-ready" else "setup wizard"
    captured[f"{label} stdout"] = res.stdout
    captured[f"{label} stderr"] = res.stderr
    target.check_for_leaks({f"{label} stdout": res.stdout, f"{label} stderr": res.stderr})
    combined = target.scrub(res.stdout + "\n" + res.stderr)
    assert res.returncode == 0, f"wizard.cjs failed (exit {res.returncode}):\n{combined}"
    lines = [line for line in res.stdout.splitlines() if line.strip()]
    assert lines, f"wizard.cjs produced no JSON output:\n{combined}"
    return json.loads(lines[-1])


def _assert_ready_page_text(req_view: dict, title_hint: str) -> None:
    stage = str(req_view.get("stageText", ""))
    body = str(req_view.get("bodyText", ""))
    assert stage.strip().lower() == "ready", f"{title_hint}: stage is {stage!r}\n{body}"
    assert "direct play on samsung s95f" in body.lower(), (
        f"{title_hint}: playback note missing direct play on Samsung S95F:\n{body}"
    )
    assert "still looking" not in body.lower(), (
        f"{title_hint}: subtitle note still says 'still looking':\n{body}"
    )
    assert "el subs" in body.lower(), f"{title_hint}: subtitle note missing EL subs:\n{body}"


def test_end_to_end_install_wizard_download_subtitle_and_idempotency(
    e2e_target: Target, tmp_path: Path
):
    """Run the full user journey from bare machine install to Ready and second-run idempotency."""
    # Note for Ubuntu runners: Ubuntu 24.04 pre-installs Docker Engine, so this run exercises
    # "Docker is already present"; installing Docker from scratch is covered on Debian 12.
    target = e2e_target
    captured_outputs: dict[str, str] = {}
    log_dir = Path(os.environ.get("ORAMA_E2E_LOG_DIR") or E2E_DIR.parents[1] / f"e2e-logs-{target.name}")

    orama_password = "pw-" + secrets.token_urlsafe(14)
    jellyfin_password = "jf-" + secrets.token_urlsafe(14)
    torznab_key = secrets.token_hex(24)
    target.register_secret(orama_password, jellyfin_password, torznab_key)

    # 1. Install
    if target.expect_docker_installed:
        assert target.run(["bash", "-c", "command -v docker"]).returncode != 0

    install_1 = _run_install(target)
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "install-1.log").write_text(target.scrub(install_1.stdout), encoding="utf-8")
    assert install_1.returncode == 0, f"install.sh failed:\n{target.scrub(install_1.stdout)}"

    match = READY_LINE_RE.search(install_1.stdout)
    assert match is not None, (
        f"missing 'Orama is ready. Open .../setup?code=...' line:\n{target.scrub(install_1.stdout)}"
    )
    orama_port = int(match.group(2))
    setup_code = match.group(3)
    target.register_secret(setup_code)
    # Exempt precisely the installer line which intentionally prints the one-time link.
    captured_outputs["install-1"] = (
        install_1.stdout[:match.start(3)] + "<hidden>" + install_1.stdout[match.end(3):]
    )

    if target.expect_docker_installed:
        apt_file = _read_remote_bytes(target, "/etc/apt/sources.list.d/docker.list").decode()
        assert "download.docker.com/linux/debian" in apt_file and "bookworm" in apt_file

    for name in ("orama-server", "orama-server-agent"):
        assert _docker_inspect(target, name)["State"]["Status"] == "running", name
    agent_info = _docker_inspect(target, "orama-server-agent")
    assert agent_info["HostConfig"]["NetworkMode"] == "none"

    health = target.run(
        ["curl", "-fsS", f"http://127.0.0.1:{orama_port}/healthz"],
        timeout=30,
    )
    assert health.returncode == 0 and health.stdout.strip() == "ok"

    # Generate 1080p E-AC-3 + embedded Greek subtitle clips and start fake indexer on orama-net.
    clips_remote = "/srv/orama-e2e/clips"
    infohashes = _prepare_clips_and_indexer(target, clips_remote, torznab_key)

    # 2. Setup wizard + real request creation + wait for Ready
    answers_path = tmp_path / "answers.json"
    _write_secret_json(
        answers_path,
        {
            "setup_code": setup_code,
            "orama_password": orama_password,
            "jellyfin_user": "owner",
            "jellyfin_password": jellyfin_password,
            "indexer_search": "Generic",
            "indexer_name": "Orama fixture",
            "indexer_url": f"http://{INDEXER_CONTAINER}:8000",
            "indexer_api_path": "/api",
            "indexer_key": torznab_key,
            "device": DEVICE_LABEL,
            "quality": QUALITY_LABEL,
            "subtitle_languages": SUBTITLE_LABELS,
            "requests": [
                {
                    "kind": "episode",
                    "query": "Breaking Bad",
                    "source_id": 81189,
                    "season": 1,
                    "episode": 1,
                },
                {
                    "kind": "movie",
                    "query": "Inception",
                    "source_id": 27205,
                },
            ],
            "device_note": DEVICE_NOTE,
            "primary_sub": "EL",
        },
    )

    wizard_result = _run_wizard([target.url(orama_port), str(answers_path)], target, captured_outputs)
    assert wizard_result["checklistOk"] is True
    checklist_lines = wizard_result["checklistLines"]
    assert checklist_lines and not any(line.lstrip().startswith("✗") for line in checklist_lines)

    setup_bytes = _read_remote_bytes(target, f"{target.config_root}/orama/setup.json")
    setup_data = json.loads(setup_bytes.decode("utf-8"))
    assert setup_data["media_servers"] == ["jellyfin"]
    assert setup_data["download_methods"] == ["torrent"]
    assert setup_data["vpn"] is None
    assert setup_data["device"] == "samsung_s95f"
    assert setup_data["quality"] == "1080p"
    assert setup_data["subtitle_languages"] == ["el", "en"]
    assert len(setup_data["indexers"]) == 1

    draft_check = target.run(["test", "!", "-e", f"{target.config_root}/orama/setup.draft.json"])
    assert draft_check.returncode == 0, "setup.draft.json was left behind after Install"

    # 3. Download really happened via qBittorrent and the fake web seed
    torrents = _qbittorrent_torrents(target, target.config_root)
    by_hash = {t["hash"].lower(): t for t in torrents}
    for release_name, _ in RELEASES:
        expected_hash = infohashes[release_name].lower()
        assert expected_hash in by_hash, (
            f"missing torrent {release_name} ({expected_hash}) in qBittorrent: {torrents}"
        )
        t = by_hash[expected_hash]
        assert t["progress"] >= 0.9999 and t["amount_left"] == 0, t
        assert t["state"] in COMPLETED_TORRENT_STATES, t

    indexer_logs = target.run(["docker", "logs", INDEXER_CONTAINER], timeout=60).stdout
    captured_outputs["indexer-logs"] = indexer_logs
    (log_dir / "indexer.log").write_text(target.scrub(indexer_logs), encoding="utf-8")
    for release_name, _ in RELEASES:
        assert f"web seed served {release_name}" in indexer_logs, (
            f"fake web seed did not serve {release_name}"
        )

    # 4. Hardlink: downloaded file and imported media file share inode and link count >= 2
    stat_res = target.run(
        [
            "find",
            target.data_root,
            "-type",
            "f",
            "-name",
            "*.mkv",
            "-exec",
            "stat",
            "-c",
            "%i|%h|%u|%g|%n",
            "{}",
            "+",
        ],
        timeout=60,
    )
    assert stat_res.returncode == 0, f"stat mkv files failed:\n{target.scrub(stat_res.stdout)}"
    mkv_entries = []
    for line in stat_res.stdout.splitlines():
        if not line.strip():
            continue
        inode, links, uid, gid, fpath = line.split("|", 4)
        mkv_entries.append(
            {
                "inode": int(inode),
                "links": int(links),
                "uid": int(uid),
                "gid": int(gid),
                "path": fpath,
            }
        )

    downloads_root = f"{target.data_root}/downloads/torrents/"
    media_root = f"{target.data_root}/media/"
    imported_videos: dict[str, dict] = {}
    for key, rel_subdir in (
        (EPISODE_PREFIX, "tv/"),
        (MOVIE_PREFIX, "movies/"),
    ):
        dl = next(
            (e for e in mkv_entries if e["path"].startswith(downloads_root) and key in e["path"]),
            None,
        )
        assert dl is not None, f"downloaded file for {key} missing under {downloads_root}: {mkv_entries}"
        imp = next(
            (
                e
                for e in mkv_entries
                if e["path"].startswith(media_root + rel_subdir) and e["inode"] == dl["inode"]
            ),
            None,
        )
        assert imp is not None, (
            f"no imported hardlink under {media_root + rel_subdir} matching inode {dl['inode']}: "
            f"{mkv_entries}"
        )
        assert imp["links"] >= 2 and dl["links"] >= 2, (dl, imp)
        assert (imp["uid"], imp["gid"]) == (target.puid, target.pgid)
        imported_videos[key] = imp

    # 5. Visible in Jellyfin via wiring notification (no manual library scan)
    jf_deadline = time.monotonic() + JELLYFIN_TIMEOUT
    found_tv = False
    found_movie = False
    jf_items: list[dict] = []
    while time.monotonic() < jf_deadline:
        jf_items = _jellyfin_items(target, target.config_root)
        bb_series_ids = {
            item["Id"]
            for item in jf_items
            if item.get("Type") == "Series" and str(item["ProviderIds"].get("Tvdb", "")) == "81189"
        }
        found_tv = any(
            item.get("Type") == "Episode"
            and item.get("ParentIndexNumber") == 1
            and item.get("IndexNumber") == 1
            and (
                item.get("SeriesId") in bb_series_ids
                or str(item["ProviderIds"].get("Tvdb", "")) == "81189"
            )
            for item in jf_items
        )
        found_movie = any(
            item.get("Type") == "Movie" and str(item["ProviderIds"].get("Tmdb", "")) == "27205"
            for item in jf_items
        )
        if found_tv and found_movie:
            break
        time.sleep(5)
    assert found_tv and found_movie, (
        f"Jellyfin did not list both titles automatically (tv={found_tv}, movie={found_movie}): "
        f"{jf_items}"
    )

    # 6. Subtitle placed next to each imported video by Bazarr
    for key, imp in imported_videos.items():
        srt_path = str(Path(imp["path"]).with_suffix(".el.srt"))
        srt_stat = target.run(
            ["stat", "-c", "%u|%g|%s", srt_path],
            timeout=30,
        )
        assert srt_stat.returncode == 0, f"missing subtitle file {srt_path}"
        suid, sgid, ssize = (int(x) for x in srt_stat.stdout.strip().split("|"))
        assert (suid, sgid) == (target.puid, target.pgid)
        assert ssize > 0, f"empty subtitle {srt_path}"
        srt_text = _read_remote_bytes(target, srt_path).decode("utf-8-sig")
        assert "Δοκιμή Orama" in srt_text, f"unexpected subtitle text in {srt_path}"

    # 7. Ready in browser with S95F direct play and Greek subtitle note (not "still looking")
    request_results = wizard_result["requests"]
    assert len(request_results) == 2
    for req_view in request_results:
        _assert_ready_page_text(req_view, req_view["title"])

    # 8. Second install changes nothing
    before_ids = _container_ids(target)
    tracked_files = (
        f"{target.config_root}/compose.yaml",
        f"{target.config_root}/core.compose.yaml",
        f"{target.config_root}/orama/setup.json",
    )
    before_bytes = {p: _read_remote_bytes(target, p) for p in tracked_files}
    before_torrents = sorted((t["hash"].lower(), t["name"]) for t in torrents)

    install_2 = _run_install(target)
    captured_outputs["install-2"] = install_2.stdout
    (log_dir / "install-2.log").write_text(target.scrub(install_2.stdout), encoding="utf-8")
    assert install_2.returncode == 0, f"second install.sh failed:\n{target.scrub(install_2.stdout)}"
    port_2_match = PORT_LINE_RE.search(install_2.stdout)
    assert port_2_match is not None and int(port_2_match.group(1)) == orama_port
    assert _container_ids(target) == before_ids
    for p, expected in before_bytes.items():
        assert _read_remote_bytes(target, p) == expected, f"{p} changed on second install"

    after_torrents = sorted(
        (t["hash"].lower(), t["name"]) for t in _qbittorrent_torrents(target, target.config_root)
    )
    assert after_torrents == before_torrents

    verify_path = tmp_path / "verify.json"
    _write_secret_json(
        verify_path,
        {
            "base_url": target.url(orama_port),
            "orama_password": orama_password,
            "request_urls": [r["url"] for r in request_results],
        },
    )
    verify_res = _run_wizard(["--verify-ready", str(verify_path)], target, captured_outputs, timeout=180)
    assert len(verify_res["requests"]) == 2
    for req_view in verify_res["requests"]:
        _assert_ready_page_text(req_view, f"post-reinstall {req_view['title']}")

    # 9. Scan raw output independently of artifact redaction, then scan the actual files.
    target.collect_stored_secrets()
    captured_outputs["indexer logs"] = indexer_logs
    captured_outputs.update(target.save_logs(log_dir))
    target.check_for_leaks(captured_outputs)
    target.check_for_leaks({
        f"artifact {path.name}": path.read_text(encoding="utf-8")
        for path in log_dir.iterdir() if path.is_file()
    })
    if scan_path := os.environ.get("ORAMA_E2E_SCAN_VALUES"):
        target.write_scan_values(Path(scan_path))
