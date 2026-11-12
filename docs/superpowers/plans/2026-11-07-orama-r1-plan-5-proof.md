# Orama Release 1 — Plan 5: Proof, Self-Update and Sign-off Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking; `- [x]` means done and checked in CI.

**Goal:** Prove that Release 1 works from a bare machine to a playable, subtitled title: Orama can update itself, a clean Ubuntu 24.04 and a clean Debian 12 install go end to end with nobody touching them, the owner's NAS passes a plain-language checklist, and the security reviews are closed before anything is published on `main`.

**Architecture:** No new product architecture. Plans 1–4 stay as they are: only the Agent has the Docker socket, Orama mounts only its own folder, apps are untrusted. This plan adds one Agent capability (self-update, two argument-less commands and one hardened helper container), one CI gate, a test harness that lives only under `tests/e2e/`, and documents.

**Tech Stack:** Existing Python ≥3.12 (`uv`, Flask, pytest), bash installer, Docker Compose, Playwright Chromium (pinned, already used by the `browser` job), GitHub Actions hosted runners. New for Task 3 only, and only inside CI: QEMU/KVM, a Debian 12 cloud image pinned by checksum, cloud-init.

**Spec:** `docs/superpowers/specs/2026-11-03-orama-release-1-design.md`, §4.2, §9, §10, §12. Read `AGENTS.md`, `docs/tasks/README.md`, the Plan 4 notes and **`2026-11-07-orama-r1-plan-5-notes.md`** first; notes override plan text.

**Status (2026-11-11):** Rewritten by Claude after a review of the `test` branch. The first version of this plan was a 60-line outline, and its Task 1 report claimed more than was built. The honest state:

| # | Task | State |
|---|---|---|
| 1 | Publish gate | **done** |
| 2 | Self-update | **done** (Gemini built it; Claude reviewed and fixed it, see the notes) |
| 3 | End-to-end proof on clean Ubuntu 24.04 and Debian 12 | **IN PROGRESS — step 1 of 5 done** (2026-11-12: the hosted runner can run a virtual machine). Nothing of the proof itself exists yet. |
| 4 | Owner NAS checklist | **open** (the checklist is written: `docs/OWNER-CHECKLIST.md`; the owner has not run it) |
| 5 | Security scans, packaging, sign-off | **open** |

Release 1 is **not** done until Tasks 3, 4 and 5 are (spec §10.3).

## Global Constraints

- Work on the `test` branch until Task 5 merges it. `main` is what users install from: nothing lands there before the security scans and code reviews in Task 5 pass.
- Only the Agent has the Docker socket. Any new or changed Agent command has exact request validation and refusal tests (extra arguments refused, unknown command refused). Never loosen a check to make a test pass.
- Test harness code (Task 3) lives under `tests/e2e/` and in the workflow. It must not add options, back doors, test-only routes or environment switches to `install.sh`, Orama or the Agent. If the product has to change for the proof to pass, that is a product bug: fix it under its own commit with its own unit test.
- No secrets, IP addresses, e-mail addresses or personal paths in code, tests, logs kept as artifacts, or docs (`tests/test_repo_hygiene.py`). Keys and passwords used by tests are made at run time and never printed. SSH keys for the VM are generated per run and never committed.
- No real indexer, tracker, Usenet provider, VPN or Plex account in CI. No copyrighted media: clips are generated.
- Third-party things CI downloads for the proof are pinned: Playwright by version (already), the Debian image by dated build and SHA-512. No update to app image pins as a side effect of this plan.
- A proof step that cannot run must **fail**, never skip. A job that skips its way to green proves nothing (same rule as `tests/integration/conftest.py::require_host`).
- Words shown to the owner are plain: no HTTP codes, no stack traces, no internal names.
- Required local checks before every commit: `uv run pytest -m "not integration"`, `uv run ruff check`, `uvx --from shellcheck-py shellcheck install.sh`; `uv run pytest tests/web/test_browser.py -m integration` when a page or its script changes. CI on `test` must be green (`unit`, `browser`, `images`, `integration`, and the Task 3 jobs once they exist) for the commit a task is closed on.

