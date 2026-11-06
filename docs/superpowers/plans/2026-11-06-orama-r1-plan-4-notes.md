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
