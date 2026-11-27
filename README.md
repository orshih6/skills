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
/dispatch sessions
/dispatch envs
/dispatch collect
/dispatch review fix-flaky-login-test
/dispatch handoff
```

- **`/dispatch <tasks>`** writes a self-contained brief for each task and starts one
  session per task: `claude --bg` in its own git worktree, or `claude --cloud` when the
  task needs nothing from your machine. A cloud task never uses GitHub: the folder is
  uploaded as it is (uncommitted changes too), and `pull` brings the work back.
- **`status`** shows one line per task. For a cloud task it asks claude.ai what the
  session is doing, through the API `claude --teleport` uses: starting, running (with
  its own one-line status), waiting for you, failed, blocked or done. `wait` waits for
  cloud sessions too.
- Every session dispatch starts, local or cloud, is named `dispatch-<task>`
  (`session_prefix` in the config), so it is easy to spot in `claude agents`, on
  claude.ai/code and in teleport's list.
- **`sessions`** lists every cloud session on your account, as `claude --teleport` does
  (`--dispatch` for only the ones dispatch started),
  and **`adopt <id>`** makes dispatch track one it did not start, so `status`, `wait`,
  `result` and `pull` work on it too.
- **`pull <name|id>`** brings a cloud session's files to your machine from its history,
  with no GitHub: the files it sent (byte for byte) and the files it wrote or edited.
  Teleport cannot do this for a session that never pushed: it fetches code from GitHub.
- **`envs`** lists your cloud environments as claude.ai/code has them (the same list as
  `/remote-env`), with the name your config gives each one. A cloud task can then pick
  one by that claude.ai name: `--environment "Light"`. It uses the claude CLI's
  own login and an undocumented API, so a Claude Code update may break it.
- **`collect`** first has each finished task reviewed by a new session that did not
  write it and has not seen its report, then gives the results in order of risk, with
  the findings the reviewer could confirm. A task whose review confirms a major problem
  goes back to a new session on the same branch with only those findings, and is
  reviewed again, up to `max_fix_rounds` times (default 2). `/dispatch review <name>`
  and `/dispatch fix <name>` do it for one task.
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

Needs Claude Code 2.1 or newer (`claude --bg`, `claude --cloud`), `git`, Python 3, a
claude.ai login for cloud tasks, and `gh` only for sessions adopted from GitHub. Each
repository must be trusted once: run `claude` in it and accept the prompt. State is kept
in `~/.claude/dispatch/`.

**Undocumented parts.** Local tasks use only documented commands. The cloud parts rely on
things Claude Code does not document, found by reading its CLI, so an update can break
them: the session API behind `status`, `wait`, `result`, `sessions` and `adopt`
(`/v1/code/sessions`, the one `claude --teleport` uses), the event and file routes behind
`pull`, the environment list behind `envs` (the one `/remote-env` uses), and the
`CCR_FORCE_BUNDLE` switch that makes `claude --cloud` upload a folder instead of cloning
it from GitHub. They use the claude CLI's own claude.ai login and never print it. Tested
with Claude Code 2.1.332. If one breaks, local tasks still work.

**Local or cloud.** The manager decides from the task: anything that needs this machine
(secrets, a VPN or cluster, a signed-in browser, local files) stays local. It cannot know
for sure that a task needs none of that, so list repositories that must never leave the
machine in `local_only`: the helper refuses to send those to the cloud.

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

## License

MIT, see [LICENSE](LICENSE).
