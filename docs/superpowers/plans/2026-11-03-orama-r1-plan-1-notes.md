# Orama Release 1 — Plan 1 (Foundation): decisions and carry-forward notes

Plan 1 is done: `unit`, `images`, `integration` and `publish` CI jobs green on `main`.
Where these notes differ from the Plan 1 text, these notes describe what was built.

## Decisions made during implementation (differ from or add to the plan)

- **Ports:** the installer (`bootstrap`) assigns host ports for every app plus Orama, because only the installer can see which host ports are taken. `apply` only fills gaps. Ports kept from an earlier setup are re-checked (1024–65535, not UGOS-reserved).
- **Trust boundary:** the Orama container mounts only `<config_root>/orama`. `config_root` and `config_root/config` are root-owned. The Agent pins every app mount to exactly what the builder emits, refuses `$` anywhere in the compose document (so Compose cannot rewrite paths), allowlists service keys, environment keys and values, and pins PUID/PGID to its own values (never 0). It also refuses symlinked config paths.
- **Secrets for apps:** env files live in an Agent-only `<config_root>/app-env/<app>.env` (root, 0600, written without following links), never inside an app's own config folder.
- **Compose runs** with a fixed command line, its own empty `--env-file`, `--project-directory <config_root>` and a scrubbed environment. It pulls images first (own 1-hour timeout). On failure the user sees a plain message pointing at `sudo docker logs orama-server-agent`, and the Agent logs the Compose error with secret values removed.
- **ensure-folders** accepts only the data folders and per-app config folders the Agent computes itself. It creates them without following links, and never changes the owner or mode of folders that already exist.
- **Folder paths with `:` are refused** everywhere, because Docker cannot mount them. Spaces, quotes, `#`, `&` and non-ASCII (e.g. Greek) work.
- **Installer:** it never replaces an existing Docker; it starts a stopped daemon (not on UGOS) or explains what's missing. NVIDIA is offered only when Docker itself has the NVIDIA runtime / CDI spec. Refusals happen before anything is installed. Piped `curl … | sudo bash` runs correctly; a partial download runs nothing.
- **Images:** LinuxServer apps are pulled from `ghcr.io/linuxserver/…` (same images as `lscr.io`), with tags pinned and checked on 2026-11-03. The users' `<version>` tag only moves after the integration test passes; every build also gets `sha-<short>`.
- **VPN:** Gluetun cannot be removed while the VPN is on, so torrents are never left unprotected.

## Must be handled in later plans

