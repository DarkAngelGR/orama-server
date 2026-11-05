"""The Agent's recyclarr-sync command: validated choices in, one fixed one-shot container out."""

import json
import logging
import os
import re
import shutil
import stat
import subprocess
import tempfile
import threading
from pathlib import Path

import pytest
import yaml
from conftest import random_secret

from orama.agent_client import AgentClient, AgentError
from orama.catalog import CATALOG
from orama.profiles.compile import compile_profile
from orama_agent import recyclarr
from orama_agent.runner import ComposeRunner, FakeRunner, RunnerError
from orama_agent.server import AgentServer, _env_file_text
from orama_agent.validate import Refused, Roots

SONARR_KEY = random_secret("son")
RADARR_KEY = random_secret("rad")


def tid(n: int) -> str:
    """A trash_id-shaped id built at run time."""
    return f"{n:032x}"


# ---------------------------------------------------------------- fixtures


@pytest.fixture
def roots(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "cfg").mkdir()
    return Roots(
        data=Path(os.path.realpath(tmp_path / "data")),
        config=Path(os.path.realpath(tmp_path / "cfg")),
        puid=1000,
        pgid=1001,
    )


def write_env(roots, app, values):
    env_dir = roots.config / "app-env"
    env_dir.mkdir(mode=0o700, exist_ok=True)
    path = env_dir / f"{app}.env"
    path.write_bytes(_env_file_text(values))
    path.chmod(0o600)


@pytest.fixture
def keys(roots):
    write_env(roots, "sonarr", {"SONARR__AUTH__APIKEY": SONARR_KEY})
    write_env(roots, "radarr", {"RADARR__AUTH__APIKEY": RADARR_KEY})


class Chowns:
    def __init__(self):
        self.calls = []

    def __call__(self, target, uid, gid, **kw):
        path = os.readlink(f"/proc/self/fd/{target}") if isinstance(target, int) else target
        self.calls.append((str(path), uid, gid))


class Seen:
    """Records what the YAML looked like while Recyclarr 'ran'."""

    def __init__(self, roots):
        self.path = roots.config / "config" / "recyclarr" / "recyclarr.yml"
        self.settings_path = self.path.with_name("settings.yml")
        self.texts = []
        self.modes = []
        self.settings = []  # (text, mode) of settings.yml while Recyclarr 'ran'
        self.alternates = []  # whether a settings.yaml was there too

    def __call__(self):
        self.texts.append(self.path.read_text())
        self.modes.append(stat.S_IMODE(os.lstat(self.path).st_mode))
        self.settings.append((self.settings_path.read_text(),
                              stat.S_IMODE(os.lstat(self.settings_path).st_mode)))
        self.alternates.append(os.path.lexists(self.path.with_name("settings.yaml")))

    @property
    def doc(self):
        return yaml.safe_load(self.texts[-1])


def serve(roots, runner, chowns=None):
    sock_dir = tempfile.mkdtemp(prefix="oa", dir="/tmp")
    path = os.path.join(sock_dir, "agent.sock")
    server = AgentServer(roots, runner, path, chown=chowns or Chowns(), read_timeout=5)
    thread = threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True)
    thread.start()

    def stop():
        server.shutdown()
        server.server_close()
        thread.join(5)
        shutil.rmtree(sock_dir, ignore_errors=True)

    return server, path, stop


class Env:
    def __init__(self, roots, runner, chowns, path):
        self.roots, self.runner, self.chowns, self.path = roots, runner, chowns, path
        self.client = AgentClient(path, timeout=10)
        self.seen = Seen(roots)
        runner.on_recyclarr = self.seen

    def call(self, args):
        import socket

        line = json.dumps({"id": "r1", "cmd": "recyclarr-sync", "args": args}).encode() + b"\n"
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(10)
            s.connect(self.path)
            s.sendall(line)
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
        return json.loads(buf)


@pytest.fixture
def env(roots, keys):
    runner = FakeRunner()
    chowns = Chowns()
    _server, path, stop = serve(roots, runner, chowns)
    try:
        yield Env(roots, runner, chowns, path)
    finally:
        stop()


def apps_for(device="samsung_s95f", quality="mixed"):
    return {app: dict(spec) for app, spec in compile_profile(device, quality).items()}


