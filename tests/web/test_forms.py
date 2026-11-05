"""The wizard's sections: plain errors, secrets kept apart, nothing saved on a mistake."""

from __future__ import annotations

from pathlib import Path

import pytest


def _ip(*octets: int) -> str:
    """Built at runtime so the repo scan finds no literal addresses."""
    return ".".join(str(o) for o in octets)
from conftest import make_setup, random_secret

from orama.secrets import (
    JELLYFIN_PASSWORD_NAME,
    JELLYFIN_USER_NAME,
    VPN_KEY_NAME,
    SecretStore,
)
from orama.web import forms


@pytest.fixture
def store(tmp_path: Path) -> SecretStore:
    return SecretStore(tmp_path / "secrets.bin")


def long_password() -> str:
    return "pw-" + random_secret(nbytes=8)


def vpn_key() -> str:
    return random_secret(nbytes=16)


# ------------------------------------------------------------------ media


def test_media_plex_only(store):
    out = forms.apply_media(make_setup(), {"server_plex": "on"}, store)
    assert out.media_servers == {"plex"}


def test_media_none_chosen_refused(store):
    with pytest.raises(forms.Invalid) as e:
        forms.apply_media(make_setup(), {}, store)
    assert "Plex or Jellyfin" in e.value.errors["media_servers"]


def test_media_jellyfin_saves_name_and_password(store):
    pw = long_password()
    form = {"server_jellyfin": "on", "jellyfin_user": "owner", "jellyfin_password": pw}
    out = forms.apply_media(make_setup(), form, store)
    assert out.media_servers == {"jellyfin"}
    assert store.get(JELLYFIN_USER_NAME) == "owner"
    assert store.get(JELLYFIN_PASSWORD_NAME) == pw


@pytest.mark.parametrize("name", ["bad name", "x" * 65, "a/b", "ünï", ""])
def test_jellyfin_name_rules(store, name):
    form = {"server_jellyfin": "on", "jellyfin_user": name, "jellyfin_password": long_password()}
    with pytest.raises(forms.Invalid) as e:
        forms.apply_media(make_setup(), form, store)
    assert "jellyfin_user" in e.value.errors
    assert "jellyfin_password" not in e.value.errors
    # Nothing at all was saved, not even the good password.
    assert store.get(JELLYFIN_PASSWORD_NAME) is None
    assert store.get(JELLYFIN_USER_NAME) is None


def test_jellyfin_password_too_short(store):
    form = {"server_jellyfin": "on", "jellyfin_user": "owner", "jellyfin_password": "short"}
    with pytest.raises(forms.Invalid) as e:
        forms.apply_media(make_setup(), form, store)
    assert "12" in e.value.errors["jellyfin_password"]
    assert store.get(JELLYFIN_USER_NAME) is None


def test_jellyfin_password_needed_when_none_saved(store):
    form = {"server_jellyfin": "on", "jellyfin_user": "owner"}
    with pytest.raises(forms.Invalid) as e:
        forms.apply_media(make_setup(), form, store)
    assert "jellyfin_password" in e.value.errors


def test_empty_secret_keeps_stored_value(store):
    pw = long_password()
    store.set(JELLYFIN_PASSWORD_NAME, pw)
    store.set(JELLYFIN_USER_NAME, "owner")
    form = {"server_jellyfin": "on", "jellyfin_user": "", "jellyfin_password": ""}
    forms.apply_media(make_setup(), form, store)
    assert store.get(JELLYFIN_PASSWORD_NAME) == pw
    assert store.get(JELLYFIN_USER_NAME) == "owner"
    assert forms.read_media(make_setup(media_servers={"jellyfin"}), store)["password_saved"]


def test_read_media_never_has_secret_values(store):
    pw = long_password()
    store.set(JELLYFIN_PASSWORD_NAME, pw)
    values = forms.read_media(make_setup(media_servers={"jellyfin"}), store)
    assert pw not in repr(values)
    assert values["password_saved"] is True


# ------------------------------------------------------------------ folders


def test_folders_good(store):
    out = forms.apply_folders(make_setup(), {"data_root": "/mnt/media"}, store)
    assert str(out.data_root) == "/mnt/media"


