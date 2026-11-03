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
