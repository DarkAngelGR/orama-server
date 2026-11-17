# CI storage regression repair

State: needs review
Owner: Codex

Fix the default wizard storage selection and the integration storage mount assertions.
Keep Agent path restrictions unchanged.

## Report

- Fix commit: `ebafe86` on the owner-requested `test` branch.
- Default app storage now selects `cand_config`, preserving the installed config root instead of nesting app settings inside the media root.
- Regression test reproduced the Agent rejection before the fix and passes afterward.
- Integration mount test reads the setup from the Orama state directory and checks media mounts against the catalogue (Prowlarr intentionally has none).
- 91 focused local tests, Ruff and ShellCheck passed. Full local suite was stopped after Windows-specific failures (including missing POSIX `/tmp`); Linux CI passed.
- CI run 44083546792 passed unit, browser, images, integration, Ubuntu and Debian end-to-end jobs. Publish was skipped on test.
- https://github.com/DarkAngelGR/orama-server/actions/runs/44083546792
- No Agent restrictions changed; no new dependencies. Owner hardware testing and security review were not repeated.
