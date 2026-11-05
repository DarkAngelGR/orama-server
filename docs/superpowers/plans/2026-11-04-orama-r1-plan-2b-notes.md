# Plan 2b notes: pinned-image contracts

Recorded from probes in `tests/integration/media_contracts.py`, run in the disposable CI install.
Run ids (GitHub Actions): **41925893375** (earlier probes), **41926191557** (probe round 1),
**41927471905** (probe round 2, commit fd79f88). Each line says *observed* or *assumed*.
Later tasks must keep an owner check (Task 11) for every *assumed* line.

Probes clean up after themselves; anything that can't be undone is listed here. Left behind: the Jellyfin
wizard user `orama-test` and the completed wizard (needed for any authenticated call), the two hidden Plex prefs
(reset to defaults, but unreadable) and, if the cleanup lines in the CI log show otherwise, whatever they report.
`_probe` catches failures and prints only the error type, so a green CI run does **not** prove a contract
still holds: read the printed `Contract ...` lines.

## Already observed before this task
- Sonarr/Radarr notification schemas: `PlexServer` fields `authToken, host, mapFrom, mapTo, port, server, signIn, updateLibrary, urlBase, useSsl`; `MediaBrowser` fields `apiKey, host, mapFrom, mapTo, notify, port, updateLibrary, urlBase, useSsl` (observed, 41926191557).
- Bazarr: key at `auth.apikey` in `/config/config/config.yaml`, header `X-API-KEY`, anonymous 401 (observed).
- Jellyfin: `/Startup/User` and `/Startup/Configuration` return 401 once the wizard is complete (observed).
- Recyclarr: `sync radarr --preview --config <file>` exits 0 (observed).

## Bazarr 1.6.2 (observed, 41927471905)
- `GET /api/system/settings` returns one JSON object with sections: `general, sonarr, radarr, auth, plex, jellyfin, subsync, proxy, backup, ...` plus one section per provider that needs settings (`opensubtitlescom, addic7ed, subdl, ...`).
- `POST /api/system/settings` is **form-encoded**, fields named `settings-<section>-<field>`, answers **204**. Repeated fields send lists.
- Sonarr/Radarr link: `settings-general-use_sonarr=true` (`use_radarr`), `settings-sonarr-ip`, `-port`, `-base_url`, `-apikey`, `-ssl` (Radarr identical). Section fields: `apikey, base_url, ip, port, ssl, http_timeout, only_monitored, series_sync/movies_sync, ...`.
- A change is **applied immediately**: re-reading settings 3 s later returned the new ip, port, key and flag; `/api/system/status` stayed 200. No restart needed.
- Languages and profiles use the same endpoint: `languages-enabled` (repeated, 2-letter codes) and `languages-profiles` (JSON string: list of `{profileId, name, cutoff, items:[{id, language, audio_exclude, hi, forced}], mustContain, mustNotContain, originalFormat, tag}`), 204. Read back: `GET /api/system/languages/profiles` (list), `GET /api/system/languages` (`code2, code3, enabled, name`; the `?enabled=true` filter is ignored, filter on `enabled` yourself). `el` (Greek) and `en` exist.
- Providers: `settings-general-enabled_providers` (repeated field) is stored as a list and `GET /api/providers` then lists each with `status: Good` within 10 s. Accepted names (all 13 tried): `podnapisi, yifysubtitles, tvsubtitles, subf2m, embeddedsubtitles, greeksubtitles, xsubs, subtitlecat, animetosho, supersubtitles, subdl, opensubtitlescom, addic7ed`. Note: "Good" only means configured, not that it can fetch.
- Which providers work **without an account** is *assumed*: `podnapisi, yifysubtitles, tvsubtitles, subf2m, supersubtitles, subtitlecat, embeddedsubtitles` (no settings section exists for them, so no credentials field); `opensubtitlescom, subdl, addic7ed` have credential sections. `greeksubtitles`/`xsubs` have no settings section either. Whether they return Greek results is not tested (owner check).
- "Prefer SRT": no such option. Relevant `general` fields: `hi_extension, utf8_encode, ignore_ass_subs, ignore_pgs_subs, ignore_vobsub_subs, subzero_mods, use_embedded_subs, embedded_subtitles_parser, minimum_score, minimum_score_movie, upgrade_subs, serie_default_enabled/_profile, movie_default_enabled/_profile, single_language`. Bazarr downloads SRT by default; `ignore_ass_subs=true` skips ASS (assumed adequate).

