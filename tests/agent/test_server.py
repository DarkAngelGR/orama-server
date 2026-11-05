import json
import logging
import os
import shutil
import socket
import stat
import subprocess
import tempfile
import threading
import time
from pathlib import Path, PurePosixPath

import pytest
import yaml
from conftest import make_setup, random_secret

from orama.agent_client import DEFAULT_TIMEOUT, AgentClient, AgentError
from orama.catalog import CATALOG
from orama.compose import build_apps_compose
from orama.layout import app_config_dir, app_env_dir, app_env_file, data_folders
from orama_agent.runner import (
    CALL_TIMEOUT,
    PULL_TIMEOUT,
    UP_TIMEOUT,
    ComposeRunner,
    FakeRunner,
    RunnerError,
)
from orama_agent.server import (
    MAX_REQUEST,
    AgentServer,
    _env_file_text,
    _parse_env_file,
    _write_atomic,
)
from orama_agent.validate import Refused, Roots

PORTS = {a: s.default_host_port for a, s in CATALOG.items() if s.default_host_port}
SECRET = random_secret()

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

class Chowns:
    """Records chown calls instead of changing owners (the suite also runs as non-root)."""

    def __init__(self):
        self.calls = []

    def __call__(self, target, uid, gid, **kw):
        path = os.readlink(f"/proc/self/fd/{target}") if isinstance(target, int) else target
        self.calls.append((str(path), uid, gid))

    def paths(self):
        return [p for p, _, _ in self.calls]

class Agent:
    def __init__(self, server, runner, socket_path, chowns):
        self.server = server
        self.runner = runner
        self.socket_path = socket_path
        self.chowns = chowns
        self.client = AgentClient(socket_path, timeout=10)

    def raw(self, line: bytes) -> bytes:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(10)
            s.connect(self.socket_path)
            s.sendall(line)
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
            return buf

    def call(self, cmd, args, id_="t1"):
        line = json.dumps({"id": id_, "cmd": cmd, "args": args}).encode() + b"\n"
        return json.loads(self.raw(line))

@pytest.fixture
def agent(roots):
    sock_dir = tempfile.mkdtemp(prefix="oa", dir="/tmp")
    socket_path = os.path.join(sock_dir, "agent.sock")
    runner = FakeRunner()
    chowns = Chowns()
    server = AgentServer(roots, runner, socket_path, chown=chowns, read_timeout=5)
    thread = threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True)
    thread.start()
    try:
        yield Agent(server, runner, socket_path, chowns)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
        shutil.rmtree(sock_dir, ignore_errors=True)

def setup_for(roots, **kw):
    values = {
        "data_root": str(roots.data), "config_root": str(roots.config), "ports": PORTS,
        "puid": roots.puid, "pgid": roots.pgid,
    }
    values.update(kw)
    return make_setup(**values)

def doc_and_env(roots):
    doc = build_apps_compose(setup_for(roots))
    env = {
        app: {name: SECRET for name in CATALOG[app].secret_env}
        for app in doc["services"]
        if CATALOG[app].secret_env
    }
    return doc, env

def all_folders(roots, doc):
    folders = [str(p) for p in data_folders(PurePosixPath(roots.data))]
    folders += [str(app_config_dir(PurePosixPath(roots.config), a)) for a in doc["services"]]
    return folders

def prepared(agent, roots):
    doc, env = doc_and_env(roots)
    agent.client.ensure_folders(all_folders(roots, doc))
    return doc, env

# ---------------------------------------------------------------- protocol

def test_unknown_command_refused(agent):
    resp = agent.call("rm -rf", {})
    assert resp == {"id": "t1", "ok": False, "error": "unknown command"}

def test_self_update_not_implemented(agent):
    resp = agent.call("self-update", {})
    assert resp == {"id": "t1", "ok": False, "error": "not implemented in Plan 1"}

@pytest.mark.parametrize(
    "line",
    [
        b"{not json\n",
        b"[1, 2]\n",
        b'{"cmd": "status", "args": {}}\n',
        b'{"id": 5, "cmd": "status", "args": {}}\n',
        b'{"id": "x", "cmd": 3, "args": {}}\n',
        b'{"id": "x", "cmd": "status", "args": []}\n',
        b"\xff\xfe\n",
        b"[" * 200000 + b"\n",
    ],
)
def test_malformed_requests_get_errors_and_server_keeps_running(agent, line):
    resp = json.loads(agent.raw(line))
    assert resp["ok"] is False and resp["error"]
    assert agent.call("status", {})["ok"] is True

def test_oversized_request_rejected(agent):
    big = b'{"id": "x", "cmd": "status", "args": {"pad": "' + b"a" * MAX_REQUEST + b'"}}\n'
    resp = json.loads(agent.raw(big))
    assert resp["ok"] is False and "too large" in resp["error"]
    assert agent.call("status", {})["ok"] is True

def test_unexpected_exception_is_internal_error(agent, roots, caplog):
    (roots.config / "compose.yaml").write_text("name: orama-apps\n")

    def boom():
        raise ValueError(SECRET)

    agent.runner.ps = boom
    with caplog.at_level(logging.DEBUG):
        resp = agent.call("status", {})
    assert resp == {"id": "t1", "ok": False, "error": "internal error"}
    assert SECRET not in caplog.text
    assert agent.call("self-update", {})["ok"] is False  # still serving

