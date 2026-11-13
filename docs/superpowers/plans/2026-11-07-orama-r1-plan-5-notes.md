# Orama Release 1 — Plan 5 notes (how it was actually built)

These notes override the plan text where they differ. Read them before touching self-update.

## Task 2: self-update, as built after the 2026-11-11 review (Claude)

Gemini's first version (`ea34774`, `da28540`) worked, but the review found three problems. All
three are fixed in `fix: harden and unblock Orama self-update`.

### What the user sees

Settings → **Update Orama** now does two things in a row:

1. **Download.** Orama asks the Agent to download the newest Orama and Agent images. Nothing
   that is running is touched. If the server is offline or the download fails, the page says
   "Orama could not download the update, so nothing was changed. Check the internet connection
   and try again." and every container keeps running as before.
2. **Restart.** Only if something newer was downloaded, Orama asks the Agent to restart Orama
   and the Agent on it. The page then says: "Update started. Orama restarts by itself if a newer
   version was downloaded — reload this page in a minute." If nothing newer was downloaded it
   says "Orama is already on the newest version. Nothing was changed." and nothing is restarted.

If an apply is running when the download ends, Orama does not restart; the page says so and asks
to press the button again later. The old message "Orama is up to date." (shown after any update)
is gone.

### Decision that differs from the spec text (§4.2): two Agent commands instead of one

The spec lists one command, `self-update`. It is now two argument-less commands:

| Command | What the Agent does | Agent locks | Orama locks |
|---|---|---|---|
| `self-update-pull` | `docker compose pull` for the core project, then compares images | update lock only | none |
| `self-update` | starts the helper that recreates the core project (never downloads) | main lock + update lock | apply gate + `apply.lock`, non-blocking |

- **Why:** the download can take many minutes. As one command it held the Agent's only lock (so
  `status`, `ensure-folders` and every apply waited) and Orama's `apply.lock` (so every Settings
  save and apply was refused) for the whole download. Only the restart needs to exclude applies,
  because it replaces the Orama process; the restart takes seconds.
- **What it costs if wrong:** a caller that only sends `self-update` restarts on whatever is
  already downloaded and never fetches anything new. Orama always sends both.
- Both commands refuse any argument. Nothing about an update (image, tag, flag, path) can be
  chosen by Orama; the image names come only from the installer's root-owned
  `<config_root>/core.compose.yaml`, and each must look like an image name before it is used.
- The update lock is separate from the Agent's main lock. A second download, or a restart during
  a download, is refused at once with a plain "already downloading" message.
- "Is there something newer?" is answered by comparing, for both core containers, the image the
  container was created from (`docker container inspect`) with what its image name points at now
  (`docker image inspect`). When in doubt the answer is "yes", which costs one `up` that changes
  nothing.

### The helper container (`orama-updater`)

The Agent cannot recreate itself: compose would stop the Agent in the middle of its own command.
So the restart step starts one short-lived container from the Agent image that runs
`docker compose ... up -d --pull never --remove-orphans --wait --wait-timeout 300` on the core
project. Its argv is fixed (`src/orama_agent/runner.py`, tested item by item). It holds the
Docker socket, so it gets nothing else:

| Option | Why |
|---|---|
| `--network none` | The Docker daemon downloads images, never this container. It needs no network. |
| `--cap-drop ALL` | It only talks to the socket and reads two files. |
| `--security-opt no-new-privileges` | Nothing in it may gain privileges. |
| `--read-only` + `--tmpfs /tmp` (16 MB, noexec) | Nothing is written except the docker CLI's scratch files (`HOME` and `DOCKER_CONFIG` point at `/tmp`). |
| `--pids-limit 256`, `--memory 512m` | A runaway helper cannot starve the host. Compose needs far less. |
| config root mounted **read-only** | It reads `core.compose.yaml` and the empty env file; it never writes them. |
| `--group-add <group of the Docker socket>` | See below. |
| `--pull never` | The restart step never downloads; an incomplete download cannot be half-used. |
| no `--rm` | The exited helper is kept so `sudo docker logs orama-updater` says what the last update did. The next update removes it first. A stopped `orama-updater` in the container list is normal. |

