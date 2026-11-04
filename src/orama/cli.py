"""Installer-facing commands: ``python -m orama.cli bootstrap|apply|set-secret``.

``bootstrap`` runs as root (the installer starts it with ``docker run --rm`` and the config
folder bind-mounted at the identical path). It writes the root-owned core compose file and
a draft setup in Orama's state folder, which it hands to PUID:PGID. Orama itself can then
never write anything the installer later runs as root.

``apply`` runs inside the Orama container as PUID. It talks to the Agent over its socket
and prints a plain-language checklist. Secret values are never printed.

``set-secret NAME`` also runs inside the Orama container as PUID. It reads one secret from
stdin (no echo on a terminal) and keeps it in the secret store next to the setup file.
"""

from __future__ import annotations

import argparse
import base64
import errno
import getpass
import json
import logging
import os
import re
import secrets
import stat
import sys
from collections.abc import Callable, Sequence
from pathlib import Path, PurePosixPath

from pydantic import ValidationError

from orama.agent_client import DEFAULT_SOCKET, AgentClient, AgentError
from orama.catalog import CATALOG, ENV_FILE_VALUE, UGOS_RESERVED_PORTS
from orama.compose import build_apps_compose, build_core_compose, dump_yaml
from orama.layout import app_config_dir, data_folders, orama_state_dir
from orama.platform import Platform, default_roots
from orama.ports import assign_ports
from orama.secrets import (
    QBITTORRENT_PASSWORD_NAME,
    SABNZBD_NZB_KEY_NAME,
    VPN_KEY_NAME,
    MissingSecret,
    SecretStore,
    ensure_app_keys,
    env_files_for,
    install_redaction,
    qbittorrent_pbkdf2,
)
from orama.setup_model import DesiredSetup, enabled_apps
from orama.wiring.engine import display_name, format_checklist, run_steps
from orama.wiring.http import Http
from orama.wiring.plan import build_steps

CORE_COMPOSE_FILE = "core.compose.yaml"
SETUP_FILE = "setup.json"
DRAFT_FILE = "setup.draft.json"
SECRETS_FILE = "secrets.bin"
RESTART_PENDING = "restart:gluetun-qbittorrent"
QBITTORRENT_HASH_NAME = "qbhash:qbittorrent"
APP_CONFIG_PARENT = "config"
SETUP_LIMIT = 1024 * 1024  # a real setup is a few KiB
SECRET_LIMIT = 4096  # characters; a WireGuard key or an API key is well under 100
SECRET_NAME = re.compile(
    rf"(?:{re.escape(VPN_KEY_NAME)}"
    r"|usenet:[a-z0-9-]+:password"
    r"|indexer:[a-z0-9-]+:[A-Za-z0-9_.]+)"
)

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_NEW_FILE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC

Chown = Callable[[int, int, int], None]


class Failed(Exception):
    """A plain-language reason the command stopped; printed as ``✗ <message>``."""


# ---------------------------------------------------------------- argument parsing

def _uint(value: str) -> int:
    if not value.isdigit():
        raise argparse.ArgumentTypeError(f"must be a whole number ≥ 0, got {value!r}")
    return int(value)


def _gid_or_none(value: str) -> int | None:
    return None if value == "none" else _uint(value)


def _flag(value: str) -> bool:
    if value not in ("0", "1"):
        raise argparse.ArgumentTypeError(f"must be 0 or 1, got {value!r}")
    return value == "1"