def yaml_path(roots):
    return roots.config / "config" / "recyclarr" / "recyclarr.yml"


# ---------------------------------------------------------------- validation


GOOD = {"radarr": {"templates": ["hd-bluray-web"], "scores": {}}}


@pytest.mark.parametrize("args", [
    {},
    {"apps": GOOD, "yaml": "radarr: {}"},
    {"apps": GOOD, "config": {"radarr": {}}},
    {"apps": "radarr:\n  movies: {}\n"},
    {"apps": []},
    {"apps": {}},
    {"apps": GOOD, "force": "yes"},
    {"apps": {"lidarr": {"templates": ["hd-bluray-web"], "scores": {}}}},
    {"apps": {"radarr": {"templates": ["hd-bluray-web"]}}},
    {"apps": {"radarr": {"templates": ["hd-bluray-web"], "scores": {}, "base_url": "http://x"}}},
    {"apps": {"radarr": {"templates": ["hd-bluray-web"], "scores": {}, "api_key": "k"}}},
    {"apps": {"radarr": ["hd-bluray-web"]}},
])
def test_refuses_caller_yaml_and_unexpected_shapes(args):
    with pytest.raises(Refused):
        recyclarr.check(args)


@pytest.mark.parametrize("templates", [
    [], ["web-1080p"],  # a Sonarr template for Radarr
    ["HD-BLURAY-WEB"], ["hd bluray"], ["../hd-bluray-web"], ["a" * 81], ["sqp-1-1080p"],
    "hd-bluray-web", [7], ["hd-bluray-web", "hd-bluray-web"],
    ["hd-bluray-web", "uhd-bluray-web"],  # exactly one template: one Orama profile per app
])
def test_refuses_unknown_or_malformed_templates(templates):
    with pytest.raises(Refused):
        recyclarr.check({"apps": {"radarr": {"templates": templates, "scores": {}}}})


@pytest.mark.parametrize("trash_id", [
    tid(0xABC).upper(), tid(1)[:-1], tid(1) + "0", "g" * 32, " " + tid(1)[1:], "", "x",
])
def test_refuses_bad_trash_ids(trash_id):
    with pytest.raises(Refused):
        recyclarr.check({"apps": {"radarr": {"templates": ["hd-bluray-web"],
                                             "scores": {trash_id: 10}}}})


@pytest.mark.parametrize("score", [-10001, 10001, True, 1.5, "5", None])
def test_refuses_out_of_range_or_non_integer_scores(score):
    with pytest.raises(Refused):
        recyclarr.check({"apps": {"radarr": {"templates": ["hd-bluray-web"],
                                             "scores": {tid(1): score}}}})


def test_score_limits_and_count():
    scores = {tid(n): -10000 if n % 2 else 10000 for n in range(200)}
    apps, force = recyclarr.check(
        {"apps": {"radarr": {"templates": ["hd-bluray-web"], "scores": scores}}})
    assert apps["radarr"]["scores"] == scores and force is False
    scores[tid(200)] = 0
    with pytest.raises(Refused, match="200"):
        recyclarr.check({"apps": {"radarr": {"templates": ["hd-bluray-web"], "scores": scores}}})


def test_accepts_compiled_profiles_for_every_device_and_quality():
    from orama.profiles.compile import device_names, quality_names

    for device in device_names():
        for quality in quality_names():
            apps, _ = recyclarr.check({"apps": apps_for(device, quality)})
            assert set(apps) == {"radarr", "sonarr"}


def test_pinned_templates_cover_the_quality_choices():
    from orama.profiles.compile import quality_names

    for quality in quality_names():
        for app, spec in compile_profile("safest", quality).items():
            for name in spec["templates"]:
                assert name in recyclarr.TEMPLATES[app]


# ---------------------------------------------------------------- render


