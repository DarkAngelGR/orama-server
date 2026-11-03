# Orama Server — Release 1 design

Status: draft for owner review
Date: 2026-11-03

## 1. Purpose

Orama Server is **one app that links a home media server together**. The user opens
only Orama: they search for a movie or a specific episode, request it, and Orama makes
sure it is downloaded, gets subtitles, and lands in Plex and/or Jellyfin in a form their
TV can **play directly, without on-the-fly transcoding**.

Under the hood Orama is a small control app that installs, wires together and manages
the official app containers (Sonarr, Radarr, Prowlarr, Bazarr, SABnzbd, qBittorrent +
Gluetun, Plex, Jellyfin, Recyclarr) via Docker Compose. Each app stays its own image and
keeps working even if Orama is stopped.

### Not repeated from earlier attempts
- No single container holding every app ("Orama Server 2.0.0").
- No custom-built media server.

## 2. Release plan (revised during brainstorm)

| Release | Contents |
|---|---|
| **1 (this spec)** | One-line install, setup wizard, deploy + auto-wiring, **requests in Orama with live status**, direct play via release selection, subtitles via Bazarr, Settings page |
| 2 | Worker container: DTS → E-AC3 1536 kbps, Dolby Vision 7 → 8.1 and 5 → HDR10, Greek subtitle translation (DeepL Free, then other free APIs) for files Release 1 flagged |
| 3 | Dashboard (status, CPU/RAM, logs, per-app one-click updates), backup/restore, add/remove apps |
| 4 | Owner's add-ons: watchlist sync (owner + friends), Orama TV app Discover/requests, Orama TV installer; reuse `watchlist-sync` code |

Release 1 must leave room for these (see §11) but not build them.

## 3. Scope of Release 1

