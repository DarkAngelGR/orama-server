# Orama Release 1 — Plan 5: Proof, Self-Update & Release Sign-off

**Goal:** Complete Release 1 operational readiness (`self-update`), verify end-to-end installation and request flows on clean Ubuntu 24.04 and Debian 12 environments, walk through the owner's manual hardware checklist on the UGREEN DXP4800 Plus NAS (UGOS Pro), and complete the deferred security scans before publishing Release 1 on `main`.

**Architecture:** Preserve all trust boundaries established in Plans 1–4. Only `orama-agent` has access to the Docker socket; `orama` mounts only `<config_root>/orama` and talks to apps over HTTP and to the Agent over the shared Unix socket.

**Tech Stack:** Python ≥3.12 (`uv`, Flask, Jinja2, vendored htmx, SQLite), Docker Compose, Playwright Chromium browser automation, GitHub Actions CI.

**Spec Reference:** `docs/superpowers/specs/2026-11-03-orama-release-1-design.md`, §§4.2, 9, 10, 11, and carry-forward notes in `docs/superpowers/plans/2026-11-0*-notes.md`.

---

## Plan 5 Tasks

### Task 1: Publish Gate & End-to-End VM Verification
- **Publish gate (`publish` job in `.github/workflows/ci.yml`):** Now that the `browser` job has passed three consecutive runs (`42381439992`, `42472203041`, `42511960464`), require `browser` and `demo` alongside `unit`, `images`, and `integration` in `publish.needs`.
- **Ubuntu 24.04 & Debian 12 VM proof (Spec §10.1.3):**
  - Run one-line `install.sh` on fresh Ubuntu 24.04 and Debian 12 targets.
  - Drive the first-run setup wizard in a real Chromium browser (`tests/web/test_browser.py` / VM harness) through password creation, media server selection, folders, download clients, playback device (`samsung_s95f`), subtitle languages (`el`, `en`), indexer test, and live apply checklist.
  - Request one TV episode and one movie through Orama; verify download, import, same-inode hardlink (`st_ino`), post-import `mediaInfo` Direct Play assessment, and Bazarr subtitle placement.
  - Re-run `install.sh` and confirm idempotence (nothing recreated or modified).

### Task 2: Root-side Self-Update Operations Subplan
- **Agent `self-update` (`src/orama_agent/`, Spec §4.2 & §9):**
  - Implement the `self-update` command on the Agent with strict argument validation (no arbitrary image names or flags; pulls the pinned/configured Orama and Agent images and recreates the core project safely via a short-lived helper container or controlled compose up).
  - Ensure offline/failed image pulls during reboot or update fail cleanly with a plain message without breaking running containers.
- **Web UI trigger (`src/orama/web/settings.py`):**
  - Wire the "Update Orama" action in Settings to the Agent's `self-update` command with CSRF protection, lock coordination (`apply.lock`), and unit/integration tests.

### Task 3: Owner Hardware Checklist (UGREEN DXP4800 Plus / UGOS Pro)
Plain-language verification steps for the owner on the physical NAS (Spec §10.2 and carry-forward items from Plans 1–4):
1. **UGOS Pro & Installer (`install.sh`):**
   - Confirm `/etc/os-release` UGOS detection places Orama under `/volume1/docker/orama` and suggests `/volume1/data`.
   - Confirm UGOS's bundled Docker daemon is used without installing an external Docker package, and UGOS reserved ports (`9999`, `9443`) are avoided.
   - Confirm Intel QuickSync `/dev/dri/renderD128` numeric group ID is detected and passed to Plex/Jellyfin.
2. **Phone Setup & LAN Session Binding:**
   - Open the one-time setup link printed by `install.sh` from a phone on the LAN.
   - Verify whether Docker's userland proxy preserves client LAN addresses or maps local connections to the bridge gateway (Plan 3 notes).
3. **Real VPN, Indexers & Downloaders:**
   - Configure ProtonVPN WireGuard in the wizard (`PORT_FORWARD_ONLY=on`); verify Gluetun connects and updates qBittorrent's forwarded listen port.
   - Verify Prowlarr through the Gluetun HTTP proxy (`prowlarr:proxy`) and real indexers + Usenet provider in SABnzbd.
4. **Plex Sign-In, Jellyfin & Hardware Transcoding:**
   - Complete real Plex sign-in (`/plex/sign-in` or `python -m orama.cli plex-sign-in`), confirm server claim, LAN discovery at the advertised RFC1918 IPv4 address, and Movies/TV library creation without duplicates on re-apply.
   - With Plex Pass (and separately in Jellyfin), play a file requiring transcoding and confirm Intel QuickSync hardware transcoding is active.
   - Optional: verify recovery of an already-configured Jellyfin instance via `set-secret user:jellyfin` and `password:jellyfin`.
5. **Real Request, Subtitles & Samsung S95F Direct Play:**
   - Request an episode and a movie in Orama.
   - Confirm Bazarr downloads Greek (`el`), then English (`en`) SRT subtitles from the enabled free providers.
   - Play the imported title on the physical **Samsung S95F** TV and confirm the Plex dashboard reports **Direct Play** (both video and audio).
6. **Idempotent Re-run & Accessibility Check:**
   - Re-run `install.sh` on the NAS and confirm nothing changes.
   - Run a quick VoiceOver or NVDA screen-reader check over the live apply checklist and confirmation pages.

### Task 4: Security Review, GHCR Packaging & Release 1 Sign-off
- **Claude Security Scans (Owner, Wednesday 2026-11-11):**
  - Run `/claude-security:claude-security` on Plan 2b (base `441c1a8`), Plan 3 (base `6a39133`), Plan 4 (base `56ac787`), and the full repository (`engineering:code-review`, `security-review`). Fix or explicitly document every finding.
- **Release Packaging & Branch Promotion:**
  - Fast-forward/merge `test` into `main` once reviews pass.
  - Make `ghcr.io/darkangelgr/orama` and `ghcr.io/darkangelgr/orama-agent` public before announcing the one-line install command.
  - Record Release 2 carry-forward items (`followups` table for the conversion and subtitle-translation worker container).