def test_render_is_fixed_recyclarr_v8_config():
    apps, _ = recyclarr.check({"apps": {
        "radarr": {"templates": ["hd-bluray-web"],
                   "scores": {tid(3): -10000, tid(1): -10000, tid(2): 1500}},
        "sonarr": {"templates": ["web-1080p"], "scores": {}},
    }})
    doc = recyclarr.render(apps, {"radarr": RADARR_KEY, "sonarr": SONARR_KEY})
    radarr_profile = recyclarr.TEMPLATES["radarr"]["hd-bluray-web"]
    assert doc["radarr"] == {"orama-radarr": {
        "base_url": "http://radarr:7878",
        "api_key": RADARR_KEY,
        "delete_old_custom_formats": False,
        "quality_definition": {"type": "movie"},
        "quality_profiles": [{
            "trash_id": radarr_profile.trash_id,
            "name": "Orama",
            "reset_unmatched_scores": {"enabled": True},
        }],
        "custom_format_groups": {"add": [{"trash_id": g} for g in radarr_profile.groups]},
        "custom_formats": [
            {"trash_ids": [tid(1), tid(3)],
             "assign_scores_to": [{"name": "Orama", "score": -10000}]},
            {"trash_ids": [tid(2)],
             "assign_scores_to": [{"name": "Orama", "score": 1500}]},
        ],
    }}
    sonarr = doc["sonarr"]["orama-sonarr"]
    assert sonarr["base_url"] == "http://sonarr:8989"
    assert sonarr["api_key"] == SONARR_KEY
    assert sonarr["quality_definition"] == {"type": "series"}
    assert sonarr["quality_profiles"][0]["name"] == "Orama"
    assert "custom_formats" not in sonarr  # nothing for Orama to score


def test_digest_ignores_keys_but_follows_choices():
    apps, _ = recyclarr.check({"apps": apps_for()})
    other, _ = recyclarr.check({"apps": apps_for("lg")})
    assert recyclarr.digest(apps) == recyclarr.digest(json.loads(json.dumps(apps)))
    assert recyclarr.digest(apps) != recyclarr.digest(other)
    assert SONARR_KEY not in recyclarr.digest(apps)


def test_settings_pin_both_guide_repositories_to_commits():
    providers = recyclarr.SETTINGS["resource_providers"]
    assert set(recyclarr.SETTINGS) == {"resource_providers"}
    assert [(p["type"], p["clone_url"]) for p in providers] == [
        ("trash-guides", "https://github.com/TRaSH-Guides/Guides.git"),
        ("config-templates", "https://github.com/recyclarr/config-templates.git"),
    ]
    assert [p["reference"] for p in providers] == [
        recyclarr.TRASH_GUIDES_COMMIT, recyclarr.CONFIG_TEMPLATES_COMMIT]
    for p in providers:
        assert re.fullmatch(r"[0-9a-f]{40}", p["reference"])  # a commit, never a branch
        assert p["replace_default"] is True  # the unpinned official copies are not used
        assert re.fullmatch(r"[a-zA-Z0-9_-]+", p["name"])
        assert set(p) == {"name", "type", "clone_url", "reference", "replace_default"}
    assert len({p["name"] for p in providers}) == len(providers)
    assert yaml.safe_load(recyclarr.settings_text()) == recyclarr.SETTINGS


def test_digest_follows_the_pinned_commits(monkeypatch):
    apps, _ = recyclarr.check({"apps": apps_for()})
    before = recyclarr.digest(apps)
    pinned = json.loads(json.dumps(recyclarr.SETTINGS))
    pinned["resource_providers"][0]["reference"] = "0" * 40
    monkeypatch.setattr(recyclarr, "SETTINGS", pinned)
    assert recyclarr.digest(apps) != before


# ---------------------------------------------------------------- the command


def test_sync_writes_private_yaml_runs_and_deletes_it(env, roots):
    resp = env.call({"apps": apps_for()})
    assert resp == {"id": "r1", "ok": True, "result": {"changed": True}}
    assert env.runner.recyclarr_calls == 1
    assert env.seen.modes == [0o600]
    doc = env.seen.doc
    assert doc["sonarr"]["orama-sonarr"]["api_key"] == SONARR_KEY
    assert doc["radarr"]["orama-radarr"]["api_key"] == RADARR_KEY
    assert not yaml_path(roots).exists()
    folder = roots.config / "config" / "recyclarr"
    chowned = {p for p, uid, gid in env.chowns.calls if (uid, gid) == (1000, 1001)}
    assert str(folder) in chowned  # the folder Recyclarr writes its state into
    assert any(p.startswith(str(folder) + "/.recyclarr.yml.") for p in chowned)
    # The pinned guide settings: written for the run like the YAML, never holding a key.
    assert env.seen.settings == [(recyclarr.settings_text(), 0o600)]
    assert SONARR_KEY not in env.seen.settings[0][0]
    assert any(p.startswith(str(folder) + "/.settings.yml.") for p in chowned)
    assert not (folder / "settings.yml").exists()
    state = roots.config / "app-env" / "recyclarr.state"
    assert stat.S_IMODE(state.stat().st_mode) == 0o600
    assert SONARR_KEY not in state.read_text() and RADARR_KEY not in state.read_text()


