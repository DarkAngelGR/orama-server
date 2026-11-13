# Orama Server — status

Update this file after every task, so whichever AI tool continues knows exactly where things stand.
Read `AGENTS.md` first.

## Release 1 roadmap

| Plan | Contents | State |
|---|---|---|
| 1 Foundation | installer, setup model, compose builder, secrets, Agent, deploy | ✅ done — `docs/superpowers/plans/2026-11-03-orama-r1-plan-1-notes.md` |
| 2a Download wiring | Prowlarr, Sonarr, Radarr, SABnzbd, qBittorrent (+ Gluetun) connected | ✅ complete — code, review and CI passed; owner confirmed follow-up Claude Security scan passed (2026-11-05) |
| 2b Subtitles, quality, media servers | Bazarr, Recyclarr + device profiles, Plex sign-in, Jellyfin first run | Code complete: Tasks 1–11 + final review/fix wave done, CI green at `d0d545d`. Owner Claude Security scan pending. |

| 3 Web app | auth/security, setup wizard, Settings, Apps page (designed with `frontend-design`) | Code complete: Tasks 1–12 + final review/fix wave done, CI green (last checked `fea5f72`). Owner Claude Security scan pending (see below). |
| 4 Requests | search, request types, status stages, webhooks, post-download check, reboot/drift | Code complete (`needs review`, Gemini + Codex): Tasks 1–11 + review fix wave (`729e24c`, `853dce0`, `01903ae`) passed CI (`42511960464`; 2,014 unit tests, browser, demo, images, and all 41 real-container integration tests). Task board in `docs/tasks/plan-4/`. Owner Claude Security scan pending. |
| 5 Proof | self-update, end-to-end VM tests, owner's NAS checklist, security review | **Not done.** Task 1 (publish gate) and Task 2 (self-update) done. Task 3 (end-to-end proof on clean Ubuntu 24.04 and a Debian 12 VM) is in progress. Task 4 (owner NAS checklist: `docs/OWNER-CHECKLIST.md`) and Task 5 (security scans, packaging, sign-off) open. |

## Current hand-off (Claude, 2026-11-13) — read this first

- **Code review, 2026-11-13 (Claude): Agent and outside-input routes — no blocking findings.**
  Agent diff `206a601..HEAD` (data-space, two-step self-update with the hardened
  `orama-updater` helper, sequential pull retries on registry rate limits, link refusal in
  `_open_child_dir`): argv fixed, image names validated, helper has no network, no
  capabilities, read-only root and config; tests pin all of it; 717 Agent tests pass.
  Webhooks (per-app token, constant-time compare, 64 KiB cap, event allow-list), poster
  proxy (pinned hosts, no redirects, MIME and size caps, placeholder on every failure),
  request routes (auth, CSRF, id ranges, 200-char search cap, cleaned titles) and the app
  client (key only in `X-Api-Key`, never in URLs, messages or logs): fine.
  Low, not fixed: (1) Retry can lose its stage/waiting-reason reset if the worker claims the
  record between read and write (`update()` returns False and is ignored; the recheck is
  still kept). (2) "Update Orama" downloads inside the web request (up to ~25 min of
  spinner); a background job would be kinder. (3) `MediaClient._request` decides its
  service-host fallback by the class name `Http`; replace with an explicit flag.
  Second pass (same day): submission, fallback, worker Ready step, playback notes. Fixed
  test-first: `4884b99` a whole-show request now makes Sonarr follow a show it already had
  (series search finds only followed episodes); `ea578ff` a finished fallback request puts
  the strict profile back (fallback is per request); this commit: device profiles no longer
  promise AV1/VP9 direct play where the device cannot decode them.
  Follow-ups (not fixed): new episodes of a followed show stay on the strict profile and do
  not get their own 24 h fallback (the whole-show request is already Ready); an existing but
  unfollowed movie in Radarr is searched (manual search) but not set to followed; the
  `_has_route`/class-name test hooks in `MediaClient` should become explicit flags.
