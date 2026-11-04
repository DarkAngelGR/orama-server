# Claude: read AGENTS.md

Shared instructions for every AI assistant are in [`AGENTS.md`](AGENTS.md), and the current progress is in
[`docs/STATUS.md`](docs/STATUS.md). Read both first.

Claude-specific:
- Plans run with `superpowers:subagent-driven-development`, using the cheaper model by default and the
  most capable model only for Agent, compose-pin and secrets tasks (owner decision).
- Before a plan is called done, scan its changes with the owner's Claude Security plugin
  (agent `claude-security:claude-security`).
- Keep `docs/STATUS.md` current after every task, so other tools can continue if Claude hits a usage limit.
