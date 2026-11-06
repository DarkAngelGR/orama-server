# Orama Release 1 — Plan 4: Requests Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Request a movie, episode, season or ongoing show from Orama and see its download, import, playback and subtitle status through restarts.

**Architecture:** Keep the existing Flask/Jinja/htmx application. A transactional SQLite request store records intent before any app write; a single elected background reconciler checks Sonarr/Radarr/Bazarr and resumes interrupted work. Authenticated webhooks only enqueue a refresh; app APIs remain the authority for status.

**Tech Stack:** Existing Python ≥3.12, Flask, Jinja, htmx, pydantic, pytest; standard-library sqlite3, threading and existing file locks/HTTP client. No new database service or frontend build.

**Spec:** `docs/superpowers/specs/2026-11-03-orama-release-1-design.md`, §§5–11. Read `AGENTS.md`, `docs/tasks/README.md`, and the Plan 1, 2b and 3 notes first; notes override earlier implementation text.

**Status:** Draft implementation plan prepared by Codex from the approved release design; awaiting plan review, implementation not started. Owner deferred the Claude Security scans on 2026-11-06 until Claude is available Wednesday 2026-11-11. Deferral permits development; it does not record the scans as passed.

## Global Constraints

- Only the Agent has the Docker socket. Any new Agent command has exact request validation and refusal tests. No media/config mounts are added to Orama.
- No secrets in HTML, URLs, argv, logs, exceptions or assertion output. Upstream text is untrusted; validate identifiers and expose plain messages rather than raw errors.
- Preserve session, Host, CSRF, Origin, response-size, redirect and CSP protections. Mutations require POST plus the existing permission check and CSRF, except the precisely named webhook routes with their own authentication.
- Use pinned app images and pinned guide data already in the repository. No updates to third-party pins as a side effect of this plan.
- Check-then-act; preserve unrelated monitoring, custom profiles and existing files. A retry or two browser tabs must not create duplicate downloads or records.
- User-visible stages: Searching → Downloading (%) → Importing → Ready. Waiting reasons and playback/subtitle annotations are separate fields, not successful completion.
- Default playable-release wait is 24 hours. Choices: always wait, never wait, custom hours. User-made profiles are never weakened to achieve fallback.
- Media roots remain `/data/media/movies` and `/data/media/tv`; the installer owns changing them.
- Server-rendered pages, vendored scripts, no CDN; 360 px without horizontal overflow; touch targets at least 44 × 44 px. Reuse Plan 3 styles and accessible live-update patterns.
- Commit/push to `main`, task claims and reports in `docs/tasks/plan-4/`. Required local checks: `uv run pytest -m "not integration"`, `uv run ruff check`, `uvx --from shellcheck-py shellcheck install.sh`. Check unit, browser, images, integration and publish CI for source changes.

## Implementation decisions and alternatives

- **SQLite instead of another JSON state file:** transactions and uniqueness cover concurrent submissions and durable work claims. JSON plus a global lock would work for a small list but makes crash-safe events, child episodes and scheduling harder. A separate queue/database service adds deployment work without an R1 need.
- **Reconciliation plus webhook hints:** webhooks alone lose events on reboot; polling alone adds needless delay. Neither may declare Ready without checking the actual imported file in the app API.
- **One executor inside the existing process:** elect it with an existing no-follow file lock; no new container. Network I/O never runs inside a SQLite write transaction. Web requests only persist intent and wake the executor.
- **Fallback uses an Orama-owned alternate profile per app:** never change the shared strict profile or somebody else's profile. Exact score threshold behavior must be proved in Task 1 before implementation; request retry state must distinguish intent, remote submission and observed result.
- **Scope boundary:** the Plan 3 carry-forward Update Orama button cannot simply call an existing updater: `self-update` currently returns "not implemented in Plan 1". Root-side self-update and its helper lifecycle need a separate operations plan before Release 1 is complete. This is recorded as unfinished, not silently replaced by a nonfunctional button. Disk warning and reboot/drift checks remain in this plan.

## Fixed values and shared types

