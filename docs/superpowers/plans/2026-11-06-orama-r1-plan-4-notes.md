# Plan 4 — decisions and handoff notes

## Planning checkpoint (2026-11-06)

- Baseline `206a601`: Plan 3 implementation and final review complete. CI run
  42178338615 passed. Working tree was clean before planning.
- Owner explicitly deferred Claude Security while Claude is unavailable until
  Wednesday 2026-11-11. Plan 2b and Plan 3 scans remain pending, not passed;
  they do not block continued development under this instruction.
- Owner approved Plan 4 on 2026-11-06. Task 1 started; no request product code yet.
- Proposed implementation choices: standard-library SQLite for transactional durable
  requests; one elected in-process reconciler; webhook hints plus authoritative API
  polling; existing Flask/Jinja/htmx security and presentation layers.
- Sonarr series-level quality profiles constrain episode fallback. The draft plan
  keeps unrelated monitored episodes safe and reports when fallback cannot be isolated.
  This is an explicit limitation to review, not a claim that episode-level profiles exist.
- The Plan 3 carry-forward Update Orama button has no working backend: Agent
  `self-update` returns not implemented. A separate root-side operations plan is
  required before Release 1 completion; no nonfunctional update button is planned.
- Plan self-review checked release-spec coverage, task ordering and API references.
  Pinned-app details are deliberately established in Task 1 before consumer tasks;
  implementers must record observed contracts and update dependent interfaces together.

### Task 1 implementation in progress

- First probe commit: `e81fa6f`. Local checks: 1,906 non-integration tests passed,
  Ruff and ShellCheck passed. Real-container CI is still pending; no contracts are
  called verified yet.
- The add APIs fetch Skyhook metadata internally even when callers supply lookup
  data (confirmed by upstream AddSeriesService/AddMovieService source). The title
  probes therefore require catalogue connectivity and fail visibly on outages.
  This changes the plan's optional-online expectation for the add path: keeping it
  optional would leave the required submission contract untested. Cost: external
  metadata outages can fail CI. Source inspection is a hint, not pinned-image proof.
- Test video and indexer releases are synthetic. Only catalogue metadata is fetched;
  no real release is downloaded. A separate profile tests the incompatible DTS score
  floor; the strict profile is compared unchanged and temporary entries are removed.
- Remaining before Task 1 handoff: green real-container evidence, minimal offline
  response fixtures, exact observed version/endpoint notes, independent review and
  report. Do not begin Task 2 or mark Task 1 complete from these notes alone.


### Test branch CI follow-up (Codex, 2026-11-07)

The owner requested repair of test-branch CI. Startup import fixed in `442191b`;
run 42316906235 passed unit/images but exposed the Express package's effect on the
CommonJS browser driver. The driver is now explicitly `.cjs`, and all four local
Chromium scenarios passed.

The branch also inherited unfinished Task 1 probes from main. Earlier source run
42279371718 showed three required-contract failures. This follow-up:
- expects Sonarr's observed 404 for a nonexistent series' episode-file list;
- filters the fallback probe by the requested title, so Radarr evaluates the synthetic
  movie rather than the TV release returned first by the shared fake indexer;
- explicitly assigns the temporary title's subtitle profile and runs subtitle indexing
  before asserting missing-language status, avoiding inspection of an initial sync row;
- includes the pending probe review fixes: credential-bearing tracebacks hidden,
  all notification cleanup attempted, profile cleanup protected immediately after
  creation, and episode monitor-none plus preservation assertions. Regression tests
  verify failure-output secrecy and movie/TV release selection.

These are test harness corrections, not permission to skip required assertions.
Real-container validation of the updated probes remains pending; Task 1 is unfinished.
Other Gemini review findings remain unresolved.

Local validation of this follow-up: 1,935 non-integration tests passed, Ruff and
ShellCheck passed; the four real Chromium scenarios passed after the driver rename.

Run 42317584294 (`9058a45`) passed unit, browser and images. Integration passed 36
tests and failed two title probes. Bazarr returned 500 on assigning the language
profile. Its pinned v1.6.2 subtitle indexer reads `language['audio_only_include']`
unconditionally; Orama's profile omitted that field. Wiring now supplies `False`
and repairs older managed profiles on apply, with fresh-profile and repair/idempotence
regressions. Other profiles remain preserved. Radarr still rejects the movie after
changing the score floor; the probe now reports only fixed rejection categories to
identify the remaining rule without exposing raw upstream data or credentials.

