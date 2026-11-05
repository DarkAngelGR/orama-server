# Task board — how Claude, ChatGPT/Codex, Gemini and Cursor share the work

The owner switches tools when one hits its usage limit. Tools never talk to each other; they talk through
these files. One file per task, in `docs/tasks/<plan>/Tnn.md`.

## Rules

1. **One owner per task.** Take only a task whose **State** is `open` (or a task the owner hands you).
   Set State `in progress` + Owner = your tool, commit and push that *before* coding. If a task is
   `in progress` by another tool, ask the owner before touching it.
2. **In order.** Tasks build on the earlier tasks' Interfaces. Start Task N only when tasks 1…N-1 are `done`.
3. **The plan is the instruction.** The task file points to its section in the plan; the plan's
   Global Constraints, Fixed values and Review Focus apply to every task. `AGENTS.md` rules always apply.
4. **Stop cleanly.** Before your usage limit: push only tested work, fill in the task's **Report**
   (including what is unfinished or untested), leave State `in progress`, push. The next tool continues
   from your report.
5. **Finish.** Fill in the Report, set State `done` (Claude) or `needs review` (other tools), update the
   board in `docs/STATUS.md`, push.

## States

`open` → `in progress` → `needs review` → `done`

`needs review`: Claude reviews the commits against the plan (security-sensitive tasks with the most
capable model), fixes or asks for fixes, then sets `done`.
