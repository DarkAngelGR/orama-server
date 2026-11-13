"""Target abstraction for running E2E proofs locally or over SSH (Step 4)."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from pathlib import Path

SETUP_CODE_RE = re.compile(r"(/setup\?code=)[A-Za-z0-9_-]+")
MIN_SECRET_LEN = 8


class Target(ABC):
    """Machine under test where install.sh runs, containers live, and media files are stored."""

    def __init__(
        self,
        *,
        name: str,
        puid: int,
        pgid: int,
        data_root: str,
        config_root: str,
        repo_dir: str,
        expect_docker_installed: bool = False,
    ) -> None:
        self.name = name
        self.puid = puid
        self.pgid = pgid
        self.data_root = data_root
        self.config_root = config_root
        self.repo_dir = repo_dir
        self.expect_docker_installed = expect_docker_installed
        self._secrets: set[str] = set()

    def register_secret(self, *values: str | None) -> None:
        """Remember generated or stored secrets so logs/outputs can always scrub them."""
        for value in values:
            if isinstance(value, str) and len(value) >= MIN_SECRET_LEN:
                self._secrets.add(value)

    def scrub(self, text: str) -> str:
        """Remove setup codes and all registered secret values from text."""
        cleaned = SETUP_CODE_RE.sub(r"\1<hidden>", text)
        for secret in sorted(self._secrets, key=len, reverse=True):
            if secret in cleaned:
                cleaned = cleaned.replace(secret, "<hidden>")
        return cleaned

    @abstractmethod
    def run(
        self,
        argv: Sequence[str],
        *,
        input: str | None = None,
        timeout: float = 120,
        env: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Execute ``argv`` on the target as root, capturing combined stdout/stderr."""

    @abstractmethod
    def put(self, local: Path, remote: str) -> None:
        """Copy a local file or directory to ``remote`` on the target."""

    @abstractmethod
    def url(self, port: int) -> str:
        """Return the base URL the test browser uses to reach ``port`` on the target."""

    def collect_stored_secrets(self) -> None:
        __tracebackhide__ = True
        snippet = (
            "import json, sys\n"
            "from pathlib import Path\n"
            "from orama.secrets import SecretStore\n"
            "s = SecretStore(Path(sys.argv[1]))\n"
            "print(json.dumps({n: s.get(n) for n in s.names()}))\n"
        )
        result = self.run(
            ["docker", "exec", "orama-server", "python", "-c", snippet,
             f"{self.config_root}/orama/secrets.bin"],
            timeout=30,
        )
        if result.returncode != 0:
            raise RuntimeError("Could not read the proof's stored secrets")
        non_secret = {"jellyfin:user", "plex:client-id"}
        self.register_secret(*[v for k, v in json.loads(result.stdout).items() if k not in non_secret])

    def check_for_leaks(self, texts: Mapping[str, str]) -> None:
        __tracebackhide__ = True
        for label, text in texts.items():
            if any(secret in text for secret in self._secrets) or SETUP_CODE_RE.search(text):
                raise AssertionError(f"Secret leaked in {label}")

    def write_scan_values(self, path: Path) -> None:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(sorted(self._secrets), handle)

    def save_logs(self, log_dir: Path) -> dict[str, str]:
        __tracebackhide__ = True
        raw = {
            "docker-ps.txt": self.run(["docker", "ps", "-a", "--no-trunc"], timeout=60).stdout,
            "docker-networks.txt": self.run(["docker", "network", "ls"], timeout=60).stdout,
        }
        listed = self.run(["docker", "ps", "-a", "--format", "{{.Names}}"], timeout=60)
        if listed.returncode != 0:
            raise RuntimeError("Could not list containers for the proof's log snapshot")
        for name in listed.stdout.split():
            if re.fullmatch(r"orama-[a-z0-9-]+", name):
                result = self.run(["docker", "logs", name], timeout=60)
                if result.returncode != 0:
                    raise RuntimeError(f"Could not collect logs from {name}")
                raw[f"container-{name}.log"] = result.stdout
        tree = self.run(["find", self.data_root, "-maxdepth", "6", "-ls"], timeout=60)
        raw["data-tree.txt"] = tree.stdout
        log_dir.mkdir(parents=True, exist_ok=True)
        for name, content in raw.items():
            (log_dir / name).write_text(self.scrub(content), encoding="utf-8", errors="replace")
        return raw