Run 42346816151 (`6d6761b`) passed unit/browser/images and 37 container tests. The
Sonarr title, generated-file media info, and Greek/English missing-subtitle assertions
now pass, confirming the Bazarr repair. Only the Radarr synthetic-release acceptance
check remains. Pinned Radarr v6.4.4.10685 LanguageParser returns Unknown for titles
without a language tag, and LanguageSpecification can reject with a message containing
neither "language" nor the other original diagnostic categories. The movie fixture now
explicitly names English and asserts the parsed language before testing score changes;
production quality and language requirements remain unchanged.

Run 42347591614 (`1fb5ff9`) reported `reason categories: ['wanted']` on the Radarr
synthetic release. Orama's default `"mixed"` quality setting compiles Radarr to
`uhd-bluray-web` (which allows only `Bluray-2160p` and `WEB 2160p`, minimum 34.5 MiB/min
for `WEBDL-2160p`) while compiling Sonarr to `web-1080p`. Radarr's
`QualityAllowedByProfileSpecification` therefore rejected the 1080p movie fixture with
`"WEBDL-1080p is not wanted in profile"`. Updating the synthetic movie fixture in
`fake_newznab.py` to `2160p` (`8589934592` bytes, ~55.3 MiB/min for a 148-minute movie)
matches the default `uhd-bluray-web` quality profile while keeping `DTS` as the sole
score-based rejection under the strict profile.

Run 42381439992 (`5526480`) passed `unit`, `browser`, `images`, and all 38 real-container
`integration` tests (including `test_pinned_request_contracts`,
`test_pinned_authenticated_webhook_contracts`, `test_pinned_title_request_contracts[sonarr]`,
and `test_pinned_title_request_contracts[radarr]`). Observed contracts verified in CI:
- Read contracts: `system/status`, paginated `queue` and `history` (`page`, `pageSize`,
  `totalRecords`, `records`), `command`, `series`/`movie`, `episodefile` (404 when series
  absent) / `moviefile` (`[]` when movie absent), `qualityprofile` (`Orama` profile with
  integer `id`, `minFormatScore`, `cutoffFormatScore`, `formatItems`, `items`).
- Webhook contracts: `Webhook` notification schema supports `url`, `method` (POST), and
  `headers` (`[{"key": "Authorization", "value": "Bearer <token>"}]`), `onGrab`,
  `onDownload`, `onUpgrade`; `notification/test` delivers `eventType == "Test"` with the
  Bearer header and no token leaked in container logs.
- Title, media-info, subtitle, and fallback contracts: lookup by `tvdb:<id>` / `tmdb:<id>`,
  add with `monitor: "none"` (Sonarr) and `searchForMissingEpisodes: False` /
  `searchForMovie: False`, `episode/monitor` preserving unrelated monitored episodes,
  `EpisodeSearch` / `MoviesSearch`, `RescanSeries` / `RescanMovie` populating `mediaInfo`
  (`videoCodec`, `audioCodec`, `resolution`, `videoBitDepth`, `audioLanguages`,
  `videoDynamicRange`, `videoDynamicRangeType`, `subtitles`), Bazarr `/api/series` and
  `/api/movies` sync/profile assignment/disk scan reporting `missing_subtitles` (`el`, `en`),
  and alternate quality profile with `minFormatScore` lowered to the unplayable format score
  (`-10000`) allowing the synthetic DTS release without modifying the shared `Orama` profile.

### Tasks 2–11 implementation and owner-only checks (Gemini, 2026-11-07)

- **Durable request store & domain (`src/orama/requests/models.py`, `store.py`)**:
  SQLite store (`<state_dir>/requests.sqlite3`, mode `0600`, schema version in a one-row `schema_version` table (not `PRAGMA user_version`),
  rollback journal (not WAL),
  `PRAGMA foreign_keys = ON`, 5 s busy timeout) persists `requests`, `refreshes`,
  `episode_targets`, and `followups`. Unique `target_key` makes concurrent or repeated
  submissions idempotent; `claim_due` and `release` coordinate worker leases across restarts.
- **Lookup, submission, and reconciliation (`client.py`, `submit.py`, `reconcile.py`, `worker.py`)**:
  `MediaClient` enforces 1–200 char queries, strips HTML/tags from titles, validates poster URLs,
  adds series with `monitor="none"` for episode/season requests (preserving unrelated monitoring),
  and queries bounded queue/history snapshots plus imported file `mediaInfo` and Bazarr subtitles.
