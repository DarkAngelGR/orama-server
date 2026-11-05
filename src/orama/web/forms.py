"""The setup sections the wizard and Settings share.

Each section has ``read_<section>(setup, store)`` (what the page shows; a secret only as
``... _saved: bool``), ``echo_<section>(setup, form, store)`` (the same, with what was just
typed, to show a page again after a mistake) and ``apply_<section>(setup, form, store)``
(the changed setup, or :class:`Invalid` with one plain sentence per field).

A section writes to the secret store only after the whole section has validated, and an
empty secret input keeps the stored value. Nothing here ever returns a secret.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from ipaddress import AddressValueError, IPv4Interface
from typing import Any

from pydantic import ValidationError

from orama.catalog import ENV_FILE_VALUE, VPN_PROVIDERS
from orama.cli import JELLYFIN_USER_VALUE
from orama.secrets import (
    JELLYFIN_PASSWORD_NAME,
    JELLYFIN_USER_NAME,
    VPN_KEY_NAME,
    SecretStore,
)
from orama.setup_model import DesiredSetup, IndexerChoice, UsenetServer, VpnConfig
from orama.web.catalogue import IndexerDefinition, IndexerField
from orama.wiring import prowlarr, sabnzbd

OPENSUBTITLES_USER_NAME = "opensubtitles:username"
OPENSUBTITLES_PASSWORD_NAME = "opensubtitles:password"

MIN_JELLYFIN_PASSWORD = 12
MAX_SECRET_TEXT = 256
MAX_FOLDER_TEXT = 1024
SUBTITLE_SLOTS = 5

DEVICES = (
    ("samsung_s95f", "Samsung S95F TV"),
    ("lg", "LG TV"),
    ("apple_tv", "Apple TV"),
    ("nvidia_shield", "NVIDIA Shield"),
    ("safest", "I am not sure — pick the safest"),
)
QUALITIES = (
    ("4k", "4K when possible"),
    ("1080p", "1080p — smaller files"),
    ("mixed", "A mix — 4K for favourites, 1080p for the rest"),
)
# ISO 639-1 codes, written the way people say them.
LANGUAGES = (
    ("ar", "Arabic"), ("bg", "Bulgarian"), ("zh", "Chinese"), ("hr", "Croatian"),
    ("cs", "Czech"), ("da", "Danish"), ("nl", "Dutch"), ("en", "English"),
    ("fi", "Finnish"), ("fr", "French"), ("de", "German"), ("el", "Greek"),
    ("he", "Hebrew"), ("hi", "Hindi"), ("hu", "Hungarian"), ("id", "Indonesian"),
    ("it", "Italian"), ("ja", "Japanese"), ("ko", "Korean"), ("no", "Norwegian"),
    ("fa", "Persian"), ("pl", "Polish"), ("pt", "Portuguese"), ("ro", "Romanian"),
    ("ru", "Russian"), ("sr", "Serbian"), ("sk", "Slovak"), ("es", "Spanish"),
    ("sv", "Swedish"), ("th", "Thai"), ("tr", "Turkish"), ("uk", "Ukrainian"),
    ("vi", "Vietnamese"),
)
_LANGUAGE_NAMES = dict(LANGUAGES)


class Invalid(Exception):
    """A section was not accepted. ``errors`` maps a form field to one plain sentence."""

    def __init__(self, errors: dict[str, str]) -> None:
        super().__init__("; ".join(errors.values()))
        self.errors = errors


# ---------------------------------------------------------------- helpers


def _text(form: Mapping[str, str], name: str) -> str:
    value = form.get(name, "")
    return value.strip() if isinstance(value, str) else ""


def _on(form: Mapping[str, str], name: str) -> bool:
    return form.get(name, "") not in ("", "0", "off", "false")


def _printable_line(value: str) -> bool:
    return value.isprintable() and len(value) <= MAX_SECRET_TEXT


def _rebuild(setup: DesiredSetup, **updates: Any) -> DesiredSetup:
    """``setup`` with ``updates``, checked by the model's own rules (ValidationError)."""
    data = setup.to_json_dict()
    data.update(updates)
    return DesiredSetup.model_validate(data)


def _model_messages(e: ValidationError, default_field: str) -> dict[str, str]:
    """The model's own refusals, in plain words, keyed by the form field they belong to."""
    errors: dict[str, str] = {}
    for err in e.errors():
        text = str(err["msg"]).removeprefix("Value error, ")
        lowered = text.lower()
        if "usenet servers require" in lowered:
            field = "download_methods"
            text = ("You added Usenet servers earlier. Keep Usenet on, or remove those "
                    "servers first.")
        elif "indexer" in lowered or "usenet server" in lowered:
            field = default_field
        else:
            field = default_field
        text = text[:1].upper() + text[1:]
        errors.setdefault(field, text.rstrip(".") + ".")
    return errors


