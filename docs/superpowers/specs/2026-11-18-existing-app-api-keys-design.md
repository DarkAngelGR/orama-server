# Existing app API credentials

## Intent
Let the owner enter existing app API keys in Orama so requests and integrations can authenticate, starting with the existing Radarr and Sonarr installations.

## Approach
Add an App connections section to Settings using the existing form styling, authenticated routes, CSRF protection and encrypted SecretStore. Reuse the existing adopted-app address resolver. This is the proposed approach; a CLI-only workaround would not meet the requested browser flow, and automatic extraction of container secrets would broaden Agent permissions unnecessarily.

## Behaviour
- Show enabled adopted API-key apps: Radarr, Sonarr, Prowlarr, Bazarr, SABnzbd and Jellyfin.
- Each app has a blank password-type API key input, a saved/not-saved indicator, instructions for locating its key, and Test and save.
- A bounded, read-only authenticated request tests the entered key against the saved app address. Do not follow redirects or print response bodies or credentials.
- Save only a verified key using the existing canonical secret name. Failed tests preserve the previous key; blank input preserves it too.
- Show plain success, rejected-key or unreachable messages. Never render a submitted or saved key back into HTML, URLs or logs.
- Use existing busy, draft and locking safeguards. Saving credentials does not run apply, seed app configuration, restart containers or change upstream app settings.
- Existing Plex sign-in and qBittorrent password handling are separate from this API-key form.

## Verification
Tests first: authenticated/CSRF-only access, supported adopted-app restriction, blank input, success, invalid/unreachable responses, old-key preservation, no secret echo, and immediate use of the saved key by media requests. Run relevant tests, Ruff, ShellCheck and Linux CI.

## Limits
This does not relocate apps, change Docker/Agent permissions, import libraries or alter existing quality profiles. Other integration requirements can still produce separate errors after authentication succeeds.
