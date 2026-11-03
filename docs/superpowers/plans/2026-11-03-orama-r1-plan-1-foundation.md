# Orama Release 1 — Plan 1: Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A one-line installer that starts Orama + Orama Agent, and an apply engine that turns a desired-setup file into the running, correctly-mounted app stack, safely and repeatably.

**Architecture:** A Python package `orama` (no Docker access) builds the apps' compose document from a validated `DesiredSetup` model and sends it over a Unix socket to `orama_agent`, which validates it against a fixed service catalogue and allowed folder roots before running `docker compose`. The installer is a bash script that does only host work (OS/UGOS detection, Docker install, port checks) and delegates all file generation to Python.

**Tech Stack:** Python ≥3.12, pydantic 2, PyYAML (safe_dump), cryptography (Fernet), pytest, ruff, shellcheck, Docker Compose v2, GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-11-03-orama-release-1-design.md`

## Release 1 roadmap (one plan per step; each later plan is written when the previous one is done, against the real code)

1. **Foundation (this plan):** installer, setup model, catalogue, compose builder, secrets, Agent, deploy via a CLI.
2. **Wiring:** spec §5.3 — Prowlarr/Sonarr/Radarr/SABnzbd/qBittorrent/Bazarr links, Gluetun port sync, Recyclarr + device profiles (§6.1–6.2), Plex PIN sign-in, Jellyfin first run.
3. **Web app:** auth and security (§8.1), wizard, Settings, Apps page; pages designed with `frontend-design`, reviewed with `design:ux-copy`, `design:accessibility-review`, `design:design-critique`.
4. **Requests:** search, request types, status stages, webhooks, post-download check, 24-hour rule, subtitle state (§6.3–6.5, §7), reboot/drift checks (§9).
5. **Proof:** end-to-end VM tests with fake indexer/seeder (§10.1.3), owner's manual DXP4800 Plus checklist (§10.2), security review with the owner's security skill + `security-review` + `engineering:code-review` (§10.3.5).

## Global Constraints

- Targets: Ubuntu 22.04/24.04, Debian 12, UGOS Pro; x86-64 only.
- On UGOS: never install Docker; use UGOS's Docker. Change nothing outside Orama's folder and the data folder.
- Orama container has **no** Docker socket. Only `orama-agent` mounts `/var/run/docker.sock`. The Agent has no network port.
- Compose/YAML is generated from data via `yaml.safe_dump`. No shell heredocs or string templates for YAML.
- Images: LinuxServer.io for sonarr, radarr, prowlarr, bazarr, sabnzbd, qbittorrent; `plexinc/pms-docker`; `jellyfin/jellyfin`; `qmcgaw/gluetun`; `recyclarr/recyclarr`. Every image pinned to an exact tag; `latest` is forbidden.
- Data layout under one root mounted as `/data` in every app: `media/movies`, `media/tv`, `downloads/usenet/{movies,tv}`, `downloads/torrents/{movies,tv}`.
- All apps run as one PUID/PGID, `UMASK=002`. Render group by **numeric** GID only.
- Every host port unique; UGOS reserved ports never used.
- qBittorrent uses `network_mode: service:gluetun` when VPN is on.
- Secrets never in logs, never in `compose.yaml`; stored encrypted; per-app env files mode `0600`.
- No secrets, IPs or personal paths in the repo (enforced by a test).
- Commits go straight to `main`; trailer `Co-Authored-By: Claude …`; no `Claude-Session:` line.

## Review Focus

1. Data folder path containing spaces, quotes, `#`, `:` or non-ASCII (e.g. `/volume1/Ταινίες & TV`) → compose still valid and mounts exactly that path.
2. A default port already taken on the host (e.g. something else on 8080) → that app gets the next free port, recorded, others unchanged; re-run keeps the same assignments.
3. Apply interrupted (Agent restarted mid-run) then re-run → converges, no duplicate containers, second run reports no changes.
4. Machine with no `/dev/dri` (VM) or no NVIDIA → compose has no device entries, no crash.
5. Hostile or sloppy paths from Orama: relative paths, `..`, data root inside config root, symlink escaping the root → Agent refuses with a clear error.

Each is pinned by a test in the owning task (marked **[RF n]**).

---

## File structure

```
pyproject.toml                     package + tool config
src/orama/__init__.py
src/orama/setup_model.py           DesiredSetup + sub-models (validated)
src/orama/platform.py              OS/UGOS detection, render GID, NVIDIA detection
src/orama/catalog.py               ServiceSpec per app, pinned images, default ports, reserved ports
src/orama/ports.py                 assign_ports
src/orama/layout.py                data/config folder lists
src/orama/compose.py               build_apps_compose, build_core_compose, dump_yaml
src/orama/secrets.py               SecretStore, generate_api_key, RedactingFilter
src/orama/agent_client.py          AgentClient (Unix socket)
src/orama/cli.py                   `python -m orama.cli bootstrap|apply`
src/orama_agent/__init__.py
src/orama_agent/validate.py        validate_compose, validate_env_files, safe_path
src/orama_agent/runner.py          ComposeRunner (fixed argv), FakeRunner for tests
src/orama_agent/server.py          socket server + command dispatch
docker/orama.Dockerfile
docker/agent.Dockerfile
install.sh
tests/...                          mirrors src/
tests/test_repo_hygiene.py
.github/workflows/ci.yml
```

