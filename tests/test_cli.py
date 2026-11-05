import io
import json
import logging
import os
import shutil
import stat
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path, PurePosixPath

import pytest
import yaml
from conftest import make_setup, random_secret
from wiring import (
    test_arr,
    test_bazarr,
    test_jellyfin,
    test_plex,
    test_prowlarr,
    test_qbittorrent,
    test_recyclarr,
    test_sabnzbd,
)

from orama import cli
from orama.__main__ import make_server
from orama.catalog import CATALOG, CORE, UGOS_RESERVED_PORTS
from orama.layout import app_config_dir, data_folders
from orama.secrets import PLEX_CLIENT_ID_NAME, VPN_KEY_NAME, SecretStore, ensure_app_keys
from orama.setup_model import DesiredSetup, IndexerChoice, VpnConfig, enabled_apps
from orama.wiring.http import Http
from orama_agent.runner import FakeRunner
from orama_agent.server import AgentServer
from orama_agent.validate import Roots

AWKWARD = "da ta 'q\"#&Ωμέγα"


class Chowns:
    """Records fchown calls by inode instead of changing owners."""

    def __init__(self):
        self.inodes = {}

    def __call__(self, fd, uid, gid):
        self.inodes[os.fstat(fd).st_ino] = (uid, gid)

    def owner(self, path):
        """The recorded owner of what now sits at ``path`` (root if never chowned)."""
        return self.inodes.get(os.lstat(path).st_ino, (0, 0))


def bootstrap_args(config_root, data_root, taken="80,443", **kw):
    values = {
        "--platform": "ubuntu",
        "--data-root": str(data_root),
        "--config-root": str(config_root),
        "--puid": "1000",
        "--pgid": "1001",
        "--docker-gid": "998",
        "--render-gid": "none",
        "--nvidia": "0",
        "--taken-ports": taken,
        "--orama-image": "orama:test",
        "--agent-image": "orama-agent:test",
    }
    values.update(kw)
    argv = ["bootstrap"]
    for k, v in values.items():
        argv += [k, v]
    return argv


@pytest.fixture
def roots(tmp_path):
    base = Path(os.path.realpath(tmp_path))
    return base / "data", base / "cfg"


def run_bootstrap(capsys, argv, chowns=None):
    chowns = chowns if chowns is not None else Chowns()
    code = cli.main(argv, chown=chowns)
    out = capsys.readouterr()
    return code, out, chowns


# ---------------------------------------------------------------- bootstrap

def test_bootstrap_writes_core_compose_and_draft(capsys, roots):
    data, cfg = roots
    code, out, _chowns = run_bootstrap(capsys, bootstrap_args(cfg, data))
    assert code == 0

    core = yaml.safe_load((cfg / "core.compose.yaml").read_text())
    orama = core["services"]["orama"]
    assert orama["image"] == "orama:test"
    assert core["services"]["orama-agent"]["image"] == "orama-agent:test"
    assert core["services"]["orama-agent"]["group_add"] == ["998"]
    assert orama["user"] == "1000:1001"

    draft = DesiredSetup.load(cfg / "orama" / "setup.draft.json")
    assert draft.media_servers == {"plex"}
    assert draft.download_methods == {"usenet"}
    assert draft.vpn is None
    assert draft.data_root == PurePosixPath(data)
    assert draft.config_root == PurePosixPath(cfg)
    assert (draft.puid, draft.pgid, draft.render_gid, draft.nvidia) == (1000, 1001, None, False)
    assert draft.timezone == "Etc/UTC"
    wanted = {a for a, s in CATALOG.items() if s.default_host_port} | {"orama"}
    assert set(draft.ports) == wanted
    assert len(set(draft.ports.values())) == len(draft.ports)
    assert orama["ports"] == [f"{draft.ports['orama']}:{CORE['orama'].container_port}"]
    assert out.out.strip() == str(draft.ports["orama"])


def test_bootstrap_modes_and_owners(capsys, roots):
    data, cfg = roots
    code, _, chowns = run_bootstrap(capsys, bootstrap_args(cfg, data))
    assert code == 0
    mode = lambda p: stat.S_IMODE(os.lstat(p).st_mode)
    assert mode(cfg) == 0o755
    assert mode(cfg / "config") == 0o755
    assert mode(cfg / "core.compose.yaml") == 0o644
    assert mode(cfg / "orama") == 0o700
    assert mode(cfg / "orama" / "setup.draft.json") == 0o600
    assert not (cfg / "app-env").exists()
    assert chowns.owner(cfg / "orama") == (1000, 1001)
    assert chowns.owner(cfg / "orama" / "setup.draft.json") == (1000, 1001)
    for root_owned in (cfg, cfg / "config", cfg / "core.compose.yaml"):
        assert chowns.owner(root_owned) == (0, 0)
    assert not list((cfg / "orama").glob(".*tmp")) and not list(cfg.glob(".*tmp"))


def test_bootstrap_rerun_keeps_earlier_ports(capsys, roots):
    data, cfg = roots
    run_bootstrap(capsys, bootstrap_args(cfg, data, taken="80,443"))
    first = DesiredSetup.load(cfg / "orama" / "setup.draft.json").ports
    # Now our own containers hold the ports, plus something new took 8765+.
    taken = ",".join(str(p) for p in [*first.values(), 8765, 8766, 8767, 80])
    code, out, _ = run_bootstrap(capsys, bootstrap_args(cfg, data, taken=taken))
    assert code == 0
    second = DesiredSetup.load(cfg / "orama" / "setup.draft.json").ports
    assert second == first
    assert out.out.strip() == str(first["orama"])


def test_bootstrap_taken_default_port_moves(capsys, roots):
    data, cfg = roots
    code, out, _ = run_bootstrap(capsys, bootstrap_args(cfg, data, taken="8765,32400"))
    assert code == 0
    ports = DesiredSetup.load(cfg / "orama" / "setup.draft.json").ports
    assert ports["orama"] not in (8765, 32400)
    assert ports["plex"] != 32400
    assert out.out.strip() == str(ports["orama"])


def test_bootstrap_keeps_existing_setup_and_fills_missing_ports(capsys, roots):
    data, cfg = roots
    (cfg / "orama").mkdir(parents=True)
    existing = make_setup(
        data_root=str(data), config_root=str(cfg), media_servers={"jellyfin"},
        download_methods={"torrent"}, ports={"jellyfin": 18096},
    )
    existing.save(cfg / "orama" / "setup.json")
    code, _out, _ = run_bootstrap(capsys, bootstrap_args(cfg, data))
    assert code == 0
    assert not (cfg / "orama" / "setup.draft.json").exists()
    kept = DesiredSetup.load(cfg / "orama" / "setup.json")
    assert kept.media_servers == {"jellyfin"}
    assert kept.download_methods == {"torrent"}
    assert kept.ports["jellyfin"] == 18096
    assert "orama" in kept.ports and "sonarr" in kept.ports


