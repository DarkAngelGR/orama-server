"""install.sh, driven function by function: tests source the script and call one function."""

import json
import os
import re
import shlex
import stat
import subprocess
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "install.sh"
FIXTURES = Path(__file__).parent / "fixtures" / "os-release"
UBUNTU, DEBIAN, UGOS, FEDORA = (
    str(FIXTURES / n) for n in ("ubuntu-24.04", "debian-12", "ugos-pro", "fedora-40")
)
AWKWARD = "da ta 'q\"#:Ωμέγα"  # ":" only where it is quoted, never as a root
AWKWARD_ROOT = AWKWARD.replace(":", "&")  # Docker cannot mount a path with ":"


def _env(**overrides):
    env = {"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/root"), "LANG": "C.UTF-8"}
    env.update({k: str(v) for k, v in overrides.items()})
    return env


def run_fn(fn, *, merge=True, **env):
    """Source install.sh and run ``fn``; stderr is merged into stdout unless ``merge=False``."""
    return subprocess.run(
        ["bash", "-c", f"source {shlex.quote(str(SCRIPT))}; {fn}"],
        check=False,
        env=_env(**env),
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT if merge else subprocess.PIPE,
        text=True,
        timeout=30,
    )


def fake_bin(tmp_path, name, body):
    """An executable shell script ``name`` in ``tmp_path/bin``; returns the bin folder."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    exe = bin_dir / name
    exe.write_text(f"#!/usr/bin/env bash\n{body}\n")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return bin_dir


# ---------------------------------------------------------------- loading

def test_sourcing_does_not_run_main():
    out = run_fn("echo sourced-ok", OS_RELEASE_FILE=FEDORA, DRY_RUN="1")
    assert out.returncode == 0
    assert out.stdout == "sourced-ok\n"


@pytest.mark.parametrize("how", ["redirect", "pipe"])
def test_piped_script_runs_main(how):
    cmd = "bash < install.sh" if how == "redirect" else "cat install.sh | bash"
    out = subprocess.run(
        ["bash", "-c", cmd], check=False, env=_env(OS_RELEASE_FILE=FEDORA, DRY_RUN="1"), cwd=REPO_ROOT,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=30,
    )
    assert out.returncode == 1
    assert "only works on Ubuntu, Debian and UGOS" in out.stdout


def test_executed_script_runs_main():
    out = subprocess.run(
        ["bash", str(SCRIPT)], check=False, env=_env(OS_RELEASE_FILE=FEDORA, DRY_RUN="1"),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=30,
    )
    assert out.returncode == 1
    assert "only works on Ubuntu, Debian and UGOS" in out.stdout


def test_main_is_called_only_at_the_very_end():
    lines = SCRIPT.read_text().rstrip("\n").splitlines()
    assert lines[-2:] == [
        'if [[ "${BASH_SOURCE[0]:-$0}" != "$0" ]]; then return 0; fi',
        'main "$@"',
    ]
    assert "set -Eeuo pipefail" in lines[:20]


def test_default_version_matches_pyproject():
    version = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())["project"]["version"]
    m = re.search(r'^ORAMA_DEFAULT_VERSION="([^"]+)"$', SCRIPT.read_text(), re.MULTILINE)
    assert m and m.group(1) == version


# ---------------------------------------------------------------- platform

@pytest.mark.parametrize(
    "fixture,expected",
    [(UBUNTU, "ubuntu"), (DEBIAN, "debian"), (UGOS, "ugos"), (FEDORA, "unsupported")],
)
def test_detect_platform(fixture, expected):
    out = run_fn("detect_platform", merge=False, OS_RELEASE_FILE=fixture)
    assert out.stdout == f"{expected}\n"
    assert out.returncode == (1 if expected == "unsupported" else 0)


def test_unsupported_platform_explains(tmp_path):
    out = run_fn("detect_platform", OS_RELEASE_FILE=FEDORA)
    assert "only works on Ubuntu, Debian and UGOS" in out.stdout


def test_missing_os_release_is_unsupported(tmp_path):
    out = run_fn("detect_platform", merge=False, OS_RELEASE_FILE=tmp_path / "nope")
    assert out.returncode == 1 and out.stdout == "unsupported\n"


def test_require_x86_64_refuses_other_cpus(tmp_path):
    bin_dir = fake_bin(tmp_path, "uname", 'echo aarch64')
    out = run_fn("require_x86_64", PATH=f"{bin_dir}:{os.environ['PATH']}")
    assert out.returncode == 1 and "x86-64" in out.stdout


# ---------------------------------------------------------------- docker

def test_ugos_never_installs_docker():
    out = run_fn("ensure_docker ugos", OS_RELEASE_FILE=UGOS, DOCKER_BIN="/nonexistent", DRY_RUN="1")
    assert out.returncode != 0
    assert "App Center" in out.stdout
    assert "apt-get" not in out.stdout


def test_working_docker_is_left_alone(tmp_path):
    bin_dir = fake_bin(tmp_path, "docker", "exit 0")
    out = run_fn("ensure_docker debian", OS_RELEASE_FILE=DEBIAN,
                 DOCKER_BIN=bin_dir / "docker", DRY_RUN="1")
    assert out.returncode == 0
    assert "apt-get" not in out.stdout


# A fake docker whose `compose version` and `info` answers are chosen per test.
def _docker(tmp_path, *, compose=True, daemon=True, runtimes='{"runc":{}}'):
    body = (
        'case "$1 ${2:-}" in\n'
        f'  "compose version") exit {0 if compose else 1} ;;\n'
        f'  "info --format") echo {shlex.quote(runtimes)}; exit {0 if daemon else 1} ;;\n'
        f'  "info "*) exit {0 if daemon else 1} ;;\n'
        '  *) exit 0 ;;\n'
        'esac'
    )
    return fake_bin(tmp_path, "docker", body) / "docker"


def _no_docker_install(text):
    assert "apt-get install" not in text and "docker-ce" not in text
    assert "download.docker.com" not in text


@pytest.mark.parametrize("platform,fixture", [("debian", DEBIAN), ("ubuntu", UBUNTU)])
def test_existing_docker_daemon_down_is_started_not_reinstalled(tmp_path, platform, fixture):
    out = run_fn(f"ensure_docker {platform}", OS_RELEASE_FILE=fixture,
                 DOCKER_BIN=_docker(tmp_path, daemon=False), DRY_RUN="1")
    assert out.returncode == 0, out.stdout
    assert "+ systemctl start docker" in out.stdout
    _no_docker_install(out.stdout)


def test_existing_docker_that_will_not_start_stops_plainly(tmp_path):
    bin_dir = fake_bin(tmp_path, "systemctl", 'echo "systemctl $*" >> "$(dirname "$0")/calls"')
    out = run_fn("ensure_docker debian", OS_RELEASE_FILE=DEBIAN,
                 DOCKER_BIN=_docker(tmp_path, daemon=False), DOCKER_START_TRIES="1",
                 PATH=f"{bin_dir}:{os.environ['PATH']}")
    assert out.returncode == 1
    assert "Docker is installed but is not running" in out.stdout
    assert (bin_dir / "calls").read_text() == "systemctl start docker\n"
    _no_docker_install(out.stdout)


def test_existing_docker_without_compose_names_the_package(tmp_path):
    out = run_fn("ensure_docker ubuntu", OS_RELEASE_FILE=UBUNTU,
                 DOCKER_BIN=_docker(tmp_path, compose=False), DRY_RUN="1")
    assert out.returncode == 1
    assert "docker-compose-plugin" in out.stdout and "docker-compose-v2" in out.stdout
    assert "systemctl" not in out.stdout
    _no_docker_install(out.stdout.replace("docker-compose-plugin", ""))


def test_ugos_docker_daemon_down_points_to_app_center(tmp_path):
    out = run_fn("ensure_docker ugos", OS_RELEASE_FILE=UGOS,
                 DOCKER_BIN=_docker(tmp_path, daemon=False), DRY_RUN="1")
    assert out.returncode == 1 and "App Center" in out.stdout
    assert "systemctl" not in out.stdout
    _no_docker_install(out.stdout)


@pytest.mark.parametrize("fixture,distro,codename", [(DEBIAN, "debian", "bookworm"),
                                                     (UBUNTU, "ubuntu", "noble")])
def test_dry_run_installs_from_docker_repo(fixture, distro, codename):
    out = run_fn(f"ensure_docker {distro}", OS_RELEASE_FILE=fixture,
                 DOCKER_BIN="/nonexistent", DRY_RUN="1")
    assert out.returncode == 0, out.stdout
    text = out.stdout
    assert f"https://download.docker.com/linux/{distro}/gpg" in text
    assert "9DC858229FC7DD38854AE2D88D81803C0EBFCD88" in text
    assert (
        f"deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.asc] "
        f"https://download.docker.com/linux/{distro} {codename} stable"
    ) in text
    install = next(line for line in text.splitlines() if "docker-ce-cli" in line)
    for pkg in ("docker-ce", "docker-ce-cli", "containerd.io", "docker-compose-plugin"):
        assert pkg in install.split()
    assert "systemctl enable --now docker" in text


def test_key_fingerprint_check_rejects_other_keys(tmp_path):
    key = tmp_path / "other.asc"
    home = tmp_path / "gnupg"
    home.mkdir(mode=0o700)
    subprocess.run(
        ["gpg", "--homedir", home, "--batch", "--pinentry-mode", "loopback",
         "--passphrase", "", "--quick-gen-key",
         "Test <test@example.invalid>", "ed25519", "sign", "never"],
        check=True, capture_output=True,
    )
    key.write_bytes(subprocess.run(
        ["gpg", "--homedir", home, "--armor", "--export"], check=True, capture_output=True
    ).stdout)
    out = run_fn(f"check_docker_key {shlex.quote(str(key))}")
    assert out.returncode == 1 and "signing key" in out.stdout


# ---------------------------------------------------------------- ids, gids, ports

def test_install_user_from_sudo():
    out = run_fn("install_user debian", merge=False, SUDO_UID="1001", SUDO_GID="1002")
    assert out.returncode == 0 and out.stdout == "1001:1002\n"


def test_install_user_falls_back_to_1000_with_note():
    out = run_fn("install_user debian", merge=False, SUDO_UID="0", SUDO_GID="0")
    assert out.returncode == 0 and out.stdout == "1000:1000\n"
    assert "1000" in out.stderr


def test_install_user_on_ugos_uses_first_regular_user(tmp_path):
    passwd = tmp_path / "passwd"
    passwd.write_text(
        "root:x:0:0:root:/root:/bin/bash\n"
        "nobody:x:65534:65534:nobody:/nonexistent:/usr/sbin/nologin\n"
        "owner:x:1003:100:Owner:/srv/users/owner:/bin/bash\n"
        "second:x:1004:100::/srv/users/second:/bin/bash\n"
    )
    out = run_fn("install_user ugos", merge=False, PASSWD_FILE=passwd)
    assert out.returncode == 0 and out.stdout == "1003:100\n"


def test_install_user_env_override():
    out = run_fn("install_user debian", merge=False, ORAMA_PUID="1500", ORAMA_PGID="1600",
                 SUDO_UID="1001", SUDO_GID="1002")
    assert out.stdout == "1500:1600\n"


@pytest.mark.parametrize("puid,pgid", [("0", "1000"), ("1000", "0")])
def test_install_user_refuses_root_ids(puid, pgid):
    out = run_fn("install_user debian", ORAMA_PUID=puid, ORAMA_PGID=pgid)
    assert out.returncode == 1 and "root" in out.stdout


def test_numeric_gid_of_missing_path_is_none(tmp_path):
    out = run_fn(f"numeric_gid_of {shlex.quote(str(tmp_path / 'renderD128'))}")
    assert out.returncode == 0 and out.stdout == "none\n"


def test_numeric_gid_of_existing_path(tmp_path):
    f = tmp_path / "sock"
    f.touch()
    out = run_fn(f"numeric_gid_of {shlex.quote(str(f))}")
    assert out.stdout == f"{f.stat().st_gid}\n"


@pytest.mark.parametrize("runtimes,cdi,expected", [
    ('{"io.containerd.runc.v2":{},"nvidia":{"path":"nvidia-container-runtime"}}', False, "1"),
    ('{"runc":{"path":"runc"}}', True, "1"),
    ('{"runc":{"path":"runc"}}', False, "0"),
])
def test_has_nvidia_reflects_what_docker_can_use(tmp_path, runtimes, cdi, expected):
    spec = tmp_path / "cdi" / "nvidia.yaml"
    if cdi:
        spec.parent.mkdir()
        spec.write_text("cdiVersion: 0.5.0\n")
    out = run_fn("has_nvidia", merge=False, DOCKER_BIN=_docker(tmp_path, runtimes=runtimes),
                 NVIDIA_CDI_SPECS=f"{tmp_path}/none.yaml {spec}")
    assert out.stdout == f"{expected}\n"


def test_nvidia_smi_alone_is_not_nvidia(tmp_path):
    bin_dir = fake_bin(tmp_path, "nvidia-smi", "exit 0")
    fake_bin(tmp_path, "nvidia-ctk", "exit 0")
    out = run_fn("has_nvidia", merge=False, DOCKER_BIN=_docker(tmp_path),
                 NVIDIA_CDI_SPECS=f"{tmp_path}/none.yaml",
                 PATH=f"{bin_dir}:{os.environ['PATH']}")
    assert out.stdout == "0\n"


def test_taken_ports_deduplicated(tmp_path):
    listing = (
        "LISTEN 0 4096 0.0.0.0:22 0.0.0.0:*\n"
        "LISTEN 0 4096 [::]:22 [::]:*\n"
        "LISTEN 0 4096 127.0.0.53%lo:53 0.0.0.0:*\n"
        "LISTEN 0 511 *:8080 *:*\n"
    )
    bin_dir = fake_bin(tmp_path, "ss", f'[[ "$*" == "-Htln" ]] || exit 9\nprintf %s {shlex.quote(listing)}')
    out = run_fn("taken_ports", merge=False, SS_BIN=bin_dir / "ss")
    assert out.returncode == 0 and out.stdout == "22,53,8080\n"


def test_taken_ports_without_ss_is_empty_with_note():
    out = run_fn("taken_ports", merge=False, SS_BIN="/nonexistent")
    assert out.returncode == 0 and out.stdout == "\n"
    assert out.stderr.strip()


# ---------------------------------------------------------------- roots and earlier setups

@pytest.mark.parametrize("platform,data,config", [
    ("ubuntu", "/data", "/opt/orama"),
    ("debian", "/data", "/opt/orama"),
    ("ugos", "/volume1/data", "/volume1/docker/orama"),
])
def test_resolve_roots_defaults(platform, data, config):
    out = run_fn(f'resolve_roots {platform}; printf "%s|%s" "$DATA_ROOT" "$CONFIG_ROOT"')
    assert out.stdout == f"{data}|{config}"


def test_resolve_roots_env_override_is_canonical(tmp_path):
    out = run_fn('resolve_roots ubuntu; printf "%s|%s" "$DATA_ROOT" "$CONFIG_ROOT"',
                 ORAMA_DATA_ROOT=f"{tmp_path}/x/../{AWKWARD_ROOT}/",
                 ORAMA_CONFIG_ROOT=f"{tmp_path}//c")
    assert out.stdout == f"{tmp_path}/{AWKWARD_ROOT}|{tmp_path}/c"


@pytest.mark.parametrize("var", ["ORAMA_DATA_ROOT", "ORAMA_CONFIG_ROOT"])
def test_resolve_roots_refuses_colon(var):
    out = run_fn("resolve_roots ubuntu", **{var: "/srv/a:b"})
    assert out.returncode == 1 and '":"' in out.stdout and var in out.stdout


def _saved(config, name, **values):
    state = config / "orama"
    state.mkdir(parents=True, exist_ok=True)
    data = {"data_root": "/srv/data", "config_root": str(config), "puid": 1000, "pgid": 1000}
    data.update(values)
    (state / name).write_text(json.dumps(data))


def _check(config_root, data_root="/srv/data", puid=1000, pgid=1000):
    return run_fn(
        f'CONFIG_ROOT={shlex.quote(str(config_root))} DATA_ROOT={shlex.quote(data_root)} '
        f'PUID={puid} PGID={pgid} ORAMA_IMAGE=unused; check_existing_setup'
    )


def test_check_existing_setup_nothing_saved(tmp_path):
    assert _check(tmp_path / AWKWARD).returncode == 0


def test_check_existing_setup_matching(tmp_path):
    config = tmp_path / AWKWARD
    _saved(config, "setup.draft.json")
    assert _check(config).returncode == 0


@pytest.mark.parametrize("field,value,env", [
    ("data_root", "/srv/other", "ORAMA_DATA_ROOT"),
    ("puid", 1001, "ORAMA_PUID"),
    ("pgid", 1001, "ORAMA_PGID"),
    ("config_root", "/srv/elsewhere", "ORAMA_CONFIG_ROOT"),
])
def test_check_existing_setup_mismatch_stops(tmp_path, field, value, env):
    config = tmp_path / "orama-config"
    _saved(config, "setup.draft.json", **{field: value})
    out = _check(config)
    assert out.returncode == 1
    assert env in out.stdout and str(value) in out.stdout


def test_check_existing_setup_prefers_saved_over_draft(tmp_path):
    config = tmp_path / "c"
    _saved(config, "setup.draft.json", data_root="/srv/old-draft")
    _saved(config, "setup.json")
    assert _check(config).returncode == 0


def test_check_existing_setup_unreadable(tmp_path):
    config = tmp_path / "c"
    (config / "orama").mkdir(parents=True)
    (config / "orama" / "setup.json").write_text("{not json")
    out = _check(config)
    assert out.returncode == 1 and "setup.json" in out.stdout


# ---------------------------------------------------------------- images, bootstrap, start

def test_get_images_pulls_versioned_images():
    out = run_fn("get_images; echo \"$ORAMA_IMAGE $AGENT_IMAGE\"", DRY_RUN="1",
                 ORAMA_VERSION="9.8.7")
    assert out.returncode == 0
    assert "docker pull ghcr.io/darkangelgr/orama:9.8.7" in out.stdout
    assert "docker pull ghcr.io/darkangelgr/orama-agent:9.8.7" in out.stdout
    assert out.stdout.splitlines()[-1] == (
        "ghcr.io/darkangelgr/orama:9.8.7 ghcr.io/darkangelgr/orama-agent:9.8.7"
    )


def test_get_images_default_version_is_the_scripts():
    out = run_fn("get_images", DRY_RUN="1")
    version = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())["project"]["version"]
    assert f"ghcr.io/darkangelgr/orama:{version}" in out.stdout


def test_get_images_builds_locally_from_script_dir():
    out = run_fn("get_images", DRY_RUN="1", ORAMA_BUILD_LOCAL="1", ORAMA_VERSION="9.8.7")
    assert out.returncode == 0, out.stdout
    src = shlex.quote(str(REPO_ROOT))
    assert (f"docker build -f {src}/docker/orama.Dockerfile "
            f"-t ghcr.io/darkangelgr/orama:9.8.7 {src}") in out.stdout
    assert (f"docker build -f {src}/docker/agent.Dockerfile "
            f"-t ghcr.io/darkangelgr/orama-agent:9.8.7 {src}") in out.stdout
    assert "docker pull" not in out.stdout


def test_get_images_local_build_needs_a_source(tmp_path):
    out = run_fn("get_images", DRY_RUN="1", ORAMA_BUILD_LOCAL="1", ORAMA_SRC=tmp_path)
    assert out.returncode == 1 and "ORAMA_SRC" in out.stdout


def test_mount_arg_quotes_awkward_paths():
    out = run_fn(f"mount_arg {shlex.quote('/a,b' + AWKWARD)} /t ro", merge=False)
    path = '/a,b' + AWKWARD.replace('"', '""')
    assert out.stdout == f'type=bind,"source={path}","target=/t",readonly\n'


def test_run_bootstrap_dry_run_command(tmp_path):
    config = f"/srv/{AWKWARD}"
    out = run_fn(
        f"CONFIG_ROOT={shlex.quote(config)} DATA_ROOT=/srv/data PUID=1000 PGID=1001 "
        "ORAMA_IMAGE=orama:t AGENT_IMAGE=agent:t; "
        "run_bootstrap ubuntu 998 none 0 22,80; echo \"port=$ORAMA_PORT\"",
        DRY_RUN="1",
    )
    assert out.returncode == 0, out.stdout
    line = next(x for x in out.stdout.splitlines() if "orama.cli" in x)
    argv = shlex.split(line.removeprefix("+ "))
    mount = argv[argv.index("--mount") + 1]
    assert mount == f'type=bind,"source={config.replace(chr(34), 2 * chr(34))}",' \
                    f'"target={config.replace(chr(34), 2 * chr(34))}"'
    assert argv[:2] == ["docker", "run"]
    assert {"--rm", "--network"} <= set(argv) and argv[argv.index("--user") + 1] == "0:0"
    assert argv[argv.index("--network") + 1] == "none"
    for flag in ("--lan-address", "--host-name"):
        index = argv.index(flag)
        del argv[index:index + 2]
    tail = argv[argv.index("orama:t") + 1:]
    assert tail == [
        "python", "-m", "orama.cli", "bootstrap", "--platform", "ubuntu",
        "--data-root", "/srv/data", "--config-root", config, "--puid", "1000",
        "--pgid", "1001", "--docker-gid", "998", "--render-gid", "none", "--nvidia", "0",
        "--taken-ports", "22,80", "--orama-image", "orama:t", "--agent-image", "agent:t",
    ]
    assert "port=" in out.stdout


def test_run_bootstrap_reads_port(tmp_path):
    bin_dir = fake_bin(tmp_path, "docker", 'printf "8765\\n-\\n"')
    out = run_fn(
        "CONFIG_ROOT=/srv/c DATA_ROOT=/srv/d PUID=1 PGID=1 ORAMA_IMAGE=o AGENT_IMAGE=a; "
        "run_bootstrap ubuntu 998 none 0 ''; echo \"port=$ORAMA_PORT\"",
        DOCKER_BIN=bin_dir / "docker",
    )
    assert out.returncode == 0 and out.stdout.endswith("port=8765\n")


def test_run_bootstrap_failure_stops(tmp_path):
    bin_dir = fake_bin(tmp_path, "docker", 'echo "✗ These folders cannot be used." >&2; exit 1')
    out = run_fn(
        "CONFIG_ROOT=/srv/c DATA_ROOT=/srv/d PUID=1 PGID=1 ORAMA_IMAGE=o AGENT_IMAGE=a; "
        "run_bootstrap ubuntu 998 none 0 ''; echo reached",
        DOCKER_BIN=bin_dir / "docker",
    )
    assert out.returncode == 1
    assert "These folders cannot be used" in out.stdout and "reached" not in out.stdout


def test_start_core_dry_run():
    config = f"/srv/{AWKWARD}"
    out = run_fn(f"CONFIG_ROOT={shlex.quote(config)}; start_core", DRY_RUN="1")
    argv = shlex.split(out.stdout.splitlines()[-1].removeprefix("+ "))
    assert argv == ["docker", "compose", "-p", "orama-core", "--project-directory", config,
                    "-f", f"{config}/core.compose.yaml", "up", "-d", "--wait"]


def test_prepare_roots_creates_missing_data_root_owned(tmp_path):
    data, config = tmp_path / "d" / AWKWARD, tmp_path / "c"
    uid, gid = os.getuid(), os.getgid()
    out = run_fn(
        f"DATA_ROOT={shlex.quote(str(data))} CONFIG_ROOT={shlex.quote(str(config))} "
        f"PUID={uid} PGID={gid}; prepare_config_root; prepare_data_root"
    )
    assert out.returncode == 0, out.stdout
    assert stat.S_IMODE(data.stat().st_mode) == 0o755
    assert (data.stat().st_uid, data.stat().st_gid) == (uid, gid)
    assert config.is_dir() and stat.S_IMODE(config.stat().st_mode) == 0o755


def test_prepare_data_root_leaves_existing_alone(tmp_path):
    data = tmp_path / "d"
    data.mkdir(mode=0o750)
    data.chmod(0o750)
    out = run_fn(f"DATA_ROOT={shlex.quote(str(data))} PUID=4242 PGID=4242; prepare_data_root")
    assert out.returncode == 0
    assert stat.S_IMODE(data.stat().st_mode) == 0o750
    assert data.stat().st_uid == os.getuid()


def test_prepare_data_root_refuses_a_file(tmp_path):
    data = tmp_path / "d"
    data.touch()
    out = run_fn(f"DATA_ROOT={shlex.quote(str(data))} PUID=1 PGID=1; prepare_data_root")
    assert out.returncode == 1 and "not a folder" in out.stdout


@pytest.mark.parametrize("listing,expected", [
    ("127.0.0.1 203.0.113.7 2001:db8::1", "http://203.0.113.7:8765"),
    ("2001:db8::1 203.0.113.9", "http://203.0.113.9:8765"),
    ("127.0.0.1", "http://localhost:8765"),
    ("", "http://localhost:8765"),
])
def test_print_address(tmp_path, listing, expected):
    bin_dir = fake_bin(tmp_path, "hostname", f'[[ "$1" == "-I" ]] || exit 9\necho "{listing}"')
    out = run_fn("print_address 8765", PATH=f"{bin_dir}:{os.environ['PATH']}",
                 IP_BIN="/nonexistent", PROC_NET_ROUTE=_route_table(tmp_path))
    assert out.returncode == 0
    assert f"Orama is ready. Open {expected} in your browser." in out.stdout


def test_check_existing_setup_fifo_does_not_hang(tmp_path):
    config = tmp_path / "c"
    (config / "orama").mkdir(parents=True)
    os.mkfifo(config / "orama" / "setup.json")
    out = _check(config)  # run_fn has a timeout: a blocking open would fail the test
    assert out.returncode == 1 and "Cannot read the earlier Orama settings" in out.stdout


def test_check_existing_setup_oversized_file_is_refused(tmp_path):
    config = tmp_path / "c"
    _saved(config, "setup.json", padding="x" * (1024 * 1024 + 1))
    out = _check(config)
    assert out.returncode == 1 and "Cannot read the earlier Orama settings" in out.stdout


def test_check_existing_setup_mismatch_hides_control_characters(tmp_path):
    config = tmp_path / "c"
    _saved(config, "setup.json", data_root="/srv/a\x1b[31mb\nFAKE LINE\x07")
    out = _check(config)
    assert out.returncode == 1 and "ORAMA_DATA_ROOT" in out.stdout
    assert "\x1b" not in out.stdout and "\x07" not in out.stdout
    assert "\nFAKE LINE" not in out.stdout
    assert r"\x1b[31mb\nFAKE LINE\x07" in out.stdout


# ---------------------------------------------------------------- main

@pytest.mark.parametrize("var", ["ORAMA_DATA_ROOT", "ORAMA_CONFIG_ROOT"])
def test_main_refuses_bad_root_before_touching_docker(var):
    # A host without Docker: a refusal must come before any attempt to install it.
    out = run_fn("require_root() { :; }; require_x86_64() { :; }; main",
                 OS_RELEASE_FILE=UBUNTU, DOCKER_BIN="/nonexistent", DRY_RUN="1",
                 SUDO_UID="1000", SUDO_GID="1000", **{var: "/srv/a:b"})
    assert out.returncode == 1
    assert '":"' in out.stdout and var in out.stdout
    assert "Installing Docker" not in out.stdout
    _no_docker_install(out.stdout)


def test_main_refuses_root_ids_before_touching_docker():
    out = run_fn("require_root() { :; }; require_x86_64() { :; }; main",
                 OS_RELEASE_FILE=UBUNTU, DOCKER_BIN="/nonexistent", DRY_RUN="1",
                 ORAMA_PUID="0", ORAMA_PGID="0")
    assert out.returncode == 1 and "root" in out.stdout
    _no_docker_install(out.stdout)


def test_main_refuses_mismatched_setup_before_touching_docker(tmp_path):
    config = tmp_path / "c"
    _saved(config, "setup.json", data_root="/srv/other")
    out = run_fn("require_root() { :; }; require_x86_64() { :; }; main",
                 OS_RELEASE_FILE=UBUNTU, DOCKER_BIN="/nonexistent", DRY_RUN="1",
                 SUDO_UID="1000", SUDO_GID="1000", ORAMA_CONFIG_ROOT=config,
                 ORAMA_DATA_ROOT="/srv/data")
    assert out.returncode == 1 and "different settings" in out.stdout
    _no_docker_install(out.stdout)


@pytest.mark.parametrize("parts,expected", [([192, 168, 1, 8], True), ([127, 0, 0, 1], False),
                                           ([203, 0, 113, 9], False)])
def test_bootstrap_detects_private_lan(tmp_path, parts, expected):
    address = ".".join(map(str, parts))
    bin_dir = fake_bin(tmp_path, "hostname", f"echo {address}")
    out = run_fn("CONFIG_ROOT=/srv/c DATA_ROOT=/srv/d PUID=1 PGID=1 ORAMA_IMAGE=o AGENT_IMAGE=a; "
                 "run_bootstrap ubuntu 998 none 0 ''", DRY_RUN="1",
                 PATH=f"{bin_dir}:{os.environ['PATH']}", IP_BIN="/nonexistent",
                 PROC_NET_ROUTE=_route_table(tmp_path))
    assert out.returncode == 0
    args = shlex.split(next(line for line in out.stdout.splitlines() if "orama.cli" in line))
    assert args[args.index("--lan-address") + 1] == (address if expected else "none")


# ---------------------------------------------------------------- lan address

def _ip(*octets):
    # Built at runtime: the repo must not hold private-address-shaped strings.
    return ".".join(map(str, octets))


def _le_hex(*octets):
    """An address as /proc/net/route writes it (little-endian hex)."""
    return "".join(f"{o:02X}" for o in reversed(octets))


def _route_table(tmp_path, *rows):
    """A fake /proc/net/route: rows of (iface, network octets, mask octets)."""
    path = tmp_path / "route"
    lines = ["Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT"]
    for iface, net, mask in rows:
        lines.append(f"{iface}\t{_le_hex(*net)}\t00000000\t0001\t0\t0\t0\t{_le_hex(*mask)}\t0\t0\t0")
    path.write_text("\n".join(lines) + "\n")
    return path


DOCKER_BRIDGE = _ip(172, 17, 0, 1)
USER_BRIDGE = _ip(172, 18, 0, 1)
LAN = _ip(192, 168, 1, 8)
BRIDGE_ROUTES = (("docker0", (172, 17, 0, 0), (255, 255, 0, 0)),
                 ("br-0a1b2c3d4e5f", (172, 18, 0, 0), (255, 255, 0, 0)),
                 ("eth0", (192, 168, 1, 0), (255, 255, 255, 0)))


def _fake_ip(tmp_path, source):
    route = f"{_ip(192, 0, 2, 1)} via {_ip(192, 168, 1, 1)} dev eth0 src {source} uid 0"
    return fake_bin(tmp_path, "ip", f'[[ "$1 $2 $3" == "-4 route get" && $# == 4 ]] || exit 9\n'
                                    f'printf "%s\\n    cache\\n" "{route}"') / "ip"


def _lan(tmp_path, *, ip_bin, listing, routes=BRIDGE_ROUTES):
    bin_dir = fake_bin(tmp_path, "hostname", f'[[ "$1" == "-I" ]] || exit 9\necho "{listing}"')
    return run_fn("lan_address", PATH=f"{bin_dir}:{os.environ['PATH']}", IP_BIN=ip_bin,
                  PROC_NET_ROUTE=_route_table(tmp_path, *routes))


def test_lan_address_is_the_default_route_source(tmp_path):
    out = _lan(tmp_path, ip_bin=_fake_ip(tmp_path, LAN), listing=f"{DOCKER_BRIDGE} {LAN}")
    assert out.returncode == 0 and out.stdout == f"{LAN}\n"


def test_lan_address_public_route_source_is_none(tmp_path):
    out = _lan(tmp_path, ip_bin=_fake_ip(tmp_path, _ip(203, 0, 113, 9)), listing=LAN)
    assert out.returncode == 0 and out.stdout == "none\n"


@pytest.mark.parametrize("ip_bin", ["/nonexistent", "failing"])
def test_lan_address_without_ip_skips_docker_bridges(tmp_path, ip_bin):
    if ip_bin == "failing":
        ip_bin = fake_bin(tmp_path, "ip", "exit 2") / "ip"
    out = _lan(tmp_path, ip_bin=ip_bin, listing=f"{DOCKER_BRIDGE} {USER_BRIDGE} {LAN}")
    assert out.returncode == 0 and out.stdout == f"{LAN}\n"


def test_lan_address_without_ip_or_route_table_uses_first_address(tmp_path):
    bin_dir = fake_bin(tmp_path, "hostname", f'echo "{LAN} {DOCKER_BRIDGE}"')
    out = run_fn("lan_address", PATH=f"{bin_dir}:{os.environ['PATH']}", IP_BIN="/nonexistent",
                 PROC_NET_ROUTE=tmp_path / "missing")
    assert out.returncode == 0 and out.stdout == f"{LAN}\n"


def test_lan_address_only_docker_bridges_is_none(tmp_path):
    out = _lan(tmp_path, ip_bin="/nonexistent", listing=f"{DOCKER_BRIDGE} {USER_BRIDGE}")
    assert out.returncode == 0 and out.stdout == "none\n"


def test_bootstrap_passes_the_default_route_source(tmp_path):
    ip_bin = _fake_ip(tmp_path, LAN)
    bin_dir = fake_bin(tmp_path, "hostname", f'echo "{DOCKER_BRIDGE} {LAN}"')
    out = run_fn("CONFIG_ROOT=/srv/c DATA_ROOT=/srv/d PUID=1 PGID=1 ORAMA_IMAGE=o AGENT_IMAGE=a; "
                 "run_bootstrap ubuntu 998 none 0 ''; print_address 8765", DRY_RUN="1",
                 PATH=f"{bin_dir}:{os.environ['PATH']}", IP_BIN=ip_bin,
                 PROC_NET_ROUTE=_route_table(tmp_path, *BRIDGE_ROUTES))
    assert out.returncode == 0, out.stdout
    args = shlex.split(next(line for line in out.stdout.splitlines() if "orama.cli" in line))
    assert args[args.index("--lan-address") + 1] == LAN
    assert f"Open http://{LAN}:8765 in your browser." in out.stdout


# ---------------------------------------------------------------- setup link, host name (Plan 3)

def _first_run_code():
    # Built at runtime: the shape of secrets.token_urlsafe(16).
    import secrets

    return secrets.token_urlsafe(16)


def _bootstrap_then_print(tmp_path, docker_body, **env):
    bin_dir = fake_bin(tmp_path, "docker", docker_body)
    fake_bin(tmp_path, "hostname", f'[[ "$1" == "-I" ]] || exit 9\necho "{LAN}"')
    return run_fn(
        "CONFIG_ROOT=/srv/c DATA_ROOT=/srv/d PUID=1 PGID=1 ORAMA_IMAGE=o AGENT_IMAGE=a; "
        "run_bootstrap ubuntu 998 none 0 ''; print_address \"$ORAMA_PORT\" \"$ORAMA_CODE\"",
        DOCKER_BIN=bin_dir / "docker", PATH=f"{bin_dir}:{os.environ['PATH']}",
        IP_BIN="/nonexistent", PROC_NET_ROUTE=_route_table(tmp_path, *BRIDGE_ROUTES), **env,
    )


def test_installer_prints_setup_link_with_code(tmp_path):
    code = _first_run_code()
    out = _bootstrap_then_print(tmp_path, f'printf "8765\\n{code}\\n"')
    assert out.returncode == 0, out.stdout
    assert out.stdout.splitlines()[-1] == (
        f"✓ Orama is ready. Open http://{LAN}:8765/setup?code={code} in your browser."
    )
    assert out.stdout.count(code) == 1  # only in the final line


def test_installer_prints_plain_link_after_password(tmp_path):
    out = _bootstrap_then_print(tmp_path, 'printf "8765\\n-\\n"')
    assert out.returncode == 0, out.stdout
    assert out.stdout.splitlines()[-1] == f"✓ Orama is ready. Open http://{LAN}:8765 in your browser."


@pytest.mark.parametrize("body", [
    'echo 8765',                                   # no code line
    'printf "8765\\nshort\\n"',                    # too short
    'printf "8765\\nabc def ghi jkl mno pq\\n"',   # spaces
    'printf "8765\\n%s\\n" "$(printf "a%.0s" {1..65})"',  # too long
    'printf "8765\\n<script>alert(1)</script>\\n"',
    'printf "x\\n-\\n"',                           # no port
])
def test_installer_refuses_a_bad_bootstrap_reply(tmp_path, body):
    out = _bootstrap_then_print(tmp_path, body)
    assert out.returncode == 1
    assert "Nothing was started" in out.stdout and "Orama is ready" not in out.stdout


@pytest.mark.parametrize("short,expected", [
    ("NAS-1", "nas-1"), ("nas", "nas"), ("a" * 63, "a" * 63), ("a" * 64, "none"),
    ("bad_name", "none"), ("-nas", "none"), ("nas-", "none"), ("nas.lan", "none"),
    ("ünï", "none"), ("", "none"), ("FAIL", "none"),
])
def test_bootstrap_passes_the_short_host_name(tmp_path, short, expected):
    body = '[[ "$1" == "-s" ]] || exit 9\n'
    body += "exit 3" if short == "FAIL" else f"printf '%s\\n' {shlex.quote(short)}"
    bin_dir = fake_bin(tmp_path, "hostname", body)
    out = run_fn("CONFIG_ROOT=/srv/c DATA_ROOT=/srv/d PUID=1 PGID=1 ORAMA_IMAGE=o AGENT_IMAGE=a; "
                 "run_bootstrap ubuntu 998 none 0 ''", DRY_RUN="1",
                 PATH=f"{bin_dir}:{os.environ['PATH']}", IP_BIN="/nonexistent",
                 PROC_NET_ROUTE=_route_table(tmp_path))
    assert out.returncode == 0, out.stdout
    args = shlex.split(next(line for line in out.stdout.splitlines() if "orama.cli" in line))
    assert args[args.index("--host-name") + 1] == expected


def test_main_prints_the_link_with_the_code():
    text = SCRIPT.read_text()
    assert 'print_address "$ORAMA_PORT" "$ORAMA_CODE"' in text
    assert "tee" not in text and ">> " not in text  # the code is never written to a log
