# Orama Release 1 — Plan 5 notes (how it was actually built)

These notes override the plan text where they differ. Read them before touching self-update.

## Task 2: self-update, as built after the 2026-11-11 review (Claude)

Gemini's first version (`399d56f`, `621c1a1`) worked, but the review found three problems. All
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