def test_respond_never_raises(agent, monkeypatch, caplog):
    import orama_agent.server as server_mod

    def boom(_line):
        raise RuntimeError(SECRET)

    monkeypatch.setattr(server_mod, "_parse", boom)
    with caplog.at_level(logging.DEBUG):
        resp = agent.call("status", {})
    assert resp == {"id": None, "ok": False, "error": "internal error"}
    assert SECRET not in caplog.text


def test_unexpected_args_refused(agent):
    assert agent.call("status", {"x": 1})["ok"] is False

def test_socket_mode_and_group(agent, roots):
    mode = os.lstat(agent.socket_path).st_mode
    assert stat.S_ISSOCK(mode) and stat.S_IMODE(mode) == 0o660
    assert (agent.socket_path, -1, roots.pgid) in agent.chowns.calls

def test_stale_socket_replaced_but_regular_file_refused(roots):
    sock_dir = tempfile.mkdtemp(prefix="oa", dir="/tmp")
    try:
        path = os.path.join(sock_dir, "agent.sock")
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(path)
        stale.close()
        server = AgentServer(roots, FakeRunner(), path, chown=Chowns())
        server.server_close()
        os.unlink(path)
        Path(path).write_text("not a socket")
        with pytest.raises(Refused):
            AgentServer(roots, FakeRunner(), path, chown=Chowns())
        assert Path(path).read_text() == "not a socket"
    finally:
        shutil.rmtree(sock_dir, ignore_errors=True)

# ---------------------------------------------------------------- ensure-folders

def test_ensure_folders_creates_with_owner_and_mode(agent, roots):
    doc, _ = doc_and_env(roots)
    wanted = all_folders(roots, doc)
    created = agent.client.ensure_folders(wanted)
    assert set(wanted) <= set(created)
    for p in wanted:
        assert stat.S_IMODE(os.lstat(p).st_mode) == 0o2775
        assert (p, roots.puid, roots.pgid) in agent.chowns.calls
    parent = roots.config / "config"
    assert stat.S_IMODE(os.lstat(parent).st_mode) == 0o755
    assert str(parent) not in agent.chowns.paths()
    assert agent.client.ensure_folders(wanted) == []  # already there

def test_ensure_folders_creates_missing_data_parents(agent, roots):
    created = agent.client.ensure_folders([str(roots.data / "media/movies")])
    assert created == [str(roots.data / "media"), str(roots.data / "media/movies")]

@pytest.mark.parametrize(
    "which",
    ["config_root", "config_dir", "app_env", "orama_state", "data_root", "unknown_app",
     "outside", "relative", "not_str"],
)
def test_ensure_folders_refuses_paths_outside_the_set(agent, roots, which):
    path = {
        "config_root": str(roots.config),
        "config_dir": str(roots.config / "config"),
        "app_env": str(app_env_dir(PurePosixPath(roots.config))),
        "orama_state": str(roots.config / "orama"),
        "data_root": str(roots.data),
        "unknown_app": str(roots.config / "config/evil"),
        "outside": "/etc/cron.d",
        "relative": "media",
        "not_str": 7,
    }[which]
    resp = agent.call("ensure-folders", {"paths": [path]})
    assert resp["ok"] is False
    assert agent.chowns.calls == [(agent.socket_path, -1, roots.pgid)]

def test_ensure_folders_refuses_outside_roots(agent):
    with pytest.raises(AgentError):
        agent.client.ensure_folders(["/etc"])

def test_ensure_folders_refuses_planted_symlink_in_data_root(agent, roots, tmp_path):
    target = tmp_path / "elsewhere"
    target.mkdir()
    (roots.data / "media").symlink_to(target)
    with pytest.raises(AgentError, match="link"):
        agent.client.ensure_folders([str(roots.data / "media/movies")])
    assert list(target.iterdir()) == []
    assert all(p == agent.socket_path for p in agent.chowns.paths())

def test_ensure_folders_refuses_file_in_place_of_folder(agent, roots):
    (roots.data / "media").write_text("x")
    with pytest.raises(AgentError):
        agent.client.ensure_folders([str(roots.data / "media")])

# ---------------------------------------------------------------- apply

def test_apply_rejects_invalid_compose_and_runs_nothing(agent, roots):
    doc, env = prepared(agent, roots)
    doc["services"]["sonarr"]["privileged"] = True
    with pytest.raises(AgentError, match="elevated"):
        agent.client.apply(doc, env)
    assert agent.runner.up_calls == 0
    assert not (roots.config / "compose.yaml").exists()
    assert not os.path.exists(app_env_file(PurePosixPath(roots.config), "sonarr"))

def test_apply_rejects_invalid_env_files_and_writes_nothing(agent, roots):
    doc, env = prepared(agent, roots)
    env["sonarr"] = {"SONARR__AUTH__APIKEY": "a'b"}
    with pytest.raises(AgentError):
        agent.client.apply(doc, env)
    assert agent.runner.up_calls == 0
    assert not (roots.config / "compose.yaml").exists()
    assert not os.path.exists(app_env_dir(PurePosixPath(roots.config)))

