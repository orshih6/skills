# skills

General-purpose [Claude Code](https://claude.com/claude-code) skills. Each one is a
directory under `skills/` with a `SKILL.md` that Claude loads when you type
`/<skill-name>` or ask for what it describes.

| Skill | What it does |
|---|---|
| [dispatch](skills/dispatch/SKILL.md) | Run tasks in other Claude Code sessions (local background or cloud) and manage them from one session, so the work does not fill that session's context. Move running work to the cloud before you close the laptop. |

## dispatch

```
/dispatch fix the flaky login test; add CSV export to the reports page
/dispatch status
/dispatch collect
/dispatch handoff
```

- **`/dispatch <tasks>`** writes a self-contained brief for each task and starts one
  session per task: `claude --bg` in its own git worktree, or `claude --cloud` when the
  repo is on GitHub and the task needs nothing from your machine.
- **`status`** shows one line per task. **`collect`** gives each finished task's result
  in a few lines.
- **`handoff`** is for when you have to leave: tasks that can run in the cloud are pushed
  to a branch and continued there; the rest are listed with how to keep them running.

Every dispatched session works on its own branch and is told never to push the default
branch, merge, deploy, spend money or message anyone. It reports in a fixed shape that
the manager reads instead of the session's log.

Needs Claude Code 2.1 or newer (`claude --bg`, `claude --cloud`), `git`, Python 3, and
`gh` for reading cloud results. State is kept in `~/.claude/dispatch/`.

## Install

```bash
git clone https://github.com/orshih6/skills.git
ln -s "$PWD/skills/skills/dispatch" ~/.claude/skills/dispatch
```

Or copy a single skill directory into `~/.claude/skills/` (all your projects) or into a
project's `.claude/skills/` (that project only).
