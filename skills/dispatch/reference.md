# dispatch: reference

Read the section you need; SKILL.md says when.

## Status states in full

Local tasks:

- `running`: still working, with no report yet.
- `done` / `failed` / `blocked` / `handoff`: the session's own report.
- `waiting-for-you`: it stopped on a prompt. Give the user the `claude attach <id>` shown.
- `stopped-no-report`: it ended without reporting. Only then look at its log, and only
  the end: `claude logs <id> | tail -c 3000` (the log is raw terminal output and large).
- `gone`: the CLI no longer lists the session and it left no report.

Cloud tasks and cloud reviews. The line comes from claude.ai, through the API
`claude --teleport` uses (`GET /v1/code/sessions/<id>`, with the CLI's own login); the note
is the session's own one-line status:

- `starting` / `running`: getting ready (it has not ended its first turn), or working.
- `done`: finished. For a task the note ends with `pull <name>`; a review reads like a
  local one once its report is fetched (automatic).
- `waiting-for-you`: stopped on a permission prompt or a question. Give the user its
  link; they answer in the browser, or bring it here with `claude --teleport <id>`.
- `failed` / `blocked`: stopped, and says why (an API error, a question). Pass it on; do
  not start it again without asking.
- `gone`: claude.ai no longer has the session; its work cannot be pulled. `forget` it
  once the user agrees.
- `quiet`: claude.ai could not be read; a `note:` line says why. Give the user the link.

Sessions adopted with `adopt` may be GitHub ones started elsewhere: their lines can also
read `pr-open`, `draft`, `branch-pushed` or `not-pushed`, from what reached GitHub.

`result <name>` of a cloud task adds the session's last action, what it needs, and its
cost.

## Cloud tasks: upload, pull, import

`cloud` runs `claude --cloud` with `CCR_FORCE_BUNDLE=1`, so the folder is uploaded as it
is on this machine (uncommitted changes included), even for a repo with a GitHub remote.
The session never pushes; its brief tells it to send every file it created or changed
with SendUserFile (one `dispatch-<name>.tar.gz` past 20 files).

`pull <name|id>` brings a session's files here from its history, into
`~/claude-cloud/<name>/` unless `--out` says otherwise (it refuses a folder that is not
empty unless `--into-existing`):

- **sent**: each file sent with SendUserFile, downloaded byte for byte.
- **rebuilt**: each file its Write and Edit calls left, as text. An edit of a file it did
  not write starts from the text the edit's result records.

Files that commands made and nobody sent (a build, a render, `cp`) are not pulled.
`PULLED.md` lists what came and what was skipped: show it to the user. If something is
missing, the user tells the session in the browser to send it, then `pull` again with
`--into-existing`. A pulled folder is a copy: never copy it over a repository unasked.

`review <name>` of a finished cloud task first **imports** it: a worktree
`<repo>/.claude/worktrees/<name>` on the branch `cloud-<name>`, from the commit the folder
was uploaded from, with everything the session sent (archive unpacked) or wrote, in one
commit. From then on `review`, `fix` and `clean` treat it like a local task. The
session's own commits are not kept (they are squashed into that one: a reviewer may
remark on it), files it deleted are not removed, and uploaded uncommitted changes show
up in the diff of any file the session edited.

## Reviews in the cloud

`review` uploads the task's worktree (the branch with its history) to a fresh machine and
names the change by its commits. The reviewer writes its report, sends it with
SendUserFile and repeats it in its last message; `status`, `result` and `fix` fetch it into
`~/.claude/dispatch/results/`. It runs on this machine instead, and the start line says
why, when the task is `needs_local`, its repo is `local_only`, there is no claude.ai login,
no folder is checked out at the change, `--local` is passed, or `review_in_cloud` is
`false`.

## Sessions, adopt, teleport

`sessions` lists every cloud session of the account, newest first (the list
`claude --teleport` shows). `--all` adds archived ones; `--dispatch` keeps only the ones
dispatch started (named `dispatch-...`). Each line: id, state, last activity, repository,
title, `(dispatch: <name>)` when tracked, and its status line. Show it as a table.

`adopt <id> --name <name> --repo <dir>` tracks a session dispatch did not start, so
`status`, `wait`, `result`, `pull` and `forget` work on it. `--repo` must be a checkout of
the session's GitHub repository. Pass `--brief-file` with what it was asked when the user
can say it; a review needs it.

`claude --teleport <id>` (the user runs it; it is interactive) brings a session's
conversation here and gets its code with `git fetch` of its branch from GitHub. For an
uploaded session there is no such branch: it brings the conversation only. Use `pull`.

## Environments

`envs` lists the cloud environments of the login `claude --cloud` uses here (the list
`/remote-env` shows), with the name the config gives each. Show it as a table.

- A line ending in `cloud_environment` is the default for cloud tasks.
- A `warning:` line: a config name points to an id this login does not have (another
  `CLAUDE_CONFIG_DIR`, or deleted).

`--environment` and `cloud_environment` take a config name, a claude.ai name (any case) or
an id. An id is checked against this login: a config id from another login gives way to
the claude.ai name, and an id this login lacks stops the start with the list. The
environment's setup script runs before every task: pick the lightest one that has what
the task needs, and say which one when you report what was started.

## doctor

`doctor` checks, without starting a session, that each undocumented part still answers
(the upload switch, the login, the session list, one session, its events, the
environment list) and names what breaks when one fails. Run it after a Claude Code update
or when a cloud command fails oddly. Local tasks do not depend on any of it.

## Config

`~/.claude/dispatch/config.json`, local to the machine, never committed:

```json
{
  "max_parallel": 4,
  "max_fix_rounds": 2,
  "review_in_cloud": true,
  "session_prefix": "dispatch-",
  "cloud_model": "claude-opus-5-5",
  "cloud_environment": "Default",
  "environments": {"vpn": "env_..."},
  "local_only": ["infra", "some-repo-name"],
  "notify": "notify-send 'dispatch: {name} finished'",
  "leftovers": [".idea/"],
  "extra_rules": "- Never touch files under deploy/.\n"
}
```

- `max_parallel`: local sessions allowed at once (default 4). Cloud ones do not count.
- `max_fix_rounds`: how many times `fix` may send one task back (default 2).
- `review_in_cloud`: `false` runs every review on this machine (default `true`).
- `session_prefix`: put before every task name to name its session (default `dispatch-`).
- `cloud_model`, `local_model`: the model sessions start on; `--model` wins for one task.
- `cloud_environment`, `environments`: see Environments.
- `local_only`: repo directory names that must never go to the cloud.
- `notify`: a command each local session runs once when it reports.
- `leftovers`: endings of untracked paths `clean` may delete, on top of the built-in ones.
- `extra_rules`: lines added to every brief's rules.

## Worktrees and branches

A local task gets its own git worktree (`<repo>/.claude/worktrees/<name>`, branch
`worktree-<name>`), listed in the repo's `.git/info/exclude`. The worktree does not hold
the repo's uncommitted changes, and it can start from the pushed state of the branch;
the helper prints a note when the repo has unpushed commits. If the task depends on
them, tell the user: push first, or use `--no-worktree`.

`clean <name>` removes the session, its worktree and its line in the list. It deletes
what tools leave behind (`__pycache__/`, `.serena/` and the like, plus `leftovers`) and
refuses when anything else is uncommitted: show the user those files and do not force
it. The commits stay on the branch it prints (`worktree-<name>`, `cloud-<name>`, or
`kept/<name>`). `forget <name>` only drops a line from the list.
