---
name: dispatch
description: Run tasks in other Claude Code sessions (local background sessions or cloud sessions) and manage them from this one, so the work does not fill this session's context. Use when the user wants to dispatch, delegate or fan out tasks to separate sessions, run something in the background or in the cloud, check on or collect dispatched work, or move running work to the cloud before closing the laptop ("dispatch these", "run this in the cloud", "how are my sessions doing", "I have to leave, hand it off").
argument-hint: "<tasks> | status | sessions | adopt <id> | pull <name|id> | envs | collect | review <name> | fix <name> | handoff"
---

# dispatch

You are the manager. You write briefs, start sessions, and read short reports. You do
not do the tasks yourself and you do not read the sessions' logs: both would fill this
session's context, which is what this skill exists to avoid.

Everything goes through one helper:

```bash
D="${CLAUDE_SKILL_DIR}/scripts/dispatch.py"   # scripts/ next to this file
python3 $D local  --repo <dir> --name <name> --brief-file <file> [--needs-local] [--no-worktree] [--model <id>]
python3 $D cloud  --repo <dir> --name <name> --brief-file <file> [--model <id>] [--environment <name|id>]
python3 $D review <name> [--local] [--model <id>]
python3 $D fix    <name> [--model <id>]
python3 $D wait   [<name> ...] [--timeout <seconds>]   # local and cloud tasks
python3 $D status [--repo <dir>]
python3 $D envs   [--json]
python3 $D sessions [--dispatch] [--all] [--limit <n>] [--json]
python3 $D adopt  <session id> --name <name> --repo <dir> [--brief-file <file>]
python3 $D pull   <name|session id> [--out <dir>] [--into-existing]
python3 $D result <name>
python3 $D clean  <name>
python3 $D forget <name>
```

If that path does not exist, the helper is the `scripts/dispatch.py` beside this file:
`.claude/skills/dispatch/` in the project when the skill is installed for one project,
`~/.claude/skills/dispatch/` when it is installed for all of them.

It keeps its state in `~/.claude/dispatch/` (task list, briefs, reports), never in a repo.

## `/dispatch <tasks>`

1. **Split** the request into independent tasks. If two tasks would edit the same files,
   make them one task. If a task is a few minutes of work, do it yourself instead.
2. **Choose where each runs.**
   - **Local** is the default. A local session has the machine's files, credentials,
     signed-in tools and network.
   - **Cloud** only when the task needs nothing that exists only on this machine
     (secrets, a cluster or VPN, a signed-in browser, hardware). Choose it for long
     work, or when the user says they will close the laptop. A cloud task never uses
     GitHub: `claude --cloud` uploads the folder as it is on this machine, uncommitted
     changes included, even when the repo has a GitHub remote (the helper sets
     `CCR_FORCE_BUNDLE`). The session never pushes or opens a pull request: it sends
     every file it changed, and `pull` brings them back. The folder must be a git
     repository with at least one commit.
   - Pass `--needs-local` for a local task that could never move to the cloud. `handoff`
     relies on it.
3. **Write a brief** for each task to a temporary file. The session knows nothing about
   this conversation, so the brief must stand alone:
   - the goal, and why it is wanted
   - where to look (files, docs, the command that shows the problem)
   - what "done" means, as something checkable (a test passes, a page renders, a PR exists)
   - what is out of scope
   Do not add rules about branches, pushing or reporting: the helper appends those.
4. **Start them**, one helper call per task, and name each task with a short slug
   (`fix-flaky-login-test`). The session itself is named `dispatch-<slug>` (reviews
   `dispatch-<slug>-review`, fix rounds `dispatch-<slug>-fix1`), in `claude agents`,
   on claude.ai/code and in teleport's list alike, so the user can tell them apart.
   Use the bare slug with the helper.
5. **Tell the user** what was started as a table (task, where, name) and stop. Do not
   wait for the sessions.

