# Working on Orama Server — instructions for AI coding assistants

This file is for any assistant working on this repo: Claude Code, ChatGPT/Codex, Gemini, Cursor or others.
The owner switches between tools when one hits its usage limit, so **all state lives in this repo**,
never in one tool's memory.

## Start here (every session)

1. `git pull origin main`.
2. Read **`docs/STATUS.md`**. It shows the current plan, which tasks are done, what's in progress and
   what comes next. If it says a task is *in progress by another tool*, ask the owner before you touch it.
3. Read the spec: `docs/superpowers/specs/2026-11-03-orama-release-1-design.md`. It's the binding
   authority: what Orama is, the architecture and the security rules.
4. Read the **notes** files in `docs/superpowers/plans/` (`*-notes.md`). They record how earlier plans
   were actually built and override the plan text where they differ.
5. Read only the plan task you're doing (in the current plan file), plus that plan's
   *Global Constraints*, *Fixed values* and *Review Focus* sections.

## How the owner works (spec §12)

- Explain things in plain language, by what changes for the user. The owner isn't a programmer.
- Commit and push **straight to `main`**. No branches, no pull requests.
- The repo will become public: never commit secrets, IP addresses, personal paths or email addresses.
  `tests/test_repo_hygiene.py` enforces this. Build fake keys at runtime in tests.
- Commit trailer: Claude adds `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
  **If you aren't Claude, don't add that line.** Use your own tool's attribution or none.
  Never add a `Claude-Session:` line.

## How to do a task

1. Mark it *in progress* in `docs/STATUS.md` (name your tool), then commit and push that change.
2. TDD: write the task's tests first, run them and see them fail, then implement until they pass.
3. Fast checks, which must all pass:
   ```bash
   uv run pytest -m "not integration"
   uv run ruff check
   uvx --from shellcheck-py shellcheck install.sh
   ```
4. Commit with the task's commit message, then push.
5. CI (`.github/workflows/ci.yml`) runs `unit`, `images`, `integration` (real Docker install) and `publish`.
   Check it's green for your commit.
6. Update `docs/STATUS.md`: mark the task done, record any decision you made that differs from the plan
   (what, why, what it costs if wrong), then commit and push.

## Security rules you must not weaken

- **Only the Orama Agent** (`src/orama_agent/`) has the Docker socket. It runs as root, has no network,
  and **validates everything** Orama sends against pinned allowlists (`src/orama_agent/validate.py`).
  - If you add a compose key, env var, mount or Agent command, add it to **both** the builder
    (`src/orama/compose.py`) **and** the Agent's pins, with tests that the Agent refuses anything else.
  - Never loosen a check to make a test pass.
- The Orama container mounts only `<config_root>/orama`. App containers are untrusted.
  - The Agent writes files only through no-follow directory file descriptors with atomic renames.
  - App secrets go only to `<config_root>/app-env/` (root-only).
- Secrets never appear in `compose.yaml`, logs, command output or error messages.
- When unsure whether a change touches this boundary, stop and ask the owner. It's reviewed with the
  owner's Claude Security plugin before each plan is called done.

## Code map

| Path | What it is |
|---|---|
| `install.sh` | One-line installer (bash, run as root) |
| `src/orama/` | Orama: setup model, catalogue (pinned images, ports), compose builder, secrets, CLI (`bootstrap`, `apply`) |
| `src/orama/wiring/` | (Plan 2a) connects the apps through their APIs |
| `src/orama_agent/` | The Agent: Unix-socket server, validation, `docker compose` runner |
| `docker/` | Dockerfiles for the Orama and Agent images |
| `tests/` | Unit tests; `tests/integration/` runs a real install in CI (`ORAMA_INTEGRATION=1`, root) |
| `docs/superpowers/` | Spec, plans, notes |

Tooling: Python ≥3.12 managed with `uv`; `ruff` for lint; `pytest`.
