"""Desired-setup model: what the owner asked for, validated and persisted as JSON."""

import json
import os
import tempfile
from pathlib import Path, PurePath, PurePosixPath
from typing import Annotated, Any, Literal, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    Field,
    PlainSerializer,
    PlainValidator,
    StrictBool,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)


class VpnConfig(BaseModel):
    """VPN settings. The key itself lives in SecretStore, never here."""

    provider: str = "protonvpn"
    port_forwarding: bool = True
    route_prowlarr: bool = False


def _validate_root(value: Any) -> PurePosixPath:
    if isinstance(value, PurePath):
        value = value.as_posix()
    if not isinstance(value, str):
        raise TypeError("must be a path string")
    if not value.startswith("/"):
        raise ValueError("must be an absolute path")
    if "\x00" in value:
        raise ValueError("must not contain NUL")
    if ":" in value:
        raise ValueError('must not contain ":" (Docker cannot mount such a folder)')
    path = PurePosixPath(value)
    if ".." in path.parts:
        raise ValueError("must not contain '..'")
    if path == PurePosixPath("/"):
        raise ValueError("must not be '/'")
    return path


Root = Annotated[PurePosixPath, PlainValidator(_validate_root), PlainSerializer(str)]

MAX_FIELDS = 40
MAX_FIELD_VALUE_LEN = 512

Slug = Annotated[str, Field(pattern=r"^[a-z0-9-]{1,40}$")]
# A Prowlarr field name; also the last part of the store name ``indexer:<slug>:<field>``.
FieldName = Annotated[str, Field(pattern=r"^[A-Za-z0-9_.]{1,64}$")]
_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_HOSTNAME = rf"^{_LABEL}(?:\.{_LABEL})*$"


def _printable(value: str) -> str:
    # Shown in logs and messages: no control characters, nothing that redraws a terminal.
    if not value.isprintable():
        raise ValueError("must contain only printable characters")
    return value


def _printable_value(value: Any) -> Any:
    if isinstance(value, str):
        if len(value) > MAX_FIELD_VALUE_LEN:
            raise ValueError(f"must be at most {MAX_FIELD_VALUE_LEN} characters")
        _printable(value)
    return value


FieldValue = Annotated[StrictStr | StrictBool | StrictInt, AfterValidator(_printable_value)]


class IndexerChoice(BaseModel):
    """An indexer Orama adds to Prowlarr. Secret field values live in SecretStore."""

    slug: Slug
    definition: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
    name: Annotated[str, Field(min_length=1, max_length=60), AfterValidator(_printable)]
    fields: dict[FieldName, FieldValue] = Field(default={}, max_length=MAX_FIELDS)
    secret_fields: tuple[FieldName, ...] = Field(default=(), max_length=MAX_FIELDS)

    @model_validator(mode="after")
    def _check_secret_fields(self) -> Self:
        if len(set(self.secret_fields)) != len(self.secret_fields):
            raise ValueError("secret field names must be unique")
        if set(self.secret_fields) & set(self.fields):
            raise ValueError("a field cannot be both secret and plain")
        return self


class UsenetServer(BaseModel):
    """A Usenet server for SABnzbd. Its password lives in SecretStore."""

    slug: Slug
    host: str = Field(max_length=253, pattern=_HOSTNAME)
    port: int = Field(default=563, ge=1, le=65535)
    ssl: bool = True
    connections: int = Field(default=20, ge=1, le=100)
    username: Annotated[str, Field(max_length=256), AfterValidator(_printable)] = ""


class DesiredSetup(BaseModel):
    media_servers: frozenset[Literal["plex", "jellyfin"]]
    download_methods: frozenset[Literal["usenet", "torrent"]]
    vpn: VpnConfig | None = None
    data_root: Root
    config_root: Root
    puid: int = Field(ge=0)
    pgid: int = Field(ge=0)
    render_gid: int | None = None
    nvidia: bool = False
    timezone: str = "Etc/UTC"
    ports: dict[str, int] = {}
    removed_apps: frozenset[str] = frozenset()
    indexers: tuple[IndexerChoice, ...] = ()
    usenet_servers: tuple[UsenetServer, ...] = ()

    @field_validator("media_servers", "download_methods")
    @classmethod
    def _not_empty(cls, v: frozenset) -> frozenset:
        if not v:
            raise ValueError("at least one is required")
        return v

    @model_validator(mode="after")
    def _check_consistency(self) -> Self:
        if self.vpn is not None and "torrent" not in self.download_methods:
            raise ValueError("a VPN requires the torrent download method")
        if self.vpn is not None and "gluetun" in self.removed_apps:
            raise ValueError(
                "the VPN container cannot be removed while the VPN is on; "
                "turn the VPN off instead"
            )
        for label, items in (("indexer", self.indexers), ("usenet server", self.usenet_servers)):
            slugs = [item.slug for item in items]
            if len(set(slugs)) != len(slugs):
                raise ValueError(f"{label} slugs must be unique")
        if self.usenet_servers and "usenet" not in self.download_methods:
            raise ValueError("usenet servers require the usenet download method")
        d, c = self.data_root, self.config_root
        if d == c or d.is_relative_to(c) or c.is_relative_to(d):
            raise ValueError("data_root and config_root must not be the same or nested")
        return self

    def to_json_dict(self) -> dict[str, Any]:
        data = self.model_dump(mode="json")
        for key in ("media_servers", "download_methods", "removed_apps"):
            data[key] = sorted(data[key])
        return data

    def save(self, path: Path | str) -> None:
        """Write deterministic JSON atomically (temp file in the same dir + os.replace)."""
        path = Path(path)
        text = json.dumps(self.to_json_dict(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    @classmethod
    def load(cls, path: Path | str) -> Self:
        return cls.model_validate_json(Path(path).read_text(encoding="utf-8"))


def enabled_apps(setup: DesiredSetup) -> list[str]:
    """Ordered catalogue service names that should run."""
    apps = [m for m in ("plex", "jellyfin") if m in setup.media_servers]
    apps += ["sonarr", "radarr", "prowlarr", "bazarr"]
    if "usenet" in setup.download_methods:
        apps.append("sabnzbd")
    if "torrent" in setup.download_methods:
        if setup.vpn is not None:
            apps.append("gluetun")
        apps.append("qbittorrent")
    return [a for a in apps if a not in setup.removed_apps]