def test_bootstrap_keeps_existing_draft(capsys, roots):
    data, cfg = roots
    run_bootstrap(capsys, bootstrap_args(cfg, data))
    path = cfg / "orama" / "setup.draft.json"
    draft = DesiredSetup.load(path)
    path.write_text(json.dumps({**draft.to_json_dict(), "timezone": "Europe/Athens"}))
    code, _, _ = run_bootstrap(capsys, bootstrap_args(cfg, data, **{"--puid": "2000"}))
    assert code == 0
    again = DesiredSetup.load(path)
    assert again.timezone == "Europe/Athens"
    assert again.puid == 1000


def test_bootstrap_awkward_data_root_survives(capsys, tmp_path):
    base = Path(os.path.realpath(tmp_path))
    data, cfg = base / AWKWARD / "data", base / f"cfg {AWKWARD}"
    code, _, _ = run_bootstrap(capsys, bootstrap_args(cfg, data))
    assert code == 0
    core = yaml.safe_load((cfg / "core.compose.yaml").read_text(encoding="utf-8"))
    env = core["services"]["orama-agent"]["environment"]
    assert env["ORAMA_DATA_ROOT"] == str(data)
    assert env["ORAMA_CONFIG_ROOT"] == str(cfg)
    draft = DesiredSetup.load(cfg / "orama" / "setup.draft.json")
    assert str(draft.data_root) == str(data)
    assert str(draft.config_root) == str(cfg)


def test_bootstrap_canonicalises_roots(capsys, tmp_path):
    base = Path(os.path.realpath(tmp_path))
    (base / "real-data").mkdir()
    (base / "data-link").symlink_to(base / "real-data")
    cfg = base / "cfg"
    code, _, _ = run_bootstrap(capsys, bootstrap_args(cfg, base / "x" / ".." / "data-link"))
    assert code == 0
    draft = DesiredSetup.load(cfg / "orama" / "setup.draft.json")
    assert draft.data_root == PurePosixPath(base / "real-data")


def test_bootstrap_render_gid_and_nvidia(capsys, roots):
    data, cfg = roots
    argv = bootstrap_args(cfg, data, **{"--render-gid": "105", "--nvidia": "1"})
    assert run_bootstrap(capsys, argv)[0] == 0
    draft = DesiredSetup.load(cfg / "orama" / "setup.draft.json")
    assert (draft.render_gid, draft.nvidia) == (105, True)


@pytest.mark.parametrize("name", ["orama", "core.compose.yaml", "config"])
def test_bootstrap_refuses_symlinks(capsys, roots, name):
    data, cfg = roots
    cfg.mkdir()
    target = cfg.parent / "elsewhere"
    target.mkdir()
    (cfg / name).symlink_to(target)
    code, out, _ = run_bootstrap(capsys, bootstrap_args(cfg, data))
    assert code == 1
    assert "link" in out.err
    assert list(target.iterdir()) == []


@pytest.mark.parametrize("name", ["setup.json", "setup.draft.json"])
def test_bootstrap_refuses_symlinked_setup_files(capsys, roots, name):
    data, cfg = roots
    (cfg / "orama").mkdir(parents=True)
    target = cfg.parent / "target.json"
    target.write_text("untouched")
    (cfg / "orama" / name).symlink_to(target)
    code, out, _ = run_bootstrap(capsys, bootstrap_args(cfg, data))
    assert code == 1
    assert out.err.startswith("✗ ") and "link" in out.err
    assert target.read_text() == "untouched"
    assert os.path.islink(cfg / "orama" / name)
    assert not (cfg / "core.compose.yaml").exists()


@pytest.mark.parametrize("bad", sorted({1023, 0, 70000} | UGOS_RESERVED_PORTS))
def test_bootstrap_reassigns_bad_kept_orama_port(capsys, roots, bad):
    data, cfg = roots
    (cfg / "orama").mkdir(parents=True)
    make_setup(data_root=str(data), config_root=str(cfg), ports={"orama": bad}).save(
        cfg / "orama" / "setup.json"
    )
    code, out, _ = run_bootstrap(capsys, bootstrap_args(cfg, data, taken=""))
    assert code == 0
    port = DesiredSetup.load(cfg / "orama" / "setup.json").ports["orama"]
    assert port == 8765
    assert out.out.strip() == "8765"
    core = yaml.safe_load((cfg / "core.compose.yaml").read_text())
    assert core["services"]["orama"]["ports"] == ["8765:8765"]


def test_bootstrap_keeps_good_kept_orama_port(capsys, roots):
    data, cfg = roots
    (cfg / "orama").mkdir(parents=True)
    make_setup(data_root=str(data), config_root=str(cfg), ports={"orama": 18080}).save(
        cfg / "orama" / "setup.json"
    )
    code, out, _ = run_bootstrap(capsys, bootstrap_args(cfg, data, taken="18080"))
    assert code == 0
    assert out.out.strip() == "18080"


@pytest.mark.parametrize(
    "override",
    [
        {"--platform": "fedora"},
        {"--render-gid": "abc"},
        {"--nvidia": "2"},
        {"--taken-ports": "80,http"},
        {"--puid": "-1"},
    ],
)
def test_bootstrap_rejects_bad_arguments(capsys, roots, override):
    data, cfg = roots
    with pytest.raises(SystemExit) as exc:
        cli.main(bootstrap_args(cfg, data, **override), chown=Chowns())
    assert exc.value.code != 0
    assert not cfg.exists()


def test_bootstrap_rejects_nested_roots(capsys, roots):
    _data, cfg = roots
    code, out, _ = run_bootstrap(capsys, bootstrap_args(cfg, cfg / "data"))
    assert code == 1
    assert out.err.startswith("✗")


@pytest.mark.parametrize("data_rel,config_rel,words", [
    ("cfg/data", "cfg", "The data folder and the Orama folder"),
    ("da:ta", "cfg", "The data folder"),
    ("data", "c:fg", "The Orama folder"),
])
def test_bootstrap_invalid_roots_are_plain(capsys, tmp_path, data_rel, config_rel, words):
    base = Path(os.path.realpath(tmp_path))
    code, out, _ = run_bootstrap(capsys, bootstrap_args(base / config_rel, base / data_rel))
    assert code == 1
    assert out.err.startswith("✗ ") and len(out.err.strip().splitlines()) == 1
    assert words in out.err
    for jargon in ("data_root", "config_root", "Value error", "validation error"):
        assert jargon not in out.err


def test_bootstrap_unexpected_os_error_is_plain(capsys, tmp_path):
    base = Path(os.path.realpath(tmp_path))
    (base / "file").write_text("")
    code, out, _ = run_bootstrap(capsys, bootstrap_args(base / "file" / "cfg", base / "data"))
    assert code == 1
    assert out.err.startswith("✗ ") and len(out.err.strip().splitlines()) == 1
    assert "Traceback" not in out.err


def _state_with(cfg, name, make):
    (cfg / "orama").mkdir(parents=True)
    make(cfg / "orama" / name)