## Decisions and alternatives

- **Self-update is two Agent commands, not one** (differs from the spec's wording in §4.2). The download holds no lock that an apply needs; only the seconds-long restart does. Alternative kept out: one long command that blocks Settings and applies for the length of a download. Full reasoning, the helper's hardening and its limits are in the Plan 5 notes.
- **The Debian 12 proof needs a real Debian 12 machine.** A Debian `os-release` file fed to the installer on an Ubuntu runner (what exists today) checks one `case` statement, not Debian. QEMU/KVM on the hosted runner is what the spec asks for (§10.1.3). Alternative considered: a Debian container with systemd and Docker-in-Docker. Rejected: it shares the runner's kernel and Docker, so it would not prove the installer's "install Docker from Docker's Debian repository" path or a Debian kernel.
- **The Ubuntu proof runs on the runner itself**, which already is a fresh Ubuntu 24.04 machine. It ships with Docker, so on Ubuntu the proof covers "Docker is already there"; "Docker is missing" is covered on Debian. This split is deliberate and must be stated in the job's log.
- **The end-to-end jobs get their own workflow file and are tried on a throw-away branch** (decided in step 1 of Task 3). They live in `.github/workflows/e2e.yml`, which `ci.yml` calls, and are first made green on the branch `e2e-dev`, which `ci.yml` ignores. Alternative kept out: trying them on `test`, where every red attempt runs the whole of CI and marks the shared branch as broken.
- **One harness, two targets.** The same driver runs against "this machine" (Ubuntu job) and "a machine reached over SSH with forwarded ports" (Debian job). Two separate scripts would drift.
- **The fake indexer is Torznab with a web seed.** It serves a Torznab feed, the `.torrent` files and the file bytes over HTTP (a BitTorrent "web seed"), so qBittorrent really downloads and no second torrent client is needed. Fallback, only if step 2 of Task 3 shows the pinned qBittorrent will not fetch a tracker-less web-seeded torrent: add a tracker endpoint to the same fake and a second container from the already pinned qBittorrent image as the seeder. No new third-party image either way.
- **Jellyfin in CI, Plex on the NAS.** Plex needs a real account, which CI must not have. "Visible in Plex" is on the owner checklist; CI asserts Jellyfin.
- **Until the images are public, the proof installs from the checkout** (`ORAMA_BUILD_LOCAL=1`), which is the same `install.sh` but builds the two images instead of downloading them. The literal `curl … | sudo bash` with a real download is proved once, after publishing (Task 5).

## Fixed values and shared types

- Agent commands (final): `self-update-pull` and `self-update`, both `{}` only. Results `{"newer": bool}` and `{"started": bool}`. Client methods `AgentClient.self_update_pull() -> bool`, `AgentClient.self_update() -> bool`; limits `SELF_UPDATE_PULL_TIMEOUT = 1500`, `SELF_UPDATE_TIMEOUT = 300`.
- Helper container: name `orama-updater`, started from the Agent image named in `<config_root>/core.compose.yaml`, argv fixed in `ComposeRunner.self_update` (`--network none`, `--cap-drop ALL`, `--security-opt no-new-privileges`, `--read-only`, tmpfs `/tmp`, `--pids-limit 256`, `--memory 512m`, config root read-only, `up -d --pull never --remove-orphans --wait --wait-timeout 300`). Kept after exit for its log.
- Web: `POST /settings/update-orama`; redirects to `/settings?saved=updating` or `/settings?saved=current`; messages `UPDATE_STARTED`, `UP_TO_DATE`, `UPDATE_WAITING` in `src/orama/web/settings.py`.
- Publish gate: `publish.needs == [unit, images, browser, integration]`, later plus `e2e-ubuntu` and `e2e-debian` (Task 3, last step).
- Task 3 names: workflow jobs `e2e-ubuntu`, `e2e-debian`; folder `tests/e2e/`; fake indexer container `orama-e2e-indexer` on the network `orama-net`, reachable by the apps as `http://orama-e2e-indexer:8000`; image pin file `tests/e2e/debian-12.image` (two lines: URL, SHA-512); VM forwards `127.0.0.1:2222 → 22` and `127.0.0.1:8765 → 8765`; artifact name `e2e-logs-<target>`; workflow file `.github/workflows/e2e.yml`; throw-away branch `e2e-dev` (ignored by `ci.yml`; holds nothing of its own between steps and is deleted at step 5 at the latest).
- Task 3 titles: one episode of one show and one film that the apps can find in their public catalogues (Plan 4 used TVDB 81189 S01E01 and TMDB 27205; catalogue metadata is the only thing fetched from outside). Release names follow real naming and say Dolby Digital Plus, for example `<Show>.S01E01.1080p.WEB-DL.DDP5.1.H.264-ORAMA`. Wizard answers: Jellyfin only, torrents without VPN, device `Samsung S95F`, subtitles Greek then English.

## Review Focus

1. **The helper holds the Docker socket.** Its argv must stay fixed and minimal; nothing a client sends may reach it; the image name comes only from the root-owned compose file and must look like an image name. Tasks 2, 5.
2. **A download must never leave a half-updated system.** Failed or offline download: nothing stopped, nothing recreated, plain message. Restart only from complete downloads (`--pull never`), never during an apply. Task 2.
3. **Green must mean proved.** No skip paths, no `|| true`, no "verified" in a report for something only parsed. Every Task 3 assertion below is checked against the real machine, and each report says what was *not* covered. Tasks 1, 3.
4. **The proof must not weaken the product.** No test-only switches in the installer or the apps; the VM's SSH key, the indexer key and the passwords exist for one run only and are not in artifacts. Task 3.
5. **Same inode, placed by the apps.** The hardlink and the subtitle must be made by Sonarr/Radarr/Bazarr through Orama's wiring, not by the test (Plan 4's pipeline test makes its own hardlink with `ln`; that is not this proof). Task 3.
6. **Second run changes nothing** is compared on container ids, the compose files' bytes and the setup file, not on the installer's exit code. Tasks 3, 4.

