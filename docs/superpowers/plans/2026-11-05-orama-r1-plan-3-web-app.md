# Orama Release 1 — Plan 3: Web app (sign-in, setup wizard, Settings, Apps) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The owner opens the address the installer prints, creates the Orama password, answers the setup wizard in a phone-friendly web page, watches a live install checklist, signs in to Plex from the page, and later changes anything in Settings and sees which apps run on the Apps page — with no command line after the installer.

**Architecture:** A Flask app served by waitress replaces the Plan 1 placeholder in `python -m orama`. Pages are server-rendered Jinja templates with vendored htmx for the live parts (no build step). Every save writes the existing desired-setup record (`DesiredSetup`) under a file lock, and every apply runs the existing `python -m orama.cli apply` as a child process whose checklist lines stream to the page. Security (Host allowlist, CSRF, sessions, argon2id, rate limits, headers) is one module the whole app passes through, and it trusts no network — not even `orama-net`.

**Tech Stack:** Python ≥3.12, Flask 3.1, waitress 3.0, argon2-cffi 25.1, Jinja2 (with Flask), htmx 2.0.11 (vendored file), pydantic 2, pytest + Flask test client; Playwright (pre-installed Chromium) for screenshots and design reviews only.

**Spec:** `docs/superpowers/specs/2026-11-03-orama-release-1-design.md` — §5.2 (wizard), §5.4 (changing things later), §8 (web UI, design skills), §8.1 (security), §9 (app down, logs), §10.1 (login/CSRF/Host tests), §11 (auth structure). Read first: `AGENTS.md`, `docs/STATUS.md`, `docs/superpowers/plans/2026-11-03-orama-r1-plan-1-notes.md` ("Must be handled in later plans → Plan 3" and "Plan 2a — done → Carry forward → Plan 3"), `docs/superpowers/plans/2026-11-04-orama-r1-plan-2b-notes.md`.

**Model policy (owner):** cheaper model (`sonnet`) by default; most capable (`opus`) for tasks marked **[opus]** (Agent, compose pins, secrets, security) and their reviews.

## Global Constraints

- Everything in the Plan 1, 2a and 2b Global Constraints still holds: only the Agent has the Docker socket; any new compose key or env var goes into both the builder and the Agent pins, with refusal tests; no secret in compose, logs, argv, URLs, exceptions, HTML or test output; check-then-act; Orama manages only what it created; plain-language messages; commits straight to `main`, no branches or PRs.
- Stack (spec §8): Python web app, server-rendered HTML templates, a little JavaScript (htmx) for live parts. **No front-end framework, no build step, no CDN** — htmx is a vendored, hash-pinned file served by Orama.
- Pages (spec §8): Setup wizard, Settings, Apps, plus sign-in. Home in this plan is a placeholder ("Requests arrive in the next update") with links; search/requests are Plan 4.
- Phone-friendly: every page works at 360 px wide with no sideways scrolling; touch targets ≥ 44 × 44 px.
- Design (spec §8): pages designed with the `frontend-design:frontend-design` skill (Task 6), then reviewed with `design:ux-copy`, `design:accessibility-review` (WCAG 2.1 AA) and `design:design-critique` (Task 12). Findings are fixed or written down as accepted with a reason.
- Security (spec §8.1), copied exactly: "Password hashed with a memory-hard algorithm (argon2id); login attempts rate-limited." "Session cookie: `HttpOnly`, `SameSite=Strict`, `Secure` when served over HTTPS." "CSRF token required on every state-changing request." "`Host` header must match an allowlist (the machine's LAN IPs, hostname, `localhost`, plus user-added names); anything else is rejected." "No CORS. API keys and passwords never sent to the browser."
- Auth must not trust `orama-net`: every app container can reach `http://orama-server:8765`, so no request is trusted for its source address.
- Users (spec §3, §11): one admin, one password; code separates "who is logged in" (`Principal`) from "what they can do" (`permissions`).
- Secrets typed into a form are never shown again: secret inputs render empty with "saved — leave empty to keep"; an empty secret input keeps the stored value.
- Wording: plain language for a non-technical owner; app names as the apps spell them (SABnzbd, qBittorrent, Prowlarr …); no field names, stack traces or HTTP codes on a page.

## Fixed values

