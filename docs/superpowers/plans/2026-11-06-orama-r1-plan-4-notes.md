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
