# Orama Server — status

Update this file after every task, so whichever AI tool continues knows exactly where things stand.
Read `AGENTS.md` first.

## Release 1 roadmap

| Plan | Contents | State |
|---|---|---|
| 1 Foundation | installer, setup model, compose builder, secrets, Agent, deploy | ✅ done — `docs/superpowers/plans/2026-11-03-orama-r1-plan-1-notes.md` |
| 2a Download wiring | Prowlarr, Sonarr, Radarr, SABnzbd, qBittorrent (+ Gluetun) connected | ▶ in progress — `docs/superpowers/plans/2026-11-03-orama-r1-plan-2a-download-wiring.md` |
| 2b Subtitles, quality, media servers | Bazarr, Recyclarr + device profiles, Plex sign-in, Jellyfin first run | not written yet (write after 2a, against the real code) |
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
| 5 | SABnzbd and qBittorrent wiring | in progress (Claude) | |
| 6 | Sonarr and Radarr wiring | todo | |
| 7 | Prowlarr wiring | todo | |
| 8 | Step plan; `apply` runs the wiring | todo | |
| 9 | Integration test: real apps wired, second run changes nothing | todo | |
| — | Claude Security scan of the Plan 2a changes | todo (Claude only) | |
| — | Final whole-plan review + one fix round | todo | |

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