def test_apply_writes_env_files_0600_and_compose(agent, roots):
    doc, env = prepared(agent, roots)
    changed = agent.client.apply(doc, env)
    assert sorted(changed) == sorted(doc["services"])

    env_dir = Path(app_env_dir(PurePosixPath(roots.config)))
    assert stat.S_IMODE(os.lstat(env_dir).st_mode) == 0o700
    for app, values in env.items():
        path = Path(app_env_file(PurePosixPath(roots.config), app))
        st = os.lstat(path)
        assert stat.S_ISREG(st.st_mode) and stat.S_IMODE(st.st_mode) == 0o600
        assert st.st_uid == os.geteuid()
        expected = "".join(f"{k}='{v}'\n" for k, v in values.items())
        assert path.read_text() == expected

    compose = roots.config / "compose.yaml"
    assert stat.S_IMODE(os.lstat(compose).st_mode) == 0o644
    assert yaml.safe_load(compose.read_text()) == doc
    assert SECRET not in compose.read_text()

    empty_env = env_dir / "compose.env"
    assert empty_env.read_text() == ""
    assert stat.S_IMODE(os.lstat(empty_env).st_mode) == 0o600
    assert agent.runner.up_files == [str(compose)]
    assert [p for p in env_dir.iterdir() if p.name.endswith(".tmp")] == []

def test_env_file_text_round_trips_through_composes_parser():
    values = {"WIREGUARD_PRIVATE_KEY": "ab+/=", "HTTPPROXY_PASSWORD": "c-d_e"}
    text = _env_file_text(values).decode()
    assert text == "WIREGUARD_PRIVATE_KEY='ab+/='\nHTTPPROXY_PASSWORD='c-d_e'\n"
    assert _parse_env_file(text) == values


def test_parse_env_file_follows_composes_single_quote_rules():
    # A backslash before the closing quote escapes it, so the value runs on into the next
    # line, and what follows the next quote is read as a new setting.
    hook_name = "VPN_PORT_FORWARDING_UP_COMMAND"
    text = f"A='x\\'\nB='{hook_name}=id #'\n"
    parsed = _parse_env_file(text)
    assert parsed is None
    assert _parse_env_file("A='x'\nB='y'\n") == {"A": "x", "B": "y"}
    assert _parse_env_file("A='x'\nA='y'\n") is None  # duplicate key
    assert _parse_env_file("A='x' B='y'\n") is None  # trailing text after the quote
    assert _parse_env_file("A='x") is None  # unterminated
    assert _parse_env_file("A=x\n") is None  # unquoted


@pytest.mark.parametrize(
    "value", ["x\\", "x'", "a'b", "x\\'y", "a\nB='c'"]
)
def test_env_file_text_refuses_values_that_do_not_round_trip(value):
    # Defence in depth: validate_env_files already refuses these; the renderer checks again.
    hook_name = "VPN_PORT_FORWARDING_UP_COMMAND"
    values = {"WIREGUARD_PRIVATE_KEY": value, "HTTPPROXY_PASSWORD": f"{hook_name}=id #"}
    with pytest.raises(Refused) as exc:
        _env_file_text(values)
    assert hook_name not in str(exc.value)
    assert value not in str(exc.value)


def test_apply_refuses_planted_symlink_at_env_file(agent, roots, tmp_path):
    doc, env = prepared(agent, roots)
    victim = tmp_path / "victim"
    victim.write_text("untouched")
    env_dir = Path(app_env_dir(PurePosixPath(roots.config)))
    env_dir.mkdir(mode=0o700)
    (env_dir / "sonarr.env").symlink_to(victim)
    with pytest.raises(AgentError, match="link"):  # validate_compose refuses it up front
        agent.client.apply(doc, env)
    assert victim.read_text() == "untouched"
    assert agent.runner.up_calls == 0

def test_atomic_write_replaces_symlink_without_following(tmp_path):
    victim = tmp_path / "victim"
    victim.write_text("untouched")
    (tmp_path / "dir").mkdir()
    (tmp_path / "dir" / "sonarr.env").symlink_to(victim)
    fd = os.open(tmp_path / "dir", os.O_RDONLY | os.O_DIRECTORY)
    try:
        _write_atomic(fd, "sonarr.env", b"K='v'\n", 0o600)
    finally:
        os.close(fd)
    assert victim.read_text() == "untouched"
    st = os.lstat(tmp_path / "dir" / "sonarr.env")
    assert stat.S_ISREG(st.st_mode) and stat.S_IMODE(st.st_mode) == 0o600
    assert (tmp_path / "dir" / "sonarr.env").read_text() == "K='v'\n"
    assert sorted(p.name for p in (tmp_path / "dir").iterdir()) == ["sonarr.env"]

def test_apply_refuses_symlinked_env_dir(agent, roots, tmp_path):
    doc, env = prepared(agent, roots)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    Path(app_env_dir(PurePosixPath(roots.config))).symlink_to(elsewhere)
    with pytest.raises(AgentError, match="link"):
        agent.client.apply(doc, env)
    assert list(elsewhere.iterdir()) == []
    assert agent.runner.up_calls == 0