def _port_list(value: str) -> set[int]:
    ports = set()
    for part in filter(None, (p.strip() for p in value.split(","))):
        if not part.isdigit() or not 1 <= int(part) <= 65535:
            raise argparse.ArgumentTypeError(f"not a port number: {part!r}")
        ports.add(int(part))
    return ports


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m orama.cli")
    sub = parser.add_subparsers(dest="command", required=True)

    b = sub.add_parser("bootstrap", help="write the core compose file and a draft setup")
    platforms = [p.value for p in Platform if p is not Platform.UNSUPPORTED]
    b.add_argument("--platform", required=True, choices=platforms)
    b.add_argument("--data-root", help="default: the platform's usual data folder")
    b.add_argument("--config-root", help="default: the platform's usual Orama folder")
    b.add_argument("--puid", required=True, type=_uint)
    b.add_argument("--pgid", required=True, type=_uint)
    b.add_argument("--docker-gid", required=True, type=_uint)
    b.add_argument("--render-gid", required=True, type=_gid_or_none, metavar="GID|none")
    b.add_argument("--nvidia", required=True, type=_flag, metavar="0|1")
    b.add_argument("--taken-ports", default="", type=_port_list, metavar="P1,P2,...")
    b.add_argument("--orama-image")
    b.add_argument("--agent-image")

    a = sub.add_parser("apply", help="(inside the Orama container) apply a setup")
    a.add_argument("--setup", required=True, type=Path)

    s = sub.add_parser(
        "set-secret", help="(inside the Orama container) save one secret read from stdin"
    )
    s.add_argument("name", metavar="NAME")
    s.add_argument("--setup", required=True, type=Path)
    return parser


def _default_chown() -> Chown:
    if os.geteuid() == 0:
        return os.fchown
    return lambda _fd, _uid, _gid: None  # not root: owners cannot (and need not) change


def main(argv: Sequence[str] | None = None, *, chown: Chown | None = None) -> int:
    parser = _parser()
    args, extra = parser.parse_known_args(argv)
    if extra and args.command != "set-secret":
        parser.error(f"unrecognized arguments: {' '.join(extra)}")
    try:
        if args.command == "set-secret":
            if extra:  # never echo them: it is likely the secret itself
                raise Failed(
                    "set-secret takes only the secret's name; type the value when asked "
                    "or pipe it in."
                )
            set_secret(args.name, args.setup)
        elif args.command == "bootstrap":
            try:
                port = bootstrap(args, chown or _default_chown())
            except OSError as e:
                where = f" at {e.filename}" if isinstance(e.filename, str) else ""
                raise Failed(
                    f"Orama could not prepare its folder{where} "
                    f"({e.strerror or type(e).__name__})."
                ) from None
            print(port)
        else:
            return apply(args.setup)
    except Failed as e:
        print(f"✗ {e}", file=sys.stderr)
        return 1
    return 0


# ---------------------------------------------------------------- bootstrap

def bootstrap(args: argparse.Namespace, chown: Chown) -> int:
    """Write the core compose file and the draft setup; return the Orama host port."""
    default_data, default_config = default_roots(Platform(args.platform))
    data_root = PurePosixPath(os.path.realpath(args.data_root or default_data))
    config_root = PurePosixPath(os.path.realpath(args.config_root or default_config))
    try:
        draft = DesiredSetup(
            media_servers={"plex"},
            download_methods={"usenet"},
            data_root=data_root,
            config_root=config_root,
            puid=args.puid,
            pgid=args.pgid,
            render_gid=args.render_gid,
            nvidia=args.nvidia,
        )
    except ValidationError as e:
        raise Failed("These folders cannot be used. " + _plain_errors(e)) from None
    state_name = orama_state_dir(PurePosixPath(".")).name

    if not os.path.lexists(config_root):
        os.makedirs(config_root, mode=0o755)
        os.chmod(config_root, 0o755)
    config_fd = _open_dir(config_root, str(config_root))
    try:
        for name in (APP_CONFIG_PARENT, state_name, CORE_COMPOSE_FILE):
            _refuse_link(config_fd, name, config_root / name)
        _make_dir(config_fd, APP_CONFIG_PARENT, config_root / APP_CONFIG_PARENT, 0o755)
        state_fd = _make_dir(config_fd, state_name, config_root / state_name, 0o700)
        try:
            chown(state_fd, args.puid, args.pgid)
            os.fchmod(state_fd, 0o700)
            name, existing = _existing_setup(state_fd, config_root / state_name)
            setup = existing or draft
            # A kept setup is Orama-writable: only sane ports survive, the rest are reassigned.
            kept = {a: p for a, p in setup.ports.items() if _usable_port(p)}
            apps = [a for a, s in CATALOG.items() if s.default_host_port] + ["orama"]
            ports = assign_ports(apps, previous=kept, taken=args.taken_ports)
            setup = setup.model_copy(update={"ports": {**kept, **ports}})
            text = _setup_text(setup)
            _write_atomic(state_fd, name, text, 0o600, owner=(args.puid, args.pgid), chown=chown)
        finally:
            os.close(state_fd)

        core = build_core_compose(
            config_root, data_root, args.puid, args.pgid, args.docker_gid,
            setup.ports["orama"], args.orama_image, args.agent_image,
        )
        _write_atomic(config_fd, CORE_COMPOSE_FILE, dump_yaml(core).encode(), 0o644)
    finally:
        os.close(config_fd)
    return setup.ports["orama"]