What the helper adds to every brief: work on its own branch, never push the default
branch, never merge, deploy, spend or message anyone, never wait on a question, run the
tests, and report in a fixed shape. A local task commits to its branch and does not
push it or open a pull request unless the brief asks; a cloud task never pushes and
sends every file it created or changed with SendUserFile, so `pull` can bring them back.
Read `scripts/dispatch.py` if you need the wording.

### When the helper refuses

| It says | Do |
|---|---|
| not a trusted folder | Tell the user to run `claude` in that folder once and accept the prompt. Never accept it for them. |
| local_only | Run the task with `local`. |
| not a git repository / no commit | Ask the user to `git init` and commit the files, or run it `local`. |
| limit reached | Tell the user what is running; start the rest later. |

## `/dispatch status`

Run `status` and show its lines as a table. States:

- `running`: still working, with no report yet.
- `done` / `failed` / `blocked` / `handoff`: the session's own report.
- `waiting-for-you`: it stopped on a prompt. Give the user the `claude attach <id>` shown.
- `stopped-no-report`: it ended without reporting. Only then look at its log, and only
  the end: `claude logs <id> | tail -c 3000` (the log is raw terminal output and large).

A cloud task's line comes from claude.ai: the helper asks what the session is doing,
through the API `claude --teleport` uses (`GET /v1/code/sessions/<id>`, with the CLI's
own login), and the note is the session's own one-line status:

- `starting` / `running`: the session is getting ready, or working now.
- `done`: it finished; the note says what it did and ends with `pull <name>`. Run
  `pull` and show the user `PULLED.md`.
- `waiting-for-you`: it stopped on a permission prompt or a question. Give the user its
  link; they answer in the browser, or bring it here with `claude --teleport <id>`.
- `failed` / `blocked`: the session stopped and says why (an API error, a question).
  Pass that on; do not start it again without asking.
- `gone`: claude.ai no longer has the session; its work cannot be pulled. `forget` it
  once the user agrees.
- `quiet`: claude.ai could not be read (the status then ends with a `note:` line saying
  why). Give the user the link and ask them to look; do not start it again on a guess.

Sessions adopted with `adopt` may be GitHub ones started elsewhere: their lines can also
read `pr-open`, `draft`, `branch-pushed` or `not-pushed`, from what reached GitHub.

`result <name>` of a cloud task adds the session's last action, what it needs, and what it
has cost. The API has no documentation, so a Claude Code update can change it.

## `/dispatch sessions` and `/dispatch adopt <id>`

`sessions` lists every cloud session of the account, newest first: the list
`claude --teleport` shows, including sessions started on claude.ai or by another tool.
Archived ones only with `--all`; only the ones dispatch started (their name starts with
`dispatch-`) with `--dispatch`. Each line: id, state, last activity, repository, title,
`(dispatch: <name>)` when dispatch already tracks it, and the session's status line.
Show it as a table.

`adopt` makes dispatch track one of them like a task it started, so `status`, `wait`,
`result`, `review` and `forget` work on it. `--repo` must be a local checkout of the
session's GitHub repository (the helper refuses another one). A review needs to know what
the session was asked: when the user can say it, write that to a file and pass
`--brief-file`; otherwise adopt without it and say that `review` will refuse.

