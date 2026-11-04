# Orama Server — status

Update this file after every task, so whichever AI tool continues knows exactly where things stand.
Read `AGENTS.md` first.

## Release 1 roadmap

| Plan | Contents | State |
|---|---|---|
| 1 Foundation | installer, setup model, compose builder, secrets, Agent, deploy | ✅ done — `docs/superpowers/plans/2026-11-03-orama-r1-plan-1-notes.md` |
| 2a Download wiring | Prowlarr, Sonarr, Radarr, SABnzbd, qBittorrent (+ Gluetun) connected | ▶ in progress — `docs/superpowers/plans/2026-11-03-orama-r1-plan-2a-download-wiring.md` |
| 2b Subtitles, quality, media servers | Bazarr, Recyclarr + device profiles, Plex sign-in, Jellyfin first run | Task 1 in progress by Codex. Linux CI supplies the disposable host. |

## Resume here (updated by Claude, 2026-11-04 ~21:00 UTC)

Codex is paused; Claude is working. Order of work, first unfinished step first:

1. **Fix the 2 verified Claude Security findings (Plan 2a)** ← NEXT. Both are confirmed 3/3 by the verification panel (scan of `2c599c9..f82dd43`, high effort):
   - **F3 (MEDIUM)** `src/orama_agent/server.py:125` `_env_file_text` + `src/orama_agent/validate.py:61/516` (`FORBIDDEN_ENV_CHARS`): env-file values are written `KEY='value'` unescaped and only `' \n \r \0` are refused. Compose's dotenv parser treats `\'` as an escaped quote, so a value ending in `\` swallows the next line and the next value becomes a new unpinned `KEY=VALUE` (e.g. `VPN_PORT_FORWARDING_UP_COMMAND` or the kill-switch off) in Gluetun. Fix: allowlist env-file value characters `[A-Za-z0-9_+/=-]` (what generated secrets use; WireGuard keys are base64), refuse everything else; defence in depth: after writing, parse the file the way Compose does and check the parsed keys equal the requested keys. Agent code → most capable model.
   - **F4 (LOW)** `src/orama/catalog.py:104` `GLUETUN_PORT_FORWARD_UP_COMMAND`: the hook puts the qBittorrent password in wget's argv (`--post-data`), readable via /proc by host users. Fix: write the login form to a private temp file with a shell builtin (umask 077) and use `wget --post-file`, then delete it; update the Agent's pinned string to match.
2. **Plan 2a final whole-plan review + one fix round** over `2c599c9..HEAD` (includes Codex's supplemental fixes). Then a smaller follow-up Claude Security scan of `f82dd43..HEAD` (Claude only) to cover Codex's Agent edits and the research the usage limit cut short.
3. **Plan 2b** (`docs/superpowers/plans/2026-11-04-orama-r1-plan-2b-media-wiring.md`) — resume Task 1, the pinned-image contract experiments (Codex had started; probes run on the disposable Linux CI host).

At every task boundary record the latest tested commit, CI result, open decisions and the first unfinished step here. Before switching tools or approaching a usage limit, push a checkpoint and label any untested work explicitly.
| 3 Web app | auth/security, setup wizard, Settings, Apps page (designed with `frontend-design`) | not written |
| 4 Requests | search, request types, status stages, webhooks, post-download check, reboot/drift | not written |
| 5 Proof | end-to-end VM tests, owner's NAS checklist, security review | not written |

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
| — | Claude Security scan of the Plan 2a changes | ✅ ran (`2c599c9..f82dd43`, high): 2 verified findings F3 (medium), F4 (low) — see "Resume here"; research partly cut by usage limit, follow-up scan of `f82dd43..HEAD` pending | |
| — | Final whole-plan review + one fix round | ▶ in progress — ChatGPT/Codex hardening; Claude Security scan remains blocked | |
| — | Supplemental review fixes (Codex) | ✅ done — source CI: unit, images, integration and publish passed | 47e6516, 2fbe547 |

### Supplemental review fixes (Codex)

- Done by Codex: duplicate indexer display names are refused before apply; removing SABnzbd or qBittorrent removes only its Orama-managed Sonarr/Radarr clients; changing an existing indexer definition under the same name reports a clear failure and preserves the entry.
- Indexer type changes are deliberately refused rather than automatically replacing an entry. Restore the original type or use a new name; the old indexer remains and is not silently deleted.
- Regression cases failed against the original source before implementation. Local setup/wiring/hygiene checks passed (171 tests), plus Ruff and ShellCheck. The full Linux unit suite and real-container integration passed in CI for `2fbe547`: https://github.com/DarkAngelGR/orama-server/actions/runs/41898589745.
- A separate supplemental review found no actionable issues in the fixes. The required carry-forward section is now in `2026-11-03-orama-r1-plan-1-notes.md`. The Agent, compose pins and secret handling were not changed in this fix round.
- The status-only follow-up exposed a race in the existing connection-limit test: it opened the recovery request before the closed client's handler released its slot. The test now waits for release with a bounded timeout, while retaining the excess-connection refusal and recovery assertions. Agent behavior is unchanged.
- The dedicated Claude Security scan and final whole-plan review remain outstanding. This fix round does not close either gate.

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
- This is an independent hardening pass, not the required Claude Security scan. Keep the scan marked blocked until Claude Code Dynamic workflows are enabled and the owner runs it.
