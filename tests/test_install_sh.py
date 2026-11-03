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
AWKWARD = "da ta 'q\"#:Ωμέγα"
AWKWARD_ROOT = AWKWARD.replace(":", "")  # Docker cannot bind folders whose path has ":"


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
    tail = argv[argv.index("orama:t") + 1:]
    assert tail == [
        "python", "-m", "orama.cli", "bootstrap", "--platform", "ubuntu",
        "--data-root", "/srv/data", "--config-root", config, "--puid", "1000",
        "--pgid", "1001", "--docker-gid", "998", "--render-gid", "none", "--nvidia", "0",
        "--taken-ports", "22,80", "--orama-image", "orama:t", "--agent-image", "agent:t",
    ]
    assert "port=" in out.stdout


def test_run_bootstrap_reads_port(tmp_path):
    bin_dir = fake_bin(tmp_path, "docker", 'echo 8765')
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
    out = run_fn("print_address 8765", PATH=f"{bin_dir}:{os.environ['PATH']}")
    assert out.returncode == 0
    assert f"Orama is ready. Open {expected} in your browser." in out.stdout
