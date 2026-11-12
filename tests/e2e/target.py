"""Target abstraction for running E2E proofs locally or over SSH (Step 4)."""

from __future__ import annotations

import logging
import os
import re
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

    @abstractmethod
    def save_logs(self, log_dir: Path) -> None:
        """Collect scrubbed container and service logs into ``log_dir``."""


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

    def _collect_stored_secrets(self) -> None:
        secrets_file = f"{self.config_root}/orama/secrets.bin"
        snippet = (
            "import json, sys\n"
            "from pathlib import Path\n"
            "from orama.secrets import SecretStore\n"
            "p = Path(sys.argv[1])\n"
            "if p.exists():\n"
            "    s = SecretStore(p)\n"
            "    print(json.dumps({n: s.get(n) for n in s.names()}))\n"
        )
        try:
            res = self.run(
                ["docker", "exec", "orama-server", "python", "-c", snippet, secrets_file],
                timeout=30,
            )
            if res.returncode == 0 and res.stdout.strip():
                import json

                data = json.loads(res.stdout)
                if isinstance(data, dict):
                    non_secret = {"jellyfin:user", "plex:client-id"}
                    self.register_secret(
                        *[str(v) for k, v in data.items() if k not in non_secret and v is not None]
                    )
        except Exception as exc:  # noqa: BLE001 - best-effort before cleanup
            logging.getLogger(__name__).debug("unable to read stored secrets before cleanup: %s", exc)

    def _write_scrubbed(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.scrub(content), encoding="utf-8", errors="replace")

    def save_logs(self, log_dir: Path) -> None:
        self._collect_stored_secrets()
        log_dir.mkdir(parents=True, exist_ok=True)

        self._write_scrubbed(
            log_dir / "docker-ps.txt",
            self.run(["docker", "ps", "-a", "--no-trunc"], timeout=60).stdout,
        )
        self._write_scrubbed(
            log_dir / "docker-networks.txt",
            self.run(["docker", "network", "ls"], timeout=60).stdout,
        )

        listed = self.run(["docker", "ps", "-a", "--format", "{{.Names}}"], timeout=60)
        for cname in listed.stdout.split():
            if not cname.startswith("orama-"):
                continue
            logs = self.run(["docker", "logs", cname], timeout=60)
            self._write_scrubbed(log_dir / f"container-{cname}.log", logs.stdout)

        recyclarr_logs = Path(self.config_root) / "config/recyclarr/logs"
        if recyclarr_logs.is_dir():
            for file_path in sorted(recyclarr_logs.rglob("*")):
                if file_path.is_file():
                    rel = str(file_path.relative_to(recyclarr_logs)).replace("/", "_")
                    self._write_scrubbed(
                        log_dir / f"recyclarr-{rel}",
                        file_path.read_text(encoding="utf-8", errors="replace"),
                    )

        data_tree = self.run(["find", self.data_root, "-maxdepth", "6", "-ls"], timeout=60)
        self._write_scrubbed(log_dir / "data-tree.txt", data_tree.stdout)