# ---------------------------------------------------------------- indexers and Usenet


def _new_slug(name: str, fallback: str, used: set[str], pending_prefix: str, store) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40].rstrip("-") or fallback
    slug = base
    suffix = 2
    while slug in used or store.get(pending_prefix + slug):
        tail = f"-{suffix}"
        slug = base[:40 - len(tail)].rstrip("-") + tail
        suffix += 1
    return slug


def _indexer_value(field: IndexerField, raw: str) -> str | int | bool:
    if field.kind == "checkbox":
        return raw in ("1", "on", "true")
    if field.kind == "number":
        return int(raw)
    if field.kind == "select":
        for value, _ in field.options:
            if str(value) == raw:
                return value
        raise ValueError
    return raw


def add_indexer(setup: DesiredSetup, definition: IndexerDefinition,
                form: Mapping[str, str], store) -> tuple[DesiredSetup, str]:
    if definition.protocol not in setup.download_methods:
        raise Invalid({"definition": "Choose an indexer for a download method you turned on."})
    name = _text(form, "name") or definition.name
    slug = _new_slug(name, "indexer", {item.slug for item in setup.indexers},
                     prowlarr.REMOVE_PREFIX, store)
    fields: dict[str, str | int | bool] = {}
    secrets: dict[str, str] = {}
    errors: dict[str, str] = {}
    for field in definition.fields:
        key = f"field:{field.name}"
        if field.kind == "info" or key not in form and field.kind != "checkbox":
            continue
        raw = form.get(key, "")
        if field.kind == "secret":
            if raw and not _printable_line(raw):
                errors[key] = "Enter a shorter value on one line."
            elif raw:
                secrets[field.name] = raw
            continue
        if not raw and field.kind != "checkbox":
            continue  # Leave the Prowlarr template default in place.
        try:
            fields[field.name] = _indexer_value(field, raw)
        except ValueError:
            errors[key] = "Enter a whole number." if field.kind == "number" else "Choose an option."
    if errors:
        raise Invalid(errors)
    try:
        item = IndexerChoice(slug=slug, name=name, definition=definition.definition,
                             fields=fields, secret_fields=tuple(secrets))
        updated = _rebuild(setup, indexers=(*setup.indexers, item))
    except ValidationError:
        raise Invalid({"name": "Check the indexer details and use a different name."}) from None
    for field_name, value in secrets.items():
        store.set(f"indexer:{slug}:{field_name}", value)
    return updated, slug


def add_usenet_server(setup: DesiredSetup, form: Mapping[str, str],
                      store) -> tuple[DesiredSetup, str]:
    if "usenet" not in setup.download_methods:
        raise Invalid({"host": "Turn on Usenet before adding a server."})
    host = _text(form, "host")
    password = form.get("password", "")
    if password and not _printable_line(password):
        raise Invalid({"password": "Enter a shorter password on one line."})
    slug = _new_slug(host, "usenet", {server.slug for server in setup.usenet_servers},
                     sabnzbd.REMOVE_PREFIX, store)
    try:
        server = UsenetServer(slug=slug, host=host, port=_text(form, "port") or "563",
                              ssl=_on(form, "ssl"), connections=_text(form, "connections") or "20",
                              username=_text(form, "username"))
        updated = _rebuild(setup, usenet_servers=(*setup.usenet_servers, server))
    except ValidationError:
        raise Invalid({"host": "Check the server address, port and number of connections."}) from None
    if password:
        store.set(f"usenet:{slug}:password", password)
    return updated, slug


def remove_indexer(setup: DesiredSetup, slug: str, store) -> DesiredSetup:
    item = next((item for item in setup.indexers if item.slug == slug), None)
    if item is None:
        return setup
    updated = _rebuild(setup, indexers=tuple(other for other in setup.indexers if other != item))
    if store.get(prowlarr.sent_marker_name(f"indexer:{slug}")):
        store.set(prowlarr.REMOVE_PREFIX + slug, item.name)
    for field_name in item.secret_fields:
        store.delete(f"indexer:{slug}:{field_name}")
    store.delete(f"test-ok:indexer:{slug}")
    return updated


