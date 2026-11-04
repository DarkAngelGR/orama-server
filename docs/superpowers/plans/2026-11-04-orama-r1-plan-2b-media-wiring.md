# Orama Release 1 — Plan 2b: Subtitles, quality and media servers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** After `apply`, Bazarr fetches subtitles in the owner's language order, Sonarr/Radarr prefer releases the chosen TV can play directly (Recyclarr + device profile), Plex and Jellyfin have Movies/TV libraries, and every import refreshes them. A second `apply` changes nothing.

**Architecture:** Same shape as Plan 2a. Orama drives the app APIs over `orama-net` with check-then-act steps (`orama.wiring.engine`). App config files Orama cannot reach are written only by the Agent's allowlisted `seed-config` command (new fixed `bazarr` format). Recyclarr runs as one fixed, Agent-run one-shot container whose YAML the Agent renders itself from validated choices. Plex account sign-in uses Plex's PIN flow from a CLI command until the Plan 3 setup page exists.

**Tech Stack:** Python ≥3.12 stdlib `urllib` (`orama.wiring.http`), pydantic 2, PyYAML (`safe_dump`/`safe_load`), pytest; app APIs: Bazarr `/api`, Sonarr/Radarr v3, Jellyfin, Plex Media Server + plex.tv v2; Recyclarr 8.

**Spec:** `docs/superpowers/specs/2026-11-03-orama-release-1-design.md` — §5.2.6–7, §5.3.3 (Bazarr, Plex, Jellyfin, Sonarr/Radarr quality + media-server connections), §6.1–6.2, §6.5, §4.6. Read first: `AGENTS.md`, `docs/STATUS.md`, `docs/superpowers/plans/2026-11-03-orama-r1-plan-1-notes.md` (incl. "Plan 2a — done"). This plan replaces Codex's 2026-11-04 draft of the same file; Codex's contract probes (`tests/integration/media_contracts.py`, `test_pinned_*`) are kept and extended in Task 1.

**Model policy (owner):** cheaper model (`sonnet`) by default; most capable (`opus`) for tasks marked **[opus]** (Agent, compose pins, secrets) and their reviews.

## Global Constraints

- Everything in Plan 1 and Plan 2a Global Constraints still holds (Agent-only Docker socket, exact Agent pins for any new compose key/env var/command with refusal tests, no secrets in compose/logs/argv/URLs/exceptions/test output, check-then-act, Orama manages only entries named "… (Orama)", plain-language messages, commits straight to `main`).
- Media roots: Movies `/data/media/movies`, TV `/data/media/tv` (spec §4.3).
- Quality (spec §6.2): `"4k"` = 4K when available; `"1080p"`; `"mixed"` = 4K movies + 1080p TV — **default `"mixed"`**.
- Devices (spec §6.1): `samsung_s95f` (reference), `lg`, `apple_tv`, `nvidia_shield`, `safest` — default `safest`. S95F rules: no DTS (any DTS variant), no multichannel LPCM, no FLAC, TrueHD OK, HDR10/HDR10+ OK, no Dolby Vision; avoid DV profile 5 hardest (no HDR10 fallback). Other devices are best-effort.
- Scores: unplayable = **-10000**; device-preferred = **+1500** (layered on TRaSH scores, which Recyclarr applies).
- Subtitles (spec §5.2.7, §6.5): ordered languages, default **`("el", "en")`**; prefer SRT (text) over image/styled subtitles.
- Plex: sign-in only via Plex's PIN/OAuth flow; Orama never sees the Plex password. Hardware transcoding needs Plex Pass — say so plainly when missing.
- Jellyfin: first run via its startup API with an admin user; QuickSync/VAAPI only with `/dev/dri`, NVENC only with `nvidia: true`.

## Fixed values