| Item | Value |
|---|---|
| Listen | `0.0.0.0:8765` inside the container (unchanged); `GET /healthz` → `200 ok` |
| State folder | env `ORAMA_STATE_DIR` (new in the core compose, = `<config_root>/orama`); files below are in it |
| Files | `setup.json`, `setup.draft.json` (existing names), `auth.json` (password hash, 0600), `first-run.code` (0600), `hardware.json` (installer's latest detection, 0644), `last-apply.json` (last job), `secrets.bin` (existing) |
| Locks | `setup.lock` (setup and draft writes: web, apply, bootstrap), `secrets.lock` (secret store writes), `apply.lock` (one apply or app test at a time, across processes) — `fcntl.flock`, files 0600 |
| Password | 12–256 characters; argon2id with argon2-cffi `PasswordHasher()` defaults (time 3, memory 64 MiB, parallelism 4); rehash on login when `check_needs_rehash` |
| First-run code | `secrets.token_urlsafe(16)`, written by `bootstrap` only while `auth.json` does not exist; installer prints `http://<address>:<port>/setup?code=<code>`; deleted when the password is created |
| Rate limits | per client address: 5 failed logins (or first-run codes) in 15 min → that address waits 15 min; all addresses together: 30 failures in 15 min → every login waits until the window clears. Message: "Too many wrong tries. Wait 15 minutes, then try again." |
| Session | cookie `orama_session`, random `token_urlsafe(32)`, kept server-side in memory (a restart signs the owner out); idle timeout 7 days, absolute 30 days; `HttpOnly`, `SameSite=Strict`, `Path=/`, `Secure` only when the request scheme is `https`; new id at sign-in; removed at sign-out and on password change (all sessions) |
| CSRF | per-session `token_urlsafe(32)`; form field `csrf_token` or header `X-CSRF-Token` (htmx sends it via `hx-headers` on `<body>`); required on every POST/PUT/PATCH/DELETE including sign-in (pre-sign-in session) ; compared with `hmac.compare_digest`; plus: an `Origin` header, when present, must equal `<scheme>://<Host>` |
| Host allowlist | `localhost`, `127.0.0.1`, `::1`, `setup.lan_address`, `setup.host_name`, `<host_name>.local`, every `setup.extra_host_names`; compared lower-case, port and one trailing dot removed, IPv6 brackets removed; read from the setup on every request (no restart needed); missing/other Host → 400 page "Orama does not answer at this address. Open it with the address the installer showed, or add this name in Settings → Addresses." (no allowed names in the reply) |
| Headers (every response) | `Content-Security-Policy: default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; font-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `X-Frame-Options: DENY`, `Cross-Origin-Opener-Policy: same-origin`, HTML `Cache-Control: no-store`; never any `Access-Control-*` header; `OPTIONS` → 405 |
| htmx | `htmx.org@2.0.11` `dist/htmx.min.js` from the npm registry, saved as `src/orama/web/static/htmx-2.0.11.min.js`; its SHA-256 pinned in a test; `<meta name="htmx-config" content='{"includeIndicatorStyles":false,"allowEval":false,"allowScriptTags":false,"selfRequestsOnly":true}'>` |
| Apply job | child process `[sys.executable, "-m", "orama.cli", "apply", "--setup", <path>]`, stdout+stderr merged, read line by line; ≤ 500 lines kept, each ≤ 500 characters; page polls `GET /jobs/current` every 2 s while running |
| VPN providers (WireGuard, key-only or key + address; verify against Gluetun v3.41.3 in Task 2) | `protonvpn` (default; port forwarding supported), `nordvpn`, `mullvad` (needs address), `ivpn` (needs address), `surfshark` (needs address) |
| Gluetun control server | env `HTTP_CONTROL_SERVER_ADDRESS=127.0.0.1:8000` (reachable only inside Gluetun's own namespace, whatever its auth defaults are) |
| Prowlarr catalogue | `GET http://prowlarr:9696/api/v1/indexer/schema` (key `apikey:prowlarr`), cached 1 hour in memory; filtered to the chosen download methods (`protocol` `usenet`/`torrent`); secret fields = `type == "password"` or `privacy` in `("apiKey", "password")`; shown fields = not `advanced`, not `hidden` |
| Pending removals (store) | `remove:indexer:<slug>` = indexer name; `remove:usenet:<slug>` = server slug; test results `test-ok:indexer:<slug>` / `test-ok:usenet:<slug>` = `"1"` |

## Review Focus

1. **Host header shapes** — `Example.LAN:8765`, `example.lan.`, `[::1]:8765`, no Host at all, two Host headers → the first three are matched after normalising (when allowed); the last two are rejected. **[T5]**
2. **The NAS gets a new LAN address, or the owner uses a Tailscale name** → re-running the installer updates `lan_address` and the allowlist follows without restarting Orama; an unknown name gets the plain recovery page, never a crash or an open door. **[T4, T5]**
3. **Install clicked twice, or two tabs open** → only one apply runs; the second click shows the running checklist. **Orama restarts mid-apply** → the page says "The last apply was interrupted. Apply again." instead of spinning forever. **[T7]**
4. **Editing Settings with secret inputs left empty** → the stored WireGuard key, indexer API keys and Usenet passwords are kept, never cleared, never echoed into the HTML. **[T9, T10]**
5. **Back, refresh or a wrong answer in the wizard** → every finished step is kept in the draft; a wrong answer shows the plain reason next to the field and keeps the other answers (secret inputs excepted). **[T8]**

---

## File structure

```
src/orama/state.py                 new: file locks, setup load/save under lock, job lock (T1)
src/orama/secrets.py               modify: locked read-modify-write, delete() (T1)
src/orama/cli.py                   modify: apply takes apply.lock + setup lock (T1); bootstrap code/hostname/hardware (T4)
src/orama/catalog.py, compose.py   modify: VPN_PROVIDERS, control-server pin, WIREGUARD_ADDRESSES (T2)
src/orama/setup_model.py           modify: provider rules, wireguard_address, host_name, extra_host_names (T2, T4)
src/orama_agent/validate.py        modify: pins for T2 (T2)
src/orama/wiring/prowlarr.py, sabnzbd.py, plan.py   modify: removals, proxy-400 message, test helpers (T3)
install.sh, src/orama/compose.py   modify: --host-name, first-run URL, ORAMA_STATE_DIR (T4)
src/orama/web/__init__.py          new: create_app() (T5)
src/orama/web/security.py          new: Host allowlist, headers, CSRF (T5)
src/orama/web/auth.py              new: Principal, PasswordFile, Sessions, RateLimiter, sign-in routes (T5)
src/orama/__main__.py              modify: serve create_app() with waitress (T5)
src/orama/web/templates/, static/  new: base layout, design tokens, htmx (T6)
src/orama/web/jobs.py              new: JobRunner + /jobs routes (T7)
src/orama/web/forms.py             new: form ↔ DesiredSetup sections, shared by wizard and Settings (T8)
src/orama/web/wizard.py            new: wizard blueprint (T8, T9)
src/orama/web/catalogue.py         new: Prowlarr catalogue + field model (T9)
src/orama/web/plex.py              new: Plex sign-in routes (T9)
src/orama/web/settings.py          new: Settings blueprint (T10)
src/orama/web/apps.py              new: Apps page + Home placeholder (T11)
tests/web/                         new: one test file per module above
```

---

### Task 1 [opus]: Safe shared state — locks for the setup, the secret store and applies

**Files:** Create `src/orama/state.py`; modify `src/orama/secrets.py`, `src/orama/cli.py` (`apply`, `bootstrap`'s setup write); tests `tests/test_state.py`, `tests/test_secrets.py`, `tests/test_cli.py`.

**Interfaces — Produces:**
- `state.locked(path: Path, *, blocking: bool = True) -> ContextManager[None]` — `fcntl.flock` on a 0600 lock file opened with `O_NOFOLLOW|O_CLOEXEC`; non-blocking raises `state.Busy`.
- `state.Busy(Exception)`; `state.setup_lock(state_dir) / apply_lock(state_dir)` → the lock paths from Fixed values.
- `state.save_setup(path: Path, setup: DesiredSetup) -> None` — under `setup.lock`, then `DesiredSetup.save`.
- `SecretStore.set` and new `SecretStore.delete(name: str) -> None` reload and write under `<store>.lock` (= `secrets.lock` next to `secrets.bin`); `delete` of a missing name is a no-op.

- [ ] **Step 1: Failing tests**
  - `test_parallel_secret_writes_keep_every_value`: 2 processes (`multiprocessing`, spawn) each `set` 50 distinct names on one store → all 100 present.
  - `test_delete_removes_one_name_only` and `test_delete_missing_is_noop`.
  - `test_apply_refuses_while_another_apply_runs`: hold `apply_lock` non-blocking in the test, run `cli.main(["apply", ...])` → exit 1, stderr has "Orama is already applying the setup — wait for it to finish, then try again."
  - `test_apply_saves_setup_under_lock`: patch `state.save_setup` and assert apply calls it (not `setup.save` directly).
  - `test_lock_file_is_never_followed`: a symlink at `setup.lock` → `locked()` raises `OSError` (ELOOP) and the link target is untouched.
- [ ] **Step 2:** Run `uv run pytest tests/test_state.py tests/test_secrets.py tests/test_cli.py -q` → the new tests FAIL.
- [ ] **Step 3:** Implement. `apply` takes `apply_lock` non-blocking for its whole run and saves through `state.save_setup`. `bootstrap` writes its setup while holding `setup.lock` (opened via its existing no-follow dir fd; owned PUID:PGID like the setup file).
- [ ] **Step 4:** Fast checks: `uv run pytest -m "not integration" -q && uv run ruff check` → all pass.
- [ ] **Step 5:** Commit `feat: lock setup, secret-store and apply writes`.

### Task 2 [opus]: VPN provider list, port-forwarding rule, Gluetun control server, unwritable folders

**Files:** modify `src/orama/catalog.py`, `src/orama/compose.py`, `src/orama/setup_model.py`, `src/orama_agent/validate.py`, `src/orama_agent/server.py`, `src/orama/agent_client.py`, `src/orama/cli.py`; tests also `tests/agent/test_server.py`, `tests/test_cli.py`; tests `tests/test_setup_model.py`, `tests/test_compose.py`, `tests/agent/test_validate.py`.

**Interfaces — Produces:**
- `catalog.VpnProvider(name: str, label: str, port_forwarding: bool, needs_address: bool)` and `catalog.VPN_PROVIDERS: dict[str, VpnProvider]` with the five providers in Fixed values (labels "Proton VPN", "NordVPN", "Mullvad", "IVPN", "Surfshark").
- `VpnConfig.provider` must be a key of `VPN_PROVIDERS`; new `VpnConfig.wireguard_address: IPv4Interface | None = None` (text like `a.b.c.d/32`, prefix 8–32), required when `needs_address`, refused otherwise; `port_forwarding=True` refused unless the provider supports it ("Port forwarding works only with Proton VPN in this version.").
- Gluetun env from the builder: always `HTTP_CONTROL_SERVER_ADDRESS=127.0.0.1:8000`; `WIREGUARD_ADDRESSES=<address>` when set. Agent pins: the control-server value exact; `WIREGUARD_ADDRESSES` fullmatch `\d{1,3}(\.\d{1,3}){3}/\d{1,2}`; `VPN_SERVICE_PROVIDER` must be a key of the same `VPN_PROVIDERS` (replacing the generic `_WORD` check for it).
- Agent `ensure-folders` result becomes `{"created": [...], "not_writable": [...]}`: an existing folder whose owner/group/mode bits do not let PUID:PGID write is listed (checked with `os.stat` on a no-follow fd; nothing is changed). `AgentClient.ensure_folders` returns that dict; `apply` prints `⚠ The apps cannot write to <folder>. Give user <PUID> or group <PGID> write access to it, then apply again.` per folder (Plan 1 carry-forward).

- [ ] **Step 1: Verify Gluetun v3.41.3** (read its source at that tag: provider names, which accept WireGuard with only a private key or key + `WIREGUARD_ADDRESSES`, and the `HTTP_CONTROL_SERVER_ADDRESS` variable). Where the source differs from Fixed values, the source wins; write the difference into the task report.
- [ ] **Step 2: Failing tests** — `test_unknown_vpn_provider_refused`, `test_port_forwarding_refused_for_nordvpn`, `test_mullvad_needs_wireguard_address`, `test_address_refused_for_protonvpn`; builder: `test_gluetun_control_server_bound_to_localhost`, `test_wireguard_address_in_gluetun_env`; Agent: `test_agent_refuses_other_control_server_address`, `test_agent_refuses_unknown_provider`, `test_agent_refuses_bad_wireguard_address` (values with spaces, `$`, IPv6, two addresses), `test_agent_accepts_builder_output_for_each_provider`, `test_ensure_folders_reports_existing_unwritable_folder`, `test_apply_warns_about_unwritable_folder`. Build IP-shaped strings at runtime (repo hygiene scan).
- [ ] **Step 3:** Run them → FAIL. **Step 4:** Implement. **Step 5:** Fast checks pass. **Step 6:** Commit `feat: shared VPN provider list; Gluetun control server only on localhost; warn about unwritable folders`.

### Task 3: Wiring for the web app — removals, single-item tests, plain proxy message

**Files:** modify `src/orama/wiring/prowlarr.py`, `src/orama/wiring/sabnzbd.py`, `src/orama/wiring/plan.py`; tests `tests/wiring/test_prowlarr.py`, `tests/wiring/test_sabnzbd.py`.

**Interfaces:**
- Consumes: `SecretStore.delete` (T1); pending-removal and test-ok names (Fixed values).
- Produces:
  - Step `prowlarr:remove:<slug>` for each store name `remove:indexer:<slug>`: deletes the Prowlarr indexer with that exact name if it exists, then deletes the store entry and the indexer's sent marker. Step `sabnzbd:remove:<slug>` for `remove:usenet:<slug>`: `mode=del_config&section=servers&keyword=<slug>` (confirm the exact SABnzbd 5 call in the task), then deletes the entry and marker. Messages: "removed the <name> indexer" / "removed the Usenet server <slug>".
  - `test_indexer(setup, store, http, slug) -> str | None` in `prowlarr.py`: runs only `wait:prowlarr` and `prowlarr:indexer:<slug>`; returns `None` when it passed (and sets `test-ok:indexer:<slug>`), else the step's plain message (and deletes `test-ok:…`).
  - `test_usenet_server(setup, store, http, slug) -> str | None` in `sabnzbd.py`: same pattern with `wait:sabnzbd`, `sabnzbd:server:<slug>`, `sabnzbd:test:<slug>`.
  - `prowlarr:proxy` failing with HTTP 400 → "The VPN is still connecting, so Prowlarr could not test it — run apply again in a minute."
  - Every "save it with set-secret …" message now says "enter it in Settings" instead.

- [ ] **Step 1: Failing tests** with the existing fake-HTTP helpers: `test_removed_indexer_deleted_by_exact_name_only` (another indexer with a similar name stays), `test_removal_entry_cleared_after_delete`, `test_removal_of_missing_indexer_is_quiet`, `test_usenet_server_removed`, `test_test_indexer_reports_plain_failure`, `test_test_indexer_sets_and_clears_test_ok`, `test_proxy_400_says_vpn_still_connecting`, `test_messages_point_to_settings_not_set_secret`.
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement. **Step 4:** Fast checks pass. **Step 5:** Commit `feat: remove Orama's indexers and Usenet servers; test one at a time`.

### Task 4 [opus]: Installer and bootstrap for the web app

**Files:** modify `install.sh`, `src/orama/cli.py` (`bootstrap`), `src/orama/compose.py` (`build_core_compose`), `src/orama/setup_model.py`; tests `tests/test_install_sh.py`, `tests/test_cli.py`, `tests/test_compose.py`, `tests/test_setup_model.py`.

**Interfaces — Produces:**
- `DesiredSetup.host_name: str | None = None` (hostname pattern `_HOSTNAME`, ≤ 63 per label) and `DesiredSetup.extra_host_names: tuple[str, ...] = ()` (≤ 10, same pattern, unique, lower-case).
- `bootstrap --host-name NAME|none` (installer passes `hostname -s`, or `none` when it does not fit the pattern); stored like `lan_address` (updated on every run, `none` changes nothing).
- `bootstrap` writes `hardware.json` = `{"render_gid": <int|null>, "nvidia": <bool>}` on every run (no-follow, PUID-owned, 0644) and, while `auth.json` does not exist, a fresh `first-run.code` (0600, PUID-owned, replacing any older code). Its stdout becomes two lines: the port, then the code or `-`.
- Core compose: the `orama` service gets `environment: {ORAMA_STATE_DIR: <state dir>}`.
- `install.sh` prints `✓ Orama is ready. Open http://<address>:<port>/setup?code=<code> in your browser.` when there is a code, else the existing line. The code is never written to a log file.

- [ ] **Step 1: Failing tests** — `test_bootstrap_writes_first_run_code_until_password_exists`, `test_bootstrap_refuses_linked_code_file`, `test_bootstrap_records_host_name_and_hardware`, `test_bootstrap_host_name_none_keeps_previous`, `test_core_compose_passes_state_dir`, `test_installer_prints_setup_link_with_code` (dry-run harness), `test_installer_prints_plain_link_after_password`, `test_bad_host_name_refused`.
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement. **Step 4:** Fast checks + `uvx --from shellcheck-py shellcheck install.sh` pass. **Step 5:** Commit `feat: installer prints a one-time setup link; records host name and graphics`.

### Task 5 [opus]: Web foundation and security

**Files:** create `src/orama/web/__init__.py`, `security.py`, `auth.py`, minimal templates `base.html`, `signin.html`, `first_run.html`, `rejected.html`; modify `src/orama/__main__.py`, `pyproject.toml`/`uv.lock` (`uv add flask==3.1.3 waitress==3.0.2 argon2-cffi==25.1.0`); tests `tests/web/test_security.py`, `tests/web/test_auth.py`, `tests/web/conftest.py`.

**Interfaces — Produces:**
- `create_app(state_dir: Path, *, agent: AgentClient | None = None, http: Http | None = None, clock: Callable[[], float] = time.time) -> Flask` — `agent`/`http`/`clock` injectable for tests.
- `auth.Principal(name: str, permissions: frozenset[str])`; `ADMIN = Principal("admin", frozenset({"setup", "settings", "apps"}))`; decorator `requires(permission: str)` → 303 to `/signin` when signed out, 403 when the permission is missing.
- `auth.PasswordFile(path)` with `exists() -> bool`, `create(password) -> None` (refuses if it exists), `verify(password) -> bool`, `change(old, new) -> bool`.
- `auth.Sessions` (in memory) and `auth.RateLimiter(clock)` with the Fixed values.
- `security.csrf_token() -> str` for templates (`{{ csrf_token() }}`); `security.allowed_hosts(setup: DesiredSetup | None) -> frozenset[str]`.
- Routes: `GET/POST /setup/password` (first run: needs a valid `code` from the query/form; shows "Open the link the installer printed" without it), `GET/POST /signin`, `POST /signout`, `GET /healthz`. After the password exists, `/setup/password` redirects to `/signin`.
- `python -m orama` serves `create_app(Path(os.environ["ORAMA_STATE_DIR"]))` with waitress on `0.0.0.0:8765`, `threads=8`, `ident=None` (no `Server` header detail).

- [ ] **Step 1: Failing tests** (Flask test client, `setup.json` fixture with a runtime-built LAN address):
  - Host: `test_allowed_hosts_accepted` (each allowlist kind, with port, upper case, trailing dot, `[::1]:8765`), `test_unknown_host_rejected_without_listing_names`, `test_missing_host_rejected`, `test_two_host_headers_rejected`, `test_new_lan_address_allowed_without_restart`, `test_healthz_on_127_0_0_1`.
  - CSRF: `test_post_without_token_refused` for every POST route the app registers (iterate `app.url_map`, so later routes are covered automatically), `test_post_with_wrong_token_refused`, `test_htmx_header_token_accepted`, `test_cross_origin_post_refused`.
  - Headers: `test_security_headers_on_every_response` (HTML, static, 400, 404), `test_no_cors_headers_even_with_origin`, `test_options_not_allowed`.
  - Sessions: `test_cookie_flags_http` (HttpOnly, SameSite=Strict, no Secure), `test_cookie_secure_over_https`, `test_session_id_changes_at_sign_in`, `test_signout_ends_session`, `test_idle_and_absolute_timeouts` (fake clock), `test_password_change_ends_other_sessions`.
  - Password and first run: `test_first_run_needs_code`, `test_first_run_wrong_code_rate_limited`, `test_first_run_creates_argon2id_hash_and_deletes_code` (hash starts `$argon2id$`, file 0600), `test_short_password_refused`, `test_first_run_closed_once_password_exists`.
  - Rate limits: `test_five_failures_lock_one_address`, `test_other_address_still_allowed`, `test_global_limit_locks_everyone`, `test_lock_clears_after_15_minutes`.
  - Leaks: `test_no_secret_in_any_page` — fill the store with runtime-built values, request every GET route signed in, assert none of the values appear.
  - Auth model: `test_principal_without_permission_gets_403`.
- [ ] **Step 2:** Run `uv run pytest tests/web -q` → FAIL. **Step 3:** Implement. Client address = the socket peer (`REMOTE_ADDR`); never `X-Forwarded-For`. **Step 4:** Fast checks pass; build the image (`docker build -f docker/orama.Dockerfile .` if Docker is available, else rely on CI `images`) and `curl -H 'Host: 127.0.0.1' localhost:8765/healthz` → `ok`. **Step 5:** Commit `feat: web app with sign-in, CSRF, Host allowlist and security headers`.

### Task 6: Design system and base layout (frontend-design)

**Files:** `src/orama/web/templates/base.html`, `signin.html`, `first_run.html`, `rejected.html`, `_macros.html` (form field, secret field, checklist, notice, button); `src/orama/web/static/orama.css`, `static/htmx-2.0.11.min.js`, `static/orama.js` (only the CSRF header and small helpers; no inline scripts); tests `tests/web/test_static.py`.

**Interfaces — Produces:** Jinja macros used by every later page: `field(name, label, value, error, help, type)`, `secret_field(name, label, saved: bool, error, help)`, `choice(name, label, options, selected, error)`, `checklist(lines, running: bool)`, `notice(kind: "ok"|"wait"|"problem", text)`, `button(label, kind)`. Layout blocks: `title`, `nav`, `content`.

- [ ] **Step 1:** Invoke the `frontend-design:frontend-design` skill; choose one clear visual direction for a calm home-media control panel (type scale, colour tokens as CSS custom properties with a dark scheme via `prefers-color-scheme`, spacing, focus rings) and record the choice at the top of `orama.css` in a short comment. System font stack only (no external fonts).
- [ ] **Step 2: Failing tests** — `test_htmx_file_hash_pinned` (SHA-256 of the vendored file equals the value recorded from the npm tarball), `test_no_inline_script_or_style_in_templates` (regex over templates: no `<script>` without `src`, no `style=` attribute, no `on*=` handlers), `test_static_served_with_long_cache_and_nosniff`.
- [ ] **Step 3:** Run → FAIL. **Step 4:** Implement the layout and macros; restyle Task 5's pages with them. **Step 5:** Fast checks pass; Playwright screenshots of sign-in and first-run at 360 px and 1280 px into the scratchpad, looked at, no overflow. **Step 6:** Commit `feat: Orama look and shared page parts`.

### Task 7: Apply jobs with a live checklist

**Files:** create `src/orama/web/jobs.py`, templates `_job.html`; tests `tests/web/test_jobs.py`.

**Interfaces:**
- Consumes: `state.apply_lock` (T1), macros (T6).
- Produces: `JobRunner(state_dir: Path, argv_for: Callable[[Path], list[str]] = default)` with `start(setup_path: Path, after: Callable[[int], None] | None = None) -> Job` (returns the running job if one is running), `current() -> Job | None`; `Job(status: "running"|"done"|"failed"|"interrupted", lines: list[str], started: float, finished: float | None)`. Persisted to `last-apply.json` at start and end; a stored `running` found at startup becomes `interrupted`. Routes: `GET /jobs/current` (partial; htmx polls while running), `POST /jobs/apply` (re-applies `setup.json`; "Apply again").
- Status words on the page: running "Applying your setup…", done "All done", failed "Something needs attention — see the ✗ lines, fix them, then apply again.", interrupted "The last apply was interrupted. Apply again."

- [ ] **Step 1: Failing tests** with a fake `argv_for` running a tiny Python script that prints lines and exits 0/1: `test_job_streams_lines`, `test_second_start_returns_running_job`, `test_failed_exit_marks_failed`, `test_interrupted_after_restart`, `test_lines_capped`, `test_cli_apply_busy_shows_plain_message` (the child prints the T1 busy line → status failed with that line).
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement (one reader thread per job; no shell; child env = parent env). **Step 4:** Fast checks pass. **Step 5:** Commit `feat: run apply from the web page with a live checklist`.

### Task 8: Wizard — media servers, data folder, downloads and VPN, playback, subtitles

**Files:** create `src/orama/web/forms.py`, `src/orama/web/wizard.py`, templates `wizard/_steps.html`, `wizard/media.html`, `wizard/folders.html`, `wizard/downloads.html`, `wizard/playback.html`, `wizard/subtitles.html`; tests `tests/web/test_forms.py`, `tests/web/test_wizard.py`.

**Interfaces:**
- Consumes: `state.save_setup`, `SecretStore` (T1), `VPN_PROVIDERS` (T2), `JobRunner` (T7), `JELLYFIN_USER_VALUE`, `ENV_FILE_VALUE`, secret names in `orama.secrets`.
- Produces: in `forms.py`, one pair per section, used again by Settings (T10): `read_<section>(setup: DesiredSetup, store) -> dict` (values for the template; secrets only as `saved: bool`) and `apply_<section>(setup: DesiredSetup, form: Mapping[str, str], store) -> DesiredSetup` raising `forms.Invalid(errors: dict[str, str])` with plain messages per field. Sections: `media` (Plex/Jellyfin; Jellyfin admin name + password → `user:jellyfin`/`password:jellyfin`, password ≥ 12), `folders` (data folder; shows "Orama does not move files that are already there" when it changes), `downloads` (Usenet/torrents; VPN on/off, provider, WireGuard key → `vpn:wireguard_private_key`, address when needed, port forwarding only when the provider supports it, "Send Prowlarr through the VPN"), `playback` (device, quality), `subtitles` (ordered languages from a fixed list of ISO 639-1 codes with names, default `el`, `en`; optional OpenSubtitles username/password).
- Routes `GET/POST /setup/<step>` for `media`, `folders`, `downloads`, `playback`, `subtitles`, editing `setup.draft.json` (copied from `setup.json` on "Change setup"); the step list shows done/current/next. Saving `downloads` the first time starts a job applying the draft ("Starting your apps — this can take a few minutes the first time"). Spec order kept: password → media → folders → downloads → indexers (T9) → playback → subtitles → review (T9).
- Secrets are written to the store only after the whole section validates; an empty secret input keeps the stored value.

- [ ] **Step 1: Failing tests** — per section: good form → expected `DesiredSetup` change; each plain error (`test_vpn_key_with_space_refused`, `test_port_forwarding_hidden_for_nordvpn`, `test_vpn_needs_torrents`, `test_jellyfin_name_rules`, `test_data_folder_inside_orama_folder_refused`, `test_duplicate_subtitle_language_refused`); `test_empty_secret_keeps_stored_value`; `test_secret_never_rendered_back`; `test_wizard_keeps_answers_after_error`; `test_back_and_refresh_keep_draft`; `test_downloads_save_starts_one_job`; `test_wizard_needs_sign_in`.
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement. **Step 4:** Fast checks pass; screenshots at 360 px looked at. **Step 5:** Commit `feat: setup wizard steps for servers, folders, downloads, playback and subtitles`.

### Task 9 [opus]: Wizard — indexers and Usenet servers, review and install, Plex sign-in

**Files:** create `src/orama/web/catalogue.py`, `src/orama/web/plex.py`; modify `src/orama/web/wizard.py`, `forms.py`; templates `wizard/indexers.html`, `wizard/_indexer_form.html`, `wizard/review.html`, `wizard/finish.html`; tests `tests/web/test_catalogue.py`, `tests/web/test_wizard_indexers.py`, `tests/web/test_plex_web.py`.

**Interfaces:**
- Consumes: `test_indexer`, `test_usenet_server` (T3), `JobRunner` (T7), forms helpers (T8), `begin_sign_in`/`poll_sign_in` (`orama.wiring.plex`), `state.apply_lock` (tests hold it while running).
- Produces:
  - `catalogue.load(store, http, methods: frozenset[str]) -> list[IndexerDefinition]`; `IndexerDefinition(definition: str, name: str, protocol: str, privacy: str, fields: list[IndexerField])`; `IndexerField(name, label, kind: "text"|"secret"|"checkbox"|"select"|"number"|"info", help, options)`. Unknown kinds become `info` (shown, not sent). Waiting state while Prowlarr is not answering yet or the start job runs: "Prowlarr is still starting. This page updates by itself." (htmx poll 5 s).
  - `forms.add_indexer(setup, definition: IndexerDefinition, form, store) -> tuple[DesiredSetup, str]` (slug from the name: lower-case, non `[a-z0-9]` → `-`, trimmed, unique suffix `-2`, `-3`…; returns the slug), `forms.add_usenet_server(setup, form, store) -> tuple[DesiredSetup, str]`, `forms.remove_indexer(...)`, `forms.remove_usenet_server(...)` (drop from the setup, delete its secrets and `test-ok:…`, set the `remove:…` entry only when Orama's sent marker exists).
  - Routes: `GET /setup/indexers` (search box over the catalogue, ≤ 50 matches, then the chosen definition's fields), `POST /setup/indexers` (add + test; shows "✓ <name> works" or the plain failure, entry kept for fixing), `POST /setup/indexers/<slug>/test`, `POST /setup/indexers/<slug>/remove`, same three for `/setup/usenet`; `GET /setup/review` (every answer in plain words, secrets as "saved"), `POST /setup/install`; `GET /setup/finish`; `POST /plex/sign-in` (starts the PIN; shows "Open plex.tv/link and enter code ABCD" with a link that opens in a new tab), `GET /plex/sign-in/status` (polled every 2 s; on success starts an apply job automatically).
  - Install is refused with a plain message until at least one indexer has `test-ok`, and — when Usenet is chosen — at least one Usenet server has `test-ok`. Install copies the draft to `setup.json` under `setup.lock`, starts the apply job and shows its checklist; when it finishes, `finish` shows the Plex button (if Plex) and "Jellyfin is ready — sign in as <name>" (if Jellyfin).
- [ ] **Step 1: Failing tests** with a fake Prowlarr schema (three definitions, one usenet, one torrent, one with an `apiKey` field and an advanced field): `test_catalogue_filtered_by_download_method`, `test_secret_fields_detected`, `test_advanced_fields_hidden`, `test_waiting_while_prowlarr_starts`, `test_add_indexer_saves_secret_in_store_not_setup`, `test_slug_unique`, `test_failed_test_keeps_entry_and_shows_reason`, `test_install_refused_without_working_indexer`, `test_install_refused_without_working_usenet_server_when_usenet`, `test_remove_sets_pending_entry_only_for_orama_items`, `test_review_shows_no_secret`, `test_install_promotes_draft_and_starts_job`, `test_plex_sign_in_shows_code_never_token`, `test_plex_done_starts_apply`, `test_tests_wait_for_apply_lock` (lock held → "Orama is busy applying the setup. Try again in a moment.").
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement. **Step 4:** Fast checks pass; screenshots looked at. **Step 5:** Commit `feat: wizard indexers, Usenet servers, review, install and Plex sign-in`.

### Task 10: Settings and Change setup

**Files:** create `src/orama/web/settings.py`, templates `settings/index.html`, `settings/_section.html`; tests `tests/web/test_settings.py`.

**Interfaces:**
- Consumes: every `read_/apply_` pair and indexer/server helpers (T8, T9), `JobRunner` (T7), `auth.PasswordFile.change` (T5), `hardware.json` (T4).
- Produces: `GET /settings` (one page, a section per wizard area plus **Addresses** — extra host names, **Graphics** — "The installer found: …  [Use this]" shown only when `hardware.json` differs from the setup — and **Orama password**), `POST /settings/<section>` (validate → `state.save_setup` on `setup.json` → start an apply job → show its checklist in place), `POST /settings/change-setup` (copy `setup.json` to the draft, redirect to `/setup/media`).
- Turning an app off says before saving: "This stops <App>. Its settings folder is kept and no media is deleted." (spec §5.4).
- [ ] **Step 1: Failing tests** — `test_each_section_saves_and_starts_apply`, `test_invalid_section_shows_errors_and_saves_nothing`, `test_empty_secret_keeps_stored_value` (VPN key, indexer key, Usenet password), `test_extra_host_name_allows_that_host_next_request`, `test_graphics_refresh_only_when_different`, `test_password_change_needs_current_password`, `test_change_setup_opens_prefilled_wizard`, `test_turning_off_app_warns_and_keeps_folder`.
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement. **Step 4:** Fast checks pass. **Step 5:** Commit `feat: Settings page and Change setup`.

### Task 11: Apps page and Home placeholder

**Files:** create `src/orama/web/apps.py`, templates `apps.html`, `home.html`; tests `tests/web/test_apps.py`.

**Interfaces:**
- Consumes: `AgentClient.status() -> dict[str, str]`, `enabled_apps`, `setup.ports`, `display_name`.
- Produces: `GET /apps` — one row per enabled app with its state in words ("Running", "Starting", "Stopped", "Not found") and an "Open" link `http://<request host without port>:<app host port>/` (opens in a new tab, `rel="noopener noreferrer"`); qBittorrent's link uses its own port also when it runs behind the VPN; Gluetun and Recyclarr have no link. Agent unreachable → "Orama cannot reach its helper right now — the apps keep running. Try again in a minute." `GET /` — when no `setup.json`: redirect to the wizard; else Home placeholder with links to Apps and Settings.
- [ ] **Step 1: Failing tests** — `test_apps_lists_enabled_apps_with_state`, `test_links_use_request_host_and_app_port`, `test_no_link_for_gluetun_and_recyclarr`, `test_agent_down_message`, `test_home_redirects_to_wizard_before_setup`.
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement. **Step 4:** Fast checks pass. **Step 5:** Commit `feat: Apps page and Home`.

### Task 12: Design reviews and an end-to-end browser pass

**Files:** create `tests/web/test_browser.py` (marked `integration`; uses the pre-installed Playwright; starts `create_app` with fakes in a thread); modify templates/CSS per findings; create `docs/superpowers/plans/2026-11-05-orama-r1-plan-3-notes.md`.

- [ ] **Step 1: Browser test** `test_wizard_end_to_end_with_fakes`: first run with the code → password → every wizard step (fake Agent, fake Prowlarr/SABnzbd HTTP, fake apply child printing a checklist) → install → finish → Settings save → Apps page. Also `test_keyboard_only_wizard` (Tab/Enter only) and `test_no_horizontal_scroll_at_360px` on every page.
- [ ] **Step 2:** Run it → PASS (fix the app until it does). Take screenshots of every page at 360 px and 1280 px, light and dark, into the scratchpad.
- [ ] **Step 3:** Run `design:ux-copy` on every page's wording, `design:accessibility-review` (WCAG 2.1 AA: contrast, labels, error association with `aria-describedby`, focus order, live checklist as `aria-live="polite"`), and `design:design-critique` on the screenshots. Fix every finding, or record it in the notes as accepted with the reason.
- [ ] **Step 4:** Fast checks pass; `uv run pytest tests/web/test_browser.py -m integration` passes.
- [ ] **Step 5:** Write the notes: decisions taken, accepted design findings, carry-forward for Plans 4/5 (below). Commit `test: browser pass; design review fixes` and `docs: Plan 3 notes`.

## Carry forward (not in this plan)

- **Plan 4:** Home becomes search + requests; disk-space warning (§9); "Update Orama" button via the Agent's `self-update`; webhooks from Sonarr/Radarr to Orama need `orama-server` in the Host allowlist and their own shared-secret check (no session, no CSRF).
- **Plan 5:** real browser wizard on the VMs (§10.1.3) and on the NAS; the owner's checklist gains "open the installer's link from a phone".

## Done when

- Tasks 1–12 committed, reviewed clean, CI (`unit`, `images`, `integration`, `publish`) green at the last commit.
- Final whole-plan review (opus) and one fix round.
- The owner runs the Claude Security scan of the Plan 3 changes (`/claude-security:claude-security` → Scan changes) and every finding is fixed or accepted.
- `docs/STATUS.md` updated after every task.