---

### Task 1: Project skeleton, CI and repo-hygiene scan

**Files:**
- Create: `pyproject.toml`, `src/orama/__init__.py`, `src/orama_agent/__init__.py`, `.gitignore`, `.github/workflows/ci.yml`, `tests/test_repo_hygiene.py`

**Interfaces:**
- Produces: `uv run pytest` and `uv run ruff check` as the project's fast checks; CI job `unit` running both plus `shellcheck install.sh` (once it exists).

- [ ] **Step 1: Write the failing hygiene test** `tests/test_repo_hygiene.py`

```python
FORBIDDEN = [
    r"\b(?:10|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\b",  # private IPv4
    r"/home/[a-z]", r"/Users/[A-Za-z]",                                  # personal paths
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    r"\b[A-Za-z0-9+/]{43}=",                                            # WireGuard key shape
    r"@gmail\.com",
]
def test_no_secrets_ips_or_personal_paths():
    hits = scan_tracked_files(FORBIDDEN, exclude={"tests/test_repo_hygiene.py"})
    assert hits == []
```
`scan_tracked_files` lists files with `git ls-files`, skips binaries, returns `[(path, line_no, pattern)]`. Test fixtures that need fake keys must build them at runtime, not store them.

- [ ] **Step 2: Run** `uv run pytest tests/test_repo_hygiene.py -v` → FAIL (no package / helper).
- [ ] **Step 3: Create `pyproject.toml`** (name `orama`, `requires-python = ">=3.12"`, deps `pydantic>=2.8`, `pyyaml>=6`, `cryptography>=43`; dev group `pytest`, `ruff`; packages `src/orama`, `src/orama_agent`), the helper, `.gitignore` (`.venv/`, `__pycache__/`, `.pytest_cache/`).
- [ ] **Step 4: Run** `uv run pytest -v && uv run ruff check` → PASS.
- [ ] **Step 5: Add `.github/workflows/ci.yml`** job `unit` on `ubuntu-24.04`: checkout, `astral-sh/setup-uv`, `uv run ruff check`, `uv run pytest -m "not integration"`, `shellcheck install.sh` guarded by `if [ -f install.sh ]`.
- [ ] **Step 6: Commit** `chore: project skeleton, CI, repo hygiene scan`.

---

### Task 2: Desired-setup model and platform detection

**Files:**
- Create: `src/orama/setup_model.py`, `src/orama/platform.py`, `tests/test_setup_model.py`, `tests/test_platform.py`, `tests/fixtures/os-release/{ubuntu-24.04,debian-12,ugos-pro,fedora-40}`

**Interfaces:**
- Produces:
  - `class VpnConfig(BaseModel)`: `provider: str = "protonvpn"`, `port_forwarding: bool = True`, `route_prowlarr: bool = False`. (Key itself lives in `SecretStore`, never in the model.)
  - `class DesiredSetup(BaseModel)`: `media_servers: frozenset[Literal["plex","jellyfin"]]` (min 1), `download_methods: frozenset[Literal["usenet","torrent"]]` (min 1), `vpn: VpnConfig | None`, `data_root: PurePosixPath`, `config_root: PurePosixPath`, `puid: int`, `pgid: int`, `render_gid: int | None`, `nvidia: bool = False`, `timezone: str = "Etc/UTC"`, `ports: dict[str, int] = {}` (assigned ports, filled by Task 3), `removed_apps: frozenset[str] = frozenset()`.
  - `DesiredSetup.load(path) -> DesiredSetup`, `.save(path)` (JSON, atomic write via temp + `os.replace`).
  - `def enabled_apps(setup) -> list[str]` — ordered list of catalogue service names that should run.
  - `class Platform(StrEnum)`: `UBUNTU`, `DEBIAN`, `UGOS`, `UNSUPPORTED`.
  - `def detect_platform(os_release: str) -> Platform`
  - `def default_roots(p: Platform) -> tuple[PurePosixPath, PurePosixPath]` — `(data, config)`: UGOS `(/volume1/data, /volume1/docker/orama)`, else `(/data, /opt/orama)`.
  - `def render_gid(dev: Path = Path("/dev/dri/renderD128")) -> int | None`, `def has_nvidia(which=shutil.which) -> bool`.

- [ ] **Step 1: Write failing tests**