- **Playable-release waiting & fallback (`fallback.py`, `setup_model.py`, `web/forms.py`, `web/settings.py`)**:
  Snapshots `request_wait_mode` (`timed` | `always` | `never`, default `timed`) and
  `request_wait_hours` (default `24`) at request creation. When `fallback_due` is reached,
  `apply_fallback` creates/updates the `Orama fallback` quality profile (`minFormatScore`
  lowered to `-10000`) and attaches it to the requested item without altering the shared `Orama`
  profile or user-assigned profiles. For Sonarr series-level profiles, fallback is blocked with
  an explicit waiting reason whenever unrelated monitored episodes without files exist on the
  same series.
- **Playback & subtitle assessments (`playback.py`, `subtitles.py`, `profiles/devices/*.yaml`)**:
  Evaluates Sonarr/Radarr `mediaInfo` against device capabilities (`samsung_s95f` verified;
  other device profiles labelled best-effort) and Bazarr subtitle availability against ordered
  user languages, recording typed `conversion` and `translation` follow-up reason codes for
  Release 2 without executing media jobs.
- **Authenticated webhooks, browser UI, poster proxy, and recovery (`web/hooks.py`, `web/requests.py`, `web/posters.py`)**:
  `POST /hooks/sonarr` and `POST /hooks/radarr` validate per-app 32-byte hex Bearer tokens in
  constant time after Host checks and coalesce refresh hints. `/posters/<kind>/<source_id>`
  proxies only validated TMDB/TVDB HTTPS images (max 2 MiB, `image/jpeg|png|webp`, no redirects)
  with an inline SVG placeholder fallback. `AgentClient.data_space()` checks free space on the
  configured data root with `O_NOFOLLOW | O_DIRECTORY` + `fstatvfs` and displays a warning on
  Home when below `disk_warning_percent` (default 5%).
- **Owner-only checks (cannot be automated in disposable CI)**:
  1. Real external Usenet/torrent indexers and download providers (CI uses `fake_newznab.py` and
     synthetic `ffmpeg` video generation with no copyrighted media or external provider accounts).
  2. Real Plex account claim/sign-in and library scan notification verification.
  3. Physical Samsung S95F TV Direct Play verification on imported files (CI verifies container
     `mediaInfo` and `assess_playback` against the pinned `samsung_s95f.yaml` rule set).
  4. Deferred Claude Security scan for Plans 2b, 3, and 4 when Claude becomes available on
     Wednesday 2026-11-11, plus the separate root-side `self-update` operations plan before
     Release 1 completion.

### Review fix wave and CI proof (`729e24c`, `853dce0`, `01903ae`, 2026-11-08)

- **Exact episode/season file readiness (`client.py`, `reconcile.py`)**: `MediaClient.snapshot`
  fetches Sonarr `episode?seriesId=<id>` alongside `episodefile?seriesId=<id>`. `reconcile`
  requires target episode/season/aired-series files (`hasFile` / matching `episodefile`) rather
  than series-wide `episodeFileCount > 0`, and filters queue/history events by requested
  coordinates so an unrelated episode never marks a request `ready`.
- **Safe retry & missing episode metadata (`submit.py`, `client.py`, `fallback.py`)**:
  `submit` persists `remote_id` immediately after `ensure_movie`/`ensure_series`, checks
  `has_active_search_or_download` before starting a new search, marks already-downloaded targets
  `ready` without searching, and sets `waiting_reason="Waiting for episode metadata"` when
  `monitor_episodes` returns `[]` so `search_targets` never falls through to a season/series
  search.
- **Ongoing show re-checks (`reconcile.py`, `store.py`, `worker.py`, `web/requests.py`)**:
  Whole-show (`series`) requests remain active in `list_active` and `claim_due` (`now + 300.0`)
  after reaching `ready`, update `episode_targets` as new episodes air, transition back to
  `downloading` when newly aired episodes lack files, and keep `live=True` on the detail page.
- **Unsubmitted request retry (`web/requests.py`, `worker.py`)**: `POST /requests/<id>/retry`
  and `retry_request` preserve `stage="requested"` when `remote_id is None`, and `worker.tick`
  routes unsubmitted requests (`remote_id is None`) through `submit()` instead of stranding them.
- **Coordinate-matched playback & subtitles (`client.py`, `subtitles.py`, `worker.py`)**:
  `file_media_info` and `subtitle_languages` match exact `(season, episode)` or `season` without
  falling back to `files[0]` or `rows[0]`; season and series requests evaluate per-episode
  subtitle availability via `assess_episode_subtitles`.