**Does `--cap-drop ALL` break socket access?** No, on the hosts tested. Root without
capabilities loses the "ignore file permissions" power, so it reaches `/var/run/docker.sock` only
as the socket's owner or through its group. On Docker's standard install the socket is
`root:docker 0660`, so the owner bits already let the helper in; `--group-add` with the socket's
own numeric group (read by the Agent from its own mount of the socket, never from a request)
covers hosts where the socket is group-accessible but not root-owned. Both the compose file and
the env file are root-owned, so reading them needs no capability either. Not covered: rootless
Docker or user-namespace remapping, which Orama does not support anywhere.

### What is proved, and where

- **Unit** (`tests/agent/test_server.py`, `tests/web/test_settings.py`): exact argv of both
  steps; every argument and near-miss command name refused; links and damaged or strange
  `core.compose.yaml` refused before Docker is called; a download does not wait for the main
  lock and does not block `status`/`ensure-folders`/`data-space`; a failed download inspects,
  removes and starts nothing; the web route holds no lock during the download and both locks
  during the restart; busy/draft/CSRF/sign-in guards; the honest messages.
- **Real Docker, in CI** (`tests/integration/test_install_and_apply.py::
  test_self_update_against_the_real_core_project`): on the installed core project, through the
  real Agent socket: arguments refused; nothing newer → no helper, no change; registry
  unreachable → plain message, no change; newer images present → the helper exits 0, its real
  `HostConfig` is the hardened one above, both core containers are recreated on the new images,
  no app container changes, `/healthz` answers, the new Agent answers, and a second update finds
  nothing to do.
- **Real registry, by hand on the review machine (not in CI):** with a local registry, a newer
  version pushed under the same tag was downloaded by `self-update-pull` (`newer: true`,
  containers untouched), `self-update` recreated both containers through the hardened helper,
  and with the registry stopped the download failed with the plain message in under a second.
  CI cannot repeat this without publishing images, so the owner checklist covers the real
  download from GHCR.

### Known limits (accepted, written down so nobody is surprised)

- The web request waits for the download (bounded at 25 minutes by the client; usually well
  under a minute). It holds no lock while it waits, so the rest of Orama keeps working.
- If the helper fails after it was started (for example the new Orama never becomes healthy),
  nobody is told in the browser: Orama may be the thing that is down. The checklist tells the
  owner to run `sudo docker logs orama-updater`.
- The helper uses the compose version inside the Agent image; the installer uses the host's.
  The integration test's last step runs the installer's own compose command after a helper-made
  restart and requires that it changes nothing (true in CI and on the review machine). If a
  host with a much older compose ever disagrees, the visible effect is one extra restart of
  Orama on the next installer run, not a failure. The owner checklist has a step that shows it.

## Task 3: end-to-end proof

### Step 1 — what the hosted runners really provide (Claude, measured 2026-11-12)

The plan guessed what a GitHub-hosted `ubuntu-24.04` machine offers. A temporary job measured
it twice (one run started by a push, one started by hand; the same numbers both times, about
20 seconds per run). The job is gone again and never existed on `test` or `main`: it ran only
on the throw-away branch `e2e-dev` (see "How the end-to-end jobs are tried" below).

**The answer that mattered: the standard runner can run a virtual machine.** No larger or
self-hosted runner is needed, so there is nothing to ask the owner and nothing extra to pay.

