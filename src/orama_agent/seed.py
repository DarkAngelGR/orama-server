"""What ``seed-config`` may write into Bazarr's, SABnzbd's and qBittorrent's config files.

These apps cannot take their credentials from the environment, so the
Agent writes them into the apps' config files. Only the keys listed here are ever
written, each value is checked against a fixed pattern, and the keys that make the apps
reachable from Orama are fixed by the Agent itself, never taken from a request.

Patching is line based: a ``key = value`` line (SABnzbd, configobj) or ``key=value`` line
(qBittorrent, Qt ini) inside the named section is replaced, and a missing section or key
is appended. Bazarr's YAML gets only ``auth.apikey`` patched, and only in the layout
Bazarr itself writes (see ``_render_bazarr``). Every other line is kept byte for byte.
Refusal messages never contain values: they may be secrets.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import yaml

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
    "bazarr": _Format(
        section="auth", separator=": ", file=("config", "config.yaml"),
        allowed={"api_key": ("apikey", _HEX32)}, fixed={},
    ),
    "sabnzbd": _Format(
        section="misc",
        separator=" = ",
        file=("sabnzbd.ini",),
        allowed={"api_key": ("api_key", _HEX32), "nzb_key": ("nzb_key", _HEX32)},
        # SABnzbd logs API kwargs (including API keys and server passwords) when this is on.
        fixed={
            "host_whitelist": "sabnzbd, gluetun, localhost",
            "api_logging": "0",
        },
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
    if app == "bazarr":
        return _render_bazarr(current_text or "", wanted)
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
        raise Refused("Orama can only seed a supported app configuration.")
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


_BAZARR_AUTH_LIKE = re.compile(r"[\"']?auth(?![\w-])")  # a top-level line about ``auth``
_BAZARR_CHILD = re.compile(r"  [^\s#]")  # an entry of the auth block, two-space indented
_BAZARR_APIKEY = re.compile(r"  apikey:(?:[ \t](.*))?")
# apikey values Orama replaces: plain one-word text, or a closed one-line quoted string.
_BAZARR_SIMPLE_VALUE = re.compile(r"[^\s'\"|>&*!{}\[\],#%@`][^\s#]*|'[^'\n]*'|\"[^\"\\\n]*\"")


def _refuse_bazarr(why: str) -> Refused:
    return Refused(f"Bazarr's settings file {why}, so Orama will not change it. "
                   "Fix or remove config/config.yaml in Bazarr's config folder and try again.")


def _load_bazarr(text: str) -> dict[Any, Any]:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        raise _refuse_bazarr("is not valid YAML") from None
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise _refuse_bazarr("is not a set of named settings")
    auth = data.get("auth")
    if auth is not None and not isinstance(auth, dict):
        raise _refuse_bazarr("has an auth section Orama does not recognise")
    return data


def _render_bazarr(current: str, wanted: dict[str, str]) -> str | None:
    """Patch only ``auth.apikey`` in Bazarr's YAML, keeping every other byte.

    Only the layout Bazarr itself writes is patched: one top-level ``auth:`` line whose
    entries are indented by two spaces, and a one-line ``apikey`` value. Anything else
    (flow style, anchors, block scalars, a second ``auth``) is refused before anything
    is stopped or written, and the result must read back with only the key changed.
    """
    key = wanted["apikey"]
    before = _load_bazarr(current)
    if (before.get("auth") or {}).get("apikey") == key:
        return None
    lines = _lines(current)
    starts = []
    for i, line in enumerate(lines):
        if line[:1].isspace() or line.startswith("#") or not _BAZARR_AUTH_LIKE.match(line):
            continue
        if line.rstrip() != "auth:":
            raise _refuse_bazarr("writes its auth section in an unusual way")
        starts.append(i)
    if len(starts) > 1:
        raise _refuse_bazarr("has more than one auth section")
    if not starts:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.extend(["auth:\n", f"  apikey: {key}\n"])
    else:
        start = starts[0]
        end = next((i for i in range(start + 1, len(lines))
                    if lines[i][:1] and not lines[i][0].isspace() and
                    not lines[i].startswith("#")), len(lines))
        found = []
        for i in range(start + 1, end):
            text = lines[i].rstrip("\r\n")
            if not text.strip() or text.lstrip().startswith("#"):
                continue
            if not _BAZARR_CHILD.match(text):
                raise _refuse_bazarr("has auth settings that are not indented by two spaces")
            match = _BAZARR_APIKEY.fullmatch(text)
            if match:
                value = (match.group(1) or "").strip()
                if value and not _BAZARR_SIMPLE_VALUE.fullmatch(value):
                    raise _refuse_bazarr("writes the API key in an unusual way")
                found.append(i)
        if len(found) > 1:
            raise _refuse_bazarr("has more than one API key")
        if found:
            i = found[0]
            ending = lines[i][len(lines[i].rstrip("\r\n")):] or "\n"
            lines[i] = f"  apikey: {key}{ending}"
        else:
            if end and not lines[end - 1].endswith("\n"):
                lines[end - 1] += "\n"
            lines.insert(end, f"  apikey: {key}\n")
    patched = "".join(lines)
    _check_bazarr_round_trip(before, patched, key)
    return patched


def _check_bazarr_round_trip(before: dict[Any, Any], patched: str, key: str) -> None:
    """The patched text must read back with only ``auth.apikey`` changed."""
    try:
        after = _load_bazarr(patched)
    except Refused:
        raise _refuse_bazarr("would not read back correctly after the change") from None
    old_auth = dict(before.get("auth") or {})
    new_auth = dict(after.get("auth") or {})
    old_auth.pop("apikey", None)
    if (new_auth.pop("apikey", None) != key or new_auth != old_auth
            or {k: v for k, v in before.items() if k != "auth"}
            != {k: v for k, v in after.items() if k != "auth"}):
        raise _refuse_bazarr("would not read back correctly after the change")