def test_apply_checks_host_paths_before_up(agent, roots):
    doc, env = doc_and_env(roots)  # app config folders never created
    with pytest.raises(AgentError, match="does not exist"):
        agent.client.apply(doc, env)
    assert agent.runner.up_calls == 0

def test_apply_twice_reports_no_changes(agent, roots):  # [RF 3]
    doc, env = prepared(agent, roots)
    first = agent.client.apply(doc, env)
    second = agent.client.apply(doc, env)
    assert first != [] and second == [] and agent.runner.up_calls == 2

def test_interrupted_apply_then_rerun_converges(agent, roots):  # [RF 3]
    doc, env = prepared(agent, roots)
    agent.runner.fail_next_up = True
    with pytest.raises(AgentError):
        agent.client.apply(doc, env)
    assert agent.client.apply(doc, env) != []  # converges on retry
    assert agent.client.apply(doc, env) == []

class SameIdsRunner(FakeRunner):
    """Like the real runner after a partial up: containers already recreated keep their IDs."""

    def up(self, project_file):
        super().up(project_file)
        return []


def test_failed_apply_reports_every_service_on_retry(roots):
    sock_dir = tempfile.mkdtemp(prefix="oa", dir="/tmp")
    path = os.path.join(sock_dir, "agent.sock")
    runner = SameIdsRunner()
    server = AgentServer(roots, runner, path, chown=Chowns())
    thread = threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True)
    thread.start()
    try:
        client = AgentClient(path, timeout=10)
        doc, env = doc_and_env(roots)
        client.ensure_folders(all_folders(roots, doc))
        marker = Path(app_env_dir(PurePosixPath(roots.config))) / ".apply-incomplete"
        runner.fail_next_up = True
        with pytest.raises(AgentError):
            client.apply(doc, env)
        assert marker.exists()
        assert client.apply(doc, env) == sorted(doc["services"])
        assert not marker.exists()
        assert client.apply(doc, env) == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
        shutil.rmtree(sock_dir, ignore_errors=True)


def test_env_values_never_logged(agent, roots, caplog):
    doc, env = prepared(agent, roots)
    with caplog.at_level(logging.DEBUG):
        agent.client.apply(doc, env)
        agent.runner.fail_next_up = True
        with pytest.raises(AgentError):
            agent.client.apply(doc, env)
        bad = dict(env, sonarr={"SONARR__AUTH__APIKEY": SECRET + "'"})
        with pytest.raises(AgentError) as err:
            agent.client.apply(doc, bad)
    assert "apply" in caplog.text
    assert SECRET not in caplog.text
    assert SECRET not in str(err.value)

# ---------------------------------------------------------------- status / restart

def test_status_before_first_apply_is_empty(agent, roots):
    agent.runner.ps = lambda: pytest.fail("ps called with no compose file")
    assert agent.client.status() == {}


def test_status_creates_agent_env_file_only_if_missing(agent, roots):
    env_file = Path(app_env_dir(PurePosixPath(roots.config))) / "compose.env"
    agent.client.status()
    assert env_file.read_text() == "" and stat.S_IMODE(os.lstat(env_file).st_mode) == 0o600
    inode = os.lstat(env_file).st_ino
    agent.client.status()
    assert os.lstat(env_file).st_ino == inode  # not rewritten


def test_status_returns_runner_ps(agent, roots):
    doc, env = prepared(agent, roots)
    agent.client.apply(doc, env)
    assert agent.client.status() == {s: "running" for s in doc["services"]}

def test_restart_known_app(agent):
    agent.client.restart("sonarr")
    assert agent.runner.restarts == ["sonarr"]

@pytest.mark.parametrize("app", ["evil", "sonarr; rm -rf /", "Sonarr", 3, ""])
def test_restart_refuses_unknown_app(agent, app):
    assert agent.call("restart", {"app": app})["ok"] is False
    assert agent.runner.restarts == []

# ---------------------------------------------------------------- client

def test_client_raises_agent_error_with_message(agent):
    with pytest.raises(AgentError, match="Sonarr|app Orama knows"):
        agent.client.restart("nope")

def test_client_timeout_exceeds_agent_worst_case():
    # One apply: ps, pull, up, ps.
    assert PULL_TIMEOUT == 3600
    assert DEFAULT_TIMEOUT > CALL_TIMEOUT + PULL_TIMEOUT + UP_TIMEOUT + CALL_TIMEOUT
    assert AgentClient().timeout == DEFAULT_TIMEOUT


def test_client_honours_timeout(tmp_path):
    sock_dir = tempfile.mkdtemp(prefix="oa", dir="/tmp")
    try:
        path = os.path.join(sock_dir, "silent.sock")
        silent = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        silent.bind(path)
        silent.listen(1)
        with pytest.raises(AgentError):
            AgentClient(path, timeout=0.3).status()
        silent.close()
    finally:
        shutil.rmtree(sock_dir, ignore_errors=True)

# ---------------------------------------------------------------- ComposeRunner