- **Plan 2 (wiring):**
  - SABnzbd and Bazarr do not take API keys from env, and Orama cannot reach `config/<app>`, so an Agent command to seed/read app config is needed.
  - Plex/Jellyfin bridge networking may break local discovery and direct play: check ADVERTISE_IP / custom connections.
  - NVENC needs `NVIDIA_DRIVER_CAPABILITIES` (extend the Agent's env allowlist with the builder).
  - Gluetun ProtonVPN port-forward server selection.
  - Any new compose key or env var must be added to both the builder and the Agent allowlist.
- **Plan 3 (web app):**
  - One shared VPN-provider list for the model and the Agent: Gluetun names contain spaces, which the Agent currently refuses.
  - A write lock on `setup.json` (bootstrap vs. web app).
  - Warn when a pre-existing data folder isn't writable by the app user.
  - `layout.config_folders` includes `orama/`, which the Agent refuses; don't send it to ensure-folders.
  - A re-run keeps an existing draft's GPU settings: offer a refresh.
- **Plan 4 (requests, reboot/drift):** a failed image pull must not fail a re-apply when images are already present (offline reboot).
- **Plan 5 (proof/release):**
  - Verify the UGOS `/etc/os-release` markers and the reserved ports (9999/9443, assumed) on the DXP4800 Plus.
  - Make the GHCR packages public before announcing the install command.
  - Decide the release-tag policy (digest pins for base images).
- **Cleanup when touching the code:**
  - Duplicated detectors in `platform.py` vs `install.sh`.
  - Unused `safe_path` for mounts and `env_file_path`.
  - Duplicated constants (socket path, network/project names).
  - Four atomic-writer implementations → one safe-IO module.
  - The Agent doesn't yet check host-port rules.
  - `/dev/dri` is passed whole (spec names `renderD128`).

## Plan 2a carry-forward notes (Codex supplemental review)

Plan 2a is not complete: the dedicated Claude Security scan and the final review after
that scan remain outstanding. Independent Codex hardening and review do not replace them.
See `docs/STATUS.md` for task commits and the detailed execution decisions.

- Download wiring uses `orama-net` and container ports. Seed-config runs after the first
  deploy so SABnzbd and qBittorrent can write defaults. qBittorrent keeps localhost
  authentication enabled; Gluetun's exact pinned hook logs in using its env-file password.
- API redirects are refused and response bodies capped. SABnzbd uses POST form bodies
  and disabled API-parameter logging. The Agent bounds concurrent socket handlers.
- Indexer display names must be unique because Prowlarr entries are matched by name.
  Existing setups with duplicate names must be corrected before apply. This refuses an
  ambiguous setup rather than overwriting one indexer with another.
- Removing a downloader removes only its Orama-managed Sonarr/Radarr clients, even when
  the corresponding download method remains selected. User-created clients are kept.
- Changing an existing indexer's definition under the same name fails clearly without
  replacing the entry. Restore its original type or use a new name. Automatic replacement
  is deferred to avoid deleting configuration; the cost is that the old indexer remains
  and must be managed explicitly. Indexers removed from setup are still not deleted.
- Plan 2b must add Bazarr subtitle settings and API-key seeding, Recyclarr/TRaSH quality
  and device profiles, Plex PIN sign-in/claim/libraries, Jellyfin first-run setup/libraries,
  and the Sonarr/Radarr media-server refresh connections. Preserve the bridge-network
  discovery and NVIDIA environment follow-ups above. Request webhooks and post-import
  playback inspection belong to Plan 4.

## Plan 2a — done (Claude, 2026-11-04)

**What it gives the owner:** after `apply`, the download apps are connected. Prowlarr hands your indexers to Sonarr and
Radarr. They send downloads to SABnzbd and qBittorrent (behind Gluetun when the VPN is on), into the shared folders, with
hardlinks on. A second `apply` changes nothing. Orama touches only entries it created (named "… (Orama)"). Proven on real
apps in CI (`tests/integration`). Subtitles, quality rules, Plex and Jellyfin come in Plan 2b.

**Security:**
- The Claude Security scan of `2c599c9..f82dd43` (high effort) found two issues, both verified 3/3 and fixed in `fa5007b`:
  - env-file values are now an allowlist `[A-Za-z0-9_+/=-]`, with a Compose-style round-trip check;
  - the qBittorrent password is no longer on wget's command line.
- That scan's research was partly cut by a usage limit.
- **Still to do (Claude only):** a follow-up scan of `f82dd43..HEAD`. It covers Codex's Agent edits and the cut research.

**Decisions taken while executing Plan 2a (beyond those already listed above and in docs/STATUS.md):**
- **Gluetun port forwarding:**
  - `PORT_FORWARD_ONLY=on` only for ProtonVPN.
  - qBittorrent keeps localhost auth ON. The port-forward hook logs in with the password from Gluetun's env file, via a
    private temp file. This is the only `$` the Agent accepts, and only in that exact string.
- **Gluetun web proxy:** it requires a login (user `orama`, password `password:gluetun-proxy` in Gluetun's env file).
- **seed-config:**
  - It runs after the first deploy, so the apps write their defaults first.
  - When qBittorrent was seeded with port forwarding on, Gluetun then qBittorrent are restarted. A store flag retries this
    until it succeeds.
  - qBittorrent 5.3 keys go in `[WebUI]`.
- **Usenet server hosts:** hostname-style only (IPv4 ok; no IPv6, underscore or non-ASCII).
- **Disabling things:**
  - Disabling a downloader removes its "(Orama)" client.
  - Disabling Sonarr/Radarr removes its Prowlarr connection.
  - Indexers removed from the setup are NOT deleted.
- **Prowlarr sync:** it runs even when one indexer fails its test.
- **Skipped steps:** they name the app that actually failed.

**Carry forward:**
- **Plan 2b (media wiring):**
  - Any new compose key or env var goes into both the builder and the Agent pins.
  - `seed-config` is the only path for writing app config files.
- **Plan 3 (web app):**
  - Removing indexers and Usenet servers (owner decision).
  - Port forwarding offered only for providers that support it.
  - `set-secret` is replaced by the setup page.
  - Orama is reachable from every app on `orama-net`, so auth must not trust that network.
  - Confirm Gluetun's control server (:8000) denies by default in v3.41.3.
- **Plans 3/5:**
  - Prowlarr tests an Http proxy when saving it. If Gluetun is still connecting, the `prowlarr:proxy` step fails with a
    generic HTTP 400. It should say plainly "the VPN is still connecting — run apply again".
  - A real VPN end-to-end run is on the Plan 5 NAS checklist.
- **Plan 5:** the repo had literal fake test secrets in early history (now replaced). GitGuardian alerts about them are
  test values and can be closed. Rewriting history is the owner's call.
