"""Read the Prowlarr schemas without carrying field values into the browser."""

from __future__ import annotations

import hashlib
import re
import time
import weakref
from dataclasses import dataclass
from threading import Lock
from typing import Literal

from orama.wiring.http import Http, Unreachable

_URL = "http://prowlarr:9696/api/v1/indexer/schema"
_TTL = 3600
_DEFINITION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
_FIELD = re.compile(r"[A-Za-z0-9_.]{1,64}")
_KINDS = {
    "textbox": "text", "text": "text", "checkbox": "checkbox", "select": "select",
    "number": "number",
}

Kind = Literal["text", "secret", "checkbox", "select", "number", "info"]


class Unavailable(Exception):
    """Prowlarr has no usable schema response yet."""


@dataclass(frozen=True)
class IndexerField:
    name: str
    label: str
    kind: Kind
    help: str = ""
    options: tuple[tuple[int | str, str], ...] = ()


@dataclass(frozen=True)
class IndexerDefinition:
    definition: str
    name: str
    protocol: str
    privacy: str
    fields: tuple[IndexerField, ...]


@dataclass(frozen=True)
class _Snapshot:
    client: weakref.ReferenceType[Http]
    key_digest: bytes
    expires: float
    definitions: tuple[IndexerDefinition, ...]


_cache: _Snapshot | None = None
_cache_lock = Lock()


def _text(value, default: str = "") -> str:
    return value if isinstance(value, str) else default


def _field(raw) -> IndexerField | None:
    if not isinstance(raw, dict) or raw.get("advanced") or raw.get("hidden"):
        return None
    name = _text(raw.get("name"))
    field_type = _text(raw.get("type"))
    if not _FIELD.fullmatch(name) or field_type == "hidden":
        return None
    privacy = _text(raw.get("privacy")).lower()
    kind = "secret" if field_type == "password" or privacy in ("apikey", "password") else (
        _KINDS.get(field_type, "info")
    )
    options = ()
    if kind == "select" and isinstance(raw.get("selectOptions"), list):
        options = tuple(
            (option["value"], option["name"])
            for option in raw["selectOptions"]
            if isinstance(option, dict) and type(option.get("value")) in (str, int)
            and isinstance(option.get("name"), str)
        )
    # Schema values can contain passwords or API keys even when metadata is wrong.
    return IndexerField(name, _text(raw.get("label")) or name, kind,
                        _text(raw.get("helpText")), options)


def _definitions(raw) -> tuple[IndexerDefinition, ...]:
    if not isinstance(raw, list):
        raise Unavailable
    items: dict[str, IndexerDefinition] = {}
    for row in raw:
        if not isinstance(row, dict):
            continue
        definition = _text(row.get("definitionName"))
        protocol = _text(row.get("protocol")).lower()
        name = _text(row.get("name")) or _text(row.get("implementationName")) or definition
        if protocol not in ("usenet", "torrent") or not _DEFINITION.fullmatch(definition):
            continue
        # Match the generic templates that the existing wiring can resolve unambiguously.
        if row.get("implementation") in ("Newznab", "Torznab") and not name.startswith("Generic "):
            continue
        fields = row.get("fields")
        visible = tuple(
            field for raw_field in fields if (field := _field(raw_field)) is not None
        ) if isinstance(fields, list) else ()
        items.setdefault(definition.lower(), IndexerDefinition(
            definition, name, protocol, _text(row.get("privacy")), visible,
        ))
    return tuple(sorted(items.values(), key=lambda item: item.name.casefold()))


def load(store, http: Http, methods: frozenset[str]) -> list[IndexerDefinition]:
    """Return fields for the selected download methods, reusing the schema for an hour."""
    global _cache
    key = store.get("apikey:prowlarr")
    if not key:
        raise Unavailable
    digest = hashlib.sha256(key.encode()).digest()
    now = time.monotonic()
    with _cache_lock:
        snapshot = _cache
    if (snapshot is None or snapshot.client() is not http
            or snapshot.key_digest != digest or now >= snapshot.expires):
        try:
            response = http.request("GET", _URL, headers={"X-Api-Key": key}, timeout=5)
            if response.status != 200:
                raise Unavailable
            definitions = _definitions(response.json())
        except (Unreachable, ValueError):
            raise Unavailable from None
        snapshot = _Snapshot(weakref.ref(http), digest, time.monotonic() + _TTL, definitions)
        with _cache_lock:
            _cache = snapshot
    return [definition for definition in snapshot.definitions if definition.protocol in methods]