## Jellyfin 12.4 (observed, 41927471905)
- `GET /Startup/Configuration` (before completion) fields: `MetadataCountryCode, PreferredMetadataLanguage, ServerName, UICulture`.
- `POST /Users/AuthenticateByName` body `{Username, Pw}` with header `Authorization: MediaBrowser Client="..", Device="..", DeviceId="..", Version=".."` returns 200 with `AccessToken, ServerId, SessionInfo, User`.
- Token header: `Authorization: MediaBrowser Client=.., Device=.., DeviceId=.., Version=.., Token="<token>"` works (200). `X-Emby-Token` alone gave **401**: use the MediaBrowser header.
- `POST /Library/VirtualFolders?name=<n>&collectionType=movies&refreshLibrary=false` with JSON body `{"LibraryOptions":{"PathInfos":[{"Path":"/data/media/movies"}]}}` returns 204; `GET /Library/VirtualFolders` then lists it (`Name, CollectionType, Locations, ItemId, LibraryOptions{PathInfos, SaveSubtitlesWithMedia, SubtitleFetcherOrder, ...}`).
- `POST /Auth/Keys?app=<name>` 204; `GET /Auth/Keys` returns `{Items:[{AccessToken, AppName, DateCreated, Id, ...}], StartIndex, TotalRecordCount}`; read the new key from `AccessToken` of the item with that `AppName`.
- `GET /System/Configuration/encoding` hardware fields: `HardwareAccelerationType` (string), `VaapiDevice`, `QsvDevice`, `EnableHardwareEncoding`, `HardwareDecodingCodecs` (list), `EnableDecodingColorDepth10Hevc`, `EnableDecodingColorDepth10HevcRext`, `EnableDecodingColorDepth10Vp9`, `AllowHevcEncoding`, `AllowAv1Encoding`, `EnableTonemapping`, `EnableVppTonemapping`, `EncoderPreset`, `EncodingThreadCount`.
- Enabling a hardware type against a real GPU is *assumed* (no GPU in CI): owner check.

## Recyclarr 8.8.1 (observed, 41927471905)
- `recyclarr list templates` does **not exist**. Use `recyclarr config list templates` (prints only a count: "Found 22 Sonarr and 35 Radarr templates") or read the cloned repo under `/config/resources/config-templates/git/official/<app>/templates/*.yml` (after any `list`/`sync` run).
- `recyclarr list custom-formats <radarr|sonarr> --raw` prints TSV `trash_id<TAB>name<TAB>group`; without `--raw` it prints only an id list. Also available: `list custom-format-groups|score-sets|qualities|quality-profiles|naming <service>`.
- Custom formats are cloned at `/config/resources/trash-guides/git/official/docs/json/<app>/cf/*.json`.
- Radarr template files (non-regional): `hd-bluray-web, uhd-bluray-web, remux-web-1080p, remux-web-2160p, remux-2160p-alternative, remux-2160p-combined, anime-remux-1080p, sqp/sqp-1-1080p ... sqp-5`. Sonarr: `web-1080p, web-1080p-alternative, web-2160p, web-2160p-alternative, web-2160p-combined, remux-web-1080p, remux-web-2160p, remux-2160p-alternative, remux-2160p-combined, anime-remux-1080p`. Which three names map to the owner's quality choices is a design decision for the consuming task; the candidates are HD `hd-bluray-web` / `web-1080p`, 4K `uhd-bluray-web` / `web-2160p`, remux `remux-web-2160p` (both).

Custom formats (Radarr / Sonarr trash_id, public TRaSH ids):

| Name | Radarr | Sonarr |
|---|---|---|
| DV (Disk) | f700d29429c023a5734505e77daeaea7 | ef4963043b0987f8485bc9106f16db38 |
| DV (w/o HDR fallback) | 923b6abef9b17f937fab56cfcf89e1f1 | 9b27ab6498ec0f31a3353992e19434ca |
| DV Boost | b337d6812e06c200ec9a2d3cfa9d20a7 | 7c3a61a9c6cb04f52f1544be6d44a026 |
| DTS | 1c1a4c5e823891c75bc50380a6866f73 | 5964f2a8b3be407d083498e4459d05d0 |
| DTS-HD MA | dcf3ec6938fa32445f590a4da84256cd | c429417a57ea8c41d57e6990a8b0033f |
| DTS X | 2f22d89048b01681dde8afe203bf2e95 | 9d00418ba386a083fbf4d58235fc37ef |
| DTS-HD HRA | 8e109e50e0a0b83a5098b056e13bf6db | cfa5fbd8f02a86fc55d8d223d06a5e1f |
| DTS-ES | f9f847ac70a0af62ea4a08280b859636 | c1a25cd67b5d2e08287c957b1eb903ec |
| TrueHD | 3cafb66171b47f226146a0770576870f | 1808e4b9cee74e064dfae3f1db99dbfe |
| TrueHD ATMOS | 496f355514737f7d83bf7aa4d24f8169 | 0d7824bb924701997f874e7ff7d4844a |
| PCM | e7c2fcae07cbada050a0af3357491d7b | 30f70576671ca933adbdcfc736a69718 |
| FLAC | a570d4a0e56a2874b64e5bfa55202a1b | 851bd64e04c9374c51102be3dd9ae4cc |
| HDR (HDR10) | 493b6d1dbec3c3364c59d7607f7e3405 | 505d871304820ba7106b693be6fe4a9e |
| HDR10+ Boost | caa37d0df9c348912df1fb1d88f9273a | 0c4b99df9206d2cfac3c05ab897dd62a |