To bring a session's conversation and branch to this machine, the user runs
`claude --teleport <id>` in that checkout. It is interactive, so never run it yourself.
Teleport gets the code only from GitHub (`git fetch` of the session's branch): for a
session that never pushed, it brings the conversation and none of the files.

## `/dispatch pull <name|id>`

Brings a cloud session's files to this machine from its history, with no GitHub: every
dispatch cloud task comes back this way, and so can any other session. Into
`~/claude-cloud/<name>/` unless `--out` says otherwise; it refuses a folder that is not
empty unless `--into-existing`. It reads the session's events (the API claude.ai uses)
and writes:

- **sent**: each file the session sent with SendUserFile (videos, images, archives),
  downloaded byte for byte.
- **rebuilt**: each file its Write and Edit calls left, as text. An edit of a file the
  session did not write (one from the upload or the clone) starts from the text the
  edit's result records.

Files that commands made (a build, a render, `cp`, `git checkout`) are not in the history
and are not pulled. `PULLED.md` in the folder lists what came and what was skipped. Show
the user that list. When something they need is missing, the session can send it: they
tell it, in the browser, to send those files with SendUserFile, then `pull` again with
`--into-existing`. The pulled folder is a copy, not a checkout: never copy it over a
repository without asking.

## `/dispatch envs`

Run `envs` and show its lines as a table: name, id, kind, state, and the name the config
gives it, if any. It lists the account's cloud environments as claude.ai/code has them,
which is the same list `/remote-env` shows. It reads the claude CLI's own claude.ai login
(the Keychain on macOS, else `.credentials.json` in the CLI's config folder) and never
prints it. It lists them for the login that `claude --cloud` in this session uses: with
`CLAUDE_CONFIG_DIR` set, that is a different login, and it can have different environments.

- A line ending in `cloud_environment` is the one every cloud task uses by default.
- A `warning:` line means a config name points to an id this login does not have. Tell
  the user: the id may belong to another login, or the environment was deleted.
- Use it before choosing an environment for a cloud task, and when the user asks which
  environments there are. `--environment` also accepts an environment's claude.ai name
  (any case), so a config entry is optional.

It calls an API with no documentation (`GET /v1/environment_providers`, the one
`/remote-env` uses), so a Claude Code update can break it. If it fails, say so and fall
back to ids from the config or from claude.ai/code.

## `/dispatch collect`

A session's own report says what it believes. Before the user reads it, a session that
did not write the change checks it.

1. **Review.** Run `status` to see the tasks (all repos, or `--repo <dir>` when the user
   names one). For each task that reports `done` and has no `<name>-review` task yet,
   run `review <name>`. Treat a `handoff` task as `done` when its report says nothing
   is left to do. It starts a new session that gets the original brief and the
   branch's diff, not the writer's report or reasoning. It runs **in the cloud by
   default**: the task's worktree is uploaded to a fresh machine, which also catches
   work that only runs on this one (a missing dependency, an uncommitted file, an
   unwritten setup step). It falls back to this machine, and says why, when the task
   is `needs_local`, its repo is `local_only`, there is no claude.ai login, or the
   worktree is gone. `--local` forces this machine. The start line says where.
   It marks each finding MAJOR (the change misses something the task calls done, or
   breaks something that worked) or MINOR, and CONFIRMED (reproduced, or the failing
   lines are named) or PLAUSIBLE. Do not review tasks that report `failed` or `blocked`, and skip the step
   when the user says so ("collect, no review"). A cloud task has no branch to review:
   `pull` it instead, and report what came back with `PULLED.md`.
2. **Wait** for the reviews: `wait <name>-review ... --timeout 540`. A cloud review's
   report is fetched when it finishes; `status` and `result` then read it like a
   local one. (`wait` also takes
   cloud tasks: it waits while claude.ai says the session is running.) It prints the
   status line of every task when the named ones have reported, and starts with a
   `still running after ...` line when the time ran out first. If some reviews are
   still running then, report the rest now and say which are pending.
3. **Send back what failed.** For each task whose newest review has a finding that is
   MAJOR and CONFIRMED, run `fix <name>` (always the task's own name, not a round's).
   It starts a new local session in the task's own worktree, on the same branch, with
   the original brief and only those findings. MINOR and PLAUSIBLE findings are never sent back: they are the
   user's call. Then `wait <name>-fix<N>`, `review <name>-fix<N>`, `wait` for that
   review, and repeat while the newest review still has MAJOR CONFIRMED findings. The
   helper stops after `max_fix_rounds` rounds (default 2, exit code 7): then the task
   goes to the user as it is. A round that reports `failed` or `blocked` stops the loop
   too. Skip this step when the user says so ("collect, no fixes").
   A round's review reads the whole change since the task started, not only the round.
4. **Report.** Run `result <name>` and `result <name>-review` for each task and give the
   user the tasks in order of the review's `RISK`, highest first:
   - `high`, or any finding that is MAJOR and CONFIRMED: what changed, the branch or
     pull request, each such finding with its file and line, and what the user must
     decide. Say that a person should read this one.
   - `normal`: two or three lines, then the other findings in a line each, with the
     PLAUSIBLE ones named as unconfirmed.
   - `low` and `clean`: one line.

   When the writer says a check passed and the review ran the same check and it
   failed, say so first. Small differences in wording or line numbers are not that.

   A task that went through rounds is reported once, by its newest review, with how
   many rounds it took and what the rounds fixed.

   Then list the tasks that have no review, with their state in a line each: still
   running, `quiet`, `failed`, `blocked` (with its question), or not reviewed and why.
5. **The rest is the user's call.** Do not fix anything yourself. For a MINOR or
   PLAUSIBLE finding the user wants fixed, or a task out of rounds, dispatch a new
   task whose brief names the branch to continue and quotes the findings.
6. **Clean up** when the user agrees. A local task's commits exist only on its branch
   on this machine (unless it handed off or was asked to push; its report says), so
   first ask what to do with each branch: merge it, push it, or drop it. Do the merge
   or push yourself only when the user says so for that branch. Clean a task's reviews
   and fix rounds before the task. Then `clean <name>` removes the session, its worktree and its line in the
   list. It deletes what tools leave behind in the worktree (`__pycache__/`, `.serena/`
   and the like, plus the `leftovers` in the config) and refuses when anything else is
   uncommitted: show the user those files and do not force it. The task's commits
   stay: `clean` prints the branch that holds them (`worktree-<name>`, or `kept/<name>`
   when the first went with the worktree). Clean the review tasks the same way; a review's worktree holds nothing
   to keep. `forget <name>` only drops a line from the list, for cloud tasks.

Never merge a branch or pull request on your own judgement, whatever the review says.

`/dispatch review <name>` runs steps 1, 2 and 4 for one task; `/dispatch fix <name>` runs
step 3 for one task.

## `/dispatch handoff`

The user is about to close the laptop. Local sessions stop when it sleeps. Go through
every local task that `status` shows as `running`:

**Can move to the cloud** (not marked `needs_local` in `~/.claude/dispatch/tasks.jsonl`):

1. Ask the session to hand off: send it the message `Hand off now, as your brief
   describes.` (SendMessage to the session's name, if that tool is available). It
   commits its work in progress to its branch and writes a report with
   `STATUS: handoff`. Wait for the report with `wait <name> --timeout 300`: a session
   only reads the message between two of its steps, and one step can take minutes.
   Read the report with `result <name>`. If it says nothing is left to do, the task is
   finished: skip step 3, stop the session, and tell the user it finished instead of
   moving.
2. If there is no way to message it, or no report appears: `claude stop <id>`, then in
   its worktree (`<repo>/.claude/worktrees/<name>`) commit everything to its branch with
   the message `wip: handoff`. Push nothing.
3. Start the continuation from the worktree, which uploads it as it is:
   `cloud --repo <repo>/.claude/worktrees/<name> --name <name>-cloud`, with a brief
   made of the original brief (`~/.claude/dispatch/briefs/<name>.md`, the part above the
   rules) plus the handoff report: what is done, what is left, what to try next.
4. `claude stop <id>` if it is still running. Keep the local worktree until the cloud
   task is pulled: `pull <name>-cloud --out <somewhere new>`, then the user decides what
   to copy into the worktree.

**A fix round** (`fix` in the `where` column) is handed off like a task. Its worktree is
its task's (`<repo>/.claude/worktrees/<task>`, the `cwd` in `tasks.jsonl`).

**A review** (`review` in the `where` column): stop it with `claude stop <id>` and
`forget` it. It is short, and `collect` starts it again later.

**Must stay on this machine**: do not stop it. Tell the user it only keeps running while
the computer is awake, and give them the options:

- leave it plugged in with the lid open, or
- stop the Mac sleeping when the lid closes: `sudo pmset -a disablesleep 1` (the user
  runs it; undo with `0` afterwards).

They can steer a session from their phone if it was started with Remote Control.

Finish with one table: each task, and whether it moved to the cloud (with its link) or
stays on the machine.

To bring a cloud task back later: `claude --teleport <session id>` (`result <name>`
prints the command).

## Config (optional)

`~/.claude/dispatch/config.json`, local to the machine, never committed:

```json
{
  "max_parallel": 4,
  "max_fix_rounds": 2,
  "review_in_cloud": true,
  "session_prefix": "dispatch-",
  "cloud_model": "claude-opus-5-5",
  "cloud_environment": "light",
  "environments": {"light": "env_...", "vpn": "env_..."},
  "local_only": ["infra", "some-repo-name"],
  "notify": "notify-send 'dispatch: {name} finished'",
  "leftovers": [".idea/"],
  "extra_rules": "- Never touch files under deploy/.\n"
}
```

- `max_parallel`: local sessions allowed to run at once (default 4).
- `max_fix_rounds`: how many times `fix` may send one task back (default 2).
- `review_in_cloud`: `false` runs every review on this machine (default `true`).
- `session_prefix`: put before every task name to name its session (default `dispatch-`).
- `cloud_model`, `local_model`: the model every cloud session, or every local session
  and review, starts on. Without them a session uses the account's default. Pass
  `--model` only when the user names a model for one task; it wins over the config.
- `environments`: short names for the user's cloud environments, each with its id.
  Optional: `envs` lists the ids, and `--environment` also takes an environment's
  claude.ai name. `cloud_environment` is the one every cloud task uses, as a config
  name, a claude.ai name or an id. Without it a task runs on the account's default
  environment.
- `local_only`: repo directory names that must never go to the cloud.
- `notify`: a command each local session runs once when it reports.
- `leftovers`: endings of untracked paths that `clean` may delete from a worktree, on
  top of the built-in ones.
- `extra_rules`: lines added to every brief's rules.

## Things to know

- A dispatched session uses the user's normal permission settings. If those skip
  permission prompts, nothing but the brief's rules holds the session back, so keep the
  brief narrow and never dispatch a task whose point is to deploy, pay or send.
- Sessions cost usage in parallel. Dispatching saves this session's context, not the
  account's limit. Say so if the user dispatches many tasks at once. A review is one
  more session per task; a local one counts towards `max_parallel`, a cloud one does not.
- A review is a second opinion from one session, not a proof. `clean` means it found
  nothing, and a PLAUSIBLE finding may be wrong.
- A local task gets its own git worktree (`<repo>/.claude/worktrees/<name>`, branch
  `worktree-<name>`), so sessions in one repo cannot touch each other's changes. The
  worktree does not hold the repo's uncommitted changes, and it can start from the
  pushed state of the branch, without local commits that are not pushed yet. The helper
  prints a note when the repo has such commits: if the task depends on them, tell the
  user, and push first or use `--no-worktree`.
  The helper lists `.claude/worktrees/` in the repo's `.git/info/exclude`, so the
  worktrees do not show up in `git status`.
- A cloud session works on the uploaded folder. It never sees local files outside it,
  or secrets.
  It runs on the config's `cloud_environment`, or on the one given with
  `--environment` for that task (`envs` lists them). The environment's setup script runs before every
  task, so a heavy one delays them all: pick the lightest environment that has what the
  task needs (its network access, its secrets), and say which one you chose when you
  tell the user what was started. The start line prints it.