@pytest.mark.parametrize("name", ["setup.json", "setup.draft.json"])
def test_bootstrap_saved_setup_fifo_does_not_hang(capsys, roots, name):
    data, cfg = roots
    _state_with(cfg, name, os.mkfifo)
    result = {}
    thread = threading.Thread(
        target=lambda: result.update(r=run_bootstrap(capsys, bootstrap_args(cfg, data))),
        daemon=True,
    )
    thread.start()
    thread.join(10)
    if thread.is_alive():  # unblock the hung open so the test run can end
        os.close(os.open(cfg / "orama" / name, os.O_WRONLY | os.O_NONBLOCK))
        pytest.fail("bootstrap blocked on a FIFO")
    code, out, _ = result["r"]
    assert code == 1 and out.err.startswith("✗ ") and "not a regular file" in out.err
    assert not (cfg / "core.compose.yaml").exists()


def test_bootstrap_saved_setup_directory_is_plain(capsys, roots):
    data, cfg = roots
    _state_with(cfg, "setup.json", os.mkdir)
    code, out, _ = run_bootstrap(capsys, bootstrap_args(cfg, data))
    assert code == 1 and out.err.startswith("✗ ") and "not a regular file" in out.err
    assert "Traceback" not in out.err


def test_bootstrap_saved_setup_oversized_is_refused(capsys, roots):
    data, cfg = roots
    _state_with(cfg, "setup.json", lambda p: p.write_bytes(b" " * (1024 * 1024 + 1)))
    code, out, _ = run_bootstrap(capsys, bootstrap_args(cfg, data))
    assert code == 1 and out.err.startswith("✗ ") and "too large" in out.err
    assert not (cfg / "core.compose.yaml").exists()


def test_bootstrap_empty_taken_ports(capsys, roots):
    data, cfg = roots
    code, out, _ = run_bootstrap(capsys, bootstrap_args(cfg, data, taken=""))
    assert code == 0
    assert out.out.strip() == "8765"


# ---------------------------------------------------------------- apply

PLEX_WAITING = (
    "… Plex: skipped, Plex is waiting for sign-in — run `python -m orama.cli plex-sign-in`"
)

class Fleet(Http):
    """All five download apps behind one Http: each request goes to that host's fake.

    The wiring tests' fakes check one fixed key each; this points them at the keys the
    store really holds (apply creates random ones) before every request.
    """

    def __init__(self, monkeypatch, store_path):
        self.mp = monkeypatch
        self.store_path = store_path
        self.sab = test_sabnzbd.FakeSab()
        self.qbt = test_qbittorrent.FakeQbt()
        self.sonarr = test_arr.FakeArr("sonarr")
        self.radarr = test_arr.FakeArr("radarr")
        self.prowlarr = test_prowlarr.FakeProwlarr()
        self.bazarr = test_bazarr.FakeBazarr()
        self.plex = test_plex.FakePlex(random_secret("tok", 10))
        self.jellyfin = test_jellyfin.FakeJellyfin(random_secret("pw"))
        # Recyclarr's work: its own fake *arr quality profiles and a stand-in for the Agent call.
        self.profiles = {app: test_recyclarr.FakeArr(app) for app in test_recyclarr.PORTS}
        self.profile_http = test_recyclarr.FakeHttp(self.profiles)
        self.recyclarr = test_recyclarr.FakeAgent(self.profiles)
        self.requests = 0

    def request(self, method, url, **kw):
        self.requests += 1
        store = SecretStore(self.store_path)
        host = url.split("//", 1)[1].split(":", 1)[0].split("/", 1)[0]
        mp = self.mp
        if host == "sabnzbd":
            mp.setattr(test_sabnzbd, "KEY", store.get("apikey:sabnzbd"))
            return self.sab.http.request(method, url, **kw)
        if host in ("qbittorrent", "gluetun"):
            mp.setattr(test_qbittorrent, "PASSWORD", store.get("password:qbittorrent"))
            return self.qbt.http.request(method, url, **kw)
        if host in ("sonarr", "radarr"):
            if "/customformat" in url or "/qualityprofile" in url:
                mp.setitem(test_recyclarr.KEYS, host, store.get(f"apikey:{host}"))
                return self.profile_http.request(method, url, **kw)
            mp.setattr(test_arr, "APP_KEY", store.get(f"apikey:{host}"))
            return getattr(self, host).http.request(method, url, **kw)
        if host == "bazarr":
            mp.setattr(test_bazarr, "KEY", store.get("apikey:bazarr"))
            return self.bazarr.http.request(method, url, **kw)
        if host in ("plex", "plex.tv"):
            return self.plex.http.request(method, url, **kw)
        if host == "jellyfin":
            if not self.jellyfin.completed:
                self.jellyfin.password = store.get("password:jellyfin")
            return self.jellyfin.http.request(method, url, **kw)
        assert host == "prowlarr", url
        mp.setattr(test_prowlarr, "PROWLARR_KEY", store.get("apikey:prowlarr"))
        return self.prowlarr.http.request(method, url, **kw)


@pytest.fixture
def agent(roots, monkeypatch):
    data, cfg = roots
    data.mkdir()
    cfg.mkdir()
    agent_roots = Roots(data=data, config=cfg, puid=1000, pgid=1001)
    sock_dir = tempfile.mkdtemp(prefix="oc", dir="/tmp")
    socket_path = os.path.join(sock_dir, "agent.sock")
    runner = FakeRunner()
    server = AgentServer(agent_roots, runner, socket_path, chown=lambda *a, **k: None,
                         read_timeout=5)
    thread = threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True)
    thread.start()
    monkeypatch.setenv("ORAMA_AGENT_SOCKET", socket_path)
    runner.fleet = Fleet(monkeypatch, cfg / "orama" / "secrets.bin")
    monkeypatch.setattr(cli, "Http", lambda: runner.fleet)
    monkeypatch.setattr(
        cli.AgentClient, "recyclarr_sync",
        lambda _self, apps, force=False: runner.fleet.recyclarr.recyclarr_sync(apps, force),
    )
    try:
        yield runner
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
        shutil.rmtree(sock_dir, ignore_errors=True)


def write_setup(cfg, data, **kw):
    (cfg / "orama").mkdir(mode=0o700, exist_ok=True)
    values = {"data_root": str(data), "config_root": str(cfg), "puid": 1000, "pgid": 1001}
    values.update(kw)
    setup = make_setup(**values)
    path = cfg / "orama" / "setup.json"
    setup.save(path)
    return path


def store_values(cfg):
    return SecretStore(cfg / "orama" / "secrets.bin").values()


def credential_values(cfg):
    """The keys, passwords and hashes in the store, without wiring's bookkeeping markers."""
    store = SecretStore(cfg / "orama" / "secrets.bin")
    prefixes = ("apikey:", "nzbkey:", "password:", "qbhash:", "indexer:", "vpn:")
    return [store.get(n) for n in store.names() if n.startswith(prefixes)
            and not n.endswith(":sent")]