| What | The plan expected | Measured (runner image `ubuntu24`, version 20261101.360.1) |
|---|---|---|
| CPUs | 4 (2 while the repository is private) | **4** (AMD EPYC, CPU flag `svm`) |
| Memory | 16 GB (7 GB while private) | **15,983 MB**, about 15,100 MB of it free when a job starts; plus a 3 GB swap file |
| Free disk on `/` | about 14 GB | **86 GB** free of 145 GB |
| `/mnt` | a second disk with free space | an empty folder on the same disk; there is no second disk |
| `/dev/kvm` | there, but the runner user needs a udev rule | there; owner `root`, group `kvm`, mode `660`. The runner user is not in the group `kvm`, so opening it fails with "Permission denied". After the rule below: mode `666`, it opens and answers (KVM API version 12) |
| Virtual machines inside the runner | not stated | the module `kvm_amd` is loaded with `nested = 1` |
| A guest under KVM | not stated | the runner's own kernel, started as a guest by the runner user with `-enable-kvm -cpu host`, 4 CPUs, 2 GB and the two port forwards of *Fixed values*, printed "Hypervisor detected: KVM" and ended cleanly within 2 seconds |
| Docker | preinstalled | Docker 28.0.4 (client and server), Compose v2.38.2, buildx v0.38.1; storage `overlay2` under `/var/lib/docker` on `/`; cgroup v2; 6 images (2.5 GB) are already on the machine |
| QEMU packages | not preinstalled | not preinstalled. `qemu-system-x86`, `qemu-utils` and `cloud-image-utils` all install from the runner's own package archive: QEMU 8.2.2 and cloud-image-utils 0.33-1 (this brings `cloud-localds` and `genisoimage`). `apt-get update` took 5 s and the install 8–9 s; 19 MB downloaded, about 240 MB of disk |
| Node | not stated | v22 is preinstalled |
| Docker Hub | "anonymous pull limit" | the limit does apply to the runner: 100 pulls per hour for its address, 96 and 98 left when the two runs asked |

What this means for the next steps:

- **These are the numbers of a public repository.** GitHub's API reported this repository as
  `public` when the probe ran, which is why the machine has 4 CPUs and 16 GB. The smaller
  machine of a private repository (the plan says 2 CPUs and 7 GB) was **not** measured. Task 5
  of the plan still describes the repository as private; the owner should check which of the
  two is intended.
- **Size of the Debian machine.** With 16 GB there is room for a virtual machine with 4 CPUs
  and 8 to 10 GB of memory next to the browser. The 24 GB copy-on-write disk and the image
  cache fit on `/`; `/mnt` is of no use.
- **The udev rule** (GitHub's documented one, tried as written):
  `KERNEL=="kvm", GROUP="kvm", MODE="0666", OPTIONS+="static_node=kvm"` in
  `/etc/udev/rules.d/99-kvm4all.rules`, then `sudo udevadm control --reload-rules`,
  `sudo udevadm trigger --name-match=kvm` and `sudo udevadm settle`. It opens `/dev/kvm` to
  every user of a machine that is thrown away after the job and on which the runner user is
  already allowed to use `sudo` for everything, so it gives away nothing.
- **The packages:** `sudo apt-get install -y --no-install-recommends qemu-system-x86 qemu-utils
  cloud-image-utils` after `sudo apt-get update`. Without the recommended extras QEMU still has
  user-mode networking with port forwards and the `virtio-net-pci` device (the probe used them).
- **No software emulation by accident.** `-enable-kvm` has no fallback: QEMU refuses to start
  when KVM cannot be used. `vm/boot.sh` should still test `/dev/kvm` itself, to fail with a
  plain sentence instead of QEMU's.
- **Docker Hub.** Only `jellyfin/jellyfin`, `recyclarr/recyclarr` and the `python` base image
  of the two Orama images come from Docker Hub in the proof; the other apps come from GHCR. A
  run needs a handful of the 100 pulls per hour, but the address is shared with other users of
  hosted runners, so a "too many requests" failure stays possible and is a reason to re-run.
- **The numbers belong to one runner image version.** GitHub changes these machines without
  notice. The `e2e-debian` job should print CPUs, memory and free disk at its start, so a later
  change is visible in its log.

How the end-to-end jobs are tried (decided with this step, used by steps 3 and 4):

- New jobs are tried on a throw-away branch, **`e2e-dev`**, in their own workflow file
  `.github/workflows/e2e.yml` (started by a push to that branch, by hand, or called from
  `ci.yml`). `ci.yml` now ignores pushes to `e2e-dev` (a test in `tests/test_ci_workflow.py`
  holds it to exactly that one branch), so a red attempt there neither runs the whole of CI nor
  marks `test` as broken. Only green jobs are wired into `ci.yml` on `test`.
- Both ways of starting were tried with the probe: `git push -f origin HEAD:e2e-dev`, and
  "run workflow" through GitHub's API on the branch `e2e-dev` (this works although the file is
  not on `main`).