- Store: `<state_dir>/requests.sqlite3`, schema version via `PRAGMA user_version`; 0600 database, private state directory, bounded SQLite busy timeout 5 s; no secrets or raw webhook payloads stored. Foreign keys ON. Use rollback journaling initially; no DB connection shared across threads.
- Executor lock: `<state_dir>/requests.lock`, existing `state.locked(path, blocking=False)`/no-follow lock pattern. Shut down with the web process; never start threads at module import or in test app creation unless explicitly enabled.
- Poll active downloads every 5 s, other unresolved requests every 30 s, ready items with missing subtitle/media-info checks every 5 min. At most one upstream call per app at a time; bounded batches of 100 records. Inject clock and transport in tests.
- `RequestKind = movie | episode | season | series`; movie identity uses TMDB id; all TV identities use TVDB series id plus season/episode where applicable. Positive external ids; season 0 is permitted; episode ≥1. Reject irrelevant coordinates. Stored UUID request id; unique canonical target key prevents duplicate intents.
- `RequestSpec(kind, source_id, season=None, episode=None)`; `RequestRecord(id, spec, created_at, updated_at, revision, stage, waiting_reason, progress, playback_note, subtitle_note)`; timestamps UTC epoch seconds. Store remote app ids separately from external ids.
- `RequestUpdate(stage, waiting_reason, progress, playback_note, subtitle_note, next_check_at)` uses the same validated field types as RequestRecord; partial updates must distinguish unchanged from clear-to-None. Define both in Task 2. `SearchResult(kind, source_id, title, year, in_library, poster_ref)` and `MediaDetail(result, remote_id, seasons, episodes)` are normalized Task 3 DTOs; `AppSnapshot(complete, items, queue, history, files)` is keyed by remote ids, with no secrets, arbitrary URLs or raw exception data. Task 1 fixtures settle provider-to-DTO mappings.
- Child episode rows belong to a parent request. Overlapping episode/season/series requests share an episode target, but parent summaries remain independent. A series subscription stays active for future episodes while reporting current known episodes; it never claims the whole future show is complete.
- Webhook routes: `POST /hooks/sonarr`, `POST /hooks/radarr`, max 64 KiB JSON; one random 32-byte hex token per app in SecretStore `webhook:<app>`, sent in `Authorization: Bearer ...` if supported by the pinned notification schema. Task 1 must confirm support. No token in URL fallback. One narrowly scoped CSRF/session exception after Host validation and token check, not a `/hooks/*` blanket exception.
- Browser routes: `GET /search?q=...&type=movie|series`, `GET /media/<type>/<source_id>`, `POST /requests`, `GET /requests/<uuid>`, `GET /requests/<uuid>/status`, `POST /requests/<uuid>/retry`. Detail IDs and episode coordinates are revalidated server-side; no upstream URL accepted from a browser.
- Posters: same-origin endpoint selected by validated media id, never an arbitrary URL proxy. Bounded cache, image MIME/size checks, redirects refused; local placeholder if a safe pinned origin contract cannot be established.
- Settings additions: `request_wait_mode=timed|always|never` (default timed), `request_wait_hours=24` (integer 1–720), `disk_warning_percent=5` (integer 1–50). Existing saved setups load with defaults.

## Review Focus

1. Double click or crash after app accepts a request but before Orama records success: retry adopts the matching remote item and does not search twice without checking command/history state. Tasks 2, 4, 5.
2. Request one episode in an existing show: preserve other monitoring and never trigger an entire-season/show search accidentally. Tasks 1, 4, 11.
3. Duplicate, reordered or forged webhooks: authenticate before parsing identifiers; fetch current app state; an old event cannot regress Ready or falsely complete a request. Tasks 5, 8.
4. First language missing or media info incomplete: report what is known; never label missing data as Direct Play or all requested subtitles present. Tasks 7, 9.
5. Restart during submission or while VPN/app is down: bounded retries and explicit waiting status, no spin, no lost durable intent, no automatic weakening of a shared profile. Tasks 5, 6, 10, 11.

## File map