def test_apply_creates_folders_sends_compose_prints_checklist(capsys, caplog, roots, agent):
    data, cfg = roots
    caplog.set_level(logging.DEBUG)
    path = write_setup(cfg, data)
    code = cli.main(["apply", "--setup", str(path)])
    out = capsys.readouterr()
    assert code == 0, out.err

    setup = DesiredSetup.load(path)
    for folder in data_folders(PurePosixPath(data)):
        assert Path(folder).is_dir()
    for app in enabled_apps(setup):
        assert Path(app_config_dir(PurePosixPath(cfg), app)).is_dir()
    assert agent.up_calls == 1
    sent = yaml.safe_load((cfg / "compose.yaml").read_text())
    assert sorted(sent["services"]) == sorted(enabled_apps(setup))

    # Ports were filled in and saved.
    assert {a for a in enabled_apps(setup) if CATALOG[a].default_host_port} <= set(setup.ports)
    assert (cfg / "app-env" / "sonarr.env").exists()

    lines = out.out.splitlines()
    assert lines[0] == "✓ Folders ready"
    assert {f"✓ {a} started" for a in enabled_apps(setup)} <= set(lines)

    secrets = credential_values(cfg)
    assert len(secrets) == 6  # includes Bazarr API key
    for value in secrets:
        assert value not in out.out and value not in out.err and value not in caplog.text


