# Fresh-install Storage Choices in the Browser Wizard — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking; `- [x]` means done and checked in CI.

**Goal:** Allow users to choose their storage locations (app settings/databases and unified media/downloads) and custom folder names in the browser wizard during fresh installation, backed by a root-authorized candidate manifest and transactional Agent activation.

**Architecture:** 
1. **Installer Preparation:** The host `install.sh` discovers valid dedicated storage volumes and writes a root-owned `storage_candidates.json` into the root-only control directory (`<config_root>/storage_candidates.json`), inaccessible to the web container.
2. **Sanitized Web Projection:** The Agent reads the root manifest, translates it into opaque candidate IDs, and exposes them to the web app over the Unix domain socket. The web UI never supplies raw host paths.
3. **Folder Naming & Validation:** Users may specify custom folder names within installer-authorized areas. Names are strictly single path components validated against a conservative grammar (`^[a-zA-Z0-9_-]{1,64}$`), rejecting separators, dots, colons, and collisions.
4. **Isolated Probe Helper:** A bounded helper container (read-only rootfs, `--network none`, no Docker socket, dropped capabilities) tests write permissions and hardlink creation between downloads and media under the app's UID/GID.
5. **Transactional Activation:** The Agent records transactions in a durable journal (`<config_root>/activation_journal.json`), validates compose documents from pinned templates, safely stages core container recreation, and rolls back on failure. Locations are frozen once activation begins.

**Tech Stack:** Python >=3.12 (`uv`, Pydantic 2, Flask, pytest), Bash installer (`shellcheck`), Docker Compose v2, Playwright Chromium.

**Spec:** `docs/superpowers/specs/2026-11-17-wizard-storage-design.md`

---

## File Structure & Map

```
install.sh                               Storage discovery & root-owned manifest generation
src/orama/setup_model.py                 DesiredSetup model with app_config_root and folder names
src/orama/layout.py                      Path layout functions for app_config_root & data_root
src/orama/compose.py                     App compose generation using dynamic app_config_root
src/orama/agent_client.py                Client RPC methods for storage candidates & validation
src/orama_agent/storage.py               Candidate manifest reader, folder grammar, probe helper
src/orama_agent/runner.py                Transactional activation, journal, and core recreation
src/orama_agent/server.py                Agent RPC endpoints for storage candidates & validation
src/orama/web/setup.py                   Wizard storage step handlers and validation
src/orama/web/templates/setup/           Wizard Folders step template & path preview components
tests/test_setup_model.py                Model validation tests for storage paths & folder names
tests/test_layout.py                     Layout tests for app_config_root and data_root
tests/agent/test_storage.py              Unit tests for manifest parsing, name grammar, helper
tests/agent/test_server.py               Agent RPC endpoint tests for storage operations
tests/web/test_setup.py                  Web wizard storage step tests
tests/web/test_browser.py                Playwright browser tests for wizard storage selection
tests/integration/test_install_and_apply.py Integration tests with custom storage configuration
```

---

## Task 1: Setup Model and Folder Layout Foundation

Extend `DesiredSetup` and `layout.py` to support `app_config_root` alongside `config_root` and `data_root`, with strict validation and conservative folder naming grammar.

**Files:**
- `src/orama/setup_model.py`
- `src/orama/layout.py`
- `src/orama/compose.py`
- `tests/test_setup_model.py`
- `tests/test_layout.py`
- `tests/test_compose.py`