class FakeRun:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        out = self.outputs.pop(0) if self.outputs else ""
        rc = 1 if out is None else 0

        return subprocess.CompletedProcess(argv, rc, stdout=out or "", stderr="")

def _prefix(roots):
    return [
        "docker", "compose",
        "--project-directory", str(roots.config),
        "--env-file", str(roots.config / "app-env" / "compose.env"),
        "-p", "orama-apps",
    ]

def test_compose_runner_up_argv_env_and_changed(roots, monkeypatch):
    monkeypatch.setenv("COMPOSE_FILE", "/evil.yaml")
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "evil")
    monkeypatch.setenv("DOCKER_HOST", "unix:///var/run/docker.sock")
    before = json.dumps([
        {"Service": "sonarr", "ID": "a1", "State": "running"},
        {"Service": "radarr", "ID": "b1", "State": "running"},
    ])
    after = "\n".join([
        json.dumps({"Service": "sonarr", "ID": "a1", "State": "running"}),
        json.dumps({"Service": "radarr", "ID": "b2", "State": "running"}),
        json.dumps({"Service": "plex", "ID": "c1", "State": "running"}),
    ])
    run = FakeRun([before, "", "", after])
    runner = ComposeRunner(roots, run=run)
    project = str(roots.config / "compose.yaml")
    assert runner.up(project) == ["plex", "radarr"]

    pull_argv, pull_kw = run.calls[1]
    assert pull_argv == _prefix(roots) + ["-f", project, "pull", "--quiet"]
    assert pull_kw["timeout"] == PULL_TIMEOUT
    up_argv, up_kw = run.calls[2]
    assert up_argv == _prefix(roots) + [
        "-f", project, "up", "-d", "--remove-orphans", "--wait",
    ]
    assert up_kw["timeout"] == UP_TIMEOUT
    assert run.calls[0][0] == _prefix(roots) + ["-f", project, "ps", "-a", "--format", "json"]
    for _argv, kw in run.calls:
        assert "shell" not in kw or kw["shell"] is False
        assert not any(k.startswith("COMPOSE") for k in kw["env"])
        assert set(kw["env"]) <= {"PATH", "HOME", "DOCKER_HOST"}
        assert kw["env"]["DOCKER_HOST"] == "unix:///var/run/docker.sock"

def test_compose_runner_ps_parses_empty_and_array(roots):
    assert ComposeRunner(roots, run=FakeRun([""])).ps() == {}
    out = json.dumps([{"Service": "plex", "ID": "x", "State": "exited"}])
    assert ComposeRunner(roots, run=FakeRun([out])).ps() == {"plex": "exited"}

def test_compose_runner_up_failure_raises(roots):
    runner = ComposeRunner(roots, run=FakeRun(["", None]))
    with pytest.raises(RunnerError):
        runner.up(str(roots.config / "compose.yaml"))

def test_compose_runner_restart(roots):
    run = FakeRun([""])
    ComposeRunner(roots, run=run).restart("sonarr")
    assert run.calls[0][0] == _prefix(roots) + [
        "-f", str(roots.config / "compose.yaml"), "restart", "sonarr",
    ]
    with pytest.raises(Refused):
        ComposeRunner(roots, run=run).restart("--help")


# ---------------------------------------------------------------- compose failures

STORED = "st0r3dsecret-fedcba9876543210"