def test_apply_second_run_reports_nothing_to_change(capsys, roots, agent):
    data, cfg = roots
    path = write_setup(cfg, data)
    assert cli.main(["apply", "--setup", str(path)]) == 0
    capsys.readouterr()
    keys = store_values(cfg)
    ports = DesiredSetup.load(path).ports
    assert cli.main(["apply", "--setup", str(path)]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out == ["✓ Folders ready", PLEX_WAITING]
    assert store_values(cfg) == keys
    assert DesiredSetup.load(path).ports == ports


def test_apply_keeps_existing_ports(capsys, roots, agent):
    data, cfg = roots
    path = write_setup(cfg, data, ports={"plex": 32401, "orama": 8800})
    assert cli.main(["apply", "--setup", str(path)]) == 0
    ports = DesiredSetup.load(path).ports
    assert ports["plex"] == 32401 and ports["orama"] == 8800
    assert len(set(ports.values())) == len(ports)


def test_apply_refusal_exits_nonzero_with_agent_message(capsys, caplog, roots, agent):
    data, cfg = roots
    other = data.parent / "other-data"
    path = write_setup(cfg, other)
    code = cli.main(["apply", "--setup", str(path)])
    out = capsys.readouterr()
    assert code == 1
    assert out.err.startswith("✗ The folder ")
    assert "is not one Orama may create." in out.err
    assert agent.up_calls == 0
    for value in store_values(cfg):
        assert value not in out.out and value not in out.err and value not in caplog.text


def test_apply_runner_failure_exits_nonzero(capsys, roots, agent):
    data, cfg = roots
    path = write_setup(cfg, data)
    agent.fail_next_up = True
    code = cli.main(["apply", "--setup", str(path)])
    out = capsys.readouterr()
    assert code == 1
    assert out.out.splitlines() == ["✓ Folders ready"]
    assert out.err.startswith("✗ ")
    assert "Starting the apps failed. To see why, run: sudo docker logs" in out.err


def test_apply_agent_unreachable(capsys, roots, monkeypatch):
    data, cfg = roots
    cfg.mkdir()
    path = write_setup(cfg, data)
    monkeypatch.setenv("ORAMA_AGENT_SOCKET", str(cfg / "nope.sock"))
    code = cli.main(["apply", "--setup", str(path)])
    out = capsys.readouterr()
    assert code == 1
    assert out.err.startswith("✗ Cannot reach the Agent")


def assert_clean_failure(out, caplog, cfg, starts):
    assert out.err.startswith(f"✗ {starts}"), out.err
    assert "Traceback" not in out.err
    assert len(out.err.strip().splitlines()) == 1
    try:
        values = store_values(cfg)
    except (OSError, ValueError):
        values = []
    for value in values:
        assert value not in out.out and value not in out.err and value not in caplog.text


def test_apply_unreadable_secret_store(capsys, caplog, roots, agent):
    data, cfg = roots
    path = write_setup(cfg, data)
    (cfg / "orama" / "secrets.bin").mkdir()
    code = cli.main(["apply", "--setup", str(path)])
    out = capsys.readouterr()
    assert code == 1
    assert_clean_failure(out, caplog, cfg, "Cannot use Orama's files")
    assert agent.up_calls == 0


def test_apply_undecryptable_secret_store(capsys, caplog, roots, agent):
    data, cfg = roots
    path = write_setup(cfg, data)
    store = SecretStore(cfg / "orama" / "secrets.bin")
    stored = random_secret()
    store.set("apikey:sonarr", stored)
    (cfg / "orama" / "secrets.key").write_bytes(b"A" * 43 + b"=")
    code = cli.main(["apply", "--setup", str(path)])
    out = capsys.readouterr()
    assert code == 1
    assert_clean_failure(out, caplog, cfg, "Cannot apply: cannot decrypt")
    assert stored not in out.err + out.out + caplog.text


def test_apply_with_prowlarr_via_vpn_sends_proxy_password(capsys, roots, agent):
    data, cfg = roots
    path = write_setup(cfg, data, download_methods={"torrent"},
                       vpn=VpnConfig(route_prowlarr=True))
    store = SecretStore(cfg / "orama" / "secrets.bin")
    store.set("vpn:wireguard_private_key", "k" * 8)
    code = cli.main(["apply", "--setup", str(path)])
    out = capsys.readouterr()
    assert code == 0, out.err
    password = store.get("password:gluetun-proxy")
    assert password
    assert f"HTTPPROXY_PASSWORD='{password}'" in (cfg / "app-env" / "gluetun.env").read_text()
    compose_text = (cfg / "compose.yaml").read_text()
    assert password not in compose_text and password not in out.out + out.err
    env = yaml.safe_load(compose_text)["services"]["gluetun"]["environment"]
    assert env["HTTPPROXY"] == "on" and env["HTTPPROXY_USER"] == "orama"


def test_apply_missing_proxy_password_is_not_called_a_vpn_key(capsys, caplog, roots, agent,
                                                              monkeypatch):
    data, cfg = roots
    path = write_setup(cfg, data, download_methods={"torrent"},
                       vpn=VpnConfig(route_prowlarr=True))
    store = SecretStore(cfg / "orama" / "secrets.bin")
    ensure_app_keys(store, ["sonarr", "radarr", "prowlarr"])
    store.set("vpn:wireguard_private_key", "k" * 8)
    monkeypatch.setattr(cli, "ensure_app_keys", lambda store, apps, **kw: None)
    code = cli.main(["apply", "--setup", str(path)])
    out = capsys.readouterr()
    assert code == 1
    assert_clean_failure(out, caplog, cfg, "Orama could not find the key it keeps for ")
    assert "VPN key" not in out.err and "password:" not in out.err
    assert agent.up_calls == 0


def test_apply_missing_vpn_key(capsys, caplog, roots, agent):
    data, cfg = roots
    path = write_setup(cfg, data, download_methods={"torrent"}, vpn=VpnConfig())
    code = cli.main(["apply", "--setup", str(path)])
    out = capsys.readouterr()
    assert code == 1
    assert_clean_failure(out, caplog, cfg, "The VPN key has not been entered yet.")
    assert "turn the VPN off" in out.err
    assert "vpn:" not in out.err and "gluetun" not in out.err
    assert agent.up_calls == 0


def test_apply_missing_app_key_is_plain(capsys, caplog, roots, agent, monkeypatch):
    data, cfg = roots
    path = write_setup(cfg, data)
    monkeypatch.setattr(cli, "ensure_app_keys", lambda store, apps, **kw: None)
    code = cli.main(["apply", "--setup", str(path)])
    out = capsys.readouterr()
    assert code == 1
    assert_clean_failure(out, caplog, cfg, "Orama could not find the key it keeps for ")
    assert "apikey:" not in out.err and "missing secret" not in out.err
    assert "Run apply again" in out.err and "install" not in out.err.lower()
    assert agent.up_calls == 0


def test_apply_agent_socket_is_not_a_socket(capsys, caplog, roots, monkeypatch):
    data, cfg = roots
    cfg.mkdir()
    path = write_setup(cfg, data)
    (cfg / "plain-file").write_text("")
    monkeypatch.setenv("ORAMA_AGENT_SOCKET", str(cfg / "plain-file"))
    code = cli.main(["apply", "--setup", str(path)])
    out = capsys.readouterr()
    assert code == 1
    assert_clean_failure(out, caplog, cfg, "Cannot reach the Agent")


def test_apply_invalid_saved_setup_gives_the_plain_reasons(capsys, roots, agent):
    data, cfg = roots
    path = write_setup(cfg, data)
    geek = {"slug": "geek", "definition": "geek", "name": "Same",
            "fields": {}, "secret_fields": ["apikey"]}
    other = {**geek, "slug": "other"}
    saved = json.loads(path.read_text())
    saved["indexers"] = [geek, other]  # two indexers with the same name
    path.write_text(json.dumps(saved))
    code = cli.main(["apply", "--setup", str(path)])
    out = capsys.readouterr()
    assert code == 1
    assert "Indexer names must be unique." in out.err
    assert "cannot be read" not in out.err
    assert "Traceback" not in out.err
    assert agent.up_calls == 0


def test_apply_garbage_setup_still_says_cannot_be_read(capsys, roots, agent):
    data, cfg = roots
    path = write_setup(cfg, data)
    path.write_text("{ not json")
    assert cli.main(["apply", "--setup", str(path)]) == 1
    assert "cannot be read" in capsys.readouterr().err


def test_apply_missing_setup(capsys, tmp_path):
    code = cli.main(["apply", "--setup", str(tmp_path / "missing.json")])
    out = capsys.readouterr()
    assert code == 1
    assert out.err.startswith("✗ ")


def test_apply_wires_the_apps_and_prints_results(capsys, caplog, roots, agent):
    data, cfg = roots
    caplog.set_level(logging.DEBUG)
    path = write_setup(cfg, data)
    code = cli.main(["apply", "--setup", str(path)])
    out = capsys.readouterr()
    assert code == 0, out.err
    lines = out.out.splitlines()
    assert "✓ SABnzbd: login details set" in lines
    assert any(line.startswith("✓ SABnzbd: ") and "folders" in line for line in lines)
    assert any(line.startswith("✓ Sonarr: ") for line in lines)
    assert any(line.startswith("✓ Radarr: ") for line in lines)
    assert not [line for line in lines if line.startswith("✗")]
    assert lines.index("✓ Folders ready") < lines.index("✓ SABnzbd: login details set")
    assert lines.index("✓ sabnzbd started") < lines.index("✓ SABnzbd: login details set")
    assert agent.fleet.sab.misc["complete_dir"] == "/data/downloads/usenet"
    store = SecretStore(cfg / "orama" / "secrets.bin")
    seed = (cfg / "config" / "sabnzbd" / "sabnzbd.ini").read_text()
    assert f"api_key = {store.get('apikey:sabnzbd')}" in seed
    assert f"nzb_key = {store.get('nzbkey:sabnzbd')}" in seed
    for value in credential_values(cfg):
        assert value not in out.out and value not in out.err and value not in caplog.text


def test_apply_second_run_changes_nothing(capsys, roots, agent):
    data, cfg = roots
    path = write_setup(cfg, data)
    assert cli.main(["apply", "--setup", str(path)]) == 0
    capsys.readouterr()
    keys = store_values(cfg)
    assert cli.main(["apply", "--setup", str(path)]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out == ["✓ Folders ready", PLEX_WAITING]
    assert store_values(cfg) == keys
    assert agent.up_calls == 2 and agent.restarts == []


def test_apply_plex_pending_sign_in_skips_only_plex_steps(capsys, roots, agent):
    data, cfg = roots
    path = write_setup(cfg, data)  # Plex only, nobody has signed in
    assert cli.main(["apply", "--setup", str(path)]) == 0
    out = capsys.readouterr().out.splitlines()
    fleet = agent.fleet
    assert out.count(PLEX_WAITING) == 1  # said once, not once per skipped step
    assert not [line for line in out if line.startswith("✗")]
    assert fleet.bazarr.profiles and fleet.bazarr.settings["general"]["use_sonarr"] is True
    assert fleet.recyclarr.calls and fleet.profiles["sonarr"].profiles[-1]["name"] == "Orama"
    assert fleet.sonarr.notifications == {} and fleet.radarr.notifications == {}
    assert fleet.plex.sections == []  # libraries wait for the owner too


def test_apply_jellyfin_only_wires_it_and_tells_the_arrs(capsys, roots, agent):
    data, cfg = roots
    path = write_setup(cfg, data, media_servers={"jellyfin"})
    assert cli.main(["apply", "--setup", str(path)]) == 0
    first = capsys.readouterr().out
    assert "Plex" not in first and "✗" not in first
    store = SecretStore(cfg / "orama" / "secrets.bin")
    fleet = agent.fleet
    for arr in (fleet.sonarr, fleet.radarr):
        note = arr.notification("Jellyfin (Orama)")
        assert arr.field(note, "apiKey") == store.get("apikey:jellyfin")
        assert arr.notification("Plex (Orama)") is None
    assert fleet.jellyfin.folders
    assert store.get("apikey:jellyfin") not in first
    assert cli.main(["apply", "--setup", str(path)]) == 0
    assert capsys.readouterr().out.splitlines() == ["✓ Folders ready", "✓ Nothing to change"]


def test_apply_both_servers_then_deselecting_jellyfin_removes_only_its_notification(
    capsys, roots, agent
):
    data, cfg = roots
    path = write_setup(cfg, data, media_servers={"plex", "jellyfin"})
    store = SecretStore(cfg / "orama" / "secrets.bin")
    token = agent.fleet.plex.token
    store.set("token:plex", token)  # the owner signed in
    agent.fleet.plex.claimed, agent.fleet.plex.owner = True, "me"
    store.set(f"{test_plex.plex.CLAIMED_MARKER}{test_plex.MACHINE}", "1")
    store.set(PLEX_CLIENT_ID_NAME, test_plex.CLIENT_ID)
    assert cli.main(["apply", "--setup", str(path)]) == 0
    first = capsys.readouterr().out
    fleet = agent.fleet
    for arr in (fleet.sonarr, fleet.radarr):
        assert sorted(n["name"] for n in arr.notifications.values()) == [
            "Jellyfin (Orama)", "Plex (Orama)"]
        assert arr.field(arr.notification("Plex (Orama)"), "authToken") == token
    assert token not in first and store.get("apikey:jellyfin") not in first
    assert cli.main(["apply", "--setup", str(path)]) == 0
    assert capsys.readouterr().out.splitlines() == ["✓ Folders ready", "✓ Nothing to change"]
    path = write_setup(cfg, data, media_servers={"plex"}, ports=DesiredSetup.load(path).ports)
    assert cli.main(["apply", "--setup", str(path)]) == 0
    capsys.readouterr()
    for arr in (fleet.sonarr, fleet.radarr):
        assert [n["name"] for n in arr.notifications.values()] == ["Plex (Orama)"]


def test_apply_plex_failure_is_a_failure_but_a_waiting_plex_is_not(capsys, roots, agent):
    data, cfg = roots
    path = write_setup(cfg, data)
    agent.fleet.recyclarr.error = "Updating the quality profiles failed."
    code = cli.main(["apply", "--setup", str(path)])
    out = capsys.readouterr().out.splitlines()
    assert code == 1
    assert "✗ Recyclarr: Updating the quality profiles failed." in out
    assert PLEX_WAITING in out


def test_apply_failing_indexer_exits_1_but_finishes_the_rest(capsys, roots, agent):
    data, cfg = roots
    geek = IndexerChoice(slug="geek", definition="geek", name="NZBGeek",
                         fields={"baseUrl": "https://geek.example"}, secret_fields=("apikey",))
    path = write_setup(cfg, data, indexers=(geek,))
    indexer_key = random_secret()
    SecretStore(cfg / "orama" / "secrets.bin").set("indexer:geek:apikey", indexer_key)
    agent.fleet.prowlarr.test_status = 400
    code = cli.main(["apply", "--setup", str(path)])
    out = capsys.readouterr()
    assert code == 1
    failed = [line for line in out.out.splitlines() if line.startswith("✗")]
    assert len(failed) == 1 and failed[0].startswith("✗ Prowlarr: ")
    assert indexer_key not in out.out + out.err
    assert "✓ Sonarr: " in "\n".join(out.out.splitlines())  # wiring before it still ran
    assert "Traceback" not in out.err


def torrent_vpn_setup(cfg, data, **vpn):
    path = write_setup(cfg, data, download_methods={"usenet", "torrent"},
                       vpn=VpnConfig(**vpn))
    SecretStore(cfg / "orama" / "secrets.bin").set("vpn:wireguard_private_key", "k" * 8)
    return path


def test_apply_seeds_qbittorrent_and_restarts_gluetun_for_port_forwarding(
    capsys, caplog, roots, agent
):
    data, cfg = roots
    caplog.set_level(logging.DEBUG)
    path = torrent_vpn_setup(cfg, data)
    code = cli.main(["apply", "--setup", str(path)])
    out = capsys.readouterr()
    assert code == 0, out.err
    assert "✓ qBittorrent: login details set" in out.out.splitlines()
    assert agent.restarts == ["gluetun", "qbittorrent"]
    store = SecretStore(cfg / "orama" / "secrets.bin")
    conf = (cfg / "config" / "qbittorrent" / "qBittorrent" / "qBittorrent.conf").read_text()
    assert f'Password_PBKDF2="{store.get("qbhash:qbittorrent")}"' in conf
    for value in credential_values(cfg):
        assert value not in out.out and value not in out.err and value not in caplog.text
    assert store.get("password:qbittorrent") not in conf


def test_apply_retries_the_gluetun_restart_after_it_failed(capsys, roots, agent, monkeypatch):
    from orama.agent_client import AgentClient, AgentError

    data, cfg = roots
    path = torrent_vpn_setup(cfg, data)
    real = AgentClient.restart
    broken = {"on": True}

    def flaky(self, app):
        if broken["on"]:
            raise AgentError("The Agent could not restart the app.")
        real(self, app)

    monkeypatch.setattr(AgentClient, "restart", flaky)
    assert cli.main(["apply", "--setup", str(path)]) == 1  # the restart failed
    assert "could not restart" in capsys.readouterr().err
    assert agent.restarts == []
    broken["on"] = False
    assert cli.main(["apply", "--setup", str(path)]) == 0  # seed unchanged: still retried
    assert agent.restarts == ["gluetun", "qbittorrent"]
    capsys.readouterr()
    assert cli.main(["apply", "--setup", str(path)]) == 0  # flag cleared: not again
    assert agent.restarts == ["gluetun", "qbittorrent"]


def test_apply_redoes_both_restarts_when_the_second_one_failed(
    capsys, roots, agent, monkeypatch
):
    from orama.agent_client import AgentClient, AgentError

    data, cfg = roots
    path = torrent_vpn_setup(cfg, data)
    real = AgentClient.restart

    def second_fails(self, app):
        if app == "qbittorrent":
            raise AgentError("The Agent could not restart the app.")
        real(self, app)

    monkeypatch.setattr(AgentClient, "restart", second_fails)
    assert cli.main(["apply", "--setup", str(path)]) == 1
    monkeypatch.setattr(AgentClient, "restart", real)
    assert cli.main(["apply", "--setup", str(path)]) == 0
    assert agent.restarts == ["gluetun", "gluetun", "qbittorrent"]


def test_apply_without_port_forwarding_does_not_restart(capsys, roots, agent):
    data, cfg = roots
    path = torrent_vpn_setup(cfg, data, port_forwarding=False)
    assert cli.main(["apply", "--setup", str(path)]) == 0
    assert "✓ qBittorrent: login details set" in capsys.readouterr().out.splitlines()
    assert agent.restarts == []


def test_apply_qbittorrent_second_run_is_identical(capsys, roots, agent):
    data, cfg = roots
    path = torrent_vpn_setup(cfg, data)
    assert cli.main(["apply", "--setup", str(path)]) == 0
    store = SecretStore(cfg / "orama" / "secrets.bin")
    first = store.get("qbhash:qbittorrent")
    capsys.readouterr()
    restarts = list(agent.restarts)
    assert cli.main(["apply", "--setup", str(path)]) == 0
    assert capsys.readouterr().out.splitlines() == ["✓ Folders ready", PLEX_WAITING]
    assert store.get("qbhash:qbittorrent") == first
    assert agent.restarts == restarts


def test_apply_new_qbittorrent_password_gets_a_new_hash(capsys, roots, agent):
    data, cfg = roots
    path = torrent_vpn_setup(cfg, data)
    assert cli.main(["apply", "--setup", str(path)]) == 0
    store = SecretStore(cfg / "orama" / "secrets.bin")
    first = store.get("qbhash:qbittorrent")
    store.set("password:qbittorrent", random_secret())
    capsys.readouterr()
    assert cli.main(["apply", "--setup", str(path)]) == 0
    assert store.get("qbhash:qbittorrent") != first
    assert "✓ qBittorrent: login details set" in capsys.readouterr().out.splitlines()


def test_apply_seed_refusal_is_plain(capsys, caplog, roots, agent, monkeypatch):
    data, cfg = roots
    path = write_setup(cfg, data)
    from orama.agent_client import AgentClient, AgentError

    def refuse(self, app, values):
        raise AgentError("The Agent refused to set the login details.")

    monkeypatch.setattr(AgentClient, "seed_config", refuse)
    code = cli.main(["apply", "--setup", str(path)])
    out = capsys.readouterr()
    assert code == 1
    assert out.err.strip() == "✗ The Agent refused to set the login details."


# ---------------------------------------------------------------- set-secret

class FakeStdin(io.StringIO):
    def __init__(self, text, tty=False):
        super().__init__(text)
        self.tty = tty

    def isatty(self):
        return self.tty


def _secret_value():
    # Built at runtime so no key-shaped literal sits in the repo.
    return "pw &=%+ #Ωμέγα-" + os.urandom(6).hex()


def run_set_secret(capsys, monkeypatch, cfg, argv, stdin):
    monkeypatch.setattr(sys, "stdin", stdin)
    code = cli.main(["set-secret", *argv, "--setup", str(cfg / "orama" / "setup.json")])
    return code, capsys.readouterr()


@pytest.mark.parametrize(
    "name",
    [
        "usenet:main-1:password",
        "indexer:geek:apiKey",
        "indexer:own:extra.field_2",
    ],
)
def test_set_secret_stores_stdin_value(capsys, caplog, monkeypatch, roots, name):
    data, cfg = roots
    cfg.mkdir()
    write_setup(cfg, data)
    caplog.set_level(logging.DEBUG)
    value = _secret_value()
    code, out = run_set_secret(capsys, monkeypatch, cfg, [name], FakeStdin(value + "\n"))
    assert code == 0, out.err
    assert out.out == f"✓ Saved {name}\n"
    assert value not in out.out + out.err + caplog.text
    # The same store apply reads.
    assert SecretStore(cfg / "orama" / "secrets.bin").get(name) == value


def test_set_secret_uses_the_vpn_name_apply_reads(capsys, monkeypatch, roots):
    data, cfg = roots
    cfg.mkdir()
    write_setup(cfg, data)
    code, _ = run_set_secret(capsys, monkeypatch, cfg, [VPN_KEY_NAME], FakeStdin("k" * 44))
    assert code == 0
    assert SecretStore(cfg / "orama" / "secrets.bin").get(VPN_KEY_NAME) == "k" * 44


def test_set_secret_accepts_a_real_vpn_key(capsys, monkeypatch, roots):
    import base64

    data, cfg = roots
    cfg.mkdir()
    write_setup(cfg, data)
    key = base64.b64encode(os.urandom(32)).decode()  # a WireGuard key's shape
    code, out = run_set_secret(capsys, monkeypatch, cfg, [VPN_KEY_NAME], FakeStdin(key + "\n"))
    assert code == 0, out.err
    assert SecretStore(cfg / "orama" / "secrets.bin").get(VPN_KEY_NAME) == key


@pytest.mark.parametrize("bad", ["\\", "$", "#", " ", '"', "'", "`", "\t", "é", "Ω"])
def test_set_secret_refuses_a_vpn_key_outside_the_allowlist(capsys, caplog, monkeypatch, roots,
                                                            bad):
    data, cfg = roots
    cfg.mkdir()
    write_setup(cfg, data)
    marker = os.urandom(6).hex()
    code, out = run_set_secret(capsys, monkeypatch, cfg, [VPN_KEY_NAME],
                               FakeStdin(marker + bad + marker + "\n"))
    assert code == 1
    assert out.err.startswith("✗ ") and len(out.err.strip().splitlines()) == 1
    assert "The VPN key should look like the one your VPN provider gave you" in out.err
    assert marker not in out.err + out.out + caplog.text
    assert not (cfg / "orama" / "secrets.bin").exists()


def test_set_secret_replaces_and_keeps_others(capsys, monkeypatch, roots):
    data, cfg = roots
    cfg.mkdir()
    write_setup(cfg, data)
    store = SecretStore(cfg / "orama" / "secrets.bin")
    kept = random_secret()
    store.set("apikey:sonarr", kept)
    store.set("usenet:main:password", random_secret())
    code, _ = run_set_secret(capsys, monkeypatch, cfg, ["usenet:main:password"],
                             FakeStdin("new-value\r\n"))
    assert code == 0
    assert store.get("usenet:main:password") == "new-value"
    assert store.get("apikey:sonarr") == kept


def test_set_secret_tty_reads_without_echo(capsys, monkeypatch, roots):
    data, cfg = roots
    cfg.mkdir()
    write_setup(cfg, data)
    value = _secret_value()
    prompts = []

    def fake_getpass(prompt="Password: ", stream=None):
        prompts.append(prompt)
        return value

    monkeypatch.setattr(cli.getpass, "getpass", fake_getpass)
    code, out = run_set_secret(capsys, monkeypatch, cfg, ["indexer:geek:apiKey"],
                               FakeStdin("", tty=True))
    assert code == 0, out.err
    assert len(prompts) == 1 and value not in prompts[0]
    assert value not in out.out + out.err
    assert SecretStore(cfg / "orama" / "secrets.bin").get("indexer:geek:apiKey") == value


@pytest.mark.parametrize(
    "name",
    [
        "vpn:other",
        "apikey:sonarr",
        "usenet:Main:password",
        "usenet:main:username",
        "indexer:geek:api key",
        "indexer::apiKey",
        "a-secret-typed-by-mistake",
        "vpn:wireguard_private_key\n",
        "usenet:" + "a" * 41 + ":password",
        "indexer:" + "a" * 41 + ":apiKey",
        "indexer:geek:" + "f" * 65,
    ],
)
def test_set_secret_refuses_bad_name_plainly(capsys, caplog, monkeypatch, roots, name):
    data, cfg = roots
    cfg.mkdir()
    write_setup(cfg, data)
    code, out = run_set_secret(capsys, monkeypatch, cfg, [name], FakeStdin("value\n"))
    assert code == 1
    assert out.err.startswith("✗ ") and len(out.err.strip().splitlines()) == 1
    assert "secret name" in out.err
    assert name not in out.err + out.out + caplog.text  # it may be a mistyped secret
    assert not (cfg / "orama" / "secrets.bin").exists()


@pytest.mark.parametrize(
    "text,words",
    [
        ("", "No value"),
        ("\n", "No value"),
        ("one\ntwo\n", "one line"),
        ("bad\x00nul\n", "one line"),
        ("x" * 5000, "too long"),
    ],
)
def test_set_secret_refuses_bad_value_plainly(capsys, caplog, monkeypatch, roots, text, words):
    data, cfg = roots
    cfg.mkdir()
    write_setup(cfg, data)
    code, out = run_set_secret(capsys, monkeypatch, cfg, ["indexer:geek:apiKey"],
                               FakeStdin(text))
    assert code == 1
    assert out.err.startswith("✗ ") and words in out.err, out.err
    for part in ("two", "nul", "x" * 20):
        assert part not in out.err + caplog.text
    assert not (cfg / "orama" / "secrets.bin").exists()


def test_set_secret_value_on_command_line_is_refused_without_echo(capsys, monkeypatch, roots):
    data, cfg = roots
    cfg.mkdir()
    write_setup(cfg, data)
    value = _secret_value()
    code, out = run_set_secret(capsys, monkeypatch, cfg, ["indexer:geek:apiKey", value],
                               FakeStdin("ignored\n"))
    assert code == 1
    assert out.err.startswith("✗ ")
    assert value not in out.err + out.out
    assert not (cfg / "orama" / "secrets.bin").exists()


def test_set_secret_accepts_names_at_the_length_limits(capsys, monkeypatch, roots):
    data, cfg = roots
    cfg.mkdir()
    write_setup(cfg, data)
    for name in ("usenet:" + "a" * 40 + ":password", "indexer:" + "a" * 40 + ":" + "F" * 64):
        code, _ = run_set_secret(capsys, monkeypatch, cfg, [name], FakeStdin("value\n"))
        assert code == 0


def test_set_secret_uses_the_store_apply_uses(capsys, monkeypatch, roots, tmp_path):
    data, cfg = roots
    cfg.mkdir()
    write_setup(cfg, data)  # config_root is cfg: the store is cfg/orama/secrets.bin
    elsewhere = Path(os.path.realpath(tmp_path)) / "elsewhere"
    elsewhere.mkdir()
    moved = elsewhere / "setup.json"
    moved.write_bytes((cfg / "orama" / "setup.json").read_bytes())
    monkeypatch.setattr(sys, "stdin", FakeStdin("value\n"))
    assert cli.main(["set-secret", "usenet:main:password", "--setup", str(moved)]) == 0
    capsys.readouterr()
    assert SecretStore(cfg / "orama" / "secrets.bin").get("usenet:main:password") == "value"
    assert not (elsewhere / "secrets.bin").exists()


def test_set_secret_with_an_unreadable_setup_uses_the_folder_next_to_it(
    capsys, monkeypatch, roots
):
    data, cfg = roots
    cfg.mkdir()
    path = write_setup(cfg, data)
    path.write_text("{ not json")
    code, _ = run_set_secret(capsys, monkeypatch, cfg, ["usenet:main:password"],
                             FakeStdin("value\n"))
    assert code == 0
    assert SecretStore(cfg / "orama" / "secrets.bin").get("usenet:main:password") == "value"


def test_set_secret_missing_setup(capsys, monkeypatch, roots):
    _, cfg = roots
    code, out = run_set_secret(capsys, monkeypatch, cfg, ["indexer:geek:apiKey"],
                               FakeStdin("value\n"))
    assert code == 1
    assert out.err.startswith("✗ No setup found")
    assert not (cfg / "orama").exists()


def test_set_secret_undecryptable_store_is_plain(capsys, caplog, monkeypatch, roots):
    data, cfg = roots
    cfg.mkdir()
    write_setup(cfg, data)
    SecretStore(cfg / "orama" / "secrets.bin").set("apikey:sonarr", random_secret())
    (cfg / "orama" / "secrets.key").write_bytes(b"A" * 43 + b"=")
    value = _secret_value()
    code, out = run_set_secret(capsys, monkeypatch, cfg, ["indexer:geek:apiKey"],
                               FakeStdin(value))
    assert code == 1
    assert out.err.startswith("✗ Cannot save the secret: cannot decrypt"), out.err
    assert value not in out.err + out.out + caplog.text


# ---------------------------------------------------------------- orama HTTP server

@pytest.fixture
def http_base():
    server = make_server("127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


def get(url):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=5) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def test_http_healthz(http_base):
    assert get(f"{http_base}/healthz") == (200, "ok")


def test_http_root(http_base):
    assert get(f"{http_base}/") == (
        200, "Orama is running — setup page arrives in a later update."
    )


def test_http_unknown_path(http_base):
    assert get(f"{http_base}/nope")[0] == 404


@pytest.mark.parametrize("name", ["password:jellyfin", "opensubtitles:username",
                                  "opensubtitles:password"])
def test_plan2b_set_secret_names(capsys, monkeypatch, roots, name):
    data, cfg = roots
    cfg.mkdir()
    write_setup(cfg, data)
    value = random_secret()
    code, out = run_set_secret(capsys, monkeypatch, cfg, [name], FakeStdin(value + "\n"))
    assert code == 0, out.err
    assert SecretStore(cfg / "orama" / "secrets.bin").get(name) == value
    assert value not in out.out + out.err


@pytest.mark.parametrize("value", ["orama-admin", "Owner.Name@home_1", "a", "b" * 64])
def test_set_secret_stores_the_jellyfin_admin_name(capsys, monkeypatch, roots, value):
    data, cfg = roots
    cfg.mkdir()
    write_setup(cfg, data)
    code, out = run_set_secret(capsys, monkeypatch, cfg, ["user:jellyfin"],
                               FakeStdin(value + "\n"))
    assert code == 0, out.err
    assert SecretStore(cfg / "orama" / "secrets.bin").get("user:jellyfin") == value


@pytest.mark.parametrize("bad", [" ", "/", "\\", "'", '"', ":", "é", "x" * 65 + "-"])
def test_set_secret_refuses_a_bad_jellyfin_admin_name(capsys, caplog, monkeypatch, roots, bad):
    data, cfg = roots
    cfg.mkdir()
    write_setup(cfg, data)
    marker = os.urandom(6).hex()
    code, out = run_set_secret(capsys, monkeypatch, cfg, ["user:jellyfin"],
                               FakeStdin(marker + bad + "\n"))
    assert code == 1
    assert out.err.startswith("✗ ") and len(out.err.strip().splitlines()) == 1
    assert "Jellyfin admin name" in out.err
    assert marker not in out.err + out.out + caplog.text
    assert not (cfg / "orama" / "secrets.bin").exists()


def test_plex_token_cannot_be_set_by_hand(capsys, monkeypatch, roots):
    data, cfg = roots
    cfg.mkdir()
    write_setup(cfg, data)
    code, out = run_set_secret(capsys, monkeypatch, cfg, ["token:plex"],
                               FakeStdin(random_secret() + "\n"))
    assert code == 1
    assert "secret name" in out.err
    assert not (cfg / "orama" / "secrets.bin").exists()


@pytest.mark.parametrize("lan", ["none", ".".join(map(str, [192, 168, 1, 8]))])
def test_bootstrap_stores_lan_address(roots, capsys, lan):
    data, cfg = roots
    assert cli.main(bootstrap_args(cfg, data, **{"--lan-address": lan})) == 0
    draft = DesiredSetup.load(cfg / "orama" / "setup.draft.json")
    assert (str(draft.lan_address) if draft.lan_address else "none") == lan


@pytest.mark.parametrize("lan", ["8.8.8.8", "not-an-address", "2001:db8::1"])
def test_bootstrap_bad_lan_address_has_its_own_message(roots, capsys, lan):
    data, cfg = roots
    assert cli.main(bootstrap_args(cfg, data, **{"--lan-address": lan})) == 1
    err = capsys.readouterr().err
    assert "LAN address" in err and "folders cannot be used" not in err
    assert not (cfg / "orama" / "setup.draft.json").exists()
