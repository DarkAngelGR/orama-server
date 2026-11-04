"""What ``seed-config`` may write into SABnzbd's and qBittorrent's own config files.

SABnzbd and qBittorrent cannot take their credentials from the environment, so the
Agent writes them into the apps' config files. Only the keys listed here are ever
written, each value is checked against a fixed pattern, and the keys that make the apps
reachable from Orama are fixed by the Agent itself, never taken from a request.

Patching is line based: a ``key = value`` line (SABnzbd, configobj) or ``key=value`` line
(qBittorrent, Qt ini) inside the named section is replaced, and a missing section or key
is appended. Every other line is kept byte for byte. Refusal messages never contain
values: they may be secrets.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from orama.catalog import QBITTORRENT_WEBUI_USER
from orama_agent.validate import Refused


@dataclass(frozen=True)
class _Format:
    section: str
    separator: str  # what a written line puts between key and value
    file: tuple[str, ...]  # path inside the app's config folder
    allowed: Mapping[str, tuple[str, re.Pattern[str]]]  # request key -> (file key, pattern)
    fixed: Mapping[str, str]  # file key -> value, supplied by the Agent
    quoted: frozenset[str] = frozenset()  # file keys written as "value"


_HEX32 = re.compile(r"[0-9a-f]{32}")
_QT_PBKDF2 = re.compile(r"@ByteArray\([A-Za-z0-9+/]{22}==:[A-Za-z0-9+/]{86}==\)")

FORMATS: dict[str, _Format] = {
    "sabnzbd": _Format(
        section="misc",
        separator=" = ",
        file=("sabnzbd.ini",),
        allowed={"api_key": ("api_key", _HEX32), "nzb_key": ("nzb_key", _HEX32)},
        fixed={"host_whitelist": "sabnzbd, gluetun, localhost"},
    ),
    # qBittorrent 5.x keeps WebUI settings in their own [WebUI] section (its settings
    # migration 12 moves them out of [Preferences] on first start, and 5.3 reads only
    # [WebUI]). Checked against the pinned 5.3.0 image.
    "qbittorrent": _Format(
        section="WebUI",
        separator="=",
        file=("qBittorrent", "qBittorrent.conf"),
        allowed={"password_pbkdf2": ("Password_PBKDF2", _QT_PBKDF2)},
        fixed={
            "Username": QBITTORRENT_WEBUI_USER,
            # Never off: Gluetun's proxy shares qBittorrent's network, so a request through
            # it arrives from localhost. The port-forward hook logs in instead.
            "LocalHostAuth": "true",
            "HostHeaderValidation": "false",
        },
        # qBittorrent itself writes the hash quoted; matching it keeps a re-run unchanged.
        quoted=frozenset({"Password_PBKDF2"}),
    ),
}


def config_file(app: str) -> tuple[str, ...]:
    """The config file's path inside ``app_config_dir(config_root, app)``, as parts."""
    return _format(app).file


def check(app: Any, values: Any) -> dict[str, str]:
    """``{file key: value}`` to write: the request's values plus the fixed keys.

    Refuses an unknown app, a missing or unknown key, or a value of the wrong shape.
    """
    fmt = _format(app)
    if not isinstance(values, dict) or set(values) != set(fmt.allowed):
        names = " and ".join(sorted(fmt.allowed))
        raise Refused(f"For {app} Orama must send exactly these settings: {names}.")
    wanted: dict[str, str] = {}
    for name, (key, pattern) in fmt.allowed.items():
        value = values[name]
        if not isinstance(value, str) or not pattern.fullmatch(value):
            raise Refused(f"The {app} setting {name} does not have the expected form.")
        wanted[key] = value
    wanted.update(fmt.fixed)
    return wanted


def render(app: str, current_text: str | None, values: Any) -> str | None:
    """The file's new text, or ``None`` when it already holds every wanted value."""
    fmt = _format(app)
    wanted = check(app, values)
    lines = _lines(current_text or "")
    start, end = _section(lines, fmt.section)
    changed = False
    missing = dict(wanted)
    if start is not None:
        for i in range(start + 1, end):
            key, value = _key_value(lines[i])
            if key not in missing:
                continue
            want = missing.pop(key)
            if _normal(value) != _normal(want):
                ending = lines[i][len(lines[i].rstrip("\r\n")):] or "\n"
                lines[i] = _line(fmt, key, want, ending)
                changed = True
    if missing:
        new = [_line(fmt, key, value, "\n") for key, value in missing.items()]
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        if start is None:
            if lines and lines[-1].strip():
                lines.append("\n")
            lines += [f"[{fmt.section}]\n", *new]
        else:
            at = end
            while at > start + 1 and not lines[at - 1].strip():
                at -= 1  # after the section's last entry, before its trailing blank lines
            lines[at:at] = new
        changed = True
    return "".join(lines) if changed else None


def _format(app: Any) -> _Format:
    if not isinstance(app, str) or app not in FORMATS:
        raise Refused("Orama can only set up the config of SABnzbd and qBittorrent.")
    return FORMATS[app]


def _lines(text: str) -> list[str]:
    """Lines with their endings; split on ``\\n`` only, so joining gives the text back."""
    parts = text.split("\n")
    lines = [p + "\n" for p in parts[:-1]]
    return lines + [parts[-1]] if parts[-1] else lines


def _section(lines: list[str], name: str) -> tuple[int | None, int]:
    """Index of the ``[name]`` header and of the line after its body (first match)."""
    for i, line in enumerate(lines):
        if line.strip() == f"[{name}]":
            for j in range(i + 1, len(lines)):
                if lines[j].lstrip().startswith("["):
                    return i, j
            return i, len(lines)
    return None, len(lines)


def _key_value(line: str) -> tuple[str | None, str]:
    text = line.strip()
    if not text or text[0] in "#;[":
        return None, ""
    key, sep, value = text.partition("=")
    return (key.strip(), value.strip()) if sep else (None, "")


def _normal(value: str) -> list[str]:
    """A value as both apps read it back: unquoted, list items trimmed (``a, b`` = ``a,b``)."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return [part.strip() for part in value.split(",") if part.strip()]


def _line(fmt: _Format, key: str, value: str, ending: str) -> str:
    shown = f'"{value}"' if key in fmt.quoted else value
    return f"{key}{fmt.separator}{shown}{ending}"