`src/orama/requests/{models,store,client,submit,reconcile,worker,playback,subtitles}.py` owns request domain logic. `src/orama/web/{requests,hooks,posters}.py` owns HTTP routes. Existing settings, security, auth, CLI and wiring modules are extended at their current boundaries. Tests mirror these modules under `tests/requests/` and `tests/web/`; real contracts and workflow checks live in `tests/integration/`.

## Task 1: Verify pinned request, media-info and webhook contracts

**Files:** create `tests/integration/request_contracts.py`, `docs/superpowers/plans/2026-11-06-orama-r1-plan-4-notes.md`; extend `tests/integration/test_install_and_apply.py`.
**Interfaces:** records exact endpoint/method/body/read-back shapes for lookup, series/movie add, episode monitoring, command search, queue/history/import, file media info, Bazarr subtitle status, notification headers and fallback scores. Later tasks consume these observations, not guesses.
- [ ] Write failing real-container assertions: movie/series lookup shapes (external catalogue network optional with explicit owner check); episode-only monitoring and search against disposable fixture data; queue/history pagination; file metadata; authenticated notification test to a disposable HTTP receiver; score thresholds blocking an incompatible release then allowing it in a separate test profile. Credentials never printed.
- [ ] Run required local checks and CI; record missing contracts as failures for required functionality, not green print-only probes. Tests touching real external catalogues must have deterministic offline fixtures and clearly reported optional online checks.
- [ ] Implement only the probes and cleanup. Store only scrubbed minimal contract fixtures with runtime-built keys. Restore modified test entries, or use separate disposable containers for irreversible work.
- [ ] Record observed versus assumed behavior, exact pinned versions and run id; adapt dependent task interfaces if evidence requires it, with rationale and cost.
- [ ] Commit `test: verify pinned request and webhook contracts`; fill task report after green CI.

## Task 2: Durable request store and idempotent identities

**Files:** create `src/orama/requests/__init__.py`, `models.py`, `store.py`, `tests/requests/test_store.py`.
**Interfaces:** `RequestStore(path: Path)`; `create(spec: RequestSpec, now: float) -> tuple[RequestRecord,bool]`; `get(id: str) -> RequestRecord | None`; `list_active(limit: int=100) -> list[RequestRecord]`; `update(id, expected_revision, changes) -> bool`; `enqueue_refresh(app, remote_id, now) -> None`; atomic `claim_due(owner, now, lease_seconds=60) -> RequestRecord | None`, `release(id, owner, next_check_at)`. Persist submission phase, app ids, child targets and per-action command observations. Schema migration is transactional; unknown newer version fails clearly without overwriting it.
- [ ] Tests: `test_duplicate_target_has_one_record` (two threads/processes); `test_episode_coordinates_validate`; `test_overlapping_requests_share_episode_target`; `test_restart_retains_intent_and_refresh`; `test_expired_claim_can_be_recovered`; `test_stale_revision_cannot_overwrite_newer_state`; `test_private_database_and_corrupt_schema_refusal`. Use real temporary SQLite databases.
- [ ] Run `uv run pytest tests/requests/test_store.py -q` → FAIL before implementation.
- [ ] Implement the interfaces with parameterized SQL, foreign keys, short transactions and uniqueness constraints. Validate every public update; no arbitrary field/SQL input.
- [ ] Run targeted tests → PASS, then required local checks and CI.
- [ ] Commit `feat: persist media requests and resumable work`.

## Task 3: Safe lookup and app read client

**Files:** create `src/orama/requests/client.py`, `tests/requests/test_client.py`.
**Interfaces:** `MediaClient(setup, store, http)` consumes existing `Http` and SecretStore; `search(kind, query) -> list[SearchResult]`; `detail(kind, source_id) -> MediaDetail`; `snapshot(app, remote_ids) -> AppSnapshot`. Define immutable SearchResult/MediaDetail/AppSnapshot in models from Task 1 fixtures; return allowlisted fields only. Queries trimmed, 1–200 characters; limit results to 50; paginated queues bounded with explicit incomplete snapshot status.
- [ ] Tests assert correct fixed origin/header, URL-encoded query, already-in-library marking, missing app, 401, timeout, malformed body, malicious titles/poster URLs, pagination and no secret/error-body echo.
- [ ] Run `uv run pytest tests/requests/test_client.py -q` → FAIL.
- [ ] Implement normalization and plain `RequestError` messages; all provider IDs and response types checked. A partial queue is not evidence that a download disappeared.
- [ ] Targeted tests → PASS; required local checks and CI.
- [ ] Commit `feat: search media through Sonarr and Radarr`.