@pytest.fixture
def fake_docker(tmp_path, monkeypatch):
    """A `docker` on PATH: pull fails with 50 lines of stderr (one with the secret) while
    the flag file exists; restart always fails printing the stored secret; else succeeds."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    flag = tmp_path / "fail-pull"
    lines = "\n".join(f"line-{i:02d}" + (f" key={SECRET}" if i == 45 else "") for i in range(50))
    script = bin_dir / "docker"
    script.write_text(
        "#!/usr/bin/env bash\n"
        'case " $* " in\n'
        f'  *" pull "*) if [[ -e {flag} ]]; then printf "%s\\n" "{lines}" >&2; exit 1; fi ;;\n'
        f'  *" restart "*) echo "restart failed: token={STORED}" >&2; exit 1 ;;\n'
        "esac\n"
        "exit 0\n"
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    return flag


@pytest.fixture
def real_agent(roots, fake_docker):
    sock_dir = tempfile.mkdtemp(prefix="oa", dir="/tmp")
    path = os.path.join(sock_dir, "agent.sock")
    server = AgentServer(roots, ComposeRunner(roots), path, chown=Chowns())
    thread = threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True)
    thread.start()
    try:
        yield AgentClient(path, timeout=10)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
        shutil.rmtree(sock_dir, ignore_errors=True)


def test_failed_apply_logs_redacted_tail_and_answers_plainly(real_agent, roots, fake_docker,
                                                             caplog):
    doc, env = doc_and_env(roots)
    real_agent.ensure_folders(all_folders(roots, doc))
    fake_docker.touch()
    with caplog.at_level(logging.INFO), pytest.raises(AgentError) as err:
        real_agent.apply(doc, env)
    assert err.value.message == (
        "Downloading the apps failed. To see why, run: sudo docker logs orama-server-agent"
    )
    assert "line-" not in err.value.message
    assert "line-10" in caplog.text and "line-49" in caplog.text
    assert "line-09" not in caplog.text  # only the last 40 lines
    assert "line-45 key=***" in caplog.text
    assert SECRET not in caplog.text


def test_failed_restart_redacts_stored_env_values(real_agent, roots, caplog):
    doc, env = doc_and_env(roots)
    real_agent.ensure_folders(all_folders(roots, doc))
    env = dict(env, sonarr={"SONARR__AUTH__APIKEY": STORED})
    real_agent.apply(doc, env)  # writes the stored env files
    with caplog.at_level(logging.INFO), pytest.raises(AgentError) as err:
        real_agent.restart("sonarr")
    assert err.value.message == (
        "Restarting the app failed. To see why, run: sudo docker logs orama-server-agent"
    )
    assert "restart failed: token=***" in caplog.text
    assert STORED not in caplog.text and STORED not in err.value.message


def test_apply_refuses_proxy_without_password(agent, roots):
    doc = build_apps_compose(setup_for(
        roots, download_methods={"torrent"}, vpn={"route_prowlarr": True}))
    env = {app: {name: SECRET for name in CATALOG[app].secret_env}
           for app in doc["services"] if CATALOG[app].secret_env}
    del env["gluetun"]["HTTPPROXY_PASSWORD"]
    agent.client.ensure_folders(all_folders(roots, doc))
    with pytest.raises(AgentError, match="proxy"):
        agent.client.apply(doc, env)
    env["gluetun"]["HTTPPROXY_PASSWORD"] = SECRET
    agent.client.apply(doc, env)


# ---------------------------------------------------------------- seed-config

def sab_values():
    # Built at runtime so no key-shaped string lands in tracked files.
    return {"api_key": "5eed" * 8, "nzb_key": "beef" * 8}


def qbt_values():
    import base64

    salt = base64.b64encode(b"\x07" * 16).decode()
    key = base64.b64encode(b"\x09" * 64).decode()
    return {"password_pbkdf2": f"@ByteArray({salt}:{key})"}


def sab_ini(roots):
    return roots.config / "config/sabnzbd/sabnzbd.ini"


def qbt_conf(roots):
    return roots.config / "config/qbittorrent/qBittorrent/qBittorrent.conf"


def torrent_doc(agent, roots):
    """Apply a setup with SABnzbd and qBittorrent, so FakeRunner reports them running."""
    doc = build_apps_compose(setup_for(roots, download_methods={"usenet", "torrent"}))
    env = {app: {name: SECRET for name in CATALOG[app].secret_env}
           for app in doc["services"] if CATALOG[app].secret_env}
    agent.client.ensure_folders(all_folders(roots, doc))
    agent.client.apply(doc, env)
    return doc


def test_seed_config_writes_private_file_owned_by_app_user(agent, roots):
    assert agent.client.seed_config("sabnzbd", sab_values()) is True
    path = sab_ini(roots)
    text = path.read_text()
    assert f"api_key = {sab_values()['api_key']}\n" in text
    assert f"nzb_key = {sab_values()['nzb_key']}\n" in text
    assert "host_whitelist = sabnzbd, gluetun, localhost\n" in text
    assert stat.S_IMODE(os.lstat(path).st_mode) == 0o600
    written = [(Path(p), uid, gid) for p, uid, gid in agent.chowns.calls
               if Path(p).name.startswith(".sabnzbd.ini.")]
    assert written == [(Path(written[0][0]), roots.puid, roots.pgid)]
    assert written[0][0].parent == path.parent
    assert (str(path.parent), roots.puid, roots.pgid) in agent.chowns.calls
    assert sorted(p.name for p in path.parent.iterdir()) == ["sabnzbd.ini"]  # no temp files


def test_seed_config_creates_qbittorrent_subfolder_owned_by_app_user(agent, roots):
    assert agent.client.seed_config("qbittorrent", qbt_values()) is True
    path = qbt_conf(roots)
    text = path.read_text()
    assert f'[WebUI]\nPassword_PBKDF2="{qbt_values()["password_pbkdf2"]}"\n' in text
    assert stat.S_IMODE(os.lstat(path).st_mode) == 0o600
    sub = path.parent
    assert (str(sub), roots.puid, roots.pgid) in agent.chowns.calls
    assert stat.S_IMODE(os.lstat(sub).st_mode) == 0o2775


def test_seed_config_keeps_the_apps_own_lines(agent, roots):
    agent.client.ensure_folders([str(app_config_dir(PurePosixPath(roots.config), "sabnzbd"))])
    original = "__version__ = 19\n[misc]\nport = 8080\n\n[servers]\n"
    sab_ini(roots).write_text(original)
    agent.client.seed_config("sabnzbd", sab_values())
    text = sab_ini(roots).read_text()
    assert text.startswith("__version__ = 19\n[misc]\nport = 8080\n")
    assert text.endswith("\n\n[servers]\n")


def test_seed_config_second_call_changes_nothing(agent, roots):  # [RF 3]
    torrent_doc(agent, roots)
    for app, values, path in [("sabnzbd", sab_values(), sab_ini(roots)),
                              ("qbittorrent", qbt_values(), qbt_conf(roots))]:
        assert agent.client.seed_config(app, values) is True
        inode = os.lstat(path).st_ino
        agent.runner.ps = lambda: pytest.fail("runner used for an unchanged file")
        assert agent.client.seed_config(app, values) is False
        assert os.lstat(path).st_ino == inode
        del agent.runner.ps
    assert agent.runner.stops == ["sabnzbd", "qbittorrent"]
    assert agent.runner.starts == ["sabnzbd", "qbittorrent"]


class OrderRunner(FakeRunner):
    """Records what the config file held when the app was stopped and started."""

    def __init__(self, path):
        super().__init__()
        self.path = path
        self.seen = []

    def _text(self):
        return self.path.read_text() if self.path.exists() else None

    def stop(self, app):
        self.seen.append(("stop", self._text()))
        super().stop(app)

    def start(self, app):
        self.seen.append(("start", self._text()))
        super().start(app)


def test_seed_config_stops_running_app_writes_then_starts_it(agent, roots):
    torrent_doc(agent, roots)
    runner = OrderRunner(sab_ini(roots))
    runner.up(agent.runner.up_files[-1])
    agent.server.agent.runner = runner
    assert agent.client.seed_config("sabnzbd", sab_values()) is True
    assert [what for what, _ in runner.seen] == ["stop", "start"]
    assert runner.seen[0][1] is None and sab_values()["api_key"] in runner.seen[1][1]
    assert runner.ps()["sabnzbd"] == "running"


def test_seed_config_leaves_a_stopped_app_stopped(agent, roots):
    torrent_doc(agent, roots)
    agent.runner.stop("sabnzbd")
    agent.runner.stops.clear()
    assert agent.client.seed_config("sabnzbd", sab_values()) is True
    assert agent.runner.stops == [] and agent.runner.starts == []
    assert agent.runner.ps()["sabnzbd"] != "running"


def test_seed_config_without_compose_file_writes_without_runner(agent, roots):
    agent.runner.ps = lambda: pytest.fail("ps called with no compose file")
    assert agent.client.seed_config("sabnzbd", sab_values()) is True
    assert agent.runner.stops == [] and agent.runner.starts == []


def test_seed_config_starts_the_app_again_when_the_write_fails(agent, roots, monkeypatch):
    import orama_agent.server as server_mod

    torrent_doc(agent, roots)

    def boom(*_a, **_kw):
        raise OSError("disk full")

    monkeypatch.setattr(server_mod, "_write_atomic", boom)
    with pytest.raises(AgentError):
        agent.client.seed_config("sabnzbd", sab_values())
    assert agent.runner.stops == ["sabnzbd"] and agent.runner.starts == ["sabnzbd"]


def test_seed_config_refuses_symlink_at_config_file(agent, roots, tmp_path):
    agent.client.ensure_folders([str(app_config_dir(PurePosixPath(roots.config), "sabnzbd"))])
    target = tmp_path / "victim"
    target.write_text("keep me\n")
    sab_ini(roots).symlink_to(target)
    with pytest.raises(AgentError, match="link|regular"):
        agent.client.seed_config("sabnzbd", sab_values())
    assert target.read_text() == "keep me\n"
    assert os.path.islink(sab_ini(roots))


def test_seed_config_refuses_symlink_at_qbittorrent_folder(agent, roots, tmp_path):
    agent.client.ensure_folders(
        [str(app_config_dir(PurePosixPath(roots.config), "qbittorrent"))])
    target = tmp_path / "elsewhere"
    target.mkdir()
    qbt_conf(roots).parent.symlink_to(target)
    with pytest.raises(AgentError, match="link"):
        agent.client.seed_config("qbittorrent", qbt_values())
    assert list(target.iterdir()) == []
    assert not any(p.startswith(str(target)) for p in agent.chowns.paths())


@pytest.mark.parametrize("kind", ["fifo", "folder", "too_big"])
def test_seed_config_refuses_non_regular_or_huge_file(agent, roots, kind):
    agent.client.ensure_folders([str(app_config_dir(PurePosixPath(roots.config), "sabnzbd"))])
    path = sab_ini(roots)
    if kind == "fifo":
        os.mkfifo(path)
    elif kind == "folder":
        path.mkdir()
    else:
        path.write_bytes(b"#" * (MAX_REQUEST + 1))
    with pytest.raises(AgentError):
        agent.client.seed_config("sabnzbd", sab_values())  # 10 s client timeout: no hang
    if kind == "too_big":
        assert path.stat().st_size == MAX_REQUEST + 1
    assert stat.S_ISFIFO(os.lstat(path).st_mode) == (kind == "fifo")


@pytest.mark.parametrize("args", [
    {"app": "sonarr", "values": {}},
    {"app": "sabnzbd", "values": {"api_key": "5eed" * 8}},
    {"app": "sabnzbd", "values": dict(sab_values(), host_whitelist="*")},
    {"app": "sabnzbd", "values": dict(sab_values(), api_key="5eed" * 7 + "\n]x=")},
    {"app": "qbittorrent", "values": {"password_pbkdf2": "plain-password"}},
    {"app": "qbittorrent", "values": qbt_values(), "path": "/etc/passwd"},
    {"app": "sabnzbd"},
])
def test_seed_config_refuses_bad_requests_and_writes_nothing(agent, roots, args):
    resp = agent.call("seed-config", args)
    assert resp["ok"] is False and resp["error"]
    assert not (roots.config / "config").exists()
    assert agent.runner.stops == []


def test_seed_config_values_never_logged(agent, roots, caplog):
    torrent_doc(agent, roots)
    secret_values = [*sab_values().values(), *qbt_values().values()]
    with caplog.at_level(logging.DEBUG):
        agent.client.seed_config("sabnzbd", sab_values())
        agent.client.seed_config("qbittorrent", qbt_values())
        bad = dict(sab_values(), api_key=sab_values()["api_key"] + "x")
        with pytest.raises(AgentError) as err:
            agent.client.seed_config("sabnzbd", bad)
    assert "seed-config" in caplog.text
    for value in [*secret_values, bad["api_key"]]:
        assert value not in caplog.text
        assert value not in str(err.value)


def test_compose_runner_stop_and_start(roots):
    run = FakeRun(["", ""])
    runner = ComposeRunner(roots, run=run)
    runner.stop("sabnzbd")
    runner.start("sabnzbd")
    project = ["-f", str(roots.config / "compose.yaml")]
    assert run.calls[0][0] == _prefix(roots) + project + ["stop", "sabnzbd"]
    assert run.calls[1][0] == _prefix(roots) + project + ["start", "sabnzbd"]
    assert run.calls[0][1]["timeout"] == run.calls[1][1]["timeout"] == UP_TIMEOUT
    for bad in ("--help", "evil"):
        with pytest.raises(Refused):
            runner.stop(bad)
        with pytest.raises(Refused):
            runner.start(bad)



def test_agent_bounds_concurrent_connections_and_recovers(agent):
    # A single slot makes the limit deterministic while the first client idles.
    agent.server._connection_slots = threading.BoundedSemaphore(1)
    held = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    held.settimeout(2)
    held.connect(agent.socket_path)
    try:
        for _ in range(100):
            if not agent.server._connection_slots.acquire(blocking=False):
                break
            agent.server._connection_slots.release()
            time.sleep(0.01)
        else:
            pytest.fail("the Agent did not accept the first connection")

        rejected = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        rejected.settimeout(2)
        try:
            rejected.connect(agent.socket_path)
            assert rejected.recv(1) == b""  # excess connection is closed immediately
        finally:
            rejected.close()
    finally:
        held.close()

    # Closing the socket wakes the handler asynchronously. Wait for its finally block
    # to release the slot before opening the recovery request.
    for _ in range(100):
        if agent.server._connection_slots.acquire(blocking=False):
            agent.server._connection_slots.release()
            break
        time.sleep(0.01)
    else:
        pytest.fail("the Agent did not release the idle connection slot")

    # Releasing an idle slot must restore normal service.
    assert agent.client.status() == {}


@pytest.mark.parametrize("kind", ["directory_link", "file_link", "fifo", "too_big"])
def test_bazarr_seed_refuses_unsafe_file(agent, roots, tmp_path, kind):
    path = roots.config / "config/bazarr/config/config.yaml"
    path.parent.parent.mkdir(parents=True)
    target = tmp_path / "victim"
    target.mkdir()
    if kind == "directory_link":
        path.parent.symlink_to(target)
    else:
        path.parent.mkdir()
        if kind == "file_link":
            (target / "keep").write_text("untouched")
            path.symlink_to(target / "keep")
        elif kind == "fifo":
            os.mkfifo(path)
        else:
            path.write_bytes(b"#" * (MAX_REQUEST + 1))
    with pytest.raises(AgentError):
        agent.client.seed_config("bazarr", {"api_key": "ab" * 16})
    assert sorted(p.name for p in target.iterdir()) == (["keep"] if kind == "file_link" else [])
    if kind == "file_link":
        assert (target / "keep").read_text() == "untouched"
    assert agent.runner.stops == []


def test_bazarr_seed_private_idempotent_and_redacted(agent, roots, caplog):
    torrent_doc(agent, roots)
    path = roots.config / "config/bazarr/config/config.yaml"
    value = "ab" * 16
    with caplog.at_level(logging.DEBUG):
        assert agent.client.seed_config("bazarr", {"api_key": value}) is True
        assert yaml.safe_load(path.read_text())["auth"]["apikey"] == value
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert any(Path(p).name.startswith(".config.yaml.") and
                   (uid, gid) == (roots.puid, roots.pgid)
                   for p, uid, gid in agent.chowns.calls)
        inode = path.stat().st_ino
        assert agent.client.seed_config("bazarr", {"api_key": value}) is False
        assert path.stat().st_ino == inode
        with pytest.raises(AgentError) as error:
            agent.client.seed_config("bazarr", {"api_key": value + "#"})
    assert agent.runner.stops == ["bazarr"]
    assert agent.runner.starts == ["bazarr"]
    assert value not in caplog.text and value not in str(error.value)