## File map

- Task 1: `.github/workflows/ci.yml`, `tests/test_ci_workflow.py`.
- Task 2: `src/orama_agent/runner.py`, `src/orama_agent/server.py`, `src/orama/agent_client.py`, `src/orama/web/settings.py`, `src/orama/web/templates/settings/index.html`; tests in `tests/agent/test_server.py`, `tests/web/test_settings.py`, `tests/integration/test_install_and_apply.py`.
- Task 3 (to create): `tests/e2e/{__init__,conftest,target,test_e2e}.py`, `tests/e2e/wizard.cjs`, `tests/e2e/fake_torznab.py`, `tests/e2e/make_clips.sh`, `tests/e2e/vm/{boot.sh,user-data.tmpl}`, `tests/e2e/debian-12.image`, `tests/test_e2e_fixtures.py` (unit tests of the fake indexer and the pin file), the jobs in `.github/workflows/e2e.yml` called from `.github/workflows/ci.yml`, `tests/test_ci_workflow.py`.
- Task 4: `docs/OWNER-CHECKLIST.md`, `docs/tasks/plan-5/T04.md`.
- Task 5: `docs/tasks/plan-5/T05.md`, `docs/STATUS.md`, the notes files.

## Task 1: Publish gate — DONE

**Files:** `.github/workflows/ci.yml`, `tests/test_ci_workflow.py`.
**Interfaces:** the `publish` job (moves the `<version>` tag users install) needs `unit`, `images`, `browser` and `integration`, and runs only for a push to `main`.
- [x] Test first: `test_version_tag_published_only_after_integration_on_main` and `test_browser_job_is_separate_pinned_and_gates_publish` require `browser` in `publish.needs`.
- [x] Add `browser` to `publish.needs` after it passed three runs in a row.
- [x] Commit `ci: gate publish on browser and prove Debian 12 installer` (`0bcf21f`).

**What this task did not do** (the old report said otherwise): it did not prove Debian 12. The same commit added `test_debian_12_installer_and_agent_self_update_contract`, which runs the installer on the **Ubuntu** runner with a Debian `os-release` file. Docker is already installed there, so the installer's Debian package-repository steps do not run in it at all; they are covered only by the dry-run unit tests in `tests/test_install_sh.py`. The real Debian 12 proof is Task 3.