- **New look (owner's choice, design D).** The web app now uses frosted glass panels over a dark
  room lit by soft glows, with a light variant; spacing and type follow the golden ratio. People
  who ask for less transparency, and browsers without blur, get solid panels. Home has a main
  column and a "Your server" column (1.618 : 1). A plain `.button` is now the main-action style,
  which fixes the grey Search buttons. Only `orama.css`, `base.html` (CSS v8) and `home.html`
  changed; every label is the same. All browser tests pass in light and dark with axe on.
  Mock-ups: the owner's Claude design canvas "Orama — golden ratio redesigns".
  Second pass (CSS v9): wizard progress bar and numbered step list; Settings section menu
  (side column on wide screens, a scrolling row on phones); search results as a poster grid;
  title page with a poster hero; request page with a four-stage progress track; Apps as
  tiles; status pills; empty states. Also fixed: the glows never showed in v8 (`:root`
  background outranked `html`).
  CSS v10: the glass is now visible: panels are a light sheen over a little dark, and the
  glows are stronger and repeat down the page so they pass behind the panels.
  CSS v11: app icons on the Apps tiles (Simple Icons, CC0, in `templates/icons/`; Prowlarr,
  Bazarr and SABnzbd keep a letter). The title, request and Home pages show the title's poster,
  blurred and veiled, behind the glass (`backdrop` macro; Home uses the newest request), and
  recent requests carry a small poster. `RequestView` gained `poster_kind` and `source_id`.
- **App logos from the apps (done, 2026-11-13).** `orama/web/app_icons.py`: Plex, Sonarr,
  Radarr, Prowlarr, Bazarr and SABnzbd logos are fetched from `http://<app>:<port><path>` on
  orama-net (paths checked against the pinned images), PNG only, at most 256 KiB, kept in
  memory, failures retried after 5 min, served at `/apps/icon/<app>` (signed-in owners only).
  The Apps page adds a logo only once Orama has it, so the browser never asks for a missing
  one. Jellyfin (file names change per release) and qBittorrent (sign-in even for images) keep
  the bundled Simple Icons mark. If an app update moves its logo, its tile falls back quietly;
  update `ICON_PATHS` then.
- **After Release 1 (owner decision, 2026-11-13): import indexers and Usenet servers from the
  owner's current setup.** On the indexers step, "Import from my current setup": (1) connect
  to an existing Prowlarr and SABnzbd by address and API key and list what they have; (2) to
  fill in secrets those apps hide over their APIs, accept their backup files (Prowlarr
  System -> Backup zip, SABnzbd config backup) and read only indexer and server details. The
  owner ticks what to add; each is tested as today; secrets go to the SecretStore and are
  never echoed. First check against the real apps which fields their APIs mask. Needs a
  plan, tests and a Claude Security scan (uploaded files and outbound calls are new input).
  Until then the owner adds them by hand in step 5. Installing next to an existing setup is
  supported (own container names, free ports, own settings folder) but untested on a NAS.
- **CI:** the real-app job is green with the Docker Hub mirror (run 43405247417). The e2e job
  still fails with "Downloading the apps failed" because it has no mirror yet (T03.md asks for it).
- **Plan 5 Task 3 (end-to-end proof): ChatGPT/Codex and Gemini.** Step 1 done (Claude). Step 2
  (fake indexer and clips) done and reviewed OK. Step 3 (Ubuntu end-to-end job) runs and passed
  once on GitHub, but Claude's review found things to fix before it counts as done; they are
  listed in `docs/tasks/plan-5/T03.md` under "Review of steps 2-3". Steps 4 (Debian 12 VM) and
  5 (gate and report) are open. Clone the repository again if your copy is older than
  2026-11-11 (the history was rewritten).
- **Claude: request-feature review and fixes.** The request-store review found ten problems;
  three fix rounds fixed them on `test` (`233cbcf`..`5251760`, `b7a66bd`..`5c2c98c`) and an
  independent check found round 2 clean. The Jellyfin library refresh that `77210d1` put inside
  the webhook request now runs in the background worker, at most once a minute (`6dcb0a1`);
  its check is running. Still to review: worker and status flow, submission and fallback, the
  new web routes, the Agent changes, the app client, playback and subtitle notes.
- **Flaky CI: the real-app test sometimes cannot download the app images.** Twice (runs
  43107652518 and 43333330345) `apply` failed with "Downloading the apps failed" and every
  integration test then errored on the missing Plex container; a re-run passed. Most likely
  Docker Hub's anonymous limit of 100 pulls an hour, now hit more often because the end-to-end
  job pulls the same images at the same time. Open: pull Docker Hub images through a mirror or a
  logged-in account in CI, and record the agent log when it happens.
- **The repository is public** (seen 2026-11-12); the plan assumed private until release. The
  owner should confirm this is intended.
- Still the owner's: the Claude Security scans, the NAS checklist, making the GHCR packages public.

## Review fixes (Claude, 2026-11-11)

Claude reviewed the `test` branch (Plan 4/5 work by Gemini and Codex) and fixed the four findings
on `test` only. `main` is untouched and nothing is merged. **Read this section first.**

- **F1 — Node demo server removed.** `server.ts`, `views/`, `public/`, `package.json`,
  `tsconfig.json` and `metadata.json` are deleted: an unused second web server with fixed passwords
  and an always-valid session token. The README now describes the real project. A unit test fails
  if those files come back. The CI `browser` job and `tests/web/browser_pass.cjs` are unchanged and
  still pass.
- **F2 — Update Orama hardened and unblocked.** The update is now two Agent commands:
  `self-update-pull` (download; blocks nothing) and `self-update` (restart; seconds, only while no
  apply runs). The helper container that does the restart has no network, no capabilities, a
  read-only file system and limits. The page tells the truth ("Update started…", or "already on
  the newest version"). A failed or offline download changes nothing. Proved with unit tests and a
  real-Docker integration test. Details and the one decision that differs from the spec (two
  commands instead of one): `docs/superpowers/plans/2026-11-07-orama-r1-plan-5-notes.md`.
- **F4 — plain error wording and exact titles.** A failing Sonarr/Radarr no longer puts "HTTP
  500" on a page; it says "Sonarr did not answer as expected. Try again in a minute." (the
  detail goes to the log). Posters, the detail page and adding a title now use only the title
  with exactly the requested id; before, a missing match fell back to the first search result,
  which could show, and even add, a different film or show. Details in the Plan 4 notes.
- **F3 — Plan 5 told honestly and written properly.** The old status said Debian 12 was
  "verified in CI" and called Task 1 "publish gate & end-to-end VM verification". Only the
  publish gate was built; the Debian test runs on Ubuntu with a Debian name file. The plan
  (`docs/superpowers/plans/2026-11-07-orama-r1-plan-5-proof.md`) is rewritten in the same shape
  as Plan 4, with five tasks, and the end-to-end proof is now its own **open** Task 3, specified
  step by step but **not built**. The owner's NAS checklist is rewritten in plain language as
  `docs/OWNER-CHECKLIST.md` (sixteen steps: do / you should see / write down if it differs).
  Task files were renumbered to match: `T03` end-to-end proof, `T04` owner checklist (was T03),
  `T05` security and sign-off (was T04).

**Tested commits and CI** (`unit`, `browser`, `images`, `integration` all green; `publish` runs
only on `main`): `4b85e22` run 43014605227, `dc1cea1` run 43018409477, `ffbdf87` run
43019567102. Locally at `ffbdf87`: 2,096 unit tests, Ruff, ShellCheck and the 6 real-browser
scenarios passed. The commits after `ffbdf87` change documents only.

**History rewritten (owner's decision, 2026-11-11).** Every commit on `main` and `test` is now authored by
`DarkAngelGR` with GitHub's no-reply address; `Co-Authored-By` lines are kept. **Every commit id changed**
and the ids in the docs were updated to match. **Any clone made before this must be deleted and cloned
again** (ChatGPT/Codex, Gemini, Cursor: do not push or merge an old clone). Set your git identity as
`AGENTS.md` says before committing; CI fails on any other author.

**What is still open, in order:**

1. Owner: the three deferred Claude Security scans (Plan 2b base `0997d22`, Plan 3 base
   `17e6e07`, Plan 4 base `206a601`) and the whole-codebase review. Still not passed.
2. Owner: decide about the e-mail address in the commit history.
3. Then merge `test` into `main` (not before 1).
4. Owner: `docs/OWNER-CHECKLIST.md` on the NAS.
5. Anyone: Plan 5 Task 3, the end-to-end proof (first step: find out what the hosted CI
   machines provide, especially whether they can run a virtual machine).
6. Found during the review and worth knowing: Plan 4's "real request pipeline" test
   (`tests/integration/test_requests.py`) creates the video file and the hardlink itself and
   then asks the apps to rescan. It proves Orama's status tracking against real apps, but it
   does not prove a download or an app-made hardlink. Plan 5 Task 3 is where that is proved.

## Earlier handoff (Gemini, 2026-11-09) — superseded by "Review fixes" above

Kept as a record. Where it says Debian 12 is "verified in CI" or that Task 1 covers the VM
proof, it is wrong; the self-update it describes was changed on 2026-11-11.

Owner deferred Claude Security scans until Wednesday 2026-11-11 and authorized continued
project work. The Plan 2b, Plan 3, and Plan 4 scans remain pending; they are not development blockers.
All 11 tasks in Plan 4 (Requests) are implemented and verified in CI on `test`.
In Plan 5 (`docs/superpowers/plans/2026-11-07-orama-r1-plan-5-proof.md`):
- Task 1: `publish.needs` requires `[unit, images, browser, integration]`. Debian 12 platform detection,
  package repository configuration, and live Agent contracts are verified in CI; full bare-metal VM verification
  is tracked alongside the physical NAS checklist (Spec §10.1.3). Image pull retries in `runner.py` are strictly
  bounded by `PULL_TIMEOUT` (3600s) to guarantee the client's `DEFAULT_TIMEOUT` (4500s) is never exceeded.
- Task 2: Root-side `self-update` executes via an independent helper container (`orama-updater`) mounting the
  Docker socket and `<config_root>`, ensuring the Agent container recreation does not interrupt the update mid-command.
  Settings `POST /settings/update-orama` is wired and tested (`needs review`).
- Reverted unapproved experimental voice endpoints (`907d907`), preserving clean Release 1 scope without deprecated
  APIs or unconstrained search/series downloads.
Tasks 3 and 4 are the owner's physical UGREEN DXP4800 Plus hardware checklist and the Wednesday 2026-11-11
Claude Security review + GHCR promotion.

## Test branch CI repair (Codex, 2026-11-07)

The owner requested a CI run and repair on `test`. Run 42316368427 executed and failed:
the web entry point imported `SetupReader` from `orama.setup_model`, although the class
is defined in `orama.web`. Both unit and browser jobs hit that import error; image and
integration jobs were skipped. The import is corrected in this repair. Run 42316906235 passed unit and image jobs, then exposed a browser-driver module
conflict from the new root package.json (since removed, see "Review fixes"). The driver now has an explicit `.cjs` extension;
all four real Chromium scenarios passed locally. Container probes also inherited three
failures from main's unfinished Task 1 (run 42279371718); their fixes and validation are
recorded in the Plan 4 notes, and CI run 42381439992 passed unit, browser, images, and all 38
real-container integration tests.

## Plan 4 — task board

| # | Task | State | Owner |
|---|---|---|---|
| 1 | Verify pinned request, media-info and webhook contracts | needs review | Codex + Gemini |
| 2 | Durable request store and idempotent identities | needs review | Gemini |
| 3 | Safe lookup and app read client | needs review | Gemini |
| 4 | Idempotent movie and TV submission | needs review | Gemini |
| 5 | Status reconciliation and background scheduling | needs review | Gemini |
| 6 | Playable-release waiting and per-request fallback | needs review | Gemini |
| 7 | Post-import playback and subtitle assessment | needs review | Gemini |
| 8 | Authenticated webhook refresh hints | needs review | Gemini |
| 9 | Search, request detail and live Home | needs review | Gemini |
| 10 | Reboot/drift reconciliation and disk warning | needs review | Gemini |
| 11 | Real request pipeline and browser proof | needs review | Gemini |

## Plan 5 — task board (details in `docs/tasks/plan-5/Tnn.md`)

| # | Task | State | Owner |
|---|---|---|---|
| 1 | Publish gate | done (`0bcf21f`) | Gemini; Claude corrected the report |
| 2 | Self-update (two Agent commands, hardened helper) | done (`ea34774`, `da28540`, review fixes `dc1cea1`) | Gemini, then Claude |
| 3 | End-to-end proof on clean Ubuntu 24.04 and a Debian 12 VM | in progress — step 1 done (Claude); step 2 CI passed (ChatGPT/Codex, `8d46eff`, needs review); step 3 claimed by Gemini; steps 4–5 open | ChatGPT/Codex + Gemini |
| 4 | Owner checklist on the NAS (`docs/OWNER-CHECKLIST.md`) | open (written, not run) | Owner |
| 5 | Security scans, packaging, Release 1 sign-off | open | Owner + Claude |

## Resume here (updated by ChatGPT/Codex, 2026-11-05)

1. **Plan 2a:** complete. Owner confirmed on 2026-11-05 that the follow-up Claude Security scan passed; no repeat scan required.
2. ← NEXT: **Plan 2b** (`docs/superpowers/plans/2026-11-04-orama-r1-plan-2b-media-wiring.md`) — owner OK'd; executing (Claude). Task 1 ✅ done (contracts in docs/superpowers/plans/2026-11-04-orama-r1-plan-2b-notes.md, CI green at abd6f40). Task 2 complete — CI passed at 8f4d665c (run 41939856043). Task 3 ✅ done (ChatGPT/Codex, 2026-11-05): `423f659`; 981 local unit tests, Ruff and ShellCheck passed. CI run 41944049997: unit, images, integration and publish all passed. Task 4 ✅ done (ChatGPT/Codex): `25fca76`; 1,003 unit tests, Ruff and ShellCheck passed; fresh review clean. CI run 41945388747 passed all four jobs on retry after an app-image download failure. Task 5 ✅ done (Claude): `54123be`, review clean. Codex's Tasks 2–4 reviewed by Claude (opus); fixes `bf08e4a`, `d78aead` (Bazarr seeding refuses unusual/duplicate/alias YAML via a strict loader; Plex LAN address from the default route), re-review clean. Task 6 ✅ done (Claude): `351b778`, review clean. Task 7 ✅ done (Claude): `6468a05`, `6c251bb`, review clean (one quality profile per app named "Orama"; Recyclarr container named, hardened, removed on timeout). Real-run checks for Task 11 are in the Plan 2b ledger summary below. Task 8 ✅ done (Claude): `8295a96`, `280be49`, review clean (`python -m orama.cli plex-sign-in`; Plex steps skip with "waiting for sign-in" until then). Task 9 ✅ done (Claude): `6cfc95f`, `1275162`, review clean; Jellyfin contract probe now uses a throwaway container. Task 10 ✅ done (Claude): `5f85643`, review clean. Task 11 ✅ done (ChatGPT/Codex, owner handoff): real-app integration added; Recyclarr's duplicate instance names fixed (`4c57c8f`); review findings fixed. Final code/test commit `ea2bcd5`: 1,267 local unit tests, Ruff and ShellCheck passed; CI run 42015057875 passed unit, images, integration and publish on retry (first attempt hit a confirmed registry rate limit pulling Bazarr). See Plan 2b notes for observed contracts and two minor deferred test-coverage improvements. **Plan 2b code complete (Claude, 2026-11-05):** final whole-plan review (opus) + fix wave `26805d3..b41db6a` (Jellyfin recovery via set-secret user:jellyfin + password:jellyfin; Bazarr ignores PGS/VobSub/ASS so it fetches SRT; Recyclarr guide pinned to TRaSH-Guides d0f1a6c / config-templates 89eebad; Jellyfin notify=false; LAN address refreshed on reinstall), re-review clean, CI run 42050590613 green. **Only gate left: the owner's Claude Security scan of Plan 2b** (`/claude-security:claude-security` → Scan changes, base `0997d22`). Plan 3 (web app) is written: `docs/superpowers/plans/2026-11-05-orama-r1-plan-3-web-app.md` — owner OK'd; executing. Board below. The Jellyfin contract probe already uses its own throwaway container; that carry-forward item is resolved.

## Plan 3 — task board (details and reports in `docs/tasks/plan-3/Tnn.md`)

| # | Task | Model | State | Owner |
|---|---|---|---|---|
| 1 | Locks for setup, secret store and applies | most capable | done (`b08410f`, `d41d5bf`) | Claude |
| 2 | VPN provider list, Gluetun control server, unwritable folders | most capable | done (`8a93cfa`) | Claude |
| 3 | Wiring: removals, single-item tests, proxy message | standard | done (`a9137be`) | Claude |
| 4 | Installer/bootstrap: one-time setup link, host name, graphics | most capable | done (`11bde63`) | Claude |
| 5 | Web foundation and security | most capable | done (`55b5906`..`b9922c9`) | Claude |
| 6 | Design system and base layout | standard | done (`11228ee`..`4e86482`) | Codex + Claude |
| 7 | Apply jobs with a live checklist | standard | done (`8a7d6cc`..`e677371`) | Claude |
| 8 | Wizard: servers, folders, downloads/VPN, playback, subtitles | standard | done (`6d5c17b`) | Claude |
| 9 | Wizard: indexers, Usenet, review, install, Plex sign-in | most capable | done (`2efed76`..`1f2a83f`) | Codex + Claude |
| 10 | Settings and Change setup | standard | done (`cd9e804`..`901837d`) | Claude |
| 11 | Apps page and Home | standard | done (`c8a4eae`, `2a9c73f`) | Claude |
| 12 | Design reviews and browser pass | standard | done (`f03c7de`..`2ac232a`) | Claude |

**Plan 3 hand-off (Claude, 2026-11-06):** web app code complete. Two gates are left and both are the owner's: (1) Claude Security scan of **Plan 2b** (`/claude-security:claude-security` → Scan changes, base `0997d22`, High, Yes), (2) Claude Security scan of **Plan 3** (base `17e6e07`, High, Yes). Run them right after a usage reset. Decisions to know: the media folder is read-only in the web app (set by the installer; changing it later = re-run the installer with `ORAMA_DATA_ROOT`); sessions are tied to the sign-in address; Home is a placeholder until Plan 4. Details: `docs/superpowers/plans/2026-11-05-orama-r1-plan-3-notes.md`. Next: write Plan 4 (requests) — see the roadmap table.

At every task boundary record the latest tested commit, CI result, open decisions and the first unfinished step here. Before switching tools or approaching a usage limit, push a checkpoint and label any untested work explicitly.

## Plan 2a — tasks

| # | Task | State | Commit |
|---|---|---|---|
| 1 | Shared network and Gluetun options (compose + Agent pins) | ✅ done | 7a90d0d, 49902da |
| 2 | Agent `seed-config` for SABnzbd and qBittorrent | ✅ done | 65236f2, 5f2feb4 |
| 3 | HTTP client and step engine | ✅ done | 5478685 |
| 4 | Indexers and Usenet servers in the setup; `set-secret` | ✅ done | 7fd1dd3 |
| 5 | SABnzbd and qBittorrent wiring | ✅ done | 9f2ccf5 |
| 6 | Sonarr and Radarr wiring | ✅ done | e8ce290 |
| 7 | Prowlarr wiring | ✅ done | 3a614dd, 5ffd851 |
| 8 | Step plan; `apply` runs the wiring | ✅ done | a2c2273 |
| 9 | Integration test: real apps wired, second run changes nothing | ✅ done | 486f269..76f25cf |
| — | Claude Security scan of the Plan 2a changes | ✅ ran (`1a77be4..f82dd43`, high): 2 verified findings F3 (medium), F4 (low) — see "Resume here"; research partly cut by usage limit, follow-up scan passed (owner confirmed 2026-11-05) | |
| — | Final whole-plan review + one fix round | ✅ done — review (opus), fix wave `289b4df..90f04d8`, re-review clean, CI green | b9734f0 |
| — | Supplemental review fixes (Codex) | ✅ done — source CI: unit, images, integration and publish passed | a0a1ed8, 56a3eb4 |

### Supplemental review fixes (Codex)

- Done by Codex: duplicate indexer display names are refused before apply; removing SABnzbd or qBittorrent removes only its Orama-managed Sonarr/Radarr clients; changing an existing indexer definition under the same name reports a clear failure and preserves the entry.
- Indexer type changes are deliberately refused rather than automatically replacing an entry. Restore the original type or use a new name; the old indexer remains and is not silently deleted.
- Regression cases failed against the original source before implementation. Local setup/wiring/hygiene checks passed (171 tests), plus Ruff and ShellCheck. The full Linux unit suite and real-container integration passed in CI for `56a3eb4`: https://github.com/DarkAngelGR/orama-server/actions/runs/41898589745.
- A separate supplemental review found no actionable issues in the fixes. The required carry-forward section is now in `2026-11-03-orama-r1-plan-1-notes.md`. The Agent, compose pins and secret handling were not changed in this fix round.
- The status-only follow-up exposed a race in the existing connection-limit test: it opened the recovery request before the closed client's handler released its slot. The test now waits for release with a bounded timeout, while retaining the excess-connection refusal and recovery assertions. Agent behavior is unchanged.
- (Superseded) The Claude Security scan has since run and its two findings are fixed (`c6220b6`); the final review has run — see Resume here.

### Decisions made while executing Plan 2a

- Task 5 also updates the integration test's data-folder list (now 11 folders, with `downloads/usenet/incomplete`).
- "Nothing changed on the second run" means: no wiring `✓ <App>:` lines, no `✗` lines except expected
  failures, and the deploy part reports nothing to change. An always-printed `✓ Folders ready` is fine.
- Gluetun's web proxy (the "send Prowlarr through the VPN" option) needs a login: user `orama` (in the compose file) and a
  generated password (`password:gluetun-proxy`, only in Gluetun's env file). Reason: with qBittorrent's localhost login
  turned off for the port-forward hook, an open proxy would let any app take over qBittorrent. Task 7 must send these
  credentials to Prowlarr.
- `PORT_FORWARD_ONLY=on` is set only for ProtonVPN (Gluetun rejects it for other providers).
- Hosts set up before Task 1 need `install.sh` run again, so it creates the shared network `orama-net`.
- qBittorrent keeps its localhost password check ON. Gluetun's port-forward hook logs in with qBittorrent's password
  (`$$QBT_WEBUI_PASSWORD`, from Gluetun's env file). This is the only `$` the Agent allows, and only in that exact string.
  The plan's Fixed-values hook string is out of date; the code (`orama.catalog`) is the source of truth.
- qBittorrent 5.3 keeps login settings in the `[WebUI]` section, not `[Preferences]`, so seed-config writes `[WebUI]`.
- Seeding runs AFTER the first deploy (Task 8 order: apply → seed-config → wiring), so the apps first write their defaults.
  When port forwarding is on and qBittorrent was seeded, Task 8 restarts Gluetun and then qBittorrent so the hook fires again.
- SABnzbd's `host_whitelist` is set to `sabnzbd, gluetun, localhost` and replaces any hostnames the user added.
- Turning the "Prowlarr through the VPN" option off removes the proxy and its tag from Orama's indexers. Turning
  Sonarr or Radarr off removes its Prowlarr connection. Indexers removed from the setup are NOT deleted from Prowlarr.
- Real-app findings (Task 9): qBittorrent 5 answers a good login with 204; Prowlarr 2.x names the generic template
  "Newznab"; SABnzbd 5 omits empty sections and creates default categories on first read; Prowlarr tests the indexer
  as saved. The test's fake Newznab returns one TV and one movie release (an empty feed can't pass the apps' tests).

### Security hardening follow-up (ChatGPT/Codex)

- API HTTP client: redirects are rejected rather than followed; response bodies are capped at 16 MiB.
- Agent Unix socket: concurrent client handlers are bounded to 16; excess connections are closed.
- SABnzbd: Orama sends API requests as POST form bodies, avoiding credentials in query strings. The Agent seeds `api_logging = 0` because SABnzbd logs parsed API parameters when that option is enabled.
- Regression tests cover redirect refusal, oversized responses, connection saturation/recovery, POST-only SABnzbd credentials and disabled SABnzbd API parameter logging.
- This was an independent hardening pass. The Claude Security scan has since run (see the scan row above).
- Plan 2b Task 11 verified the carried real-app checks; results and owner checks are recorded in the Plan 2b notes.