- [x] **Step 1: Write failing unit tests for storage paths and folder naming grammar**
  - Add tests in `tests/test_setup_model.py` verifying:
    - Conservative folder name regex: allows `^[a-zA-Z0-9_-]{1,64}$`; rejects `/`, `\`, `..`, `.`, colons, null bytes, leading spaces, empty strings, and names >64 chars.
    - `app_config_root` validation: absolute `PurePosixPath`, must not overlap `data_root`, must not equal `config_root` except for the default `config_root / "config"`.
    - Backward-compatibility: `app_config_root` defaults to `config_root / "config"` when omitted in JSON.
  - Add tests in `tests/test_layout.py` verifying:
    - `app_config_dir(app_config_root, app)` maps to `app_config_root / app`.
    - `config_root` retains exclusive ownership of `orama_state_dir`, `app_env_dir`, secrets, and sockets.
  - Run `uv run pytest tests/test_setup_model.py tests/test_layout.py` and confirm tests fail as expected.

- [x] **Step 2: Implement storage model and layout changes**
  - In `src/orama/setup_model.py`:
    - Define `FOLDER_NAME_PATTERN = r"^[a-zA-Z0-9_-]{1,64}$"`.
    - Add `FolderName = Annotated[str, Field(pattern=FOLDER_NAME_PATTERN)]`.
    - Add `app_config_root: Root | None = None` to `DesiredSetup`.
    - Add model validator ensuring `app_config_root` and `data_root` do not overlap, and that `app_config_root` does not collide with `orama_state_dir` or `app_env_dir`.
  - In `src/orama/layout.py`:
    - Update `app_config_dir(app_config_root: PurePosixPath, app: str) -> PurePosixPath`.
    - Update `config_folders(setup: DesiredSetup)` to use `setup.app_config_root or (setup.config_root / "config")`.
  - In `src/orama/compose.py`:
    - Ensure app volume bindings use `app_config_dir(setup.app_config_root or (setup.config_root / "config"), app)`.

- [x] **Step 3: Verify and ensure lint and tests pass**
  - Run `uv run pytest tests/test_setup_model.py tests/test_layout.py tests/test_compose.py`.
  - Run `uv run ruff check src/ tests/`.

---

## Task 2: Host Installer Storage Discovery & Root Manifest

Update `install.sh` to safely discover host filesystem volumes, prepare dedicated candidate directories, and write the root-owned manifest.

**Files:**
- `install.sh`
- `tests/test_installer.py` (or installer unit tests)

- [ ] **Step 1: Write test for installer candidate manifest structure and safety**
  - Verify `storage_candidates.json` schema:
    - `version`: int (1)
    - `candidates`: list of `{ id: str, label: str, mount_point: str, prepared_path: str, filesystem: str, default_folder: str }`
  - Verify that system mounts (`/`, `/boot`, `/dev`, `/proc`, `/sys`, `/run`) are never offered as raw general destinations without dedicated subfolder sandboxing.
  - Verify permissions: mode `0600`, owned by `root:root`, stored inside `<config_root>/storage_candidates.json`.

- [ ] **Step 2: Implement volume discovery in `install.sh`**
  - Scan mounts via `findmnt` or `/proc/mounts`, filtering for ext4, btrfs, zfs, xfs.
  - For each eligible mount, establish a dedicated Orama directory (e.g. `<mount>/orama-data`) with ownership `PUID:PGID` and mode `0775`.
  - Always register the default candidate pointing to the installer's configured `--data-root` and `--config-root`.
  - Atomically write `<config_root>/storage_candidates.json`.

- [ ] **Step 3: ShellCheck and installer verification**
  - Run `uvx --from shellcheck-py shellcheck install.sh`.

---

## Task 3: Agent Storage Module, Probe Helper, and RPC Endpoints

Implement storage manifest reading, sanitized web projection, bounded validation helper container, and transactional activation journal in `orama_agent`.

**Files:**
- `src/orama_agent/storage.py`
- `src/orama_agent/runner.py`
- `src/orama_agent/server.py`
- `src/orama/agent_client.py`
- `tests/agent/test_storage.py`
- `tests/agent/test_server.py`

- [ ] **Step 1: Write tests for candidate parsing, projection, and probe helper**
  - Test `StorageManifest.load(path)`:
    - Rejects missing, unreadable, or malformed JSON.
    - Generates opaque candidate IDs (e.g. `cand_01`, `cand_02`).
    - Produces sanitized projection for Web API (never disclosing internal system mount options or unauthorized paths).
  - Test folder name validation and path derivation:
    - Validates single-component folder names against conservative grammar.
    - Derives exact subfolder path within the candidate's prepared directory.
    - Rejects existing foreign files/directories that do not belong to Orama.
  - Test bounded probe helper command construction:
    - Image: pinned agent image.
    - Flags: `--network none`, `--cap-drop ALL`, `--security-opt no-new-privileges`, `--read-only`, tmpfs `/tmp`.
    - Mounts only the exact candidate directories (no Docker socket, no `/`).
    - Probes hardlink creation between downloads and media dirs.

- [ ] **Step 2: Implement `src/orama_agent/storage.py`**
  - Implement manifest loading and candidate ID resolution.
  - Implement folder name validation with conservative regex.
  - Implement probe helper container execution via Docker CLI runner.
  - Report hardlink and permission probe results back to caller.

- [ ] **Step 3: Implement Agent RPC endpoints and client methods**
  - In `src/orama_agent/server.py`:
    - Command `storage-candidates`: returns list of `{ id, label, default_app_folder, default_data_folder, free_bytes }`.
    - Command `storage-validate`: takes `{ app_candidate_id, app_folder_name, data_candidate_id, data_folder_name }`, runs probe helper, returns `{ valid: bool, hardlinks_supported: bool, error: str | None, resolved_app_path: str, resolved_data_path: str }`.
    - Command `storage-lock`: transitions installation state from bootstrap to locked, preventing any subsequent storage reconfiguration.
  - In `src/orama/agent_client.py`:
    - Implement `AgentClient.get_storage_candidates()`.
    - Implement `AgentClient.validate_storage(...)`.

- [ ] **Step 4: Implement Transactional Activation Journal**
  - In `src/orama_agent/runner.py`:
    - Implement journal file `<config_root>/activation_journal.json`.
    - Track phases: `prepared` -> `activating` -> `core-ready` -> `deployed`.
    - Before modifying core compose or starting containers, stage configuration and save previous verified state.
    - Implement automatic rollback if container start fails or health check times out.

- [ ] **Step 5: Run unit tests**
  - Run `uv run pytest tests/agent/test_storage.py tests/agent/test_server.py`.

---

## Task 4: Web Wizard Folders Step & Path Previews

Implement the Folders step in the Orama browser setup wizard, allowing users to choose storage locations and customize folder names with real-time preview and validation.

**Files:**
- `src/orama/web/setup.py`
- `src/orama/web/templates/setup/folders.html` (or step component)
- `tests/web/test_setup.py`
- `tests/web/test_browser.py`

- [ ] **Step 1: Write tests for the Folders wizard step**
  - Test GET `/setup/folders`:
    - Renders candidate dropdowns for App Settings and Media/Downloads.
    - Populates default folder names (`apps` and `data`).
    - Previews full resulting paths based on default candidates.
  - Test POST `/setup/folders`:
    - Validates candidate IDs exist in Agent projection.
    - Validates folder names against conservative grammar.
    - Calls Agent `storage-validate` before advancing.
    - Returns clear, plain error messages if name has invalid characters or probe fails.
    - Rejects access once setup has been completed/locked.

- [ ] **Step 2: Implement wizard template and route handler**
  - In `src/orama/web/setup.py`:
    - Add `storage_candidates` query to setup session state.
    - Wire form parsing, validation, and Agent verification into wizard progression.
    - Save selected `app_config_root` and `data_root` into `DesiredSetup`.
  - In `src/orama/web/templates/`:
    - Style the Folders step coherent with Orama's Design D (frosted glass, dark theme, clean typography).
    - Provide dynamic/JS live path preview updating as custom folder names are typed.
    - Show helper hints explaining hardlink requirements for instant downloads.

- [ ] **Step 3: Browser testing with Playwright**
  - In `tests/web/test_browser.py`:
    - Add browser test navigating through the wizard to the Folders step.
    - Type custom folder names, verify live preview updates.
    - Submit valid choices, assert progression to next step.
    - Attempt invalid characters (e.g. `../bad:folder`), assert plain error message is displayed.
  - Run `uv run pytest tests/web/test_setup.py`.

---

## Task 5: Integration, End-to-End Proofs, and Verification

Verify the entire storage configuration workflow under real Docker and VM environments.

**Files:**
- `tests/integration/test_install_and_apply.py`
- `tests/e2e/test_e2e.py`
- `tests/test_repo_hygiene.py`

- [ ] **Step 1: Integration test with custom storage roots**
  - Add integration test case in `tests/integration/test_install_and_apply.py`:
    - Set up mock storage candidates.
    - Run apply with custom `app_config_root` and `data_root`.
    - Verify app containers mount the exact specified host paths.
    - Verify `orama` state folder remains in `config_root` and is not moved.

- [ ] **Step 2: End-to-end proof validation**
  - Ensure the wizard script in `tests/e2e/wizard.cjs` handles the Folders step gracefully (using defaults or custom selection).
  - Verify `tests/e2e/test_e2e.py` passes on clean install targets.

- [ ] **Step 3: Repository hygiene and CI check**
  - Run `uv run pytest tests/test_repo_hygiene.py` (author identity, no forbidden IP literals).
  - Run `uv run ruff check src/ tests/`.
  - Run `uvx --from shellcheck-py shellcheck install.sh`.
  - Run unit test suite: `uv run pytest -m "not integration"`.

---

## Review Focus & Invariants

1. **Host Isolation:** Only the Agent touches the host or runs Docker commands. The web app communicates only via Unix socket and receives sanitized candidate projections with opaque IDs.
2. **Conservative Folder Naming:** All custom names must match `^[a-zA-Z0-9_-]{1,64}$`. Separators, dots, and colons are rejected before any path derivation occurs.
3. **No General Host Volume Mounts:** The validation helper container mounts only the exact candidate directories being probed, with `--network none`, `--cap-drop ALL`, read-only rootfs, and no Docker socket.
4. **Hardlink Integrity:** Hardlink probe must verify inode sharing between proposed downloads and media subdirectories. If on separate filesystems, warning is presented or layout is refused.
5. **Fresh-Install Lock:** Once activation begins, storage layout cannot be changed via the wizard or Agent requests. Re-running installer preserves existing trusted activation records.
