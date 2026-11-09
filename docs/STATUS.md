# Orama Server — status

Update this file after every task, so whichever AI tool continues knows exactly where things stand.
Read `AGENTS.md` first.

## Release 1 roadmap

| Plan | Contents | State |
|---|---|---|
| 1 Foundation | installer, setup model, compose builder, secrets, Agent, deploy | ✅ done — `docs/superpowers/plans/2026-11-03-orama-r1-plan-1-notes.md` |
| 2a Download wiring | Prowlarr, Sonarr, Radarr, SABnzbd, qBittorrent (+ Gluetun) connected | ✅ complete — code, review and CI passed; owner confirmed follow-up Claude Security scan passed (2026-11-05) |
| 2b Subtitles, quality, media servers | Bazarr, Recyclarr + device profiles, Plex sign-in, Jellyfin first run | Code complete: Tasks 1–11 + final review/fix wave done, CI green at `b41db6a`. Owner Claude Security scan pending. |

| 3 Web app | auth/security, setup wizard, Settings, Apps page (designed with `frontend-design`) | Code complete: Tasks 1–12 + final review/fix wave done, CI green (last checked `dab238f`). Owner Claude Security scan pending (see below). |
| 4 Requests | search, request types, status stages, webhooks, post-download check, reboot/drift | Code complete (`needs review`, Gemini + Codex): Tasks 1–11 + review fix wave (`fd5fa71`, `58287fb`, `f32d993`) passed CI (`42511960464`; 2,014 unit tests, browser, demo, images, and all 41 real-container integration tests). Task board in `docs/tasks/plan-4/`. Owner Claude Security scan pending. |
| 5 Proof | end-to-end VM tests, owner's NAS checklist, security review | draft in `docs/superpowers/plans/2026-11-07-orama-r1-plan-5-proof.md`; real VM CI & owner NAS checklist next |

## Current handoff (Gemini, 2026-11-08)

Owner deferred Claude Security scans until Wednesday 2026-11-11 and authorized continued
project work. The Plan 2b, Plan 3, and Plan 4 scans remain pending; they are not development blockers.
All 11 tasks in Plan 4 (Requests) plus the review fix wave (`fd5fa71`, `58287fb`, `f32d993`) are
implemented and verified in CI run `42511960464` on `test` (2,014 non-integration Python tests,
`npm test`, Ruff, ShellCheck, 4 real Chromium browser scenarios, Docker image builds, and all 41
real-container integration tests including `tests/integration/test_requests.py`). Self-update
remains a separate outstanding operations subplan before Release 1 completion; see the Plan 4 notes.

## Test branch CI repair (Codex, 2026-11-07)

The owner requested a CI run and repair on `test`. Run 42316368427 executed and failed:
the web entry point imported `SetupReader` from `orama.setup_model`, although the class
is defined in `orama.web`. Both unit and browser jobs hit that import error; image and
integration jobs were skipped. The import is corrected in this repair. Run 42316906235 passed unit and image jobs, then exposed a browser-driver module
conflict from the new root package.json. The driver now has an explicit `.cjs` extension;
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
| 1 | Publish gate & end-to-end VM verification | open | — |
| 2 | Root-side self-update operations subplan | open | — |
| 3 | Owner hardware checklist (DXP4800 Plus / UGOS Pro) | open | Owner |
| 4 | Security review, GHCR packaging & Release 1 sign-off | open | Owner + Claude |

## Resume here (updated by ChatGPT/Codex, 2026-11-05)