def remove_usenet_server(setup: DesiredSetup, slug: str, store) -> DesiredSetup:
    remaining = tuple(server for server in setup.usenet_servers if server.slug != slug)
    if len(remaining) == len(setup.usenet_servers):
        return setup
    updated = _rebuild(setup, usenet_servers=remaining)
    if store.get(sabnzbd.sent_marker_name(slug)):
        store.set(sabnzbd.REMOVE_PREFIX + slug, slug)
    store.delete(f"usenet:{slug}:password")
    store.delete(f"test-ok:usenet:{slug}")
    return updated


# ---------------------------------------------------------------- media


def read_media(setup: DesiredSetup, store: SecretStore) -> dict[str, Any]:
    return {
        "plex": "plex" in setup.media_servers,
        "jellyfin": "jellyfin" in setup.media_servers,
        "user_saved": store.get(JELLYFIN_USER_NAME) is not None,
        "password_saved": store.get(JELLYFIN_PASSWORD_NAME) is not None,
    }


def echo_media(setup: DesiredSetup, form: Mapping[str, str], store: SecretStore) -> dict[str, Any]:
    values = read_media(setup, store)
    values.update(plex=_on(form, "server_plex"), jellyfin=_on(form, "server_jellyfin"),
                  user=_text(form, "jellyfin_user"))
    return values


def apply_media(setup: DesiredSetup, form: Mapping[str, str], store: SecretStore) -> DesiredSetup:
    errors: dict[str, str] = {}
    servers = {name for name in ("plex", "jellyfin") if _on(form, f"server_{name}")}
    if not servers:
        errors["media_servers"] = "Choose at least one: Plex or Jellyfin."
    user = password = ""
    if "jellyfin" in servers:
        user = _text(form, "jellyfin_user")
        password = form.get("jellyfin_password", "")
        if not user:
            if store.get(JELLYFIN_USER_NAME) is None:
                errors["jellyfin_user"] = "Choose a name for the Jellyfin administrator."
        elif not JELLYFIN_USER_VALUE.fullmatch(user):
            errors["jellyfin_user"] = (
                "The name may use only letters, digits and . _ @ - (up to 64 characters)."
            )
        if not password:
            if store.get(JELLYFIN_PASSWORD_NAME) is None:
                errors["jellyfin_password"] = (
                    f"Choose a Jellyfin password of at least {MIN_JELLYFIN_PASSWORD} characters."
                )
        elif not MIN_JELLYFIN_PASSWORD <= len(password) <= MAX_SECRET_TEXT:
            errors["jellyfin_password"] = (
                f"Use a password between {MIN_JELLYFIN_PASSWORD} and {MAX_SECRET_TEXT} "
                "characters long."
            )
        elif not password.isprintable():
            errors["jellyfin_password"] = "The password may not contain line breaks."
    if errors:
        raise Invalid(errors)
    try:
        result = _rebuild(setup, media_servers=sorted(servers))
    except ValidationError as e:
        raise Invalid(_model_messages(e, "media_servers")) from None
    if user:
        store.set(JELLYFIN_USER_NAME, user)
    if password:
        store.set(JELLYFIN_PASSWORD_NAME, password)
    return result


# ---------------------------------------------------------------- folders


def read_folders(setup: DesiredSetup, store: SecretStore) -> dict[str, Any]:
    return {"data_root": str(setup.data_root), "config_root": str(setup.config_root)}


def echo_folders(
    setup: DesiredSetup, form: Mapping[str, str], store: SecretStore
) -> dict[str, Any]:
    values = read_folders(setup, store)
    values["data_root"] = str(form.get("data_root", ""))[:MAX_FOLDER_TEXT]
    return values


def _folder_message(e: ValidationError, config_root: str) -> str:
    for err in e.errors():
        text = str(err["msg"])
        if "same or nested" in text:
            return (f"That folder is inside the Orama folder ({config_root}), or the Orama "
                    "folder is inside it. Choose a different folder.")
        if "absolute" in text:
            return "Write the whole path, starting with /, like /mnt/media."
        if '":"' in text:
            return "The folder path cannot contain a colon (:). Choose a different folder."
        if "'..'" in text:
            return "Leave .. out of the folder path."
        if "'/'" in text:
            return "Choose a folder for your media, not the top of the disk (/)."
    return "That folder cannot be used. Write a full path, like /mnt/media."


def apply_folders(
    setup: DesiredSetup, form: Mapping[str, str], store: SecretStore
) -> DesiredSetup:
    value = _text(form, "data_root")
    if not value:
        raise Invalid({"data_root": "Type the folder where your media should live, "
                                    "like /mnt/media."})
    if len(value) > MAX_FOLDER_TEXT or not value.isprintable():
        raise Invalid({"data_root": "That folder path cannot be used. Write a full path, "
                                    "like /mnt/media."})
    try:
        return _rebuild(setup, data_root=value)
    except ValidationError as e:
        raise Invalid({"data_root": _folder_message(e, str(setup.config_root))}) from None


