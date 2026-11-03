"""Encrypted secret store, API-key generation and log redaction.

Secrets live in one Fernet-encrypted JSON blob. The key sits beside it in a
``.key`` file. Both files are created mode 0600 from the start (never
chmod'ed afterwards) and written atomically via a temp file in the same
directory.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import traceback
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from orama.catalog import CATALOG
from orama.setup_model import DesiredSetup, enabled_apps

_KEYED_APPS = ("sonarr", "radarr", "prowlarr")
_VPN_KEY_NAME = "vpn:wireguard_private_key"
_MIN_LITERAL_LEN = 4
_PATTERN = re.compile(r"(?i)(api[_-]?key|token|password|private[_-]?key)[\"'=:\s]+\S+")


def _write_private(path: Path, data: bytes, *, exclusive: bool = False) -> None:
    """Write ``data`` to ``path`` atomically with mode 0600 from creation.

    With ``exclusive`` the write fails with FileExistsError if ``path`` exists.
    """
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if exclusive:
            os.link(tmp, path)  # atomic, fails if path exists
            os.unlink(tmp)
        else:
            os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


class SecretStore:
    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._key_path = self._path.with_suffix(".key")

    def _fernet(self) -> Fernet:
        self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            key = self._key_path.read_bytes()
        except FileNotFoundError:
            try:
                _write_private(self._key_path, Fernet.generate_key(), exclusive=True)
            except FileExistsError:
                pass  # lost a creation race; use the winner's key
            key = self._key_path.read_bytes()
        return Fernet(key)

    def _load(self) -> dict[str, str]:
        try:
            blob = self._path.read_bytes()
        except FileNotFoundError:
            return {}
        try:
            return json.loads(self._fernet().decrypt(blob))
        except InvalidToken:
            raise ValueError(
                f"cannot decrypt secret store {self._path}: wrong or missing key file"
            ) from None

    def get(self, name: str) -> str | None:
        return self._load().get(name)

    def set(self, name: str, value: str) -> None:
        data = self._load()
        data[name] = value
        token = self._fernet().encrypt(json.dumps(data).encode())
        _write_private(self._path, token)

    def names(self) -> list[str]:
        return list(self._load())

    def values(self) -> list[str]:
        return list(self._load().values())


def generate_api_key() -> str:
    """32 lowercase hex chars, the format the *arr apps use."""
    return secrets.token_hex(16)


def ensure_app_keys(store: SecretStore, apps: list[str]) -> None:
    """Create ``apikey:<app>`` for sonarr/radarr/prowlarr if missing; never overwrite."""
    for app in apps:
        if app in _KEYED_APPS and store.get(f"apikey:{app}") is None:
            store.set(f"apikey:{app}", generate_api_key())


def env_files_for(setup: DesiredSetup, store: SecretStore) -> dict[str, dict[str, str]]:
    """``{app: {ENV: value}}`` for enabled apps that have secret env vars."""
    result: dict[str, dict[str, str]] = {}
    for app in enabled_apps(setup):
        spec = CATALOG[app]
        if not spec.secret_env:
            continue
        secret_name = _VPN_KEY_NAME if app == "gluetun" else f"apikey:{app}"
        value = store.get(secret_name)
        if value is None:
            raise KeyError(f"missing secret {secret_name!r} required by app {app!r}")
        result[app] = {env: value for env in spec.secret_env}
    return result


def _redact_text(text: str, store: SecretStore) -> str:
    literals = sorted(
        {v for v in store.values() if len(v) >= _MIN_LITERAL_LEN}, key=len, reverse=True
    )
    for v in literals:
        text = text.replace(v, "***")
    return _PATTERN.sub("***", text)


class RedactingFilter(logging.Filter):
    """Replaces stored secret values and secret-looking key/value pairs with ***."""

    def __init__(self, store: SecretStore) -> None:
        super().__init__()
        self.store = store

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = _redact_text(record.getMessage(), self.store)
        record.args = ()
        if record.exc_info and not record.exc_text:
            record.exc_text = "".join(traceback.format_exception(*record.exc_info))
        if record.exc_text:
            record.exc_text = _redact_text(record.exc_text, self.store)
        if record.stack_info:
            record.stack_info = _redact_text(record.stack_info, self.store)
        return True


def install_redaction(store: SecretStore) -> None:
    """Attach a RedactingFilter to every root-logger handler (once per handler)."""
    for handler in logging.getLogger().handlers:
        if not any(
            isinstance(f, RedactingFilter) and f.store is store for f in handler.filters
        ):
            handler.addFilter(RedactingFilter(store))