| Item | Value |
|---|---|
| Bazarr | base `http://bazarr:6767`, header `X-API-KEY`, key in `/config/config/config.yaml` at `auth.apikey` (observed, CI run 41918698706); store `apikey:bazarr` |
| Bazarr Orama profile | language profile named `Orama`, languages in setup order, no forced/HI |
| Jellyfin | base `http://jellyfin:8096`; admin user `orama-admin`, password store `password:jellyfin` (`token_urlsafe(24)`); API key store `apikey:jellyfin` (created via Jellyfin, app name `Orama`); `/Startup/*` returns 401 after completion (observed) |
| Plex | base `http://plex:32400`; plex.tv `https://plex.tv/api/v2`; client id store `plex:client_id` (`uuid4`), product `Orama`; token store `token:plex` |
| Libraries | names `Movies` → `/data/media/movies`, `TV Shows` → `/data/media/tv` |
| *arr → media server | Sonarr/Radarr notifications `Plex (Orama)` (implementation `PlexServer`: `host=plex`, `port=32400`, `authToken`, `updateLibrary=true`) and `Jellyfin (Orama)` (implementation `MediaBrowser`: `host=jellyfin`, `port=8096`, `apiKey`, `updateLibrary=true`, `notify=false`) — fields observed in CI; triggers on download, upgrade, rename |
| Recyclarr | image from `CATALOG["recyclarr"]`; Agent command `recyclarr-sync`; config dir `<config_root>/config/recyclarr`, file `recyclarr.yml` written by the Agent (0600, PUID) and deleted after the run |
| Plex LAN | env `ADVERTISE_IP=http://<lan_address>:<plex host port>/` when `setup.lan_address` is set |

## Review Focus