### In scope
- **Targets:** plain Ubuntu 22.04/24.04 and Debian 12 on x86-64, with or without Docker;
  **UGOS Pro** (UGREEN NAS, Debian-based; owner's DXP4800 Plus) as a first-class target.
- **Users:** one admin, one password. Auth code structured so accounts can be added later.
- **Fresh installs only.** Orama manages only apps it created. Migrating from an existing
  setup = point Orama at the existing data folder and let Sonarr/Radarr rescan.
- Media server: **Plex, Jellyfin, or both**.
- Downloads: **Usenet (SABnzbd), torrents (qBittorrent), or both**; optional VPN for torrents.

### Out of scope
- Remote access (owner uses Tailscale / Cloudflare Tunnel).
- Synology, Unraid, TrueNAS, ARM boards.
- Adopting already-running containers; importing library lists.
- Friend accounts (friends come in via watchlist sync, later).
- File conversion and subtitle translation (Release 2).

## 4. Architecture

### 4.1 Components
| Component | Role | Docker access |
|---|---|---|
| **Install script** | One-line install. Plain Debian/Ubuntu: installs Docker from Docker's repo if missing. UGOS: detects UGOS and uses its bundled Docker, never installs its own. Creates Orama's folder, starts Orama + Agent, prints the setup URL. Idempotent: re-running only fixes what is missing. | — |
| **Orama** (container) | Web UI, wizard, requests, settings, talks to every app over its HTTP API. | **None** |
| **Orama Agent** (container) | Tiny helper, no web UI, **no network port**. Only piece with the Docker socket. | Yes |
| **Managed apps** | Official/recommended images (§4.4). | None |

### 4.2 Orama ↔ Agent
- Communicate over a Unix socket in a volume shared only by Orama and the Agent.
- The Agent accepts a fixed allowlist of commands, each with validated arguments:
  - `apply` — write the compose file Orama generated (validated against a schema: only
    known services, known images, known mount roots) and run `docker compose up -d`.
  - `restart <app>`, `status` (container states).
  - `ensure-folders` — create the data/config folders with the right owner and mode.
  - `self-update` — pull the new Orama image and recreate Orama (Agent updates itself via
    a short-lived helper container).
- The Agent refuses anything else. It never accepts arbitrary compose YAML, volume paths
  outside the configured roots, privileged flags, or host-network requests beyond those
  defined in Orama's own service catalogue.
- Container control uses the `docker compose` CLI (so the result is a normal compose
  project a human can manage by hand); read-only status may use the Docker API.

### 4.3 Disk layout
- **Orama folder** — default `/opt/orama` on Debian/Ubuntu; on UGOS on the storage
  volume (e.g. `/volume1/docker/orama`) so UGOS updates cannot wipe it.
  - `compose.yaml` (generated)
  - `config/<app>/` — each app's own config
  - `orama/` — Orama's state (desired setup, request records), secrets encrypted, logs
- **Data folder** — one root (suggested `/data`, `/volume1/data` on UGOS), mounted into
  every relevant container as `/data` (TRaSH Guides layout):
  - `/data/media/movies`, `/data/media/tv`
  - `/data/downloads/usenet/{movies,tv}`, `/data/downloads/torrents/{movies,tv}`
  - One mount → same filesystem → imports are hardlinks/atomic moves, never copies.

### 4.4 Images
- Sonarr, Radarr, Prowlarr, Bazarr, SABnzbd, qBittorrent: **LinuxServer.io** images
  (recommended by these projects; consistent `PUID`/`PGID`/`UMASK`).
- Plex: official `plexinc/pms-docker`. Jellyfin: official `jellyfin/jellyfin`.
- Gluetun: `qmcgaw/gluetun`. Recyclarr: `recyclarr/recyclarr` (runs on demand).
- Every Orama release **pins tested image versions**. Apps do not auto-update.

### 4.5 Network
- One private Docker network; apps reach each other by service name.
- Each app's web UI gets a **fixed, unique host port** from Orama's port table. The
  table must contain no duplicates (tested) and must avoid UGOS's own ports. The
  installer checks every port is free before starting; if one is taken it picks the
  next free port and records it.
- qBittorrent runs in Gluetun's network namespace (`network_mode: service:gluetun`);
  its UI port is published on Gluetun. With the VPN down it has no route out.

### 4.6 Permissions
- All apps run as one `PUID`/`PGID` — default: the user who ran the installer (on UGOS,
  the admin account). `UMASK=002`.
- Folders created by the Agent with that owner, group-writable.
- Hardware transcoding: pass `/dev/dri/renderD128` and add the device's **numeric** GID
  (read with `stat` on the host), never a group name. NVIDIA detected (`nvidia-smi` /
  container toolkit present) and offered.

## 5. Setup

### 5.1 Desired state
All wizard answers form one **desired-setup record**. "Apply" brings the machine in line
with it. First setup, "Change setup", Settings edits and post-reboot checks all use the
same apply engine. Every step is **check-then-act** (idempotent): running apply twice
changes nothing the second time.

### 5.2 Wizard steps
1. Create Orama password.
2. Plex, Jellyfin, or both.
3. Data folder (suggested per platform; subfolders created).
4. Usenet / torrents / both; VPN for torrents (provider list from Gluetun, ProtonVPN
   pre-selected; WireGuard key).
5. Indexers: list from **Prowlarr's own indexer catalogue** shown in Orama; user enters
   API key/login; each one tested. Usenet provider server/username/password for SABnzbd,
   tested. Setup cannot finish until at least one indexer passes.
6. Playback device (§6.1) and quality (§6.2).
7. Subtitle languages in priority order (suggested: Greek, English).
8. Review → **Install** with a live checklist.
9. Plex sign-in / Jellyfin admin creation once those apps are up.

### 5.3 Apply steps
1. Generate `compose.yaml` **from data with a YAML library** (never string templates).
2. Agent: `ensure-folders`, then `apply`. Wait until each app answers its health/API
   endpoint.
3. Wire via APIs:
   - **API keys:** Orama generates each *arr/Bazarr/SABnzbd key and injects it at first
     start (environment/config), rather than scraping it later.
   - **Prowlarr:** add indexers; add Sonarr and Radarr as applications (full sync).
     Optional setting: route Prowlarr through Gluetun's HTTP proxy.
   - **Sonarr / Radarr:** root folders; download clients with categories `tv` /
     `movies`; hardlinks on; quality via Recyclarr (TRaSH) plus device custom formats;
     Plex/Jellyfin connections (library refresh on import); a webhook to Orama
     (grab/import/upgrade events).
   - **SABnzbd:** provider server, categories and folders, host whitelist for its
     service name.
   - **qBittorrent:** categories and save paths; Gluetun port-forwarding hook keeps
     qBittorrent's listen port in sync (ProtonVPN, paid plan, P2P server).
   - **Bazarr:** Sonarr/Radarr connections, language profile from §5.2.7, free
     providers (OpenSubtitles account optional), prefer SRT text subtitles.
   - **Plex:** "Sign in with Plex" via Plex's PIN/OAuth flow (Orama never sees the Plex
     password) → claim server → create Movies/TV libraries → enable hardware
     transcoding (tells the user if Plex Pass is missing).
   - **Jellyfin:** complete the first-run wizard through its startup API with the
     admin user from the wizard → create libraries → enable QuickSync/NVENC.