# ---------------------------------------------------------------- downloads


def _provider_rows() -> list[dict[str, Any]]:
    return [
        {"value": p.name, "label": p.label, "port_forwarding": p.port_forwarding,
         "needs_address": p.needs_address}
        for p in VPN_PROVIDERS.values()
    ]


def read_downloads(setup: DesiredSetup, store: SecretStore) -> dict[str, Any]:
    vpn = setup.vpn
    fresh = VpnConfig()
    return {
        "usenet": "usenet" in setup.download_methods,
        "torrent": "torrent" in setup.download_methods,
        "vpn": vpn is not None,
        "provider": (vpn or fresh).provider,
        "providers": _provider_rows(),
        "address": "" if vpn is None or vpn.wireguard_address is None
        else str(vpn.wireguard_address),
        "port_forwarding": vpn.port_forwarding if vpn is not None else fresh.port_forwarding,
        "route_prowlarr": vpn.route_prowlarr if vpn is not None else False,
        "key_saved": store.get(VPN_KEY_NAME) is not None,
    }


def echo_downloads(
    setup: DesiredSetup, form: Mapping[str, str], store: SecretStore
) -> dict[str, Any]:
    values = read_downloads(setup, store)
    values.update(
        usenet=_on(form, "usenet"), torrent=_on(form, "torrent"), vpn=_on(form, "vpn"),
        provider=_text(form, "vpn_provider") if _text(form, "vpn_provider") in VPN_PROVIDERS
        else values["provider"],
        address=_text(form, "vpn_address")[:64],
        port_forwarding=_on(form, "port_forwarding"),
        route_prowlarr=_on(form, "route_prowlarr"),
    )
    return values


def apply_downloads(
    setup: DesiredSetup, form: Mapping[str, str], store: SecretStore
) -> DesiredSetup:
    errors: dict[str, str] = {}
    methods = {name for name in ("usenet", "torrent") if _on(form, name)}
    if not methods:
        errors["download_methods"] = "Choose at least one: Usenet or torrents."
    vpn: VpnConfig | None = None
    new_key = ""
    if _on(form, "vpn"):
        if "torrent" not in methods:
            errors["vpn"] = ("The VPN is used for torrents. Turn torrents on, or turn the "
                             "VPN off.")
        provider = VPN_PROVIDERS.get(_text(form, "vpn_provider"))
        if provider is None:
            errors["vpn_provider"] = "Choose one of the VPN services in the list."
        new_key = _text(form, "vpn_key")
        if not new_key:
            if store.get(VPN_KEY_NAME) is None:
                errors["vpn_key"] = ("Paste the WireGuard private key from your VPN "
                                     "service.")
        elif not ENV_FILE_VALUE.fullmatch(new_key):
            errors["vpn_key"] = ("That does not look like a WireGuard key. It has only "
                                 "letters, digits and + / = and no spaces — copy it again "
                                 "from your VPN service.")
        address = None
        if provider is not None and provider.needs_address:
            text = _text(form, "vpn_address")
            if not text:
                errors["vpn_address"] = (
                    f"{provider.label} needs the Address line from its WireGuard file, "
                    "it ends in /32."
                )
            else:
                try:
                    address = IPv4Interface(text)
                except (AddressValueError, ValueError):
                    errors["vpn_address"] = ("Write the address as it appears in the file, ending in /32, as in "
                                             "the Address line of the WireGuard file.")
        if provider is not None and "vpn_address" not in errors and "vpn_key" not in errors:
            try:
                vpn = VpnConfig(
                    provider=provider.name,
                    # The box is only offered for providers that can forward a port.
                    port_forwarding=provider.port_forwarding and _on(form, "port_forwarding"),
                    route_prowlarr=_on(form, "route_prowlarr"),
                    wireguard_address=address,
                )
            except ValidationError as e:
                errors["vpn_address"] = _model_messages(e, "vpn")["vpn"]
    if errors:
        raise Invalid(errors)
    try:
        result = _rebuild(
            setup,
            download_methods=sorted(methods),
            vpn=None if vpn is None else vpn.model_dump(mode="json"),
        )
    except ValidationError as e:
        raise Invalid(_model_messages(e, "vpn")) from None
    if new_key:
        store.set(VPN_KEY_NAME, new_key)
    return result


# ---------------------------------------------------------------- playback