## Task 4: Idempotent movie and TV submission

**Files:** create `src/orama/requests/submit.py`, `tests/requests/test_submit.py`.
**Interfaces:** `submit(record: RequestRecord, client: MediaClient, requests: RequestStore, now: float) -> None`. Extend client with typed `ensure_movie`, `ensure_series`, `monitor_episodes`, `search_targets`, `command_status` using Task 1 contracts. Use existing Orama quality profile and pinned roots. Persist remote identity before starting searches; an ambiguous timeout triggers read-back rather than blind creation/research.
- [ ] Tests: movie absent/present/file already imported; new single episode leaves other episodes unmonitored; existing show preserves unrelated monitoring; season 0; whole show includes future episodes; overlapping requests; add/search response lost; unknown episode/aired-later item; app disabled; concurrent apply lock refusal.
- [ ] Run `uv run pytest tests/requests/test_submit.py -q` → FAIL.
- [ ] Implement phase-based check-then-act. Never delete media or unmonitor unrelated items. Do not hold setup/DB locks across HTTP; share apply.lock non-blockingly with remote mutations so setup changes and submission do not race.
- [ ] Targeted tests → PASS; required checks and CI.
- [ ] Commit `feat: submit movie episode season and series requests`.

## Task 5: Status reconciliation and background scheduling

**Files:** create `src/orama/requests/reconcile.py`, `worker.py`, `tests/requests/test_reconcile.py`, `test_worker.py`; modify `src/orama/__main__.py`, `web/__init__.py`.
**Interfaces:** `reconcile(record, snapshot, now) -> RequestUpdate`; `RequestWorker(store, client_factory, clock)` with `start()`, `wake()`, `stop(timeout=5)` and deterministic `tick(now)`; `create_app(..., request_worker=None)` injection. Launcher owns lifecycle and exclusive election lock.
- [ ] Tests: waiting/app down; downloader queue progress clamped 0–100; unknown total omits percentage; import pending; file exists → Ready; failed import stays non-ready; queue entry gone uses history/file; duplicate/reordered hints; independent tasks still run; worker crash expires lease; two workers cannot perform duplicate work; active series discovers future episodes without losing child history.
- [ ] Run `uv run pytest tests/requests/test_reconcile.py tests/requests/test_worker.py -q` → FAIL.
- [ ] Implement authoritative snapshots, bounded polling/backoff and stage derivation. A Ready file later deleted becomes explicitly unavailable after a current API confirmation, never from a stale webhook. Clock or app outages preserve timestamps/intent.
- [ ] Targeted tests → PASS; required checks and CI.
- [ ] Commit `feat: reconcile request progress and resume after restart`.

## Task 6: Playable-release waiting and per-request fallback

**Files:** modify `setup_model.py`, `web/forms.py`, `web/settings.py`, settings templates; create `src/orama/requests/fallback.py`, `tests/requests/test_fallback.py`; extend request client, submit/reconcile and wiring quality tests.
**Interfaces:** `fallback_due(record, setup, now) -> bool`; `ensure_fallback_profile(app, client, setup) -> int`. Alternate name `Orama fallback`; copy current strict qualities and adjust only Orama's device-score blockers/threshold proven in Task 1. Attach only to requested remote items eligible for fallback; preserve a user's profile assignment. Snapshot fallback policy/deadline at request creation; explicit Retry uses current policy without resetting elapsed wait silently.
- [ ] Tests: 23:59 versus 24:00, always/never/custom hours, restart retains deadline, stricter quality constraints preserved, strict shared profile unchanged, user profile unchanged, existing unrelated series blocks automatic fallback with a clear reason, series-level fallback applies only when safe for all monitored targets, failed fallback assignment retries by read-back.
- [ ] Run `uv run pytest tests/requests/test_fallback.py -q` → FAIL.
- [ ] Implement settings validation plus policy; follow Task 1 evidence for profile thresholds. Sonarr has series-level quality profiles: never promise per-episode profile isolation. If unrelated monitored targets prevent safe fallback, keep waiting and explain why rather than modifying their policy.
- [ ] Targeted tests → PASS; required checks and CI.
- [ ] Commit `feat: wait for playable releases and track safe fallback`.