```python
def test_vpn_requires_torrent():
    with pytest.raises(ValidationError):
        make_setup(download_methods={"usenet"}, vpn=VpnConfig())

@pytest.mark.parametrize("bad", ["relative/path", "/data/../etc", "/"])
def test_roots_must_be_absolute_normalised_and_not_root(bad):   # [RF 5]
    with pytest.raises(ValidationError):
        make_setup(data_root=bad)

def test_roots_must_not_nest():                                  # [RF 5]
    with pytest.raises(ValidationError):
        make_setup(data_root="/volume1/docker/orama/data", config_root="/volume1/docker/orama")

def test_enabled_apps_usenet_plex():
    assert enabled_apps(make_setup(media_servers={"plex"}, download_methods={"usenet"})) == \
        ["plex", "sonarr", "radarr", "prowlarr", "bazarr", "sabnzbd"]

def test_enabled_apps_both_with_vpn():
    s = make_setup(media_servers={"plex", "jellyfin"}, download_methods={"usenet", "torrent"}, vpn=VpnConfig())
    assert enabled_apps(s) == ["plex", "jellyfin", "sonarr", "radarr", "prowlarr", "bazarr",
                               "sabnzbd", "gluetun", "qbittorrent"]

def test_removed_apps_are_not_enabled():
    assert "bazarr" not in enabled_apps(make_setup(removed_apps={"bazarr"}))

def test_save_load_roundtrip(tmp_path): ...

@pytest.mark.parametrize("fixture,expected", [("ubuntu-24.04", Platform.UBUNTU), ("debian-12", Platform.DEBIAN),
                                              ("ugos-pro", Platform.UGOS), ("fedora-40", Platform.UNSUPPORTED)])
def test_detect_platform(fixture, expected): ...

def test_render_gid_missing_device_is_none(tmp_path):            # [RF 4]
    assert render_gid(tmp_path / "renderD128") is None
```
`make_setup(**overrides)` is a helper in `tests/conftest.py` with defaults: plex, usenet, no VPN, `/srv/data`, `/srv/orama`, puid/pgid 1000, render_gid None.

UGOS fixture: an `/etc/os-release` with `ID=debian` plus UGOS markers. Detection rule: UGOS if os-release contains `UGOS` (case-insensitive) in `NAME`, `PRETTY_NAME` or `ID_LIKE`/`VARIANT`. **Before trusting this, confirm the real file on the owner's NAS** (Plan 5 checklist item); until then the fixture is labelled `# assumed — verify on device`.

- [ ] **Step 2: Run** `uv run pytest tests/test_setup_model.py tests/test_platform.py -v` → FAIL.
- [ ] **Step 3: Implement** models with pydantic validators (`model_validator(mode="after")` for VPN/torrent and nesting; root validator rejects non-absolute, `..` parts, `/`). `enabled_apps` order: media servers (plex, jellyfin), sonarr, radarr, prowlarr, bazarr, then sabnzbd, then gluetun (only if torrent+vpn), qbittorrent; minus `removed_apps`.
- [ ] **Step 4: Run** tests → PASS.
- [ ] **Step 5: Commit** `feat: desired-setup model and platform detection`.

---

### Task 3: Service catalogue and port assignment

**Files:**
- Create: `src/orama/catalog.py`, `src/orama/ports.py`, `tests/test_catalog.py`, `tests/test_ports.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class ServiceSpec`: `name: str`, `image: str` (`repo:tag`), `container_port: int`, `default_host_port: int`, `config_mount: str` (path inside container, e.g. `/config`), `mounts_data: bool`, `env_style: Literal["linuxserver","plex","jellyfin","gluetun","none"]`, `secret_env: tuple[str, ...]` (env var names allowed in its env file), `cap_add: tuple[str, ...] = ()`, `devices: tuple[str, ...] = ()`.
  - `CATALOG: dict[str, ServiceSpec]` with entries: plex, jellyfin, sonarr, radarr, prowlarr, bazarr, sabnzbd, gluetun, qbittorrent, recyclarr; plus `CORE: dict[str, ServiceSpec]` for `orama`, `orama-agent`.
  - `UGOS_RESERVED_PORTS: frozenset[int]`
  - `def assign_ports(apps: list[str], previous: dict[str,int], taken: set[int]) -> dict[str,int]`

Default host ports (spec §4.5 — unique, no SABnzbd/qBittorrent clash):

| service | container | host default |
|---|---|---|
| orama | 8765 | 8765 |
| plex | 32400 | 32400 |
| jellyfin | 8096 | 8096 |
| sonarr | 8989 | 8989 |
| radarr | 7878 | 7878 |
| prowlarr | 9696 | 9696 |
| bazarr | 6767 | 6767 |
| sabnzbd | 8080 | 8080 |
| qbittorrent (published on gluetun if VPN) | 8090 (`WEBUI_PORT=8090`) | 8090 |

`secret_env`: sonarr `("SONARR__AUTH__APIKEY",)`, radarr `("RADARR__AUTH__APIKEY",)`, prowlarr `("PROWLARR__AUTH__APIKEY",)`, gluetun `("WIREGUARD_PRIVATE_KEY",)`, others `()`. gluetun: `cap_add=("NET_ADMIN",)`, `devices=("/dev/net/tun",)`.