- **Traceback secrecy & CI (`tests/integration/test_requests.py`)**: Integration helpers keep
  credentials inside `__tracebackhide__ = True` frames. CI runs `42472203041` (`853dce0`) and
  `42511960464` (`01903ae`) passed `unit` (2,014 tests), `demo`, `browser`, `images`, and all
  41 real-container `integration` tests.

### Node demo server removed (Claude review, 2026-11-11)

The `test` branch carried a second web server that was never part of the design: a Node/Express
demo (`server.ts`, `views/*.ejs`, `public/`, `package.json`, `tsconfig.json`, `metadata.json`).
Nothing in the installer, the images or CI ran it, it had fixed passwords and an always-valid
session token, and the commit that tightened it (`01903ae`) was reverted (`5604a36`). All of it is
deleted. Where earlier notes and task reports mention the `demo` CI job, `npm test`, `server.ts`
or the "Express package", they describe that removed scaffold. Two things stay as they are:
`tests/web/browser_pass.cjs` keeps its `.cjs` name, and the CI `browser` job still installs
Playwright into its own folder with `npm install --prefix` (no root `package.json` is needed;
checked locally and in CI). `tests/test_repo_hygiene.py::test_node_demo_server_stays_removed`
fails if the demo files come back.

### Review fixes to requests (Claude, 2026-11-11)

- **Plain words on the page.** When Sonarr or Radarr answers with an error, the page used to say
  "Sonarr returned HTTP 500" (or "returned malformed JSON"). It now says "Sonarr did not answer
  as expected. Try again in a minute." The status is written to Orama's log only
  (`orama.requests`: app, method, fixed API path, status), never the search words, the key or
  the app's own error text.
- **Only the exact title.** Posters, the detail page and adding a title all looked a title up by
  sending its numeric id as a search word and, when no result had that id, took the **first**
  result. A number is also a word ("300", "24", "1917"), so this could show another title's
  poster and description and, worse, add a different film or show than the one requested. Now
  the app is asked by id (`tmdb:<id>` for films, `tvdb:<id>` for shows; contract already checked
  in `tests/integration/request_contracts.py`) and only an item whose own TMDB id (films) or
  TVDB id (shows) is exactly that number is used. The app's row number (`id`) and the other
  catalogue's id never count. No exact match means: placeholder poster, a "Media #<id>" detail
  page, and "Could not locate ... metadata" instead of adding anything.
  New helper: `MediaClient.find(kind, source_id)`. Files: `src/orama/requests/client.py`,
  `src/orama/web/posters.py`; tests in `tests/requests/test_client.py`,
  `tests/requests/test_submit.py`, `tests/web/test_posters.py`.

### Review fixes to the request store and worker (2026-11-12)

- Store errors: every store method goes through one helper that turns `sqlite3.Error` into
  `StoreError` (`CorruptStoreError` for a damaged file or wrong tables, `StoreBusyError` when
  the database is locked). The worker loop survives any tick error and logs one warning line
  without request data. If the request database is damaged at start, Orama still serves the web
  app and request pages answer with a plain "requests are unavailable" page. The request worker
  always exists and starts; it opens the store on demand, so it picks the database up again
  once it is repaired or removed, without a restart. A Ready item whose file is gone becomes
  "unavailable", is looked at only every 6 hours, and never falls back by itself: it waits for
  the owner to ask again. Failed worker ticks are logged once per kind of error (and at most
  once a minute) with back-off; a stored row above the input limits is still readable.
- Ready requests are claimed whenever `next_check_at` is due, whatever their notes say. The
  worker picks the interval: 5 minutes while playback or subtitles are unknown or partial,
  6 hours once both are final.
- The worker clears its own "Waiting for Radarr/Sonarr/VPN" note on the next tick where the app
  answers. Asking again for a Ready or unavailable item (not a series) restarts the same record
  and keeps its remote id. A webhook hint, a Retry click or asking again that arrives
  while the worker holds the record is always stored as `refresh_hint_at` (Retry and the
  re-activation in `create()` record it unconditionally); `release()` keeps the earlier of the
  hint and the worker's own time, so a "check now" is never pushed back. `RequestSpec` and `RequestUpdate` validate and bound their values;
  `updated_at` changes on every update.
- Executor election lock: the plan's `<state_dir>/requests.lock` was not built and is not
  needed in the shipped setup. The Orama container runs one `python -m orama` process
  (one waitress process, one worker thread; `docker/orama.Dockerfile` CMD) and nothing else
  creates a `RequestWorker`, so every worker id `worker-1` is the only worker on the database.
  Revisit (add the lock with `state.locked(..., blocking=False)`) if a second process or
  replica is ever allowed to share the state folder.