1. **Plan 2a:** complete. Owner confirmed on 2026-11-05 that the follow-up Claude Security scan passed; no repeat scan required.
2. ← NEXT: **Plan 2b** (`docs/superpowers/plans/2026-11-04-orama-r1-plan-2b-media-wiring.md`) — owner OK'd; executing (Claude). Task 1 ✅ done (contracts in docs/superpowers/plans/2026-11-04-orama-r1-plan-2b-notes.md, CI green at 9c18626). Task 2 complete — CI passed at 06f25f13 (run 41939856043). Task 3 ✅ done (ChatGPT/Codex, 2026-11-05): `26f4c02`; 981 local unit tests, Ruff and ShellCheck passed. CI run 41944049997: unit, images, integration and publish all passed. Task 4 ✅ done (ChatGPT/Codex): `cd24f32`; 1,003 unit tests, Ruff and ShellCheck passed; fresh review clean. CI run 41945388747 passed all four jobs on retry after an app-image download failure. Task 5 ✅ done (Claude): `9953e07`, review clean. Codex's Tasks 2–4 reviewed by Claude (opus); fixes `c56e68f`, `05c0b26` (Bazarr seeding refuses unusual/duplicate/alias YAML via a strict loader; Plex LAN address from the default route), re-review clean. Task 6 ✅ done (Claude): `ed56218`, review clean. Task 7 ✅ done (Claude): `db5eb0d`, `5f25e77`, review clean (one quality profile per app named "Orama"; Recyclarr container named, hardened, removed on timeout). Real-run checks for Task 11 are in the Plan 2b ledger summary below. Task 8 ✅ done (Claude): `3cae6da`, `8472c7b`, review clean (`python -m orama.cli plex-sign-in`; Plex steps skip with "waiting for sign-in" until then). Task 9 ✅ done (Claude): `2e3d118`, `35d346a`, review clean; Jellyfin contract probe now uses a throwaway container. Task 10 ✅ done (Claude): `5f85643`, review clean. Task 11 ✅ done (ChatGPT/Codex, owner handoff): real-app integration added; Recyclarr's duplicate instance names fixed (`1d488ba`); review findings fixed. Final code/test commit `e2f4faa`: 1,267 local unit tests, Ruff and ShellCheck passed; CI run 42015057875 passed unit, images, integration and publish on retry (first attempt hit a confirmed registry rate limit pulling Bazarr). See Plan 2b notes for observed contracts and two minor deferred test-coverage improvements. **Plan 2b code complete (Claude, 2026-11-05):** final whole-plan review (opus) + fix wave `477845c..b41db6a` (Jellyfin recovery via set-secret user:jellyfin + password:jellyfin; Bazarr ignores PGS/VobSub/ASS so it fetches SRT; Recyclarr guide pinned to TRaSH-Guides d0f1a6c / config-templates 89eebad; Jellyfin notify=false; LAN address refreshed on reinstall), re-review clean, CI run 42050590613 green. **Only gate left: the owner's Claude Security scan of Plan 2b** (`/claude-security:claude-security` → Scan changes, base `441c1a8`). Plan 3 (web app) is written: `docs/superpowers/plans/2026-11-05-orama-r1-plan-3-web-app.md` — owner OK'd; executing. Board below. The Jellyfin contract probe already uses its own throwaway container; that carry-forward item is resolved.

## Plan 3 — task board (details and reports in `docs/tasks/plan-3/Tnn.md`)

| # | Task | Model | State | Owner |
|---|---|---|---|---|
| 1 | Locks for setup, secret store and applies | most capable | done (`eb9eed9`, `01cea5e`) | Claude |
| 2 | VPN provider list, Gluetun control server, unwritable folders | most capable | done (`d813622`) | Claude |
| 3 | Wiring: removals, single-item tests, proxy message | standard | done (`535a44b`) | Claude |
| 4 | Installer/bootstrap: one-time setup link, host name, graphics | most capable | done (`f36c939`) | Claude |
| 5 | Web foundation and security | most capable | done (`8f1ddce`..`147df8a`) | Claude |
| 6 | Design system and base layout | standard | done (`655939e`..`73ba9e0`) | Codex + Claude |
| 7 | Apply jobs with a live checklist | standard | done (`a107a6b`..`43b9fa8`) | Claude |
| 8 | Wizard: servers, folders, downloads/VPN, playback, subtitles | standard | done (`b1fa2ab`) | Claude |
| 9 | Wizard: indexers, Usenet, review, install, Plex sign-in | most capable | done (`fd8f827`..`d441491`) | Codex + Claude |
| 10 | Settings and Change setup | standard | done (`f2c8cf2`..`2f983ce`) | Claude |
| 11 | Apps page and Home | standard | done (`ec7ee0e`, `634482f`) | Claude |
| 12 | Design reviews and browser pass | standard | done (`f9883e8`..`a456b90`) | Claude |