def test_yaml_deleted_and_failure_reported_plainly(env, roots):
    env.runner.fail_next_recyclarr = True
    resp = env.call({"apps": apps_for()})
    assert resp["ok"] is False
    assert resp["error"].startswith("Updating the quality profiles failed.")
    assert env.runner.recyclarr_calls == 1
    assert not yaml_path(roots).exists()
    assert not yaml_path(roots).with_name("settings.yml").exists()
    # Nothing recorded as done: the same request runs Recyclarr again.
    assert env.call({"apps": apps_for()})["result"] == {"changed": True}
    assert env.runner.recyclarr_calls == 2
    assert not yaml_path(roots).exists()


def test_second_identical_call_is_unchanged_without_docker(env):
    assert env.client.recyclarr_sync(apps_for()) is True
    assert env.client.recyclarr_sync(apps_for()) is False
    assert env.runner.recyclarr_calls == 1
    assert env.client.recyclarr_sync(apps_for(), force=True) is True
    assert env.runner.recyclarr_calls == 2


def test_device_switch_drops_old_orama_scores(env):  # [RF3]
    env.client.recyclarr_sync(apps_for("samsung_s95f"))
    old = env.seen.doc
    assert env.client.recyclarr_sync(apps_for("apple_tv")) is True
    new = env.seen.doc
    for app in ("radarr", "sonarr"):
        new_ids = {t for cf in new[app][f"orama-{app}"].get("custom_formats", [])
                   for t in cf["trash_ids"]}
        assert new_ids == set(apps_for("apple_tv")[app]["scores"])
        old_ids = {t for cf in old[app][f"orama-{app}"].get("custom_formats", [])
                   for t in cf["trash_ids"]}
        assert old_ids - new_ids  # S95F blocks formats Apple TV does not
        # Recyclarr resets every score in Orama's profile it was not given (TRaSH or 0).
        for profile in new[app][f"orama-{app}"]["quality_profiles"]:
            assert profile["reset_unmatched_scores"] == {"enabled": True}


def test_quality_switch_keeps_the_one_orama_profile(env):  # [RF3]
    env.client.recyclarr_sync(apps_for(quality="4k"))
    old = env.seen.doc
    assert env.client.recyclarr_sync(apps_for(quality="1080p")) is True
    new = env.seen.doc
    for app in ("radarr", "sonarr"):
        (before,) = old[app][f"orama-{app}"]["quality_profiles"]
        (after,) = new[app][f"orama-{app}"]["quality_profiles"]
        assert before["trash_id"] != after["trash_id"]  # another TRaSH profile ...
        assert before["name"] == after["name"] == "Orama"  # ... in the same app profile
        for cf in new[app][f"orama-{app}"].get("custom_formats", []):
            assert cf["assign_scores_to"][0]["name"] == "Orama"


def test_user_custom_formats_are_left_alone(env):
    env.client.recyclarr_sync(apps_for())
    for inst in env.seen.doc.values():
        cfg = next(iter(inst.values()))
        assert cfg["delete_old_custom_formats"] is False
        names = {p["name"] for p in cfg["quality_profiles"]}
        assert names == {"Orama"}
        for cf in cfg.get("custom_formats", []):
            assert {a["name"] for a in cf["assign_scores_to"]} <= names


def test_only_requested_apps_are_synced(env):
    radarr_only = {"radarr": apps_for()["radarr"]}
    assert env.client.recyclarr_sync(radarr_only) is True
    assert set(env.seen.doc) == {"radarr"}