## Task 7: Post-import playback and subtitle assessment

**Files:** create `src/orama/requests/playback.py`, `subtitles.py`, tests for both; extend device YAML files with explicitly sourced capability data while preserving compile-profile keys.
**Interfaces:** `assess_playback(media_info, device) -> PlaybackAssessment`; `assess_subtitles(available_languages, wanted_languages) -> SubtitleAssessment`. Both immutable results carry known/unknown status, reason codes and safe plain text. Worker stores typed follow-up reasons for the Release 2 conversion/translation list, not executable jobs or media paths from webhook input.
- [ ] Tests: S95F DTS variants, multichannel LPCM, FLAC, DV5, DV with/without HDR fallback, TrueHD, HDR10/HDR10+; unknown audio/video/DV profile; text versus image subtitles; Greek present, English-only, none, changed language order; multi-episode files and partial season readiness. Missing media info is "playback not checked", never verified Direct Play.
- [ ] Run `uv run pytest tests/requests/test_playback.py tests/requests/test_subtitles.py -q` → FAIL.
- [ ] Implement from actual Task 1 metadata; keep best-effort profiles labelled. Populate "Ready · may transcode (<reason>)" and "Ready · EN subs, still looking for EL" only when supported by evidence.
- [ ] Targeted tests → PASS; required checks and CI.
- [ ] Commit `feat: report playback compatibility and subtitle readiness`.

## Task 8: Authenticated webhook refresh hints

**Files:** create `src/orama/web/hooks.py`, `tests/web/test_hooks.py`; modify web security/bootstrap hooks, secrets name validation, `wiring/arr.py`, wiring plan and tests.
**Interfaces:** `register_hooks(app)`; each route validates its app-specific stored token then calls `RequestStore.enqueue_refresh`. Notification name `Orama events (Orama)`, fixed internal URL `http://orama-server:8765/hooks/<app>`, no request-controlled URLs. Only needed event types from Task 1 enabled. Existing Plex/Jellyfin notifications retained.
- [ ] Tests: correct token accepts Test event without a request mutation; missing/wrong/other-app token rejected; oversized/non-JSON payload; unknown media ignored; duplicate delivery coalesced; stale event cannot regress state; GET/HEAD/OPTIONS cannot mutate; ordinary browser routes still require CSRF; Host exception only for these exact routes; all auth/security regression tests unchanged.
- [ ] Run `uv run pytest tests/web/test_hooks.py tests/wiring/test_arr.py tests/web/test_security.py -q` → FAIL.
- [ ] Implement exact exceptions after Host verification. Constant-time token compare, bounded body, allowlisted event fields, generic reply. Token rotation rewires notifications without printing it; polling remains sufficient if webhook delivery fails.
- [ ] Targeted tests → PASS; required checks and CI.
- [ ] Commit `feat: refresh requests from authenticated app webhooks`.

## Task 9: Search, request detail and live Home

**Files:** create `src/orama/web/requests.py`, `posters.py`, templates `requests/{search,media,detail,_status}.html`; modify `templates/home.html`, base navigation, CSS, auth permissions, app registration; tests `web/test_requests.py`, `test_posters.py` and browser scenarios.
**Interfaces:** browser routes in Fixed values; permissions `requests:read`/`requests:write` granted only to existing admin. POST persists request intent and wakes worker; GET/HEAD only read. Home shows search plus recent requests. Detail expands season/show children and explains progress, selected release, playback and subtitles.
- [ ] Tests: all four request types, duplicate POST → same detail, tampered IDs, XSS title escaping, no credentials/upstream URLs, unauthorized/CSRF, in-library without duplicate submit, disabled app, unknown poster MIME/oversize/redirect/private-origin rejection, no work from polling GET, partial season status, future-show subscription wording.
- [ ] Run `uv run pytest tests/web/test_requests.py tests/web/test_posters.py -q` → FAIL.
- [ ] Build on existing design system and live-region practices; no full-form poll replacement or repeated screen-reader announcements. Same-origin poster proxy restricted by Task 1 observed trusted origins and media id, otherwise placeholder.
- [ ] Targeted tests → PASS; required checks and CI; real browser at 360/1280 px, light/dark, keyboard/axe checks as in Plan 3.
- [ ] Commit `feat: search and track media requests in Orama`.

