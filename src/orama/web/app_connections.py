"""App connections: API credential entry and bounded read-only verification for adopted apps."""

from __future__ import annotations

from typing import Any

from orama.secrets import SecretStore
from orama.setup_model import DesiredSetup, enabled_apps
from orama.wiring.engine import app_host_and_port
from orama.wiring.http import Http, Unreachable, UnsafeRedirect

SUPPORTED_APPS: tuple[str, ...] = (
    "radarr",
    "sonarr",
    "prowlarr",
    "bazarr",
    "sabnzbd",
    "jellyfin",
)

APP_METADATA: dict[str, dict[str, Any]] = {
    "radarr": {
        "name": "Radarr",
        "default_port": 7878,
        "help_text": "In Radarr: Settings → General → Security → API Key",
    },
    "sonarr": {
        "name": "Sonarr",
        "default_port": 8989,
        "help_text": "In Sonarr: Settings → General → Security → API Key",
    },
    "prowlarr": {
        "name": "Prowlarr",
        "default_port": 9696,
        "help_text": "In Prowlarr: Settings → General → Security → API Key",
    },
    "bazarr": {
        "name": "Bazarr",
        "default_port": 6767,
        "help_text": "In Bazarr: Settings → General → Security → API Key",
    },
    "sabnzbd": {
        "name": "SABnzbd",
        "default_port": 8080,
        "help_text": "In SABnzbd: Settings → General → SABnzbd Web Server → API Key",
    },
    "jellyfin": {
        "name": "Jellyfin",
        "default_port": 8096,
        "help_text": "In Jellyfin: Dashboard → Advanced → API Keys",
    },
}

BLANK_KEY_MESSAGE = "Enter an API key to test."
INVALID_CHARS_MESSAGE = "The API key contains invalid characters."


def validate_key_input(key: str | None) -> tuple[str, str | None]:
    """Validate submitted API key string. Returns (stripped_key, error_message)."""
    if not key or not key.strip():
        return "", BLANK_KEY_MESSAGE
    stripped = key.strip()
    if any(ord(c) < 32 or ord(c) == 127 for c in stripped):
        return "", INVALID_CHARS_MESSAGE
    return stripped, None


def probe_key(app: str, setup: Any, key: str, http: Http) -> str | None:
    """Bounded, read-only test of the entered API key against the app.

    Returns None on success, or a plain user-facing error message on failure.
    Does not follow redirects and never logs or leaks the secret.
    """
    if app not in APP_METADATA:
        return "Unknown application."

    meta = APP_METADATA[app]
    name = meta["name"]
    default_port = meta["default_port"]

    host, port = app_host_and_port(app, setup, default_port)
    base_url = f"http://{host}:{port}"

    try:
        if app in ("radarr", "sonarr"):
            url = f"{base_url}/api/v3/system/status"
            resp = http.request("GET", url, headers={"X-Api-Key": key}, timeout=10.0)
            if resp.status in (401, 403):
                return f"{name} did not accept this API key."
            if resp.status != 200:
                return f"{name} gave an answer Orama could not read."
            data = resp.json()
            if not isinstance(data, dict) or "version" not in data:
                return f"{name} gave an answer Orama could not read."

        elif app == "prowlarr":
            url = f"{base_url}/api/v1/system/status"
            resp = http.request("GET", url, headers={"X-Api-Key": key}, timeout=10.0)
            if resp.status in (401, 403):
                return f"{name} did not accept this API key."
            if resp.status != 200:
                return f"{name} gave an answer Orama could not read."
            data = resp.json()
            if not isinstance(data, dict) or "version" not in data:
                return f"{name} gave an answer Orama could not read."

        elif app == "bazarr":
            url = f"{base_url}/api/system/status"
            resp = http.request("GET", url, headers={"X-API-KEY": key}, timeout=10.0)
            if resp.status in (401, 403):
                return f"{name} did not accept this API key."
            if resp.status != 200:
                return f"{name} gave an answer Orama could not read."
            data = resp.json()
            if not isinstance(data, dict):
                return f"{name} gave an answer Orama could not read."

        elif app == "sabnzbd":
            url = f"{base_url}/api"
            resp = http.request(
                "POST",
                url,
                form={"mode": "version", "apikey": key, "output": "json"},
                timeout=10.0,
            )
            if resp.status in (401, 403):
                return f"{name} did not accept this API key."
            if resp.status != 200:
                return f"{name} gave an answer Orama could not read."
            data = resp.json()
            if isinstance(data, dict) and data.get("status") is False:
                return f"{name} did not accept this API key."
            if not isinstance(data, dict) or "version" not in data:
                return f"{name} gave an answer Orama could not read."

        elif app == "jellyfin":
            url = f"{base_url}/System/Info"
            headers = {
                "Accept": "application/json",
                "Authorization": f'MediaBrowser Client="Orama", Device="Orama", DeviceId="orama", Version="1", Token="{key}"',
                "X-Emby-Token": key,
            }
            resp = http.request("GET", url, headers=headers, timeout=10.0)
            if resp.status in (401, 403):
                return f"{name} did not accept this API key."
            if resp.status != 200:
                return f"{name} gave an answer Orama could not read."
            data = resp.json()
            if not isinstance(data, dict) or not any(k in data for k in ("ServerName", "Version", "Id")):
                return f"{name} gave an answer Orama could not read."

        else:
            return f"{name} is not supported for API key testing."

    except UnsafeRedirect:
        return f"{name} sent a redirect instead of an answer."
    except Unreachable:
        return f"{name} could not be reached. Check the address and port."
    except (ValueError, KeyError, TypeError):
        return f"{name} gave an answer Orama could not read."

    return None


def get_adopted_app_connections(setup: DesiredSetup | None, store: SecretStore) -> list[dict[str, Any]]:
    """Return status and metadata for enabled adopted apps supported for API keys."""
    if setup is None:
        return []
    connections = []
    for app in enabled_apps(setup):
        if app in setup.adopted_apps and app in SUPPORTED_APPS:
            meta = APP_METADATA[app]
            saved = store.get(f"apikey:{app}") is not None
            connections.append({
                "app": app,
                "name": meta["name"],
                "saved": saved,
                "help_text": meta["help_text"],
            })
    return connections