Also present: `DD+ ATMOS` (Radarr 1af239278386be2919e1bcee0bde047e, Sonarr 4232a509ce60c4e208d13825b7c06264), `ATMOS (undefined)` (Radarr 417804f7f2c4308c1f4c5d380d4c4475, Sonarr b6fbafa7942952a13e17e2b1152b539a). There is no separate plain "DV" CF other than the three DV entries above, and no separate "HDR10" besides `HDR`.

## Plex 1.43.5 (observed, 41927471905, except where marked)
- Unclaimed `GET /identity` (send `Accept: application/json`): 200, `MediaContainer` fields `apiVersion, claimed (false), machineIdentifier, size, version`.
- Unclaimed, **no token needed** from the local network for: `/`, `/library/sections`, `/:/prefs`, `/servers`, `/myplex/account`, `/status/sessions`, `/transcode/sessions`, `/updater/status`, `/web/index.html` (all 200), and for writes below. Whether a claimed server demands a token for these is *assumed* yes (Plex docs): wiring must be able to send `X-Plex-Token` after the owner claims.
- Library create: `POST /library/sections?name=&type=movie&agent=tv.plex.agents.movie&scanner=Plex%20Movie&language=en-US&location=/data/media/movies` returned **201** with JSON `MediaContainer.Directory[]`. The query was percent-encoded (`%20` for spaces) and succeeded; the `+` encoding was not tested. Read back `GET /library/sections` shows `Directory[]{key, type, title, agent, scanner, language, uuid, Location[]{id, path}}`.
- Prefs: `GET /:/prefs` lists 151 `Setting` entries. `HardwareAcceleratedCodecs` and `LanNetworksBandwidth` are **not in that list** (hidden prefs). `PUT /:/prefs?<key>=<value>` answers 200 for both, but the value cannot be read back through `/:/prefs` (observed `None`). Whether they take effect is *assumed* (Plex docs; hardware transcoding also needs Plex Pass): owner check.
- Claiming and account sign-in (plex.tv, claim token) are owner-only and *assumed* from Plex docs; nothing in CI touches them.

## Task 3 — continuation (ChatGPT/Codex, 2026-11-05)

- Resumed from a clean checkout at `a8284c5`; Task 2 and the existing Task 3
  implementation were already committed. CI for `a8284c5` passed (run 41942899479).
- Added the missing Bazarr-specific Agent tests for symlinked directories/files,
  FIFOs, oversized files, private file mode and ownership, stop/start behavior,
  unchanged-file idempotence and secret redaction.
- A regression test failed when an existing auth section had no API key and its
  last line lacked a newline: the inserted API key joined the preceding setting.
  Added the required separator before insertion; the regression now passes.
  This is a Task 3 correctness fix, with no new command or broader allowlist.
- Local verification at `26f4c02`: 981 unit tests passed (27 integration tests
  deselected), Ruff passed, ShellCheck passed. CI run 41944049997 passed: unit, images, integration and publish.
- Task 3 complete. Next task: Task 4 (Plex LAN and NVIDIA capabilities).

## Task 4 — Plex LAN and NVIDIA settings (ChatGPT/Codex)

- Plex advertises the configured private IPv4 address using its assigned host port.
  Bootstrap accepts `--lan-address` (default `none` for older callers); the installer
  uses the same address selection as its printed URL, forwarding only RFC1918 IPv4.
- Plex and Jellyfin receive the exact NVIDIA visibility and driver capability values
  when NVIDIA is selected. The Agent requires both with its exact GPU reservation
  and refuses them without it. No catalogue change was needed: these settings depend
  on setup choices and belong in the builder and Agent validation.