## Task 10: Reboot/drift reconciliation and disk warning

**Files:** extend request worker, CLI/bootstrap integration, setup settings and Home; modify Agent server/client for `data-space` only if needed; tests `requests/test_recovery.py`, `agent/test_server.py`, `web/test_requests.py`.
**Interfaces:** `AgentClient.data_space() -> {total_bytes:int, free_bytes:int}`; no caller path, Agent uses its configured data root. `schedule_startup_check()` coalesces with running applies; explicit startup/app drift handling reuses JobRunner and apply.lock. Data-space failure displays unavailable, not zero free.
- [ ] Tests: process restart with active request, busy apply, apps absent/restarting, data disk below/equal/above configured 5% threshold, stat error, malicious path/extra argument refusal, no repeated apply loop after permanent failure, normal Usenet work while torrent VPN is unavailable.
- [ ] Run targeted recovery/Agent/web tests → FAIL.
- [ ] Implement bounded recovery; app availability errors are retryable with backoff. Never infer VPN leak protection from a web page; rely on existing Gluetun network isolation and observed Agent status.
- [ ] Targeted tests → PASS; required checks and CI.
- [ ] Commit `feat: recover request tracking and warn about low disk space`.

## Task 11: Real request pipeline and browser proof

**Files:** create `tests/integration/test_requests.py`, deterministic media/indexer fixtures; extend existing integration cleanup and browser scenarios; append owner checks to Plan 4 notes.
**Interfaces:** real pinned Sonarr/Radarr/downloader/Bazarr apps; bundled fake indexer/seeder and generated noncopyright video; no public tracker/account credentials. Use narrowly scoped disposable fixtures and isolated projects where first-run state is mutated.
- [ ] Assertions: request a single episode and movie → correct monitored targets → download/import → same-inode hardlink → media info/subtitle annotations → Ready; duplicate submit; overlapping season request; same request after restart; webhooks rejected/accepted; lost webhook recovered by polling; interrupted submission; VPN/app-down wait. No global monitoring/profile changes.
- [ ] Prove fallback threshold with controlled releases and injected scheduler clock; prove no fallback before deadline. Record owner-only checks for real providers, Plex account and S95F Direct Play separately.
- [ ] Run required local checks and CI until unit, browser, images, integration and publish are green. Repeat apply and requests and confirm no extra downloads, files or records. Scan outputs/HTML and request DB for secret values.
- [ ] Complete design/accessibility/browser review using available project design tools; missing tools explicitly reported. Test mobile, keyboard, focus retention, reduced motion, readable status and empty/error/loading states.
- [ ] Commit `test: prove requests through real apps and the browser`; fill task reports with exact tested SHA/run.

## Completion and carry-forward

Task reports must distinguish implemented, reviewed, real-app tested and owner-only assumptions. Final whole-plan review plus one fix round; findings fixed or explicitly documented. Claude Security remains deferred per owner until Wednesday 2026-11-11, never represented as passed. Plan 5 still owns clean Ubuntu/Debian VM installs and the NAS checklist. Root-side self-update remains an explicitly outstanding operations subplan required before Release 1 completion.

**Spec coverage:** §7 → Tasks 1–5,8,9,11; §6.3/6.5 → 7; §6.4 → 6; §8/8.1 → 8,9,11; reboot/app/VPN/disk (§9) → 5,10; §10 proof → 11 plus Plan 5; future records (§11) → 2,7. No Release 2 conversion/translation execution is added.