1. **Plex sign-in not done yet** (the owner hasn't approved the PIN) → Plex steps are skipped with "Plex is waiting for sign-in — run `python -m orama.cli plex-sign-in`", while Jellyfin, Bazarr and Recyclarr steps still succeed. **[T8, T10]**
2. **Jellyfin already set up by hand** (startup completed, Orama's password unknown) → apply never resets or recreates the admin; it reports "Jellyfin is already set up — enter its admin password with set-secret password:jellyfin" and continues with the other apps. **[T9]**
3. **Owner changes device or quality later** → the old Orama device scores are removed or replaced, no stale -10000 scores remain, user-made custom formats keep their scores. **[T6, T7]**
4. **A subtitle language list that changes order** (EN first, then back to EL first) → Bazarr's Orama profile follows exactly; nothing else in Bazarr changes. **[T5]**
5. **An existing user library at the same path under another name** (e.g. "Films" at `/data/media/movies`) → found by path, not duplicated, not renamed. **[T8, T9]**

---

## File structure

```
src/orama/setup_model.py           modify: playback prefs, lan_address (T2)
src/orama/secrets.py               modify: new app secrets (T2)
src/orama/cli.py                   modify: set-secret names (T2), plex-sign-in (T8), apply order (T10)
src/orama_agent/seed.py            modify: bazarr YAML-block format (T3)
src/orama/compose.py, catalog.py   modify: ADVERTISE_IP, NVIDIA caps (T4)
src/orama_agent/validate.py        modify: pins for T4 (T4)
install.sh, src/orama/cli.py       modify: --lan-address (T4)
src/orama/wiring/bazarr.py         (T5)
src/orama/profiles/                devices/*.yaml, trash.yaml, compile.py (T6)
src/orama_agent/recyclarr.py       Agent-side render + fixed argv (T7)
src/orama/wiring/recyclarr.py      (T7)
src/orama/wiring/plex.py           (T8)
src/orama/wiring/jellyfin.py       (T9)
src/orama/wiring/arr.py, plan.py   modify: media-server notifications, order (T10)
docs/superpowers/plans/2026-11-04-orama-r1-plan-2b-notes.md   observed contracts (T1)
tests/...                          mirrors src
```

---

### Task 1: Finish the pinned-image contract experiments

**Files:** Modify `tests/integration/media_contracts.py`, `tests/integration/test_install_and_apply.py`; create `docs/superpowers/plans/2026-11-04-orama-r1-plan-2b-notes.md`.

**Interfaces:** Produces the notes file every later task reads. Each contract line says *observed* (with CI run id) or *assumed*.

Already observed (record them in the notes as-is): `PlexServer`/`MediaBrowser` notification fields; Bazarr `auth.apikey` + `X-API-KEY`, anonymous 401; Jellyfin `/Startup/*` 401 after completion; Recyclarr `sync radarr --preview --config` exits 0.

- [ ] **Step 1: Add probes (print sanitized shapes only, never values):**
  - Bazarr 1.6.2: the settings write endpoint and field names for enabling Sonarr/Radarr (host, port, apikey, base url), language profiles, enabled languages, providers, and "prefer SRT"/subtitle format options; how a settings change is applied (immediate vs restart); which Greek/English providers work without an account (list names).
  - Jellyfin 12.4: `/Startup/Configuration` fields; `POST /Users/AuthenticateByName` header format; `/Library/VirtualFolders` create (name, `collectionType`, `PathInfos`); `/Auth/Keys` create/list; `/System/Configuration/encoding` hardware fields.
  - Recyclarr 8.8.1: `recyclarr list templates`, `recyclarr list custom-formats radarr` and `sonarr` — record the template names for the three quality choices and the trash_ids + names for: DV (all), DV without HDR10 fallback, DTS, DTS-HD MA, DTS-X, DTS-HD HRA, DTS-ES, TrueHD, TrueHD Atmos, PCM/LPCM, FLAC, HDR10, HDR10+.
  - Plex 1.43.5: unclaimed `/identity`; which local endpoints need a token; library create query fields (`type`, `agent`, `scanner`, `location`, `language`); prefs keys `HardwareAcceleratedCodecs`, `LanNetworksBandwidth`. Claim/account steps are owner-only — mark *assumed* from Plex docs.
- [ ] **Step 2: Push, read the CI log, write the notes file** with every observed shape and the run id. A contract that cannot be observed in CI is marked *assumed* and its consuming task must keep an owner check in Task 11.
- [ ] **Step 3: Commit** `test: record Plan 2b pinned-image contracts`.

---

### Task 2 [opus]: Playback preferences and new secrets in the setup

**Files:** Modify `src/orama/setup_model.py`, `src/orama/secrets.py`, `src/orama/cli.py`; tests `tests/test_setup_model.py`, `tests/test_secrets.py`, `tests/test_cli.py`.

**Interfaces:**
- Produces in `DesiredSetup`:
  - `subtitle_languages: tuple[LanguageCode, ...] = ("el", "en")` — `LanguageCode` = ISO 639-1 lowercase `^[a-z]{2}$`, 1–5 entries, unique.
  - `device: Literal["samsung_s95f", "lg", "apple_tv", "nvidia_shield", "safest"] = "safest"`.
  - `quality: Literal["4k", "1080p", "mixed"] = "mixed"`.
  - `lan_address: IPv4Address | None = None` (private ranges only; it is used in the Plex env in Task 4).
  - Plan 1/2a setup files load unchanged with these defaults.
- Produces in `secrets.py`: constants `BAZARR_KEY_NAME = "apikey:bazarr"`, `JELLYFIN_PASSWORD_NAME = "password:jellyfin"`, `JELLYFIN_KEY_NAME = "apikey:jellyfin"`, `PLEX_TOKEN_NAME = "token:plex"`, `PLEX_CLIENT_ID_NAME = "plex:client_id"`. `ensure_app_keys` creates `apikey:bazarr` (when bazarr enabled), `password:jellyfin` (when jellyfin enabled) and `plex:client_id` (when plex enabled); never overwrites.
- `set-secret` name allowlist gains exactly `password:jellyfin` (owner-set Jellyfin admin password) and `opensubtitles:username` / `opensubtitles:password` (optional provider account). `token:plex` is NOT settable by hand.

- [ ] **Step 1: Failing tests:** defaults; old setup files load; duplicate language refused; unknown device/quality refused; `lan_address` public IP refused; round-trip keeps language order; secrets never appear in `setup.json` text; new `ensure_app_keys` names created once; set-secret accepts the three new names and refuses `token:plex`.
- [ ] **Step 2: Run** `uv run pytest tests/test_setup_model.py tests/test_secrets.py tests/test_cli.py -v` → FAIL. **Step 3: Implement.** **Step 4: Run** `uv run pytest -m "not integration" && uv run ruff check` → PASS.
- [ ] **Step 5: Commit** `feat: playback preferences and media-server secrets in the setup`.

---

### Task 3 [opus]: `seed-config` format for Bazarr's API key

**Files:** Modify `src/orama_agent/seed.py`, `src/orama_agent/server.py` (only if the format needs a new path shape), tests `tests/agent/test_seed.py`, `tests/agent/test_server.py`; `src/orama/cli.py` seeding step.

**Interfaces:**
- `seed-config {"app": "bazarr", "values": {"api_key": <[0-9a-f]{32}>}}` writes `config/bazarr/config/config.yaml`, YAML block `auth:` with two-space-indented `apikey: <value>`; keeps every other line byte-for-byte; appends the block if missing; `None` when unchanged. Same no-follow, 0600 PUID, stop/patch/start-if-changed rules as SABnzbd/qBittorrent (Plan 2a T2).
- apply seeds Bazarr right after the deploy, with the other seeds.

- [ ] **Step 1: Failing tests:** patch an existing `auth:` block (other keys under `auth:` and other sections untouched); append when `auth:` is absent; unchanged → `None`; refuse a non-hex key, a key with `\n`/`:`/`#`; refuse a symlinked `config` dir or file; a FIFO; an oversized file; no key value in any log or error.
- [ ] **Step 2–4:** RED, implement, GREEN (`uv run pytest -m "not integration" && uv run ruff check`).
- [ ] **Step 5: Commit** `feat: agent seeds bazarr's api key`.

---

### Task 4 [opus]: Plex LAN address and NVIDIA capabilities (compose + Agent pins + installer)

**Files:** Modify `src/orama/compose.py`, `src/orama/catalog.py`, `src/orama_agent/validate.py`, `install.sh`, `src/orama/cli.py` (bootstrap); tests `tests/test_compose.py`, `tests/agent/test_validate.py`, `tests/test_install_sh.py`, `tests/test_cli.py`.

**Interfaces:**
- Plex env gains `ADVERTISE_IP=http://<lan_address>:<setup.ports["plex"]>/` only when `setup.lan_address` is set. Agent pin: value must fullmatch `http://<private IPv4>:<1-65535>/` and the port must equal the Plex service's published host port.
- When `setup.nvidia`, Plex and Jellyfin env gain `NVIDIA_DRIVER_CAPABILITIES=compute,video,utility` and `NVIDIA_VISIBLE_DEVICES=all`; Agent pins the exact values and refuses them without the GPU reservation.
- `bootstrap --lan-address A.B.C.D|none`; install.sh passes the address it already prints (`print_address` logic) if it is a private IPv4, else `none`; the draft stores it.

- [ ] **Step 1: Failing tests:** builder output with/without `lan_address`/`nvidia`; Agent accepts builder output and refuses: public IP, wrong port, extra path, `$`, NVIDIA env without reservation, other values; install.sh DRY_RUN passes `--lan-address` with a TEST-NET or 127.0.0.1-style value per hygiene rules (use a value built at runtime in tests).
- [ ] **Step 2–4:** RED, implement, GREEN.
- [ ] **Step 5: Commit** `feat: advertise plex on the lan; nvidia capabilities for media servers`.

---

### Task 5: Bazarr wiring

**Files:** Create `src/orama/wiring/bazarr.py`, `tests/wiring/test_bazarr.py`.

**Interfaces:**
- `bazarr_steps(setup, store, http) -> list[Step]`: `wait:bazarr` (`/api/system/status` 200, 300 s); `bazarr:apps` (enable Sonarr/Radarr per enabled app, host/port/apikey per Fixed values; needs `wait:sonarr`/`wait:radarr`); `bazarr:languages` (enabled languages = setup order; profile `Orama`, default for series and movies); `bazarr:providers` (the no-account providers recorded in Task 1 notes; OpenSubtitles.com only when both `opensubtitles:*` secrets exist); `bazarr:format` (prefer SRT). Endpoints and field names come from the Task 1 notes.
- Compare-then-write; Orama touches only its own profile and the fields above.

- [ ] **Step 1: Failing tests (FakeHttp):** first run writes each once, second run all unchanged; language order change rewrites only the profile **[RF4]**; Sonarr disabled → Bazarr's Sonarr link disabled; missing OpenSubtitles creds → provider not enabled, no error; key never in messages.
- [ ] **Step 2–4:** RED, implement, GREEN. **Step 5: Commit** `feat: wire bazarr`.

---

### Task 6: Device and quality profiles (pure data + compiler)

**Files:** Create `src/orama/profiles/__init__.py`, `src/orama/profiles/trash.yaml` (symbolic name → trash_id per app, from Task 1 notes), `src/orama/profiles/devices/{samsung_s95f,lg,apple_tv,nvidia_shield,safest}.yaml`, `src/orama/profiles/compile.py`; `tests/test_profiles.py`; package data in `pyproject.toml`.

**Interfaces:**
- Device file: `{name: str, verified: bool, unplayable: [symbolic names], preferred: [symbolic names]}`. S95F: unplayable `dv`, `dv_no_fallback`, `dts`, `dts_hd_ma`, `dts_x`, `dts_hd_hra`, `dts_es`, `pcm_multichannel`, `flac`; preferred `hdr10`, `hdr10_plus`, `truehd_atmos`, `truehd`. `safest`: unplayable = S95F's list plus `truehd`, `truehd_atmos`; preferred `hdr10`. Others: best-effort lists, `verified: false`.
- `compile_profile(device: str, quality: str) -> dict[str, AppRecyclarr]` where `AppRecyclarr = {"templates": list[str], "scores": dict[trash_id, int]}` for `sonarr`/`radarr`: templates per quality (`mixed` → radarr UHD template, sonarr 1080p template; names from Task 1 notes), scores = -10000 for unplayable, +1500 for preferred. Deterministic (sorted).

- [ ] **Step 1: Failing tests:** every symbolic name in every device file exists in `trash.yaml` for both apps (or is explicitly app-specific); S95F output pins the exact unplayable/preferred trash_ids and scores; `mixed` = radarr UHD + sonarr 1080p; deterministic output; switching device produces a score map with no leftover entries from the old device **[RF3]**; unknown device/quality refused.
- [ ] **Step 2–4:** RED, implement, GREEN. **Step 5: Commit** `feat: device and quality profiles`.

---

### Task 7 [opus]: Recyclarr one-shot via the Agent

**Files:** Create `src/orama_agent/recyclarr.py`, `src/orama/wiring/recyclarr.py`; modify `src/orama_agent/server.py`, `src/orama_agent/runner.py`, `src/orama/agent_client.py`; tests `tests/agent/test_recyclarr.py`, `tests/wiring/test_recyclarr.py`.

**Interfaces:**
- Agent command `recyclarr-sync {"apps": {"sonarr"|"radarr": {"templates": [..], "scores": {trash_id: int}}}}` → `{"changed": bool}`.
  - Validates: apps ⊆ {sonarr, radarr}; template names match `^[a-z0-9-]{1,80}$` and appear in a pinned allowlist (`TEMPLATES` constant from Task 1 notes); trash_ids `^[0-9a-f]{32}$`; scores ints in −10000..10000; ≤ 200 scores per app.
  - Renders YAML itself with `yaml.safe_dump` (never accepts YAML from the caller); API keys read by the Agent from its own `app-env/<app>.env`; base URLs fixed `http://sonarr:8989` / `http://radarr:7878`; custom formats Orama scores are grouped under a quality profile named per template.
  - Writes `config/recyclarr/recyclarr.yml` (no-follow, 0600, PUID), runs exactly `docker run --rm --network orama-net --user PUID:PGID -v <config_root>/config/recyclarr:/config <CATALOG recyclarr image> sync --config /config/recyclarr.yml` (fixed argv, scrubbed env, own timeout 900 s), then deletes the YAML whether the run succeeded or not. `changed` comes from Recyclarr's exit status + a hash of the rendered config stored in `app-env/recyclarr.state`.
- Orama step `recyclarr:sync` (needs `wait:sonarr`/`wait:radarr`): sends `compile_profile(setup.device, setup.quality)`; then verifies through each *arr's API (`/api/v3/customformat`, `/api/v3/qualityprofile`) that every score is set; "changed" only after verification.

- [ ] **Step 1: Failing tests:** caller YAML/extra keys/unknown templates/bad trash_id/out-of-range scores refused; rendered YAML contains keys only in the file (never argv/logs/errors); exact argv pinned; YAML deleted after success and failure; second identical call `changed: false` and no `docker run`; verification failure → step failed with a plain message; device switch removes old Orama scores **[RF3]**; user custom formats untouched.
- [ ] **Step 2–4:** RED, implement, GREEN. **Step 5: Commit** `feat: recyclarr profiles via the agent`.

---

### Task 8 [opus]: Plex sign-in, claim, libraries

**Files:** Create `src/orama/wiring/plex.py`, `tests/wiring/test_plex.py`; modify `src/orama/cli.py` (`plex-sign-in`).

**Interfaces:**
- `begin_sign_in(store, http) -> SignIn(code: str, url: str, pin_id: int, expires_at: float)` (POST `https://plex.tv/api/v2/pins?strong=true`, headers `X-Plex-Product: Orama`, `X-Plex-Client-Identifier: <plex:client_id>`); `poll_sign_in(store, http, pin_id) -> Literal["waiting", "done", "expired"]` — on done stores `token:plex`, returns only the status. Only `https://plex.tv` is ever contacted for account calls (fixed origin, no redirects).
- CLI `python -m orama.cli plex-sign-in --setup PATH`: prints "Open <url> and enter code <code>", polls every 2 s up to expiry, prints "✓ Plex signed in" or a plain failure. Never prints the token.
- `plex_steps(setup, store, http)`: `wait:plex` (`/identity` 200); `plex:claim` — skipped with the RF1 message when no token; if `/identity` shows claimed by another account → fail "This Plex server belongs to another Plex account — Orama won't take it over."; else claim via plex.tv claim token + local `/myplex/claim`; `plex:libraries` (find by path, else create `Movies`/`TV Shows`) **[RF5]**; `plex:network` (`LanNetworksBandwidth` = the `/24` of `lan_address` when set); `plex:hardware` (`HardwareAcceleratedCodecs=1` only with `/dev/dri` or NVIDIA; if the account has no Plex Pass, step reports "Hardware transcoding needs Plex Pass — playback still works with direct play." as changed-free info, not a failure).

- [ ] **Step 1: Failing tests (FakeHttp incl. a fake plex.tv):** pending / expired / denied PIN; token never in output, logs, exceptions; claimed-by-another refused; already claimed by this account → unchanged; library found by path under another name → not duplicated; only one of two libraries created then retry → creates only the missing one; no Pass → plain info line; no GPU → no hardware pref write.
- [ ] **Step 2–4:** RED, implement, GREEN. **Step 5: Commit** `feat: plex sign-in, claim and libraries`.

---

### Task 9: Jellyfin first run, libraries and hardware

**Files:** Create `src/orama/wiring/jellyfin.py`, `tests/wiring/test_jellyfin.py`.

**Interfaces:**
- `jellyfin_steps(setup, store, http)`: `wait:jellyfin` (`/System/Info/Public` 200); `jellyfin:startup` (if `StartupWizardCompleted` false: `/Startup/Configuration`, `/Startup/User` with `orama-admin` + `password:jellyfin`, `/Startup/Complete`); `jellyfin:auth` (AuthenticateByName → session token, kept in memory only; failure on a completed server → the RF2 message, steps after it skipped); `jellyfin:api-key` (create `Orama` key if `apikey:jellyfin` missing or invalid, store it); `jellyfin:libraries` (find by path, else create `Movies`/`TV Shows` per Fixed values) **[RF5]**; `jellyfin:hardware` (`vaapi` with `/dev/dri`, `nvenc` with nvidia, unchanged otherwise). Endpoints from Task 1 notes.

- [ ] **Step 1: Failing tests (FakeHttp):** fresh server → full first run once, second run unchanged; failure after `/Startup/User` then retry → resumes without creating a second admin; completed server + wrong password → RF2 message, no reset attempt **[RF2]**; existing library at the path → kept; no GPU → no encoding write; password/key/token never in messages.
- [ ] **Step 2–4:** RED, implement, GREEN. **Step 5: Commit** `feat: jellyfin first run, libraries and hardware`.

---

### Task 10: Import refresh, step order and `apply`

**Files:** Modify `src/orama/wiring/arr.py`, `src/orama/wiring/plan.py`, `src/orama/cli.py`; tests `tests/wiring/test_arr.py`, `tests/wiring/test_plan.py`, `tests/test_cli.py`.

**Interfaces:**
- `arr_steps` gains `<app>:notify:plex` (needs `plex:libraries`; `Plex (Orama)` per Fixed values with `token:plex`) and `<app>:notify:jellyfin` (needs `jellyfin:api-key`; `Jellyfin (Orama)`); deselected media server → its `(Orama)` notification deleted; masked `authToken`/`apiKey` handled with the Plan 2a SHA-256 marker rule.
- `build_steps` order: sabnzbd, qbittorrent, sonarr, radarr, prowlarr, bazarr, recyclarr, plex, jellyfin, then the notify steps. Pending Plex sign-in skips only Plex-dependent steps **[RF1]**.

- [ ] **Step 1: Failing tests:** Plex only / Jellyfin only / both; Plex pending while Jellyfin, Bazarr, Recyclarr succeed **[RF1]**; deselect Jellyfin → only `Jellyfin (Orama)` removed; second run unchanged; apply prints plain lines and exit 1 only for real failures.
- [ ] **Step 2–4:** RED, implement, GREEN. **Step 5: Commit** `feat: media servers refresh on import; apply wires plan 2b`.

---

### Task 11: Integration — real apps, second run, owner checks

**Files:** Modify `tests/integration/test_install_and_apply.py`, `tests/integration/media_contracts.py` (keep only probes still useful as regression checks).

- [ ] **Step 1: Assertions after first apply** (pinned images, CI): Bazarr linked to Sonarr/Radarr with profile `Orama` = `["el","en"]`; Recyclarr scores present in Radarr/Sonarr for the default device/quality; Jellyfin first run done, `Movies`/`TV Shows` at the right paths, API key works; Sonarr/Radarr have `Jellyfin (Orama)`; Plex reported "waiting for sign-in" (CI has no Plex account) and nothing else failed.
- [ ] **Step 2: Second apply:** nothing changed, container IDs unchanged except restarts the seeds legitimately cause on first install; leak scan covers the new secrets.
- [ ] **Step 3: Interruption:** stop Jellyfin mid-apply (kill the container before `jellyfin:libraries`), re-apply → only unfinished steps change.
- [ ] **Step 4: Owner checks** appended to the Plan 5 checklist in the notes file: Plex PIN sign-in and claim, Plex Pass + hardware transcoding, LAN discovery via `ADVERTISE_IP`, a real Greek subtitle fetched, an S95F file showing **Direct Play** in Plex.
- [ ] **Step 5:** Push; iterate until `unit`, `images`, `integration`, `publish` are green. **Commit** `test: integration for plan 2b`.

---

## Plan 2b done when

- All four CI jobs green on `main`; final whole-plan review + one fix round done.
- Claude Security scan of the Plan 2b range run by the owner (`/claude-security:claude-security` → Scan changes) and every finding fixed or accepted.
- `docs/STATUS.md` and the Plan 2b notes file record the last tested commit, observed vs assumed contracts, and the owner checks for Plan 5.
- Owner summary (plain language): "After apply, subtitles download in Greek then English, Sonarr/Radarr prefer releases your TV plays directly, Jellyfin is set up with Movies and TV, Plex is set up after you sign in once with `python -m orama.cli plex-sign-in`, and new downloads appear in both automatically."