4. Failure: the checklist shows which step and app failed in plain words, with Retry.
   Retry resumes; other independent steps still run.

### 5.4 Changing things later
- **Settings page** exposes every wizard field (indexers, provider, VPN, folders,
  languages, device, quality, media servers, download methods) for editing.
- **Change setup** reopens the wizard pre-filled.
- Both save the desired-setup record and run apply. Removing an app stops it but keeps
  its config folder; media is never deleted.

## 6. Playback and subtitles

### 6.1 Device rules
- The wizard asks what the user watches on: Samsung TV, LG TV, Apple TV, Nvidia Shield,
  "not sure (safest)". The **Samsung S95F** profile is the reference and is tested on
  real hardware; others start as best-effort.
- Each profile is a data file in the repo listing supported/unsupported audio, video
  (HDR10, HDR10+, Dolby Vision profiles), subtitle formats and containers. Example,
  S95F: no DTS, no multichannel LPCM/FLAC, TrueHD OK, HDR10/HDR10+ OK, no Dolby Vision
  (prefer HDR10/HDR10+; avoid DV profile 5, which has no HDR10 fallback).
- Profiles are converted into Sonarr/Radarr **custom formats with scores** (unplayable
  = strong negative, playable preferred), layered on top of the TRaSH formats.

### 6.2 Quality
- Wizard choice: "4K when available" / "1080p" / **"4K for movies, 1080p for shows"**
  (default). Implemented with Recyclarr applying TRaSH Guides profiles, then device
  formats.

### 6.3 Post-download check
- Release names are only a guess. After import Orama reads the file's media info from
  Sonarr/Radarr. If it will not direct-play on the chosen device, the request shows
  **"Ready · may transcode (<reason>)"** and the file is recorded on a list for the
  Release 2 worker.

### 6.4 No playable release
- Default: keep searching for **24 hours**, then accept the best available release and
  label it "may transcode". Settings: "always wait" / "never wait" / custom hours.

### 6.5 Subtitles
- Bazarr searches languages in the user's order and prefers SRT (image/styled subtitles
  often force transcoding to burn in).
- If the first language is missing: request shows e.g. **"Ready · EN subs, still
  looking for EL"**; Bazarr keeps searching. These items are recorded for Release 2
  translation.

## 7. Requests

- Search goes through Sonarr/Radarr lookup. Results show posters; items already in the
  library are marked "In library" (no duplicate requests).
- Request types: one episode, one season, whole show + future episodes, one movie.
- An episode request adds the series with **only that episode monitored** and triggers
  a search.
- Status stages: **Searching → Downloading (%) → Importing → Ready**, plus subtitle
  state and playback note. Waiting states are explicit: "Waiting for VPN", "Waiting for
  Sonarr", "No playable release yet — will keep looking".
- Updates come from Sonarr/Radarr webhooks, with queue polling while a request is
  downloading and as a fallback.