**Plan 3 hand-off (Claude, 2026-11-06):** web app code complete. Two gates are left and both are the owner's: (1) Claude Security scan of **Plan 2b** (`/claude-security:claude-security` → Scan changes, base `441c1a8`, High, Yes), (2) Claude Security scan of **Plan 3** (base `6a39133`, High, Yes). Run them right after a usage reset. Decisions to know: the media folder is read-only in the web app (set by the installer; changing it later = re-run the installer with `ORAMA_DATA_ROOT`); sessions are tied to the sign-in address; Home is a placeholder until Plan 4. Details: `docs/superpowers/plans/2026-11-05-orama-r1-plan-3-notes.md`. Next: write Plan 4 (requests) — see the roadmap table.

At every task boundary record the latest tested commit, CI result, open decisions and the first unfinished step here. Before switching tools or approaching a usage limit, push a checkpoint and label any untested work explicitly.

## Plan 2a — tasks

| # | Task | State | Commit |
|---|---|---|---|
| 1 | Shared network and Gluetun options (compose + Agent pins) | ✅ done | 77088f8, f530272 |
| 2 | Agent `seed-config` for SABnzbd and qBittorrent | ✅ done | be74c0f, 1c82c35 |
| 3 | HTTP client and step engine | ✅ done | c657bc2 |
| 4 | Indexers and Usenet servers in the setup; `set-secret` | ✅ done | ba80dfe |
| 5 | SABnzbd and qBittorrent wiring | ✅ done | cba82cc |
| 6 | Sonarr and Radarr wiring | ✅ done | 9455d71 |
| 7 | Prowlarr wiring | ✅ done | 64b8b4d, 2d6a8c2 |
| 8 | Step plan; `apply` runs the wiring | ✅ done | 25da23e |
| 9 | Integration test: real apps wired, second run changes nothing | ✅ done | 065cad7..76f25cf |
| — | Claude Security scan of the Plan 2a changes | ✅ ran (`2c599c9..f82dd43`, high): 2 verified findings F3 (medium), F4 (low) — see "Resume here"; research partly cut by usage limit, follow-up scan passed (owner confirmed 2026-11-05) | |
| — | Final whole-plan review + one fix round | ✅ done — review (opus), fix wave `538750a..90f04d8`, re-review clean, CI green | 90f04d8 |
| — | Supplemental review fixes (Codex) | ✅ done — source CI: unit, images, integration and publish passed | 47e6516, 2fbe547 |

### Supplemental review fixes (Codex)

- Done by Codex: duplicate indexer display names are refused before apply; removing SABnzbd or qBittorrent removes only its Orama-managed Sonarr/Radarr clients; changing an existing indexer definition under the same name reports a clear failure and preserves the entry.
- Indexer type changes are deliberately refused rather than automatically replacing an entry. Restore the original type or use a new name; the old indexer remains and is not silently deleted.
- Regression cases failed against the original source before implementation. Local setup/wiring/hygiene checks passed (171 tests), plus Ruff and ShellCheck. The full Linux unit suite and real-container integration passed in CI for `2fbe547`: https://github.com/DarkAngelGR/orama-server/actions/runs/41898589745.
- A separate supplemental review found no actionable issues in the fixes. The required carry-forward section is now in `2026-11-03-orama-r1-plan-1-notes.md`. The Agent, compose pins and secret handling were not changed in this fix round.
- The status-only follow-up exposed a race in the existing connection-limit test: it opened the recovery request before the closed client's handler released its slot. The test now waits for release with a bounded timeout, while retaining the excess-connection refusal and recovery assertions. Agent behavior is unchanged.
- (Superseded) The Claude Security scan has since run and its two findings are fixed (`fa5007b`); the final review has run — see Resume here.

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