Image tags: look up the current stable tag of each image at implementation time (`docker buildx imagetools inspect <repo>:<tag>` or the registry's tags page), use a specific version tag (LinuxServer `version-x.y.z` style or project release tag), and record the date checked in a comment at the top of `catalog.py`.

UGOS reserved ports: start with the UGOS web UI ports as documented by UGREEN at implementation time; mark `# verify on device` (Plan 5). The installer's live free-port check (Task 9) is the real safety net.

- [ ] **Step 1: Write failing tests**

```python
def test_no_latest_tags():
    for s in [*CATALOG.values(), *CORE.values()]:
        assert ":" in s.image and not s.image.endswith(":latest")

def test_default_ports_unique_and_not_reserved():
    ports = [s.default_host_port for s in [*CATALOG.values(), *CORE.values()] if s.default_host_port]
    assert len(ports) == len(set(ports))
    assert not set(ports) & UGOS_RESERVED_PORTS

def test_assign_ports_uses_defaults():
    assert assign_ports(["sonarr", "sabnzbd"], {}, set()) == {"sonarr": 8989, "sabnzbd": 8080}

def test_taken_port_moves_only_that_app():                       # [RF 2]
    got = assign_ports(["sonarr", "sabnzbd"], {}, taken={8080})
    assert got["sonarr"] == 8989 and got["sabnzbd"] not in {8080, 8989}

def test_previous_assignments_are_kept():                        # [RF 2]
    assert assign_ports(["sabnzbd"], {"sabnzbd": 8081}, taken={8081})["sabnzbd"] == 8081
    # 8081 is "taken" by our own running sabnzbd; keeping it is correct

def test_never_assigns_reserved_or_other_apps_defaults():
    got = assign_ports(["sabnzbd"], {}, taken={8080, 8081})
    assert got["sabnzbd"] not in UGOS_RESERVED_PORTS | {s.default_host_port for s in CATALOG.values()}
```

- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.** `assign_ports`: keep `previous[app]` if present; else default if not in `taken`, not reserved, not already assigned; else scan upward from default+1 skipping taken, reserved, all catalogue defaults and already-assigned ports; error if above 65535.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat: service catalogue with pinned images and port assignment`.

---

### Task 4: Folder layout and compose builder

**Files:**
- Create: `src/orama/layout.py`, `src/orama/compose.py`, `tests/test_layout.py`, `tests/test_compose.py`

**Interfaces:**
- Consumes: `DesiredSetup`, `enabled_apps` (Task 2); `CATALOG`, `CORE` (Task 3).
- Produces:
  - `def data_folders(data_root: PurePosixPath) -> list[PurePosixPath]` — exactly these 10, parents first: `media`, `media/movies`, `media/tv`, `downloads`, `downloads/usenet`, `downloads/usenet/movies`, `downloads/usenet/tv`, `downloads/torrents`, `downloads/torrents/movies`, `downloads/torrents/tv`.
  - `def config_folders(setup) -> list[PurePosixPath]` — `config_root/config/<app>` per enabled app + `config_root/orama`.
  - `def env_file_path(setup, app) -> PurePosixPath` — `config_root/config/<app>/orama.env`.
  - `def build_apps_compose(setup: DesiredSetup) -> dict` — project `orama-apps`.
  - `def build_core_compose(config_root, data_root, puid, pgid, docker_gid, orama_port) -> dict` — project `orama-core`.
  - `def dump_yaml(doc: dict) -> str` — `yaml.safe_dump(doc, sort_keys=False, allow_unicode=True)`.

Rules for `build_apps_compose` (each is a test):
- top-level `name: orama-apps`; one network `orama` (bridge).
- each service: `image` from catalogue, `container_name: orama-<app>`, `restart: unless-stopped`, volumes as **long-syntax dicts** `{"type": "bind", "source": str, "target": str}` (avoids `:` parsing issues), `env_file` only if `secret_env` non-empty.
- `mounts_data` apps get `data_root → /data`.
- `linuxserver` env: `PUID`, `PGID`, `UMASK=002`, `TZ`. plex: `PLEX_UID`, `PLEX_GID`, `TZ`. jellyfin: `user: "<puid>:<pgid>"`.
- plex and jellyfin: if `render_gid` set → `devices: [/dev/dri:/dev/dri]`, `group_add: [render_gid as str]`; if `nvidia` → `deploy.resources.reservations.devices` with `driver: nvidia, capabilities: [gpu]`.
- qbittorrent with VPN: `network_mode: service:gluetun`, no `ports`, no `networks`; gluetun publishes `8090` mapping and gets `depends_on`-free; qbittorrent `depends_on: {gluetun: {condition: service_healthy}}`. Gluetun env: `VPN_SERVICE_PROVIDER`, `VPN_TYPE=wireguard`, `VPN_PORT_FORWARDING=on` when `port_forwarding`.
- ports: `"<host>:<container>"` strings from `setup.ports`.
- No secret values anywhere in the doc.

`build_core_compose`: services `orama` (no docker socket, mounts `config_root` and a named volume `orama-run` at `/run/orama`, port `orama_port:8765`, `user: puid:pgid`) and `orama-agent` (mounts `/var/run/docker.sock`, `orama-run`, and `config_root` and `data_root` at **identical** paths, env `ORAMA_DATA_ROOT`, `ORAMA_CONFIG_ROOT`, `ORAMA_PUID`, `ORAMA_PGID`, `group_add: [docker_gid]`, no `ports`, `network_mode: none`).

- [ ] **Step 1: Write failing tests**

```python
def test_compose_roundtrips_through_yaml_with_awkward_path():    # [RF 1]
    s = make_setup(data_root="/volume1/Ταινίες & TV #1: \"x\"")
    doc = yaml.safe_load(dump_yaml(build_apps_compose(s)))
    vols = doc["services"]["sonarr"]["volumes"]
    assert {"type": "bind", "source": "/volume1/Ταινίες & TV #1: \"x\"", "target": "/data"} in vols

def test_every_data_app_sees_same_data_mount(): ...             # hardlinks
def test_qbittorrent_behind_gluetun_has_no_own_network_or_ports(): ...
def test_qbittorrent_without_vpn_has_its_own_port(): ...
def test_render_gid_is_numeric_string_in_group_add(): ...
def test_no_dri_device_when_render_gid_none():                   # [RF 4]
    doc = build_apps_compose(make_setup(render_gid=None))
    assert "devices" not in doc["services"]["plex"] and "group_add" not in doc["services"]["plex"]
def test_compose_contains_no_secret_env_values(): ...            # only env_file references
def test_only_orama_agent_mounts_docker_socket():
    core = build_core_compose(...)
    socket_users = [n for n, s in core["services"].items()
                    if any(v["source"] == "/var/run/docker.sock" for v in s.get("volumes", []))]
    assert socket_users == ["orama-agent"]
    assert "ports" not in core["services"]["orama-agent"]
def test_data_folders_exact_list(): ...
```

- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** per the rules above.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Validate generated YAML with real Compose:** `docker compose -f <(python -c 'dump sample') config -q` inside CI (`unit` job, Docker CLI is available on the runner). Expected: exit 0.
- [ ] **Step 6: Commit** `feat: folder layout and compose builder`.

---

### Task 5: Secret store, API-key generation, log redaction

**Files:**
- Create: `src/orama/secrets.py`, `tests/test_secrets.py`

**Interfaces:**
- Produces:
  - `class SecretStore`: `__init__(self, path: Path)` — key file `path.with_suffix(".key")` created mode `0600` on first use; `get(name) -> str | None`, `set(name, value)`, `names() -> list[str]`, `values() -> list[str]`. Stored as one Fernet-encrypted JSON blob, atomic write, mode `0600`.
  - `def generate_api_key() -> str` — 32 lowercase hex chars (`secrets.token_hex(16)`), the format *arr apps use.
  - `def ensure_app_keys(store, apps: list[str]) -> None` — creates `apikey:<app>` for sonarr/radarr/prowlarr if missing; never overwrites.
  - `def env_files_for(setup, store) -> dict[str, dict[str, str]]` — `{app: {ENV: value}}` using each app's `secret_env`; gluetun reads `vpn:wireguard_private_key`.
  - `class RedactingFilter(logging.Filter)`: `__init__(self, store)`; replaces every stored value and anything matching `(?i)(api[_-]?key|token|password|private[_-]?key)["'=:\s]+\S+` with `***`.
  - `def install_redaction(store) -> None` — attaches the filter to the root logger's handlers.

- [ ] **Step 1: Write failing tests**

```python
def test_store_file_is_encrypted_and_private(tmp_path):
    st = SecretStore(tmp_path / "secrets.bin"); st.set("apikey:sonarr", "abc123secret")
    raw = (tmp_path / "secrets.bin").read_bytes()
    assert b"abc123secret" not in raw
    assert stat.S_IMODE((tmp_path / "secrets.bin").stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "secrets.key").stat().st_mode) == 0o600

def test_ensure_app_keys_is_idempotent(tmp_path): ...
def test_generated_key_format(): assert re.fullmatch(r"[0-9a-f]{32}", generate_api_key())

def test_logs_never_contain_secrets(tmp_path, caplog):
    st = SecretStore(tmp_path / "s.bin"); st.set("apikey:radarr", "f00dfeed" * 4)
    install_redaction(st)
    logging.getLogger("x").warning("calling radarr with %s", "f00dfeed" * 4)
    logging.getLogger("x").warning("url=http://radarr:7878/api?apikey=zzz999")
    assert "f00dfeed" not in caplog.text and "zzz999" not in caplog.text
```
(`caplog` captures via a handler; attach the filter to it in the test via `install_redaction` covering all root handlers including caplog's.)

- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat: encrypted secret store, api keys, log redaction`.

---

### Task 6: Agent validation (the security boundary)

**Files:**
- Create: `src/orama_agent/validate.py`, `tests/agent/test_validate.py`

**Interfaces:**
- Consumes: `CATALOG` (Task 3) — the Agent imports the catalogue from the `orama` package baked into its own image; it never trusts a catalogue sent over the socket.
- Produces:
  - `class Refused(Exception)` with a plain-language message.
  - `@dataclass(frozen=True) class Roots`: `data: Path`, `config: Path` — read **only** from the Agent's own env (`ORAMA_DATA_ROOT`, `ORAMA_CONFIG_ROOT`) at start.
  - `def safe_path(p: str, roots: Roots) -> Path` — absolute, no `..`, `os.path.realpath` stays under one root.
  - `def validate_compose(doc: dict, roots: Roots) -> None`
  - `def validate_env_files(env_files: dict[str, dict[str,str]], roots: Roots) -> None`

`validate_compose` refuses when any of: top-level keys other than `name`, `services`, `networks`; `name != "orama-apps"`; service name not in `CATALOG`; `image` ≠ catalogue image; `privileged`, `pid`, `ipc`, `security_opt`, `userns_mode`, `volumes_from` present; `network_mode` other than absent or `service:gluetun` (and only on qbittorrent); `cap_add` ≠ catalogue `cap_add`; `devices` not in {catalogue devices, `/dev/dri:/dev/dri`}; GPU reservation on non-media-server; any volume not `type: bind` or with `source` failing `safe_path`; `env_file` other than that app's `env_file_path`; any `environment` key matching a `secret_env` name.

- [ ] **Step 1: Write failing tests** — one test per refusal rule, plus:

```python
def test_accepts_builder_output(roots):
    validate_compose(build_apps_compose(make_setup(...matching roots...)), roots)

@pytest.mark.parametrize("src", ["/etc", "/", "relative", "/srv/data/../../etc"])
def test_refuses_mounts_outside_roots(roots, src):               # [RF 5]
    doc = good_doc(); doc["services"]["sonarr"]["volumes"][0]["source"] = src
    with pytest.raises(Refused): validate_compose(doc, roots)

def test_refuses_symlink_escape(tmp_path):                       # [RF 5]
    (tmp_path / "data").mkdir(); (tmp_path / "data" / "evil").symlink_to("/etc")
    with pytest.raises(Refused): safe_path(str(tmp_path / "data" / "evil"), Roots(tmp_path/"data", tmp_path/"cfg"))

def test_refuses_docker_socket_mount(roots): ...
def test_refuses_unknown_service(roots): ...
def test_refuses_swapped_image(roots): ...
def test_refuses_privileged(roots): ...
def test_env_files_only_allowed_names(roots): ...
```

- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat: agent validation of compose and env files`.

---

### Task 7: Agent server, runner and client

**Files:**
- Create: `src/orama_agent/runner.py`, `src/orama_agent/server.py`, `src/orama_agent/__main__.py`, `src/orama/agent_client.py`, `tests/agent/test_server.py`

**Interfaces:**
- Consumes: `validate_compose`, `validate_env_files`, `safe_path`, `Roots`, `Refused` (Task 6); `dump_yaml`, `data_folders`, `config_folders` (Task 4).
- Produces:
  - Protocol: one JSON object per line over `/run/orama/agent.sock`. Request `{"id": str, "cmd": str, "args": {...}}`; response `{"id": str, "ok": bool, "result": ...}` or `{"id", "ok": false, "error": str}`. Max request 1 MiB.
  - Commands (allowlist, anything else → `error: "unknown command"`):
    - `ensure-folders {"paths": [str]}` → each via `safe_path`, `mkdir(parents=True)`, chown `PUID:PGID`, mode `2775`; returns created paths.
    - `apply {"compose": dict, "env_files": {app: {k: v}}}` → validate → write env files (mode `0600`, owner PUID) and `<config_root>/compose.yaml` (mode `0644`) atomically → `runner.up()`; returns `{"changed": [service names recreated/created]}`.
    - `status {}` → `runner.ps()` → `{service: state}`.
    - `restart {"app": str}` → app must be in `CATALOG`.
    - `self-update {}` → placeholder returning `error: "not implemented in Plan 1"` (implemented in Plan 3 with the Settings button).
  - `class ComposeRunner`: `up(project_file) -> list[str]` runs exactly `["docker","compose","-p","orama-apps","-f",file,"up","-d","--remove-orphans","--wait"]` and parses `docker compose ps --format json` before/after to compute changed services; `ps() -> dict[str,str]`; `restart(app)`. No `shell=True`, no user strings in argv except validated app names.
  - `class FakeRunner` with the same methods, recording calls (tests).
  - `class AgentClient`: `__init__(self, socket_path="/run/orama/agent.sock", timeout=900)`; `ensure_folders(paths) -> list[str]`, `apply(compose, env_files) -> list[str]`, `status() -> dict[str,str]`, `restart(app)`. Raises `AgentError(message)` on `ok: false`.
  - Socket created mode `0660`, group = `ORAMA_PGID`. Server logs with `RedactingFilter`-style redaction of env-file values (never log `args.env_files`).

- [ ] **Step 1: Write failing tests** (server runs in a thread on a `tmp_path` socket with `FakeRunner`):

```python
def test_unknown_command_refused(agent): assert agent.call("rm -rf", {})["ok"] is False
def test_apply_rejects_invalid_compose_and_runs_nothing(agent): ...
def test_apply_writes_env_files_0600_and_compose(agent, roots): ...
def test_apply_twice_reports_no_changes(agent):                  # [RF 3]
    first = client.apply(doc, env); second = client.apply(doc, env)
    assert second == [] and agent.runner.up_calls == 2
def test_interrupted_apply_then_rerun_converges(agent):          # [RF 3]
    agent.runner.fail_next_up = True
    with pytest.raises(AgentError): client.apply(doc, env)
    assert client.apply(doc, env) != []  # converges on retry
def test_ensure_folders_refuses_outside_roots(agent): ...
def test_env_values_never_logged(agent, caplog): ...
def test_oversized_request_rejected(agent): ...
```
(`FakeRunner.up` returns all services on first call, `[]` when the compose file content is unchanged since the last successful up.)

- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat: orama agent socket server, compose runner, client`.

---

### Task 8: Container images and the bootstrap/apply CLI

**Files:**
- Create: `docker/orama.Dockerfile`, `docker/agent.Dockerfile`, `src/orama/cli.py`, `src/orama/__main__.py`, `tests/test_cli.py`, `.dockerignore`

**Interfaces:**
- Consumes: everything above.
- Produces:
  - `python -m orama.cli bootstrap --platform P --data-root D --config-root C --puid U --pgid G --docker-gid DG --render-gid R|none --nvidia 0|1 --taken-ports 80,443,... --orama-image IMG --agent-image IMG` → writes `C/core.compose.yaml` (via `build_core_compose`) and `C/orama/setup.draft.json` (a `DesiredSetup` default for that platform with assigned ports for the core). Idempotent: keeps an existing draft/setup and existing port assignments. Prints the chosen Orama port.
  - `python -m orama.cli apply --setup PATH` (run **inside** the Orama container) → `assign_ports` (previous from setup), `ensure_app_keys`, `AgentClient.ensure_folders(data_folders + config_folders)`, `AgentClient.apply(build_apps_compose, env_files_for)`; saves setup with ports; prints a plain-language checklist (`✓ Folders ready`, `✓ sonarr started`, …) and exits non-zero with the Agent's message on failure.
  - Orama image: `python:3.12-slim`, non-root, installs the package, `CMD ["python","-m","orama"]` (Plan 1: a minimal HTTP server on 8765 that answers `GET /healthz` → `ok` and `GET /` → "Orama is running — setup page arrives in a later update."; replaced by the web app in Plan 3).
  - Agent image: `python:3.12-slim` + Docker CLI and compose plugin from Docker's apt repo, installs the package, `CMD ["python","-m","orama_agent"]`.
  - Image names: `ghcr.io/darkangelgr/orama:<version>` and `ghcr.io/darkangelgr/orama-agent:<version>`; version from `pyproject.toml`. While the repo is private, the installer supports `ORAMA_BUILD_LOCAL=1` (build from a checkout) — see Task 9.

- [ ] **Step 1: Write failing tests** for `bootstrap` (writes both files; second run with different `--taken-ports` keeps the earlier Orama port; awkward data root survives) and `apply` (with `FakeRunner`-backed agent fixture: creates folders, sends compose, prints checklist, non-zero exit + Agent message on refusal).
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement** CLI and Dockerfiles.
- [ ] **Step 4: Run** `uv run pytest -v` → PASS; `docker build -f docker/orama.Dockerfile .` and `docker build -f docker/agent.Dockerfile .` → both succeed (CI step).
- [ ] **Step 5: Add CI job** `images`: builds both images on every push; on `main` pushes them to GHCR with tags `<version>` and `sha-<short>`.
- [ ] **Step 6: Commit** `feat: orama and agent images, bootstrap and apply CLI`.

---

### Task 9: One-line installer

**Files:**
- Create: `install.sh`, `tests/test_install_sh.py`, `tests/fixtures/os-release/*` (reuse Task 2's)

**Interfaces:**
- Consumes: images and `bootstrap` CLI (Task 8).
- Produces: `curl -fsSL https://raw.githubusercontent.com/DarkAngelGR/orama-server/main/install.sh | sudo bash` (works once public; private-repo testing uses a checkout + `ORAMA_BUILD_LOCAL=1`).

Script structure: `set -Eeuo pipefail`; every step a function; `main` only runs when executed, not sourced (`[[ "${BASH_SOURCE[0]}" == "$0" ]] || return 0` guard placed before `main "$@"`), so tests can source and call functions. Overridable for tests: `OS_RELEASE_FILE` (default `/etc/os-release`), `DOCKER_BIN`, `DRY_RUN=1` (print actions instead of doing them).

Functions:
- `detect_platform` → prints `ubuntu|debian|ugos|unsupported` (same rule as Task 2; unsupported → plain message and exit 1).
- `require_root`, `require_x86_64`.
- `ensure_docker`: if `docker` and `docker compose version` work → nothing. Else on ugos → stop with "Please install/enable Docker from the UGOS App Center, then run this again." (never install). Else install from Docker's official apt repo (`docker-ce docker-ce-cli containerd.io docker-compose-plugin`), enable service.
- `install_user`: `SUDO_UID`/`SUDO_GID` if set and not 0; else on ugos the first UID ≥1000; else prompt-free fallback 1000:1000 with a printed note.
- `numeric_gid_of <path>` → `stat -c %g` or `none`; used for `/var/run/docker.sock` and `/dev/dri/renderD128`.
- `taken_ports` → from `ss -Htln`, comma-separated.
- `get_images`: pull `ghcr.io/darkangelgr/orama{,-agent}:$ORAMA_VERSION`, or build locally when `ORAMA_BUILD_LOCAL=1` from `ORAMA_SRC` (default: script's dir).
- `run_bootstrap`: `docker run --rm` the Orama image with the config root bind-mounted, invoking `python -m orama.cli bootstrap ...` with the gathered values.
- `start_core`: `docker compose -f "$CONFIG_ROOT/core.compose.yaml" -p orama-core up -d --wait`.
- `print_address`: first non-loopback IPv4 from `hostname -I` + chosen port: "Orama is ready. Open http://<ip>:<port> in your browser."

- [ ] **Step 1: Write failing tests** (pytest driving bash via `subprocess.run(["bash","-c", f'source install.sh; {fn}'], env=...)`):

```python
@pytest.mark.parametrize("fixture,expected", [...4 fixtures...])
def test_detect_platform(fixture, expected): ...
def test_ugos_never_installs_docker():
    out = run_fn("ensure_docker", OS_RELEASE_FILE=ugos, DOCKER_BIN="/nonexistent", DRY_RUN="1")
    assert out.returncode != 0 and "App Center" in out.stdout and "apt-get" not in out.stdout
def test_debian_dry_run_installs_from_docker_repo(): ...
def test_numeric_gid_of_missing_path_is_none(): ...              # [RF 4]
def test_sourcing_does_not_run_main(): ...
```
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement** `install.sh`. **Step 4: Run** tests + `shellcheck install.sh` → PASS, no warnings.
- [ ] **Step 5: Commit** `feat: one-line installer`.

---

### Task 10: Integration — real Docker, install twice, deploy the stack

**Files:**
- Create: `tests/integration/test_install_and_apply.py`, `tests/integration/conftest.py`; Modify: `.github/workflows/ci.yml` (job `integration`), `pyproject.toml` (marker `integration`)

**Interfaces:**
- Consumes: the whole plan.
- Produces: the Plan 1 deliverable — proven on a real Docker host.

Test flow (runs on the `ubuntu-24.04` GitHub runner, as root via `sudo -E`, `ORAMA_BUILD_LOCAL=1`, data root `/srv/orama test/Ταινίες` to exercise [RF 1], config root `/srv/orama-cfg`):
1. Occupy port 8080 with `python -m http.server 8080` before installing **[RF 2]**.
2. Run `install.sh`. Assert: exit 0; `orama-core` containers running; `curl http://localhost:<port>/healthz` = `ok`; output contains the address line.
3. `docker inspect orama-orama` has **no** `/var/run/docker.sock` mount; `orama-orama-agent` has no published ports.
4. Write a test setup (plex + jellyfin, usenet + torrent, **VPN off** in CI — no real VPN; VPN covered by unit tests and the Plan 5 manual checklist) and run `docker exec orama-orama python -m orama.cli apply --setup ...`. Assert every enabled app container is `running`/`healthy`; sabnzbd's host port ≠ 8080; the 10 data folders exist owned by the install user, mode `2775`.
5. Hardlink proof: inside `orama-sonarr`, `touch /data/downloads/usenet/tv/x && ln /data/downloads/usenet/tv/x /data/media/tv/x`; assert same inode via `stat -c %i` in `orama-radarr` (different container, same mount).
6. Each *arr answers `GET /api/v3/system/status` (Prowlarr `/api/v1/...`) with `X-Api-Key` = the stored key → 200 (proves key injection).
7. Run `install.sh` again and `apply` again. Assert installer exit 0, Orama port unchanged, `apply` reports no changes, container IDs unchanged **[RF 3]**.
8. `docker logs` of orama and agent contain none of the stored secret values.

- [ ] **Step 1: Write the integration test** as above (`@pytest.mark.integration`).
- [ ] **Step 2: Run locally if a Docker daemon is available** (`uv run pytest -m integration -v`); otherwise push and run in CI. Expected first run: FAIL until wiring issues are fixed; iterate until PASS.
- [ ] **Step 3: Add CI job** `integration` (needs `unit`, `images`): `sudo -E uv run pytest -m integration -v`, upload `docker compose logs` as an artifact on failure.
- [ ] **Step 4: Confirm CI green** on `main` (both `unit` and `integration`).
- [ ] **Step 5: Commit** `test: integration test for install and apply`.

---

## Plan 1 done when
- `unit`, `images` and `integration` CI jobs pass on `main`.
- Owner-facing summary written in plain language: what now works ("one command installs Orama and starts all the apps with the shared folder layout; nothing is connected yet — that's Plan 2").