def test_missing_api_key_refused_plainly(roots):
    write_env(roots, "radarr", {"RADARR__AUTH__APIKEY": RADARR_KEY})
    runner = FakeRunner()
    _server, path, stop = serve(roots, runner)
    try:
        with pytest.raises(AgentError, match="Sonarr's API key"):
            AgentClient(path, timeout=10).recyclarr_sync(apps_for())
    finally:
        stop()
    assert runner.recyclarr_calls == 0
    assert not yaml_path(roots).exists()


def test_refused_request_writes_and_runs_nothing(env, roots):
    resp = env.call({"apps": {"radarr": {"templates": ["nope"], "scores": {}}}})
    assert resp["ok"] is False
    assert env.runner.recyclarr_calls == 0
    assert not (roots.config / "config" / "recyclarr").exists()


def test_symlinked_recyclarr_folder_refused(env, roots, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (roots.config / "config").mkdir()
    (roots.config / "config" / "recyclarr").symlink_to(outside)
    resp = env.call({"apps": apps_for()})
    assert resp["ok"] is False and "link" in resp["error"]
    assert list(outside.iterdir()) == []
    assert env.runner.recyclarr_calls == 0


def test_planted_link_at_yaml_is_replaced_not_followed(env, roots, tmp_path):
    target = tmp_path / "target"
    target.write_text("keep")
    folder = roots.config / "config" / "recyclarr"
    folder.mkdir(parents=True)
    (folder / "recyclarr.yml").symlink_to(target)
    assert env.client.recyclarr_sync(apps_for()) is True
    assert target.read_text() == "keep"
    assert not os.path.lexists(folder / "recyclarr.yml")


def test_planted_settings_are_replaced_not_followed(env, roots, tmp_path):
    target = tmp_path / "target"
    target.write_text("keep")
    folder = roots.config / "config" / "recyclarr"
    folder.mkdir(parents=True)
    (folder / "settings.yml").symlink_to(target)
    # Recyclarr refuses to start when both spellings exist, and would read this one instead.
    (folder / "settings.yaml").write_text("resource_providers: []\n")
    assert env.client.recyclarr_sync(apps_for()) is True
    assert env.seen.settings == [(recyclarr.settings_text(), 0o600)]
    assert env.seen.alternates == [False]
    assert target.read_text() == "keep"
    assert not os.path.lexists(folder / "settings.yml")
    assert not os.path.lexists(folder / "settings.yaml")


def test_keys_never_logged(env, caplog):
    with caplog.at_level(logging.DEBUG):
        env.client.recyclarr_sync(apps_for())
        env.runner.fail_next_recyclarr = True
        with pytest.raises(AgentError) as err:
            env.client.recyclarr_sync(apps_for("lg"))
    for key in (SONARR_KEY, RADARR_KEY):
        assert key not in caplog.text and key not in err.value.message


# ---------------------------------------------------------------- the docker run


RM = ["docker", "rm", "-f", "orama-recyclarr"]


class FakeRun:
    """Answers ``docker rm`` with ``rm`` and ``docker run`` with ``run``: (rc, stderr) or an
    exception to raise."""

    def __init__(self, run=(0, ""), rm=(0, "")):
        self.results = {"rm": rm, "run": run}
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        result = self.results[argv[1]]
        if isinstance(result, Exception):
            raise result
        rc, stderr = result
        return subprocess.CompletedProcess(argv, rc, stdout="", stderr=stderr)

    def argvs(self):
        return [argv for argv, _ in self.calls]


def run_argv(roots):
    return [
        "docker", "run", "--rm", "--name", "orama-recyclarr",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--network", "orama-net", "--user", "1000:1001",
        "-v", f"{roots.config}/config/recyclarr:/config",
        CATALOG["recyclarr"].image, "sync", "--config", "/config/recyclarr.yml",
    ]


def test_docker_run_argv_is_pinned(roots, monkeypatch):
    monkeypatch.setenv("DOCKER_HOST", "unix:///var/run/docker.sock")
    monkeypatch.setenv("RECYCLARR_CONFIG_DIR", "/evil")
    run = FakeRun()
    ComposeRunner(roots, run=run).recyclarr_sync()
    assert run.argvs() == [RM, run_argv(roots)]  # a leftover container is removed first
    rm_kw, run_kw = (kw for _, kw in run.calls)
    assert rm_kw["timeout"] == 60 and run_kw["timeout"] == 900
    for kw in (rm_kw, run_kw):
        assert kw["shell"] is False
        assert set(kw["env"]) <= {"PATH", "HOME", "DOCKER_HOST"}


def test_no_leftover_container_is_fine(roots):
    run = FakeRun(rm=(1, "Error response from daemon: No such container: orama-recyclarr"))
    ComposeRunner(roots, run=run).recyclarr_sync()
    assert run.argvs() == [RM, run_argv(roots)]


def test_container_that_cannot_be_removed_stops_the_run(roots):
    run = FakeRun(rm=(1, "Error response from daemon: permission denied"))
    with pytest.raises(RunnerError, match="Updating the quality profiles failed"):
        ComposeRunner(roots, run=run).recyclarr_sync()
    assert run.argvs() == [RM]  # never started next to a keyed container


def test_docker_run_failure_is_plain(roots):
    run = FakeRun(run=(1, f"api_key: {RADARR_KEY}"))
    with pytest.raises(RunnerError) as err:
        ComposeRunner(roots, run=run).recyclarr_sync()
    assert RADARR_KEY not in str(err.value)
    assert RADARR_KEY in err.value.detail  # only ever logged redacted
    assert run.argvs() == [RM, run_argv(roots), RM]


def test_docker_run_timeout_removes_the_container(roots):
    run = FakeRun(run=subprocess.TimeoutExpired(["docker"], 900, stderr=RADARR_KEY))
    with pytest.raises(RunnerError, match="did not finish in time") as err:
        ComposeRunner(roots, run=run).recyclarr_sync()
    assert RADARR_KEY not in str(err.value)
    # The killed CLI leaves the container running with the keys: it is removed.
    assert run.argvs() == [RM, run_argv(roots), RM]


def test_failed_cleanup_after_timeout_still_reports_the_timeout(roots):
    class RmFailsLater(FakeRun):
        def __call__(self, argv, **kw):
            if argv == RM and self.calls:  # the clean-up after the run, not the first rm
                self.results["rm"] = (1, "Cannot connect to the Docker daemon")
            return super().__call__(argv, **kw)

    run = RmFailsLater(run=subprocess.TimeoutExpired(["docker"], 900))
    with pytest.raises(RunnerError, match="did not finish in time"):
        ComposeRunner(roots, run=run).recyclarr_sync()
    assert run.argvs() == [RM, run_argv(roots), RM]


@pytest.fixture
def fake_docker(tmp_path, monkeypatch):
    """A `docker` whose run prints the YAML it was given (keys and all) and fails."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "docker"
    script.write_text(
        "#!/usr/bin/env bash\n"
        '[[ "$1" == rm ]] && exit 0\n'
        'for a in "$@"; do case "$a" in *:/config) dir="${a%:/config}";; esac; done\n'
        'cat "$dir/recyclarr.yml" >&2\n'
        "exit 3\n"
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")


def test_real_runner_failure_logs_redacted_and_answers_plainly(roots, keys, fake_docker, caplog):
    _server, path, stop = serve(roots, ComposeRunner(roots))
    try:
        with caplog.at_level(logging.INFO), pytest.raises(AgentError) as err:
            AgentClient(path, timeout=10).recyclarr_sync(apps_for())
    finally:
        stop()
    assert err.value.message == (
        "Updating the quality profiles failed. "
        "To see why, run: sudo docker logs orama-server-agent"
    )
    assert "api_key: '***'" in caplog.text or "api_key: ***" in caplog.text
    for key in (SONARR_KEY, RADARR_KEY):
        assert key not in caplog.text and key not in err.value.message
    assert not yaml_path(roots).exists()


def test_render_instance_names_are_globally_unique():
    doc = recyclarr.render(compile_profile("safest", "mixed"),
                           {"sonarr": SONARR_KEY, "radarr": RADARR_KEY})
    names = [name.casefold() for instances in doc.values() for name in instances]
    assert len(names) == len(set(names)) == 2