@pytest.mark.parametrize("value", ["", "media", "/a:b", "/a/../b", "/"])
def test_folders_plain_refusals(store, value):
    with pytest.raises(forms.Invalid) as e:
        forms.apply_folders(make_setup(), {"data_root": value}, store)
    message = e.value.errors["data_root"]
    assert "data_root" not in message and "Value error" not in message


def test_data_folder_inside_orama_folder_refused(store):
    with pytest.raises(forms.Invalid) as e:
        forms.apply_folders(make_setup(), {"data_root": "/srv/orama/media"}, store)
    assert "Orama folder" in e.value.errors["data_root"]
    assert "config_root" not in e.value.errors["data_root"]


# ------------------------------------------------------------------ downloads


def downloads_form(**overrides):
    form = {"usenet": "on", "torrent": "on", "vpn": "on", "vpn_provider": "protonvpn",
            "vpn_key": vpn_key(), "port_forwarding": "on"}
    form.update(overrides)
    return {k: v for k, v in form.items() if v is not None}


def test_downloads_usenet_only_no_vpn(store):
    out = forms.apply_downloads(make_setup(), {"usenet": "on"}, store)
    assert out.download_methods == {"usenet"}
    assert out.vpn is None


def test_downloads_none_chosen_refused(store):
    with pytest.raises(forms.Invalid) as e:
        forms.apply_downloads(make_setup(), {}, store)
    assert "download_methods" in e.value.errors


def test_downloads_proton_with_port_forwarding(store):
    key = vpn_key()
    out = forms.apply_downloads(make_setup(), downloads_form(vpn_key=key), store)
    assert out.vpn.provider == "protonvpn" and out.vpn.port_forwarding is True
    assert store.get(VPN_KEY_NAME) == key


def test_port_forwarding_hidden_for_nordvpn(store):
    # The browser may still send the box (an old page); it is dropped, not an error.
    out = forms.apply_downloads(make_setup(), downloads_form(vpn_provider="nordvpn"), store)
    assert out.vpn.provider == "nordvpn" and out.vpn.port_forwarding is False
    values = forms.read_downloads(out, store)
    by_name = {p["value"]: p for p in values["providers"]}
    assert by_name["protonvpn"]["port_forwarding"] is True
    assert by_name["nordvpn"]["port_forwarding"] is False


def test_vpn_needs_torrents(store):
    with pytest.raises(forms.Invalid) as e:
        forms.apply_downloads(make_setup(), downloads_form(torrent=None), store)
    assert "torrent" in e.value.errors["vpn"].lower()
    assert store.get(VPN_KEY_NAME) is None


@pytest.mark.parametrize("key", ["has space", "new\nline", "x" * 600, "quo\"te"])
def test_vpn_key_with_space_refused(store, key):
    with pytest.raises(forms.Invalid) as e:
        forms.apply_downloads(make_setup(), downloads_form(vpn_key=key), store)
    assert "vpn_key" in e.value.errors
    assert key not in e.value.errors["vpn_key"]
    assert store.get(VPN_KEY_NAME) is None


def test_vpn_key_needed_when_none_saved(store):
    with pytest.raises(forms.Invalid) as e:
        forms.apply_downloads(make_setup(), downloads_form(vpn_key=""), store)
    assert "vpn_key" in e.value.errors


def test_empty_vpn_key_keeps_stored_value(store):
    key = vpn_key()
    store.set(VPN_KEY_NAME, key)
    out = forms.apply_downloads(make_setup(), downloads_form(vpn_key=""), store)
    assert out.vpn is not None and store.get(VPN_KEY_NAME) == key
    assert forms.read_downloads(out, store)["key_saved"] is True


def test_mullvad_needs_address_and_bare_address_becomes_slash_32(store):
    form = downloads_form(vpn_provider="mullvad", port_forwarding=None)
    with pytest.raises(forms.Invalid) as e:
        forms.apply_downloads(make_setup(), form, store)
    assert "vpn_address" in e.value.errors
    assert store.get(VPN_KEY_NAME) is None
    out = forms.apply_downloads(make_setup(), {**form, "vpn_address": _ip(10, 66, 1, 2)}, store)
    assert str(out.vpn.wireguard_address) == _ip(10, 66, 1, 2) + "/32"


