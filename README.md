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
/dispatch review fix-flaky-login-test
/dispatch handoff
```

- **`/dispatch <tasks>`** writes a self-contained brief for each task and starts one
  session per task: `claude --bg` in its own git worktree, or `claude --cloud` when the
  repo is on GitHub and the task needs nothing from your machine.
- **`status`** shows one line per task.
- **`collect`** first has each finished task reviewed by a new session that did not
  write it and has not seen its report, then gives the results in order of risk, with
  the findings the reviewer could confirm. `/dispatch review <name>` does it for one task.
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

# for one project only
ln -s "$PWD/skills/skills/dispatch" /path/to/project/.claude/skills/dispatch

# or for all your projects
ln -s "$PWD/skills/skills/dispatch" ~/.claude/skills/dispatch
```

Copying the skill directory works too.
