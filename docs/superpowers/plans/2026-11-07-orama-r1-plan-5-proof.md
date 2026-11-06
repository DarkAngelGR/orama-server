# Orama Release 1 — Plan 5: Proof & Validation

**Goal:** Verify Release 1 end-to-end against real test layers, document the owner's manual hardware checklist for the DXP4800 Plus NAS (UGOS Pro), and perform repository security hygiene auditing.

**Architecture:** Retain all architectural isolation guarantees from Plans 1–4. The Agent alone communicates with the Docker engine; Orama communicates via authenticated APIs.

**Tech Stack:** Node.js 22 runtime, Express, EJS, HTMX; TypeScript; Docker Compose.

**Spec Reference:** `docs/superpowers/specs/2026-11-03-orama-release-1-design.md`, §10 (Testing & Definition of Done) and §12.

---

## Plan 5 Tasks

### Task 1: End-to-end VM Pipeline Verification
- Verify initial setup link generation and first-run password initialization.
- Test wizard steps: media server selection (Plex & Jellyfin), storage folders, download clients (qBittorrent & SABnzbd), Gluetun VPN routing, playback device profiles, and subtitle preferences.
- Verify search, movie/show submission, and live 4-stage pipeline progression (`Searching` → `Downloading` → `Importing` → `Ready`).
- Verify post-import Direct Play assessment and Bazarr subtitle language matching.

### Task 2: Owner NAS Checklist (DXP4800 Plus / UGOS Pro)
- Plain-language verification checklist for UGREEN NAS (UGOS Pro / Debian-based).
- Check storage volume persistence at `/volume1/docker/orama`.
- Validate hardware video transcoding group detection (Intel QuickSync render GID).
- Test ProtonVPN WireGuard port forwarding integration.
- Verify playback on Samsung S95F / Apple TV 4K achieves Direct Play without server transcoding.

### Task 3: Security & Secrets Hygiene Audit
- Enforce zero secrets in client HTML, URLs, logs, or error responses.
- Audit SameSite=None, Secure cookie policies and token fallback mechanics.
- Verify Agent Unix socket isolation and allowlisted compose schemas.

### Task 4: Release 1 Completion Sign-off
- Update `docs/STATUS.md` reflecting Release 1 status.
- Document carry-forward items for Release 2 (worker container for audio transcoding and subtitle translation).