class LocalTarget(Target):
    """Run commands on the current host directly (when root) or via passwordless sudo."""

    def __init__(
        self,
        *,
        name: str = "ubuntu",
        puid: int,
        pgid: int,
        data_root: str,
        config_root: str,
        repo_dir: str,
    ) -> None:
        super().__init__(
            name=name,
            puid=puid,
            pgid=pgid,
            data_root=data_root,
            config_root=config_root,
            repo_dir=repo_dir,
            expect_docker_installed=False,
        )

    def run(
        self,
        argv: Sequence[str],
        *,
        input: str | None = None,
        timeout: float = 120,
        env: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        merged_env = {**os.environ, **env} if env else None
        cmd = list(argv)
        if os.geteuid() != 0:
            cmd = ["sudo", "-n", *(["-E"] if env else []), *cmd]
        return subprocess.run(
            cmd,
            input=input,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
            env=merged_env,
            check=False,
        )

    def put(self, local: Path, remote: str) -> None:
        dest = Path(remote)
        if os.geteuid() == 0:
            dest.parent.mkdir(parents=True, exist_ok=True)
            if local.is_dir():
                shutil.copytree(local, dest, dirs_exist_ok=True)
            else:
                shutil.copy2(local, dest)
            return
        self.run(["mkdir", "-p", str(dest.parent)], timeout=30)
        res = self.run(["cp", "-a", str(local), remote], timeout=60)
        if res.returncode != 0:
            raise RuntimeError(f"failed to copy {local} to {remote}: {self.scrub(res.stdout)}")

    def url(self, port: int) -> str:
        return f"http://127.0.0.1:{port}"


class SshTarget(Target):
    """Use the pinned host key of the disposable Debian guest."""

    def __init__(self, state: Path) -> None:
        self._ssh = [
            "ssh", "-T", "-i", str(state / "id_ed25519"), "-p", "2222",
            "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "ConnectTimeout=10",
            "-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={state / 'known_hosts'}",
            "-o", "GlobalKnownHostsFile=/dev/null", "proof@127.0.0.1",
        ]
        result = self.run(["id", "-u", "proof"], timeout=30)
        group = self.run(["id", "-g", "proof"], timeout=30)
        if result.returncode or group.returncode:
            raise RuntimeError("Could not read the Debian install user's IDs")
        puid, pgid = int(result.stdout.strip()), int(group.stdout.strip())
        if not puid or not pgid:
            raise RuntimeError("The Debian install user must be non-root")
        super().__init__(
            name="debian", puid=puid, pgid=pgid, data_root="/srv/orama-e2e/data",
            config_root="/srv/orama-e2e/cfg", repo_dir="/srv/orama-e2e/source",
            expect_docker_installed=True,
        )

    def run(
        self,
        argv: Sequence[str],
        *,
        input: str | None = None,
        timeout: float = 120,
        env: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        __tracebackhide__ = True
        command = ["sudo", "-n", "--", *argv]
        if env:
            # Pass values through SSH stdin. They must not appear in host command arguments.
            prelude = "".join(f"export {key}={shlex.quote(value)}\n" for key, value in env.items())
            if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) for key in env):
                raise ValueError("Invalid guest environment name")
            if input is not None:
                raise ValueError("Guest commands with environment overrides reserve stdin")
            input = prelude + "exec " + shlex.join(argv) + "\n"
            command = ["sudo", "-n", "--", "sh", "-s"]
        try:
            return subprocess.run(
                [*self._ssh, shlex.join(command)], input=input, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, encoding="utf-8", errors="replace",
                timeout=timeout, check=False,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError("Debian guest command timed out") from None

    def put(self, local: Path, remote: str) -> None:
        if local.is_dir():
            archive = ["tar", "-C", str(local), "-cf", "-", "."]
            dest = remote
            extract = ["tar", "-xf", "-", "-C", dest]
        else:
            archive = ["tar", "-C", str(local.parent), "-cf", "-", local.name]
            dest = str(Path(remote).parent)
            if local.name != Path(remote).name:
                raise ValueError("Debian guest file copies must keep the basename")
            extract = ["tar", "-xf", "-", "-C", dest]
        self._transfer(archive, dest, extract)

    def put_checkout(self, local: Path) -> None:
        self._transfer(
            ["git", "-C", str(local), "archive", "HEAD"],
            self.repo_dir, ["tar", "-xf", "-", "-C", self.repo_dir],
        )

    def _transfer(self, archive: list[str], dest: str, extract: list[str]) -> None:
        if self.run(["mkdir", "-p", dest]).returncode:
            raise RuntimeError("Could not create checkout directory on the Debian guest")
        with subprocess.Popen(archive, stdout=subprocess.PIPE) as source:
            try:
                result = subprocess.run(
                    [*self._ssh, shlex.join(["sudo", "-n", "--", *extract])],
                    stdin=source.stdout, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    timeout=180, check=False,
                )
            finally:
                source.stdout.close()
                if source.poll() is None:
                    try:
                        source.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        source.kill()
            if result.returncode or source.wait():
                raise RuntimeError("Could not transfer files into the Debian guest")

    def url(self, port: int) -> str:
        if port != 8765:
            raise ValueError("The Debian guest must use the forwarded Orama port 8765")
        return "http://localhost:8765"
