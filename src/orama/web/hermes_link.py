"""Where the Hermes link sends the owner: the mini PC, or the page inside Orama.

The address is read on each visit. The Orama container does not receive ``orama.env``,
so a file in the state folder is what survives an update.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

from hermes.providers import private_server_url

HERMES_URL_FILE = "hermes.url"
_MAX_LEN = 200
_BAD = "Use an address on your home network, like http://mini-pc.local:8787."


def _checked(raw: str) -> str:
    text = raw.strip()
    if not text or len(text) > _MAX_LEN or any(c in text for c in "\r\n\x00"):
        return ""
    try:
        return private_server_url(text)
    except ValueError:
        return ""


def companion_url(state_dir: Path | None) -> str:
    """The mini PC address to open, or "" to keep the Hermes page inside Orama.

    ``HERMES_URL`` on the Orama process wins. Otherwise the saved file is used.
    A public or unreadable address is ignored.
    """
    from_env = _checked(os.environ.get("HERMES_URL", ""))
    if from_env:
        return from_env
    if state_dir is None:
        return ""
    try:
        saved = (Path(state_dir) / HERMES_URL_FILE).read_text(encoding="utf-8")
    except OSError:
        return ""
    return _checked(saved)


def save_companion_url(state_dir: Path, raw: str) -> str | None:
    """Save a home-network address. A blank value removes it. Returns an error sentence."""
    path = Path(state_dir) / HERMES_URL_FILE
    if raw.strip() == "":
        path.unlink(missing_ok=True)
        return None
    url = _checked(raw)
    if not url:
        return "That address is too long." if len(raw) > _MAX_LEN else _BAD
    _write(path, url + "\n")
    return None


def _write(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