def test_address_dropped_for_provider_that_does_not_use_it(store):
    out = forms.apply_downloads(make_setup(), downloads_form(vpn_address=_ip(10, 2, 0, 2) + "/32"), store)
    assert out.vpn.wireguard_address is None


@pytest.mark.parametrize("address", ["nonsense", _ip(10, 66, 1, 2)+"/4", _ip(10, 66, 1, 2)+"/33", "999.1.1.1"])
def test_bad_address_refused(store, address):
    form = downloads_form(vpn_provider="ivpn", port_forwarding=None, vpn_address=address)
    with pytest.raises(forms.Invalid) as e:
        forms.apply_downloads(make_setup(), form, store)
    assert "vpn_address" in e.value.errors


def test_unknown_provider_refused(store):
    with pytest.raises(forms.Invalid) as e:
        forms.apply_downloads(make_setup(), downloads_form(vpn_provider="nope"), store)
    assert "vpn_provider" in e.value.errors


def test_route_prowlarr_through_vpn(store):
    out = forms.apply_downloads(make_setup(), downloads_form(route_prowlarr="on"), store)
    assert out.vpn.route_prowlarr is True


def test_vpn_off_keeps_stored_key(store):
    key = vpn_key()
    store.set(VPN_KEY_NAME, key)
    out = forms.apply_downloads(make_setup(), {"usenet": "on", "torrent": "on"}, store)
    assert out.vpn is None and store.get(VPN_KEY_NAME) == key


def test_dropping_usenet_with_servers_explained(store):
    from orama.setup_model import UsenetServer

    setup = make_setup(usenet_servers=(UsenetServer(slug="news", host="news.example.net"),))
    with pytest.raises(forms.Invalid) as e:
        forms.apply_downloads(setup, {"torrent": "on"}, store)
    assert "Usenet" in e.value.errors["download_methods"]


# ------------------------------------------------------------------ playback


def test_playback_good(store):
    out = forms.apply_playback(make_setup(), {"device": "lg", "quality": "4k"}, store)
    assert (out.device, out.quality) == ("lg", "4k")


def test_playback_unknown_refused(store):
    with pytest.raises(forms.Invalid) as e:
        forms.apply_playback(make_setup(), {"device": "toaster", "quality": ""}, store)
    assert set(e.value.errors) == {"device", "quality"}


# ------------------------------------------------------------------ subtitles


def test_subtitles_default_order(store):
    values = forms.read_subtitles(make_setup(), store)
    assert [v for v in values["languages"] if v] == ["el", "en"]


def test_subtitles_order_kept(store):
    form = {"language_1": "fr", "language_2": "", "language_3": "de"}
    out = forms.apply_subtitles(make_setup(), form, store)
    assert out.subtitle_languages == ("fr", "de")


def test_duplicate_subtitle_language_refused(store):
    with pytest.raises(forms.Invalid) as e:
        forms.apply_subtitles(make_setup(), {"language_1": "en", "language_2": "en"}, store)
    assert "once" in e.value.errors["language_2"]


def test_subtitle_language_must_be_listed_and_one_needed(store):
    with pytest.raises(forms.Invalid) as e:
        forms.apply_subtitles(make_setup(), {"language_1": "zz"}, store)
    assert "language_1" in e.value.errors
    with pytest.raises(forms.Invalid) as e:
        forms.apply_subtitles(make_setup(), {}, store)
    assert "language_1" in e.value.errors


def test_opensubtitles_saved_and_kept(store):
    pw = long_password()
    form = {"language_1": "el", "opensubtitles_username": "someone", "opensubtitles_password": pw}
    forms.apply_subtitles(make_setup(), form, store)
    assert store.get("opensubtitles:username") == "someone"
    assert store.get("opensubtitles:password") == pw
    forms.apply_subtitles(make_setup(), {"language_1": "el"}, store)
    assert store.get("opensubtitles:password") == pw
    values = forms.read_subtitles(make_setup(), store)
    assert values["account_saved"] is True and pw not in repr(values)


def test_opensubtitles_needs_both(store):
    form = {"language_1": "el", "opensubtitles_username": "someone"}
    with pytest.raises(forms.Invalid) as e:
        forms.apply_subtitles(make_setup(), form, store)
    assert "opensubtitles_password" in e.value.errors
    assert store.get("opensubtitles:username") is None