## Task 2: Self-update — DONE (as fixed on 2026-11-11)

**Files:** see the file map.
**Interfaces:** see *Fixed values*. `Agent.self_update_pull(args)` and `Agent.self_update(args)` both refuse any argument, check `core.compose.yaml` through no-follow descriptors, and take the Agent's update lock without waiting; only `self_update` also takes the main lock. `ComposeRunner.self_update_pull() -> bool` pulls the core project and reports whether a core container is behind its image; `ComposeRunner.self_update() -> bool` starts the helper, or returns False and starts nothing when both containers are current. `FakeRunner` mirrors both.
- [x] Tests first (they failed before the change): exact argv of both steps; arguments and near-miss command names refused; links and damaged or strange compose files refused before Docker is called; a download does not wait for the main lock and blocks neither `status`, `ensure-folders` nor `data-space`; a second download or a restart during a download is refused; a failed download inspects, removes and starts nothing; the web route holds no lock during the download and both during the restart; busy, draft, CSRF and sign-in guards; the three messages.
- [x] Implement the split, the hardened helper and the honest messages.
- [x] Real Docker: `test_self_update_against_the_real_core_project` in the `integration` job (arguments refused over the real socket; nothing newer → no helper; registry unreachable → plain message, nothing touched; newer images → helper exits 0 with the hardened settings, both core containers recreated, no app touched, Orama and the new Agent answer; the installer's own compose command then changes nothing).
- [x] Commit `fix: harden and unblock Orama self-update` (`dc1cea1`), CI green.

**Not covered by automation:** a real download of a newer *published* image (CI has no newer published image to download). It was done by hand against a local registry during the review (see the notes) and is step 15 of the owner checklist.

## Task 3: End-to-end proof on clean Ubuntu 24.04 and Debian 12 — IN PROGRESS (step 1 of 5 done)

Spec §10.1.3 and §10.3.1. **Step 1 is done; nothing of the proof itself is built.** Do the steps in order; each ends with a commit and a green CI run on `test`. Stop and write the report if a step's evidence contradicts this plan: change the plan with the reason, do not bend the test.

**What exists today, so nobody mistakes it for this task:**

| Already tested | Where | Why it is not this proof |
|---|---|---|
| Real `install.sh` twice, real apps wired, second run changes nothing | `integration` job, Ubuntu runner | Setup is written with `docker exec`, not by the wizard; nothing is requested through Orama's pages |
| Wizard and requests pages in a real browser | `browser` job | Everything behind the pages is a fake; nothing is installed |
| Episode and film go to Ready | `tests/integration/test_requests.py` | The test creates the video and the hardlink itself and rescans; no indexer result is grabbed, nothing is downloaded, no subtitle is placed, the request worker runs on the test host |
| "Debian 12" | `test_debian_12_installer_and_agent_self_update_contract` | Ubuntu runner with a Debian `os-release` file |

**Files:** see the file map.

**Interfaces (to create):**
- `tests/e2e/target.py`: `class Target` with `run(argv, *, input=None, timeout) -> CompletedProcess` (a command on the machine under test, as root), `put(local: Path, remote: str)`, `url(port: int) -> str` (how the test's browser reaches a port of that machine), `save_logs(dir: Path)`. Two implementations: `LocalTarget` (this machine, `sudo`) and `SshTarget(host="127.0.0.1", port=2222, key=<run-time key>)` (commands over SSH, ports through the VM's forwards).
- `tests/e2e/wizard.cjs <setup-url> <answers.json>`: drives the real first-run link in Chromium: create password → Media servers (Jellyfin, generated administrator name and password) → Folders → Downloads (torrents, no VPN) → Indexers (add the generic Torznab entry for `http://orama-e2e-indexer:8000`, generated key, "Save and test indexer" must pass) → Playback (Samsung S95F) → Subtitles (Greek, English) → Review → Install → waits until the live checklist ends → then, on Home, searches each title, opens it, requests the episode and the film, and polls each request page until it shows Ready or the time limit passes. Prints one JSON object with the request page addresses and the final status texts. Fails on any console error, failed request or page that asks for something the answers file does not have. Reuses the checks of `tests/web/browser_pass.cjs` (shared file, not a copy).
- `tests/e2e/fake_torznab.py` (stdlib only): `GET /api?t=caps`, `t=search|tvsearch|movie` (requires the generated key; returns the two releases with seeders ≥ 1 and the right categories), `GET /torrent/<n>.torrent` (single-file torrents built at start with a `url-list` web seed pointing back at itself), `GET /files/<name>` with `Range` support. Reads the clips from a read-only folder.
- `tests/e2e/make_clips.sh <folder>`: generates the two clips with the ffmpeg inside the pinned Jellyfin image: 1080p H.264, **E-AC-3 5.1 audio (Dolby Digital Plus)**, an embedded Greek text subtitle track, long enough that the apps do not treat them as samples (Plan 4 needed 120 s for an episode; the film's length is whatever the first red run shows Radarr requires).
- `tests/e2e/vm/boot.sh`: verifies the image against `tests/e2e/debian-12.image`, makes a 24 GB copy-on-write disk on top of it, builds a cloud-init seed (a user with the run's public key and password-less `sudo`, nothing else), starts `qemu-system-x86_64 -enable-kvm -cpu host` with user-mode networking and the two forwards, waits for SSH. Fails if `/dev/kvm` is not usable: no slow software emulation fallback.

**The exact assertions** (`tests/e2e/test_e2e.py`, one ordered module, same for both targets):

1. *Install.* `install.sh` from the checkout, run as the installer says (`sudo`, `ORAMA_BUILD_LOCAL=1`), exits 0 and prints the "Orama is ready. Open … /setup?code=…" line. On Debian: `docker` did not exist before and exists after, installed from Docker's Debian repository (the apt source file names `download.docker.com/linux/debian` and `bookworm`). `orama-server` and `orama-server-agent` are running; the Agent has no network; `/healthz` answers.
2. *Wizard.* `wizard.cjs` finishes; `setup.json` exists and says Jellyfin, torrents, no VPN, the S95F device, Greek then English; no `setup.draft.json` is left; the checklist had no line starting with `✗`.
3. *Download really happened.* For each title: qBittorrent lists the torrent as complete with the fake's info-hash, and the fake's log shows the file bytes were fetched by qBittorrent (not copied by the test).
4. *Hardlink.* The imported file under the media folder and the downloaded file under the downloads folder have the **same inode** and a link count of at least 2 (`stat -c '%i %h'` on the machine under test), for the episode and for the film.
5. *Visible in Jellyfin.* Jellyfin's library lists the episode and the film by their catalogue ids after the wiring's own notification (no manual library scan by the test).
6. *Subtitle placed.* A Greek `.srt` file exists next to each imported video, written by Bazarr (owner and time later than the import), and it is not empty.
7. *Ready.* Each request page (read by the browser, not from the database) shows Ready, a playback note that says direct play on the S95F, and no "still looking" for Greek.
8. *Second install changes nothing.* Run the installer again with the same settings: exit 0; every container id is the same as before; `compose.yaml`, `core.compose.yaml` and `setup.json` have the same bytes; the request pages still show Ready; no new download appears in qBittorrent.
9. *Nothing leaks.* No stored secret, generated key or password appears in any command output, container log or uploaded artifact (the same value scan as `test_no_secret_in_logs_or_outputs`).

**Steps:**

- [x] **Step 1 — What the hosted runners really provide (probe, no product code).** Add a temporary job that prints, for `ubuntu-24.04`: CPU count, memory, free disk on `/` and `/mnt`, whether `/dev/kvm` exists and who may open it, the Docker and compose versions, and whether `qemu-system-x86`, `qemu-utils` and `cloud-image-utils` install from the runner's package archive. Record the numbers in the Plan 5 notes. If KVM is not usable on the standard runner, stop and ask the owner (the options are a larger runner or a self-hosted one; both cost money or upkeep). Remove the probe job in the same step. Commit `ci: record what the hosted runners provide for the VM proof`.
  *Result (measured twice on 2026-11-12, full table in the Plan 5 notes):* **KVM is usable on the standard runner**, so the owner does not have to be asked. 4 CPUs and 16 GB (the repository was public when measured; the smaller private-repository machine was not measured); **86 GB** free on `/`, not the 14 GB this plan expected, and `/mnt` is an empty folder, not a second disk; `/dev/kvm` is there but closed to the runner user until the udev rule is applied, after which a guest really ran under KVM; Docker 28.0.4 and Compose v2.38.2 are preinstalled; the three QEMU packages are not preinstalled and install from the runner's archive in under 15 seconds. The probe ran only on the throw-away branch `e2e-dev` and is removed. The same commit makes `ci.yml` ignore pushes to that branch (with a test), which steps 3 and 4 use.
- [ ] **Step 2 — Fake indexer and clips against the pinned apps (contract probe).** Write `tests/test_e2e_fixtures.py` first (fails: the fake does not exist): caps and search XML are well-formed and need the key; the `.torrent` info-hash matches its file; `Range` requests return the right bytes; a wrong key is refused. Then, inside the existing `integration` job where the real apps already run: Prowlarr accepts the fake as a generic Torznab indexer; Sonarr and Radarr see its releases and do not reject them (sample, size, quality, language); qBittorrent downloads one clip from the web seed; Bazarr's embedded-subtitle provider writes the Greek `.srt`. Record what was observed in the notes; switch to the tracker-plus-seeder fallback only on evidence. Commit `test: fake Torznab indexer and generated clips the real apps accept`.
- [ ] **Step 3 — The harness on Ubuntu.** Write `tests/e2e/test_e2e.py` with all nine assertion groups and run it red (no wizard driver yet). Write `target.py` (`LocalTarget`) and `wizard.cjs`. Add the job `e2e-ubuntu` (`needs: [unit, images]`, `timeout-minutes: 60`, its own Playwright install as in `browser`, logs uploaded on failure with the setup code and every generated value scrubbed). Iterate until green on `test` three runs in a row. Any product bug found gets its own commit and unit test. Commit `test: end-to-end install, wizard and requests on Ubuntu 24.04`.
- [ ] **Step 4 — The Debian 12 machine.** Choose one dated build of `debian-12-generic-amd64` from `cloud.debian.org/images/cloud/bookworm/`, write its URL and the SHA-512 from that folder's `SHA512SUMS` into `tests/e2e/debian-12.image`; unit test that the file is exactly two lines, an `https://cloud.debian.org/` URL with a dated folder (never `latest`) and 128 hex characters. Write `vm/boot.sh` and `SshTarget`. The checkout reaches the VM as a tar stream over SSH; the installer runs there exactly as on Ubuntu; the browser on the runner opens `http://localhost:8765/…` (the printed link with only the host replaced; `localhost` is on Orama's own allow-list, so no product change is needed). Add the job `e2e-debian` (`needs: [unit, images]`, `timeout-minutes: 90`, the KVM udev rule from step 1, image download cached by its checksum). Green three runs in a row. Commit `test: end-to-end proof on a Debian 12 virtual machine`.
- [ ] **Step 5 — Gate and report.** Add `e2e-ubuntu` and `e2e-debian` to `publish.needs` (test in `tests/test_ci_workflow.py` first). Replace `test_debian_12_installer_and_agent_self_update_contract`'s Debian half with a pointer to the real job (keep its Agent probe). Fill in `docs/tasks/plan-5/T03.md` with the tested commit, the run links, the run times and everything not covered. Commit `ci: gate publish on the end-to-end proofs`.

**Known risks, written down before starting:** the app images are several GB and the VM has to pull them through user-mode networking (time, and Docker Hub's anonymous pull limit, which already causes occasional red runs in `integration`); the runner measured in step 1 has 16 GB, which leaves the VM 8 to 10 GB, but only while the repository is public: a private repository's smaller runner (7 GB by this plan's estimate, not measured) would leave the VM about 5 GB for six apps; catalogue lookups need the internet. None of these is a reason to skip an assertion; they are reasons a run may need a retry, and each retry is recorded.

## Task 4: Owner NAS checklist — OPEN (owner)

**Files:** [`docs/OWNER-CHECKLIST.md`](../../OWNER-CHECKLIST.md) (written 2026-11-11), `docs/tasks/plan-5/T04.md`.
**Interfaces:** none. The checklist is plain-language, numbered steps for the UGREEN DXP4800 Plus: what to do, what you should see, what to write down if it differs. Technical detail is only in its last section, "For the developer".
- [x] Write the checklist (spec §10.2), covering the carry-forward owner checks from the Plan 1–4 notes.
- [ ] Owner runs it on the NAS and fills in the "what happened" column.
- [ ] Every "differs" entry becomes a fix (with a test) or a decision the owner accepts, recorded in `docs/tasks/plan-5/T04.md`.
- [ ] Commit `docs: owner checklist results`.

## Task 5: Security scans, packaging and sign-off — OPEN

**Files:** `docs/tasks/plan-5/T05.md`, `docs/STATUS.md`, notes.
**Interfaces:** none. Order matters; each line is a gate.
- [x] Delete the Node demo server from `test` (`4b85e22`); a unit test keeps it out.
- [ ] **Owner's gate — Claude Security scans** (`/claude-security:claude-security` → Scan changes): Plan 2b (base `0997d22`), Plan 3 (base `17e6e07`), Plan 4 (base `206a601`; run it after the review fixes so it covers Plan 5's code too). These have been deferred twice and are **not** recorded as passed anywhere. Every finding is fixed or explicitly accepted by the owner.
- [ ] Whole-codebase review with the built-in `security-review` skill and `engineering:code-review` (spec §10.3.5). If a named skill is not available in the session, say so; do not skip silently.
- [x] **Owner decision — commit history.** **Done 2026-11-11 (owner decided, Claude did it):** the whole history of `main` and `test` was rewritten so every commit is authored and committed by `DarkAngelGR` with GitHub's no-reply address; the `Co-Authored-By` lines are kept; files, order, dates and messages are unchanged. Every commit id changed; ids in the docs were replaced with the new ones. A test (`tests/test_repo_hygiene.py`) fails on any other author. Old commits may stay reachable on GitHub by their old id until GitHub cleans up; only GitHub Support can purge them sooner.
- [ ] **Merge `test` into `main` only after the two review lines above pass** and CI on `test` is green on the final commit (fast-forward where possible). `publish` then moves the version tag. The repository and the packages are still private at this point, so nobody else can install it yet.
- [ ] Owner runs the NAS checklist (Task 4) on that published version. While the packages are private the NAS needs a one-time sign-in to the registry; the owner may instead choose to do the next line first.
- [ ] Make the two GHCR packages (`orama`, `orama-agent`) public, then run the literal one-line install once on a clean Debian 12 machine (the `e2e-debian` job started by hand on `main` with the checkout build switched off) to prove the real download.
- [ ] Task 3 is done and green. Release 1 is not announced, and the repository is not made public, before this and the lines above.
- [ ] Record the Release 2 carry-forward (the follow-up records for conversion and subtitle translation; the limits listed in the Plan 5 notes).
- [ ] Commit `docs: Release 1 sign-off`.

## Completion and carry-forward

Each task report says what was built, what was tested where (unit, real Docker in CI, by hand, owner only) and what was **not** covered. "Verified" is used only for something a test or a person actually did on the thing named. Carry-forward that is already known: the web request waits for the update download; a helper failure after start is visible only in `docker logs orama-updater`; the Ubuntu proof does not cover installing Docker; Plex is owner-only; the proof installs from the checkout until the images are public.

**Spec coverage:** §4.2 `self-update` → Task 2 (as two commands, see notes); §9 "Orama update" and offline pulls → Task 2; §9 publish safety → Task 1; §10.1.3 and §10.3.1 → Task 3 (**open**); §10.2 and §10.3.2–3 → Task 4 (**open**); §10.3.4 → existing hygiene test plus the demo-server removal in Task 5; §10.3.5 → Task 5 (**open**); §12 public repository and history → Task 5.
