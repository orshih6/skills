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
/dispatch envs
/dispatch collect
/dispatch review fix-flaky-login-test
/dispatch handoff
```

- **`/dispatch <tasks>`** writes a self-contained brief for each task and starts one
  session per task: `claude --bg` in its own git worktree, or `claude --cloud` when the
  repo is on GitHub and the task needs nothing from your machine.
- **`status`** shows one line per task. For a cloud task it asks claude.ai what the
  session is doing, through the API `claude --teleport` uses: running (with its own
  one-line status), waiting for you, failed, blocked, or stopped with nothing pushed.
  Its pull request, when there is one, is still the final report. Without the CLI's
  login only GitHub is read, and a task with nothing there after 20 minutes shows as
  `quiet`. `wait` waits for running cloud sessions too.
- **`envs`** lists your cloud environments as claude.ai/code has them (the same list as
  `/remote-env`), with the name your config gives each one. A cloud task can then pick
  one by that claude.ai name: `--environment "Light"`. It uses the claude CLI's
  own login and an undocumented API, so a Claude Code update may break it.
- **`collect`** first has each finished task reviewed by a new session that did not
  write it and has not seen its report, then gives the results in order of risk, with
  the findings the reviewer could confirm. `/dispatch review <name>` does it for one task.
- After `collect`, the finished sessions and their worktrees are removed once you
  agree. A local task's commits stay on its branch; nothing is pushed or merged for you.
- **`handoff`** is for when you have to leave: tasks that can run in the cloud are pushed
  to a branch and continued there; the rest are listed with how to keep them running.

To run a task on a particular model, name the model when you dispatch it; the helper
passes it as `--model <id>`. Without one, a session uses `cloud_model` / `local_model`
from the config, else the account's default.

Every dispatched session works on its own branch and is told never to push the default
branch, merge, deploy, spend money or message anyone. A local session does not push its
branch either, unless the task asks for it. It reports in a fixed shape that
the manager reads instead of the session's log.

Needs Claude Code 2.1 or newer (`claude --bg`, `claude --cloud`), `git`, Python 3, and
`gh` for reading cloud results. State is kept in `~/.claude/dispatch/`.

### Config

Optional, in `~/.claude/dispatch/config.json` (local to the machine, never committed):

- `max_parallel`: how many local sessions may run at once (default 4; reviews count).
- `cloud_model`: the model every cloud session starts on.
- `local_model`: the model every local session and review starts on.
- `cloud_environment`: the cloud environment every cloud task uses (a config name, a
  claude.ai name, or an id).
- `environments`: short names for environment ids. Optional, since `envs` lists the ids.
- `local_only`: repo directory names that must never go to the cloud.
- `notify`: a command each local session runs once when it reports (`{name}` is the task).
- `leftovers`: extra untracked paths `clean` may delete from a worktree.
- `extra_rules`: lines added to the rules of every brief.

See [SKILL.md](skills/dispatch/SKILL.md) for an example file.

## Install

```bash
git clone https://github.com/orshih6/skills.git

# for one project only
ln -s "$PWD/skills/skills/dispatch" /path/to/project/.claude/skills/dispatch

# or for all your projects
ln -s "$PWD/skills/skills/dispatch" ~/.claude/skills/dispatch
```

Copying the skill directory works too.