- Request detail shows the chosen release and why (e.g. "2160p · E-AC3 · fits Samsung
  S95F").

## 8. Web UI

- **Pages:** Home (search + current requests), Search results, Request detail,
  Settings, Apps (running/stopped + link to each app's own UI), Setup wizard.
- Phone-friendly.
- **Stack:** Python web app serving server-rendered HTML templates with a little
  JavaScript (htmx) for live parts. No front-end framework, no build step.
- **Design work:** the page design uses the `frontend-design` skill if it is installed
  when that work starts; otherwise the design plugin skills (`design:design-system`,
  `design:ux-copy`, `design:accessibility-review`, `design:design-critique`) are used to
  design and review the pages.

### 8.1 Security
- Password hashed with a memory-hard algorithm (argon2id); login attempts rate-limited.
- Session cookie: `HttpOnly`, `SameSite=Strict`, `Secure` when served over HTTPS.
- CSRF token required on every state-changing request.
- **DNS-rebinding protection:** `Host` header must match an allowlist (the machine's
  LAN IPs, hostname, `localhost`, plus user-added names); anything else is rejected.
- No CORS. API keys and passwords never sent to the browser.
- These protections have their own tests (§10).

## 9. Operations and errors

- **Reboot:** all containers `restart: unless-stopped`; on start Orama runs a check pass
  and re-applies anything that drifted.
- **App down:** Apps page shows it; affected requests show a waiting state.
- **VPN down:** kill switch stops torrents; Usenet unaffected.
- **Disk nearly full:** warning on Home (threshold configurable, default 5% free).
- **App API changes:** that apply step fails visibly with the app name; others continue.
- **Secrets:** stored encrypted with a key file readable only by Orama (mode 600);
  a logging filter redacts every known secret value and common key patterns.
- **Logs:** in Orama's folder, safe to share.
- **Orama update:** button in Settings → Agent `self-update`; new Orama versions carry
  the next tested set of app image versions.

## 10. Testing and definition of done

### 10.1 Test layers
1. **Unit (every change):** compose generation (incl. special characters in paths and
   passwords), device profile → custom formats, port table uniqueness, secret
   redaction, Agent command validation (rejects unknown services, paths outside roots,
   privileged flags), login/CSRF/Host-allowlist behaviour, repo scan for secrets, IPs
   and personal paths.
2. **Integration:** real app containers started; Orama wires them; assertions that each
   link exists (indexers in Sonarr/Radarr, download clients, root folders, Bazarr
   links, libraries). Apply runs a second time and must change nothing.
3. **End-to-end on clean VMs (GitHub Actions):** fresh Ubuntu 24.04 runner plus a
   Debian 12 VM booted in QEMU/KVM on the runner. Each: one-line install → wizard
   completed by an automated browser → request an episode and a movie. A bundled
   **fake Torznab indexer and seeder** serve short generated video clips named like
   real releases (with Dolby Digital Plus audio). Assert: downloaded, **hardlinked**
   (same inode), visible in Plex/Jellyfin, subtitle placed (from a fake provider or a
   bundled SRT), request shows Ready. Then re-run the installer: nothing changes.

### 10.2 Manual checklist (owner, DXP4800 Plus)
Written as plain-language steps: UGOS detection and UGOS Docker, real ProtonVPN with
port forwarding, real indexers and Usenet provider, Plex sign-in, request an episode
in Orama, playback on the S95F shows **Direct Play** in Plex.

### 10.3 Release 1 is done when
1. All three test layers pass on Ubuntu 24.04 and Debian 12.
2. The manual checklist passes on the DXP4800 Plus: an episode requested in Orama
   downloads, has subtitles (or is honestly labelled fallback-only), and direct-plays on
   the S95F.
3. Re-running the installer on the NAS changes nothing.
4. The repo contains no secrets, IPs or personal paths (enforced by the unit-layer scan).
5. A **security review of the whole codebase** has been run with the `security-review`
   skill (plus `engineering:code-review` for security/correctness), and every finding is
   fixed or explicitly accepted by the owner.

## 11. Room for later releases
- Desired-setup record and apply engine are the base for add/remove apps (Release 3).
- Agent command list is where per-app updates and backup/restore commands will be
  added.
- Request records and the "may transcode" / "missing subtitle language" lists are the
  Release 2 worker's input queue.
- The worker will be its own container with no Docker socket.
- Auth code separates "who is logged in" from "what they can do" so friend accounts can
  be added.

## 12. Working agreements
- Explanations to the owner in plain language, by what changes mean for the user.
- Commits pushed straight to `main`; no branches or pull requests.
- Commit trailer `Co-Authored-By: Claude …` kept; no `Claude-Session:` link.
- Repo will become public: no secrets, IPs or personal paths in code or history.
