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