- Between steps the branch holds nothing of its own, and it should be deleted when it is not
  needed. The session that did step 1 was not allowed to delete a branch (the connection it
  works through refused the delete, HTTP 403), so `e2e-dev` still exists: it was set to the
  same commit as `test`, without the probe. The owner can delete it on GitHub's *Branches* page
  at any time; the next step simply overwrites it.

How to repeat the probe: a job on `ubuntu-24.04` that runs `nproc`, `free -m`, `df -h / /mnt`,
`ls -l /dev/kvm`, `id`, `docker version`, `docker compose version`, the install command and the
udev rule above, and then

```bash
sudo install -m 0644 "/boot/vmlinuz-$(uname -r)" "$RUNNER_TEMP/vmlinuz"
timeout 120 qemu-system-x86_64 -enable-kvm -cpu host -smp "$(nproc)" -m 2048 \
  -display none -serial "file:$RUNNER_TEMP/guest.log" -no-reboot \
  -netdev user,id=net0,hostfwd=tcp:127.0.0.1:2222-:22,hostfwd=tcp:127.0.0.1:8765-:8765 \
  -device virtio-net-pci,netdev=net0 \
  -kernel "$RUNNER_TEMP/vmlinuz" -append 'console=ttyS0 panic=-1'
grep 'Hypervisor detected: KVM' "$RUNNER_TEMP/guest.log"
```

(the guest has no disk: it prints its start-up lines, finds nothing to start from and resets,
which `-no-reboot` turns into a clean exit).

Not covered by the probe: a full Debian start with cloud-init and SSH, pulling the app images
through user-mode networking, and how long the six apps take inside the machine. Those are
step 4.

### Step 2 — Torznab and generated clips (ChatGPT/Codex)

The pinned Prowlarr accepted the keyed generic Torznab feed and synced it to Sonarr and Radarr.
Both releases were accepted with Orama's 1080p quality setting and the Samsung S95F device.
The default Mixed movie profile rejected the 1080p film as intended. Use 1080p in the wizard
harness for these fixtures. The probe applies the supported choice through Orama and restores
the original setup afterward.

The indexer reports the exact generated file sizes. The clips use H.264 filler to meet the
size limits based on catalogue runtime and run 610 seconds to clear the 600-second sample
cutoff in the pinned importers. qBittorrent downloaded the episode from a trackerless HTTP
web seed, and Sonarr imported it. Bazarr extracted the embedded Greek ASS text to an SRT
sidecar. Orama's existing ASS scan setting keeps the embedded track from counting as a
sidecar before extraction. No second seeder or tracker was needed.

CI passed at `8d46eff` ([run 43286020192](https://github.com/DarkAngelGR/orama-server/actions/runs/43286020192))
and `238c8aa` ([run 43287636683](https://github.com/DarkAngelGR/orama-server/actions/runs/43287636683)).
See the [task report](../../tasks/plan-5/T03.md) for the intermediate failures and test counts.
The probe covers the episode download and extraction. The movie download, same-inode
links, real wizard and full-machine proof remain for the next steps.

### Jellyfin library refresh (observation and open question)

Observed: in the Ubuntu end-to-end run (run 43330046261, Jellyfin as pinned there) the
imported episode and film show up in Jellyfin's item list without the test asking for a scan,
once Orama asks Jellyfin to refresh its libraries (`POST /Library/Refresh`) after an item
became Ready or Sonarr/Radarr reported a Download, Upgrade or Rename.

Not settled: whether Sonarr's and Radarr's own per-path Jellyfin notification alone would have
done it, and how much `EnableRealtimeMonitor` adds. No run has been made with the refresh
switched off, so nothing here says the notification is ignored; it is only known that the
refresh is enough. Real-time monitoring is also unreliable on NFS/SMB mounts, so the refresh
stays as the guaranteed path.

Design: a webhook never calls Jellyfin. It records "media changed" in the request store and
wakes the worker. The worker asks the client to refresh the media server after it has released
the request lease, at most once per 60 s (many events become one call), when an item became
Ready or the note is set. A failing or slow Jellyfin is logged as one line (no key) and never
stops a tick. Open question for a later run: switch the refresh off in the end-to-end test once
to see whether the notification alone is enough.