- Local verification at `cd24f32`: 1,003 unit tests passed, Ruff and ShellCheck passed.
  New builder, Agent and bootstrap tests failed before implementation; installer
  test setup was corrected to provide the existing required image variables.
- Fresh review found no actionable correctness or security issues.
- CI run 41945388747: first attempt failed while pulling app images, before app
  startup (apply reported download failure). Detailed log artifact retrieval was
  denied by storage; failed jobs retried with unchanged source. Retry passed: unit, images, integration
  and publish all green at `cd24f32`. Task 4 complete.
  Next task: Task 5 (Bazarr wiring).

## Task 6 — profiles (Claude)

- Recyclarr templates per quality (chosen from the candidates above): `4k` = Radarr `uhd-bluray-web` + Sonarr
  `web-2160p`; `1080p` = `hd-bluray-web` + `web-1080p`; `mixed` = Radarr `uhd-bluray-web` + Sonarr `web-1080p`
  (4K movies, 1080p TV). The plain "bluray-web" and "web" templates are TRaSH's standard non-remux ones, so
  downloads stay reasonably sized.
- Symbolic `dv` maps to both DV formats that can win a release on score: `DV (Disk)` and `DV Boost`.
  `dv_no_fallback` maps to `DV (w/o HDR fallback)`. TRaSH has no single "all DV" format. `hdr10` is TRaSH's `HDR`,
  `hdr10_plus` is `HDR10+ Boost`. A symbol may map to several ids; scores are set on each.
- Only `samsung_s95f` and `safest` are marked `verified: true`; `lg`, `apple_tv`, `nvidia_shield` are best effort.

## Plan 5 owner checks carried from Plan 2b

- Sign in with `python -m orama.cli plex-sign-in`; approve the PIN, confirm the server
  is claimed, and verify Movies/TV libraries appear without duplicates after reapply.
- With Plex Pass, play a file requiring transcoding and confirm hardware transcoding
  works on the NAS GPU. Verify Jellyfin hardware transcoding separately.
- Confirm Plex is discovered and reachable on the LAN at its advertised address/port.
- Fetch a real Greek subtitle through the selected free providers; confirm the Greek,
  then English preference and text subtitle behavior on a real movie and TV episode.
- Play an S95F-compatible file in Plex and confirm the playback dashboard says Direct Play.
- Other device profiles remain best effort; test the actual client before treating
  their compatibility as verified. CI has no Plex account or GPU.

## Task 11 — real-app integration (ChatGPT/Codex)

- Owner handed off Claude's work at `8d3bb0a`. Tasks 5–10 were implemented; CI failed
  because neither Sonarr nor Radarr had an Orama quality profile.
- Root cause: Recyclarr 8.8.1 filters duplicate instance names globally across app
  types. Both instances were named `orama`, so both were skipped despite a successful
  process exit. Agent rendering now uses `orama-sonarr` and `orama-radarr`; user-visible
  profile names stay `Orama`. A regression test failed before this fix and passed after.
  The rendered-config digest changes, so existing installs sync again automatically.
- Observed on pinned real apps: Bazarr links and Greek/English profile plus defaults;
  Orama quality scores in both apps; Jellyfin wizard, libraries and API key; both
  Jellyfin notifications; Plex waiting for sign-in. Repeated install/apply changes
  nothing and keeps container IDs. Device/quality changes reuse profile IDs, replace
  device scores, and restore default scores correctly. Killing Jellyfin immediately
  before library creation is recoverable; only the missing library changes on retry.
- Secret checks now include Bazarr/Jellyfin container logs and Recyclarr's persisted
  logs. Failed API-key comparisons withhold values instead of using pytest's rewritten
  comparisons. New secret types are included through the existing secret-store scan.
- Task review: installer ordering and assertion secret disclosure findings fixed;
  final reviewer found no remaining Important/Critical issues. Deferred minor coverage:
  explicitly assert allowed resolutions after quality changes; explicitly assert fake
  Usenet is the only failure after each recovery apply (current checks assert changed
  steps and API-key preservation, alongside the earlier whole-stack failure checks).
- Verification: 1,267 local unit tests, Ruff and ShellCheck passed. All four CI jobs
  passed at `4c84502` (run 41988527186). Final assertion-only follow-up `e2f4faa`: all four CI jobs passed in run
  42015057875 on retry. First attempt hit a confirmed GHCR rate limit pulling Bazarr
  before app startup; unchanged source passed on retry. Task 11 complete.
- Plan 2b still requires its final whole-plan review/fix round and the owner's Claude
  Security scan. Task review above is not a substitute for those gates.
