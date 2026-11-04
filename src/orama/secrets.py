"""Encrypted secret store, API-key generation and log redaction.

Secrets live in one Fernet-encrypted JSON blob. The key sits beside it in a
``.key`` file. Both files are created mode 0600 from the start (never
chmod'ed afterwards) and written atomically via a temp file in the same
directory.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import secrets
from collections.abc import Callable
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from orama.catalog import CATALOG
from orama.setup_model import DesiredSetup, enabled_apps

_KEYED_APPS = ("sonarr", "radarr", "prowlarr", "sabnzbd")
VPN_KEY_NAME = "vpn:wireguard_private_key"
_PROXY_PASSWORD_NAME = "password:gluetun-proxy"
QBITTORRENT_PASSWORD_NAME = "password:qbittorrent"
SABNZBD_NZB_KEY_NAME = "nzbkey:sabnzbd"
_QBT_PBKDF2_ITERATIONS = 100_000
_QBT_SALT_BYTES = 16
_QBT_KEY_BYTES = 64
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
        if self._path.suffix == ".key":
            raise ValueError(f"secret store path must not end in .key: {self._path}")
        self._key_path = self._path.with_suffix(".key")

    def _fernet(self, *, create: bool = False) -> Fernet:
        try:
            key = self._key_path.read_bytes()
        except FileNotFoundError:
            if not create:
                raise ValueError(
                    f"cannot decrypt secret store {self._path}: key file is missing"
                ) from None
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
        self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        token = self._fernet(create=True).encrypt(json.dumps(data).encode())
        _write_private(self._path, token)

    def names(self) -> list[str]:
        return list(self._load())

    def values(self) -> list[str]:
        return list(self._load().values())


def generate_api_key() -> str:
    """32 lowercase hex chars, the format the *arr apps use."""
    return secrets.token_hex(16)


def ensure_app_keys(store: SecretStore, apps: list[str], *, gluetun_proxy: bool = False) -> None:
    """Create missing app secrets; never overwrite.

    ``apikey:<app>`` for sonarr/radarr/prowlarr/sabnzbd, SABnzbd's ``nzbkey:sabnzbd``,
    qBittorrent's WebUI password, and with ``gluetun_proxy`` (Prowlarr via the VPN) the
    password for Gluetun's HTTP proxy when Gluetun runs.
    """
    wanted: dict[str, Callable[[], str]] = {}
    for app in apps:
        if app in _KEYED_APPS:
            wanted[f"apikey:{app}"] = generate_api_key
    if "sabnzbd" in apps:
        wanted[SABNZBD_NZB_KEY_NAME] = generate_api_key
    if "qbittorrent" in apps:
        wanted[QBITTORRENT_PASSWORD_NAME] = _new_password
    if gluetun_proxy and "gluetun" in apps:
        wanted[_PROXY_PASSWORD_NAME] = _new_password
    for name, make in wanted.items():
        if store.get(name) is None:
            store.set(name, make())


def _new_password() -> str:
    return secrets.token_urlsafe(24)


def qbittorrent_pbkdf2(password: str, salt: bytes | None = None) -> str:
    """qBittorrent's ``WebUI\\Password_PBKDF2`` value for ``password``.

    PBKDF2-HMAC-SHA512, 100000 iterations, 16-byte salt, 64-byte key, written the way
    qBittorrent stores it: ``@ByteArray(<base64 salt>:<base64 key>)``. Only this hash is
    sent to the Agent, never the password.
    """
    if salt is None:
        salt = secrets.token_bytes(_QBT_SALT_BYTES)
    if len(salt) != _QBT_SALT_BYTES:
        raise ValueError(f"the salt must be {_QBT_SALT_BYTES} bytes")
    key = hashlib.pbkdf2_hmac(
        "sha512", password.encode(), salt, _QBT_PBKDF2_ITERATIONS, dklen=_QBT_KEY_BYTES
    )
    return f"@ByteArray({base64.b64encode(salt).decode()}:{base64.b64encode(key).decode()})"


class MissingSecret(KeyError):
    """A secret an enabled app needs is not in the store (named, never shown)."""

    def __init__(self, secret_name: str, app: str) -> None:
        super().__init__(f"missing secret {secret_name!r} required by app {app!r}")
        self.secret_name = secret_name
        self.app = app


def env_files_for(setup: DesiredSetup, store: SecretStore) -> dict[str, dict[str, str]]:
    """``{app: {ENV: value}}`` for enabled apps that have secret env vars."""
    result: dict[str, dict[str, str]] = {}
    for app in enabled_apps(setup):
        values = {}
        for env, secret_name in _secret_names(app, setup).items():
            value = store.get(secret_name)
            if value is None:
                raise MissingSecret(secret_name, app)
            values[env] = value
        if values:
            result[app] = values
    return result


def _secret_names(app: str, setup: DesiredSetup) -> dict[str, str]:
    """``{ENV: store name}`` for one app's env file."""
    if app == "gluetun":
        names = {"WIREGUARD_PRIVATE_KEY": VPN_KEY_NAME}
        if setup.vpn is not None and setup.vpn.route_prowlarr:
            names["HTTPPROXY_PASSWORD"] = _PROXY_PASSWORD_NAME
        if setup.vpn is not None and setup.vpn.port_forwarding:
            # The port-forward hook logs in to qBittorrent with it.
            names["QBT_WEBUI_PASSWORD"] = QBITTORRENT_PASSWORD_NAME
        return names
    return {env: f"apikey:{app}" for env in CATALOG[app].secret_env}


class RedactingFilter(logging.Filter):
    """Replaces stored secret values and secret-looking key/value pairs with ***."""

    def __init__(self, store: SecretStore) -> None:
        super().__init__()
        self.store = store

    def filter(self, record: logging.LogRecord) -> bool:
        # Handler.handle() runs filters outside its error guard, so this must never raise.
        try:
            literals = self._literals()
        except Exception:  # unreadable store: fall back to the pattern rule alone  # noqa: BLE001
            literals = []
        try:
            message = record.getMessage()
        except Exception:  # bad format args: drop them, keep the raw message  # noqa: BLE001
            message = str(record.msg)
        record.msg = self._redact(message, literals)
        record.args = ()
        try:
            if record.exc_info and not record.exc_text:
                record.exc_text = logging.Formatter().formatException(record.exc_info)
        except Exception:  # noqa: BLE001
            record.exc_text = "<exception>"
        if record.exc_text:
            record.exc_text = self._redact(record.exc_text, literals)
        record.exc_info = None  # no handler may re-render the unredacted exception
        if record.stack_info:
            record.stack_info = self._redact(record.stack_info, literals)
        return True

    def _literals(self) -> list[str]:
        return [v for v in self.store.values() if len(v) >= _MIN_LITERAL_LEN]

    @staticmethod
    def _redact(text: str, literals: list[str]) -> str:
        for v in sorted(set(literals), key=len, reverse=True):
            text = text.replace(v, "***")
        return _PATTERN.sub("***", text)


def install_redaction(store: SecretStore) -> None:
    """Attach a RedactingFilter to every root-logger handler (once per handler)."""
    for handler in logging.getLogger().handlers:
        if not any(
            isinstance(f, RedactingFilter) and f.store is store for f in handler.filters
        ):
            handler.addFilter(RedactingFilter(store))