def read_playback(setup: DesiredSetup, store: SecretStore) -> dict[str, Any]:
    return {"device": setup.device, "quality": setup.quality,
            "devices": DEVICES, "qualities": QUALITIES}


def echo_playback(
    setup: DesiredSetup, form: Mapping[str, str], store: SecretStore
) -> dict[str, Any]:
    values = read_playback(setup, store)
    values.update(device=_text(form, "device"), quality=_text(form, "quality"))
    return values


def apply_playback(
    setup: DesiredSetup, form: Mapping[str, str], store: SecretStore
) -> DesiredSetup:
    errors: dict[str, str] = {}
    device, quality = _text(form, "device"), _text(form, "quality")
    if device not in dict(DEVICES):
        errors["device"] = "Choose the device you watch on most."
    if quality not in dict(QUALITIES):
        errors["quality"] = "Choose the quality you prefer."
    if errors:
        raise Invalid(errors)
    return _rebuild(setup, device=device, quality=quality)


# ---------------------------------------------------------------- subtitles


def _language_options(current: list[str]) -> list[tuple[str, str]]:
    # A code saved some other way is still shown, by its code.
    extra = [(c, c) for c in current if c and c not in _LANGUAGE_NAMES]
    return [*LANGUAGES, *extra]


def read_subtitles(setup: DesiredSetup, store: SecretStore) -> dict[str, Any]:
    chosen = list(setup.subtitle_languages)
    slots = chosen + [""] * (SUBTITLE_SLOTS - len(chosen))
    return {
        "languages": slots[:SUBTITLE_SLOTS],
        "options": _language_options(chosen),
        "account_saved": store.get(OPENSUBTITLES_USER_NAME) is not None
        and store.get(OPENSUBTITLES_PASSWORD_NAME) is not None,
        "username_saved": store.get(OPENSUBTITLES_USER_NAME) is not None,
        "password_saved": store.get(OPENSUBTITLES_PASSWORD_NAME) is not None,
    }


def echo_subtitles(
    setup: DesiredSetup, form: Mapping[str, str], store: SecretStore
) -> dict[str, Any]:
    values = read_subtitles(setup, store)
    slots = [_text(form, f"language_{i}")[:8] for i in range(1, SUBTITLE_SLOTS + 1)]
    values.update(languages=slots, options=_language_options(slots))
    return values


def apply_subtitles(
    setup: DesiredSetup, form: Mapping[str, str], store: SecretStore
) -> DesiredSetup:
    errors: dict[str, str] = {}
    chosen: list[str] = []
    for i in range(1, SUBTITLE_SLOTS + 1):
        code = _text(form, f"language_{i}")
        if not code:
            continue
        if code not in _LANGUAGE_NAMES:
            errors[f"language_{i}"] = "Choose a language from the list."
        elif code in chosen:
            errors[f"language_{i}"] = (
                f"{_LANGUAGE_NAMES[code]} is already in the list. Pick each language once."
            )
        else:
            chosen.append(code)
    if not chosen and not errors:
        errors["language_1"] = "Choose at least one subtitle language."
    user = _text(form, "opensubtitles_username")
    password = form.get("opensubtitles_password", "")
    for name, value in (("opensubtitles_username", user), ("opensubtitles_password", password)):
        if value and not _printable_line(value):
            errors[name] = "That is too long or has a line break. Type it again."
    if not errors:
        have_user = bool(user) or store.get(OPENSUBTITLES_USER_NAME) is not None
        have_password = bool(password) or store.get(OPENSUBTITLES_PASSWORD_NAME) is not None
        if have_user and not have_password:
            errors["opensubtitles_password"] = (
                "Enter the OpenSubtitles password too, or leave both empty."
            )
        elif have_password and not have_user:
            errors["opensubtitles_username"] = (
                "Enter the OpenSubtitles username too, or leave both empty."
            )
    if errors:
        raise Invalid(errors)
    try:
        result = _rebuild(setup, subtitle_languages=chosen)
    except ValidationError as e:
        raise Invalid(_model_messages(e, "language_1")) from None
    if user:
        store.set(OPENSUBTITLES_USER_NAME, user)
    if password:
        store.set(OPENSUBTITLES_PASSWORD_NAME, password)
    return result


# ---------------------------------------------------------------- the sections together

SECTIONS = {
    "media": (read_media, echo_media, apply_media),
    "folders": (read_folders, echo_folders, apply_folders),
    "downloads": (read_downloads, echo_downloads, apply_downloads),
    "playback": (read_playback, echo_playback, apply_playback),
    "subtitles": (read_subtitles, echo_subtitles, apply_subtitles),
}
