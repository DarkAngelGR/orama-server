# Plan 4 — decisions and handoff notes

## Planning checkpoint (2026-11-06)

- Baseline `56ac787`: Plan 3 implementation and final review complete. CI run
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

- First probe commit: `2f8f03b`. Local checks: 1,906 non-integration tests passed,
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

The owner requested repair of test-branch CI. Startup import fixed in `33ef942`;
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

Run 42317584294 (`d790359`) passed unit, browser and images. Integration passed 36
tests and failed two title probes. Bazarr returned 500 on assigning the language
profile. Its pinned v1.6.2 subtitle indexer reads `language['audio_only_include']`
unconditionally; Orama's profile omitted that field. Wiring now supplies `False`
and repairs older managed profiles on apply, with fresh-profile and repair/idempotence
regressions. Other profiles remain preserved. Radarr still rejects the movie after
changing the score floor; the probe now reports only fixed rejection categories to
identify the remaining rule without exposing raw upstream data or credentials.

Run 42346816151 (`97cc09f`) passed unit/browser/images and 37 container tests. The
Sonarr title, generated-file media info, and Greek/English missing-subtitle assertions
now pass, confirming the Bazarr repair. Only the Radarr synthetic-release acceptance
check remains. Pinned Radarr v6.4.4.10685 LanguageParser returns Unknown for titles
without a language tag, and LanguageSpecification can reject with a message containing
neither "language" nor the other original diagnostic categories. The movie fixture now
explicitly names English and asserts the parsed language before testing score changes;
production quality and language requirements remain unchanged.

Run 42347591614 (`5b733b1`) reported `reason categories: ['wanted']` on the Radarr
synthetic release. Orama's default `"mixed"` quality setting compiles Radarr to
`uhd-bluray-web` (which allows only `Bluray-2160p` and `WEB 2160p`, minimum 34.5 MiB/min
for `WEBDL-2160p`) while compiling Sonarr to `web-1080p`. Radarr's
`QualityAllowedByProfileSpecification` therefore rejected the 1080p movie fixture with
`"WEBDL-1080p is not wanted in profile"`. Updating the synthetic movie fixture in
`fake_newznab.py` to `2160p` (`8589934592` bytes, ~55.3 MiB/min for a 148-minute movie)
matches the default `uhd-bluray-web` quality profile while keeping `DTS` as the sole
score-based rejection under the strict profile.

Run 42381439992 (`9d5e2e7`) passed `unit`, `browser`, `images`, and all 38 real-container
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
  SQLite store (`<state_dir>/requests.sqlite3`, mode `0600`, `PRAGMA user_version = 1`,
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