_FIELD_NAMES = {
    "data_root": "the data folder",
    "config_root": "the Orama folder",
    "puid": "the user id",
    "pgid": "the group id",
    "render_gid": "the graphics group id",
}


def _plain_errors(e: ValidationError) -> str:
    """One plain sentence per validation error, in words instead of field names."""
    sentences = []
    for err in e.errors():
        msg = str(err["msg"]).removeprefix("Value error, ")
        msg = msg.replace("data_root and config_root", "the data folder and the Orama folder")
        msg = msg.replace("the same or nested", "the same folder or one inside the other")
        for field, words in _FIELD_NAMES.items():
            msg = msg.replace(field, words)
        loc = err.get("loc") or ()
        subject = _FIELD_NAMES.get(str(loc[0])) if loc else None
        sentence = f"{subject} {msg}" if subject else msg
        sentences.append(sentence[:1].upper() + sentence[1:].rstrip(".") + ".")
    return " ".join(sentences)


def _usable_port(port: object) -> bool:
    return (
        type(port) is int and 1024 <= port <= 65535 and port not in UGOS_RESERVED_PORTS
    )


def _setup_text(setup: DesiredSetup) -> bytes:
    data = setup.to_json_dict()
    return (json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()


def _open_dir(path: PurePosixPath | str, shown: str, dir_fd: int | None = None) -> int:
    try:
        return os.open(path, _DIR_FLAGS, dir_fd=dir_fd)
    except OSError as e:
        if e.errno in (errno.ELOOP, errno.ENOTDIR):
            raise Failed(f"{shown} is a link or not a folder; Orama will not use it.") from None
        raise Failed(f"Cannot open {shown} ({e.strerror}).") from None


def _refuse_link(dir_fd: int, name: str, shown: PurePosixPath) -> None:
    try:
        st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(st.st_mode):
        raise Failed(f"{shown} is a link; Orama will not write through it.")


def _make_dir(parent_fd: int, name: str, shown: PurePosixPath, mode: int) -> int:
    """Open ``name`` under ``parent_fd`` (never through a link), creating it with ``mode``."""
    try:
        os.mkdir(name, mode, dir_fd=parent_fd)
        made = True
    except FileExistsError:
        made = False
    fd = _open_dir(name, str(shown), dir_fd=parent_fd)
    if made:
        os.fchmod(fd, mode)
    return fd


def _existing_setup(state_fd: int, shown: PurePosixPath) -> tuple[str, DesiredSetup | None]:
    """The saved setup, else the earlier draft, else nothing (the draft name)."""
    for name in (SETUP_FILE, DRAFT_FILE):
        # Orama (PUID) can write this file and we run as root: never follow a link, never
        # block on a FIFO, read only a regular file, and only so much of it.
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
        try:
            fd = os.open(name, flags, dir_fd=state_fd)
        except FileNotFoundError:
            continue
        except OSError as e:
            if e.errno == errno.ELOOP:
                raise Failed(f"{shown / name} is a link; Orama will not read it.") from None
            raise Failed(f"Cannot read {shown / name} ({e.strerror}).") from None
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise Failed(f"{shown / name} is not a regular file; move it away and run again.")
        with os.fdopen(fd, "rb") as f:
            try:
                raw = f.read(SETUP_LIMIT + 1)
            except OSError as e:
                raise Failed(f"Cannot read {shown / name} ({e.strerror}).") from None
        if len(raw) > SETUP_LIMIT:
            raise Failed(f"{shown / name} is too large to be a setup; fix or remove it.")
        try:
            return name, DesiredSetup.model_validate_json(raw)
        except ValueError:
            raise Failed(
                f"{shown / name} is not a valid setup; fix or remove it and run again."
            ) from None
    return DRAFT_FILE, None


def _write_atomic(
    dir_fd: int,
    name: str,
    data: bytes,
    mode: int,
    *,
    owner: tuple[int, int] | None = None,
    chown: Chown | None = None,
) -> None:
    """Write via a fresh temp file in the same folder, owner and mode set before the rename."""
    tmp = f".{name}.{secrets.token_hex(8)}.tmp"
    fd = os.open(tmp, _NEW_FILE_FLAGS, mode, dir_fd=dir_fd)
    try:
        try:
            os.fchmod(fd, mode)
            if owner is not None and chown is not None:
                chown(fd, *owner)
            _write_all(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except BaseException:
        try:
            os.unlink(tmp, dir_fd=dir_fd)
        except FileNotFoundError:
            pass
        raise


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


# ---------------------------------------------------------------- apply

def apply(setup_path: Path) -> int:
    """Apply the saved setup through the Agent, wire the apps; print a checklist.

    Returns 0, or 1 when a wiring step failed.
    """
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")
    try:
        setup = DesiredSetup.load(setup_path)
    except FileNotFoundError:
        raise Failed(f"No setup found at {setup_path}.") from None
    except (OSError, ValueError):
        raise Failed(f"The setup at {setup_path} cannot be read.") from None

    store = SecretStore(Path(orama_state_dir(setup.config_root)) / SECRETS_FILE)
    install_redaction(store)
    apps = enabled_apps(setup)
    client = AgentClient(os.environ.get("ORAMA_AGENT_SOCKET", DEFAULT_SOCKET))
    try:
        others = {p for a, p in setup.ports.items() if a not in apps}
        ports = assign_ports(apps, previous=setup.ports, taken=others)
        setup = setup.model_copy(update={"ports": {**setup.ports, **ports}})
        ensure_app_keys(
            store, apps, gluetun_proxy=setup.vpn is not None and setup.vpn.route_prowlarr
        )
        folders = [str(p) for p in data_folders(setup.data_root)]
        folders += [str(app_config_dir(setup.config_root, a)) for a in apps]
        client.ensure_folders(folders)
        print("✓ Folders ready", flush=True)
        changed = client.apply(build_apps_compose(setup), env_files_for(setup, store))
        setup.save(setup_path)
        for app in changed:
            print(f"✓ {app} started", flush=True)
        # After the deploy: the apps first write their own default config files.
        seeded = _seed_logins(client, store, apps)
        for app in seeded:
            print(f"✓ {display_name(app)}: login details set", flush=True)
        forwarding = setup.vpn is not None and setup.vpn.port_forwarding
        if "qbittorrent" in seeded and forwarding:
            # Remembered until both restarts worked, so a failed one is tried again next apply.
            store.set(RESTART_PENDING, "1")
        if "qbittorrent" in apps and forwarding and store.get(RESTART_PENDING) == "1":
            # Gluetun's port-forward hook logs in to qBittorrent; run it again with the new login.
            client.restart("gluetun")
            client.restart("qbittorrent")
            store.set(RESTART_PENDING, "")
    except AgentError as e:
        raise Failed(e.message) from None
    except MissingSecret as e:  # named in plain words, never shown
        if e.secret_name == VPN_KEY_NAME:
            raise Failed(
                "The VPN key has not been entered yet. VPN setup arrives with the setup "
                "page; turn the VPN off for now."
            ) from None
        raise Failed(
            f"Orama could not find the key it keeps for {e.app}. Run the installer again "
            "to recreate it."
        ) from None
    except ValueError as e:  # e.g. the secret store cannot be decrypted; messages hold no values
        raise Failed(f"Cannot apply: {e}.") from None
    except OSError as e:
        raise Failed(f"Cannot use Orama's files ({e.strerror or type(e).__name__}).") from None
    results = run_steps(build_steps(setup, store, Http()))
    lines = format_checklist(results)
    if lines == ["✓ Nothing to change"] and (changed or seeded):
        lines = []  # the deploy or the login details already reported a change
    for line in lines:
        print(line)
    return 1 if any(r.status == "failed" for r in results) else 0


def _seed_logins(client: AgentClient, store: SecretStore, apps: list[str]) -> list[str]:
    """Give SABnzbd and qBittorrent the keys Orama holds; the apps that changed."""
    seeded = []
    if "sabnzbd" in apps:
        values = {
            "api_key": _required(store, "apikey:sabnzbd", "sabnzbd"),
            "nzb_key": _required(store, SABNZBD_NZB_KEY_NAME, "sabnzbd"),
        }
        if client.seed_config("sabnzbd", values):
            seeded.append("sabnzbd")
    if "qbittorrent" in apps:
        values = {"password_pbkdf2": _qbittorrent_hash(store)}
        if client.seed_config("qbittorrent", values):
            seeded.append("qbittorrent")
    return seeded


def _required(store: SecretStore, name: str, app: str) -> str:
    value = store.get(name)
    if value is None:
        raise MissingSecret(name, app)
    return value


def _qbittorrent_hash(store: SecretStore) -> str:
    """The stored hash of qBittorrent's password, made once so a re-run sends the same value.

    A new salt only when there is no hash yet, or the password changed since it was made.
    """
    password = _required(store, QBITTORRENT_PASSWORD_NAME, "qbittorrent")
    kept = store.get(QBITTORRENT_HASH_NAME)
    if kept is not None:
        try:
            salt = base64.b64decode(kept.removeprefix("@ByteArray(").split(":", 1)[0], validate=True)
            if qbittorrent_pbkdf2(password, salt) == kept:
                return kept
        except ValueError:
            pass
    fresh = qbittorrent_pbkdf2(password)
    store.set(QBITTORRENT_HASH_NAME, fresh)
    return fresh


# ---------------------------------------------------------------- set-secret

def set_secret(name: str, setup_path: Path) -> None:
    """Store one secret, read from stdin, next to the setup file. Never shows the value."""
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")
    if not SECRET_NAME.fullmatch(name):  # never echoed: it may be a mistyped secret
        raise Failed(
            "That is not a secret name Orama knows. Use vpn:wireguard_private_key, "
            "usenet:<server>:password or indexer:<indexer>:<field>."
        )
    if not setup_path.is_file():
        raise Failed(f"No setup found at {setup_path}.")
    store = SecretStore(setup_path.parent / SECRETS_FILE)
    install_redaction(store)
    value = _read_secret()
    # The only secret typed by the user that goes into an Agent env file; the Agent accepts
    # only ENV_FILE_VALUE there, so refuse anything else now rather than at apply.
    if name == VPN_KEY_NAME and not ENV_FILE_VALUE.fullmatch(value):
        raise Failed(
            "The VPN key should look like the one your VPN provider gave you: letters, "
            "digits, + / =. Nothing was saved."
        )
    try:
        store.set(name, value)
    except ValueError as e:  # e.g. the store cannot be decrypted; messages hold no values
        raise Failed(f"Cannot save the secret: {e}.") from None
    except OSError as e:
        raise Failed(f"Cannot use Orama's files ({e.strerror or type(e).__name__}).") from None
    print(f"✓ Saved {name}")


def _read_secret() -> str:
    """One line from stdin without its line ending; no echo when stdin is a terminal."""
    try:
        if sys.stdin.isatty():
            text = getpass.getpass("Value (not shown): ")
        else:
            text = sys.stdin.read(SECRET_LIMIT + 3)  # room for a line ending, then one more
    except (EOFError, KeyboardInterrupt):
        raise Failed("Cancelled; nothing was saved.") from None
    except (OSError, UnicodeDecodeError):
        raise Failed("Cannot read the value from the input; nothing was saved.") from None
    text = text.removesuffix("\n").removesuffix("\r")
    if not text:
        raise Failed("No value was given; nothing was saved.")
    if len(text) > SECRET_LIMIT:
        raise Failed(f"The value is too long (over {SECRET_LIMIT} characters); nothing was saved.")
    if any(c in text for c in "\n\r\x00"):
        raise Failed("The value must be one line of text; nothing was saved.")
    return text


if __name__ == "__main__":
    sys.exit(main())
