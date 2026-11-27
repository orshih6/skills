---
name: dispatch
description: Run tasks in other Claude Code sessions (local background sessions or cloud sessions) and manage them from this one, so the work does not fill this session's context. Use when the user wants to dispatch, delegate or fan out tasks to separate sessions, run something in the background or in the cloud, check on or collect dispatched work, or move running work to the cloud before closing the laptop ("dispatch these", "run this in the cloud", "how are my sessions doing", "I have to leave, hand it off").
argument-hint: "<tasks> | status | collect | review <name> | handoff"
---

# dispatch

You are the manager. You write briefs, start sessions, and read short reports. You do
not do the tasks yourself and you do not read the sessions' logs: both would fill this
session's context, which is what this skill exists to avoid.

Everything goes through one helper:

```bash
D="${CLAUDE_SKILL_DIR}/scripts/dispatch.py"   # scripts/ next to this file
python3 $D local  --repo <dir> --name <name> --brief-file <file> [--needs-local] [--no-worktree]
python3 $D cloud  --repo <dir> --name <name> --brief-file <file> [--base <branch>]
python3 $D review <name>
python3 $D wait   [<name> ...] [--timeout <seconds>]
python3 $D status
python3 $D result <name>
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
   - **Cloud** only when all of these hold: the repo has a GitHub remote, the work the
     task depends on is pushed, and the task needs nothing that exists only on this
     machine (secrets, a cluster or VPN, a signed-in browser, local media, hardware).
     Choose it for long work, or when the user says they will close the laptop.
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
   (`fix-flaky-login-test`).
5. **Tell the user** what was started as a table (task, where, name) and stop. Do not
   wait for the sessions.

What the helper adds to every brief: work on its own branch, never push the default
branch, never merge, deploy, spend or message anyone, never wait on a question, run the
tests, and report in a fixed shape. Read `scripts/dispatch.py` if you need the wording.

### When the helper refuses

| It says | Do |
|---|---|
| not a trusted folder | Tell the user to run `claude` in that folder once and accept the prompt. Never accept it for them. |
| no GitHub remote / local_only | Run the task with `local`. |
| uncommitted or unpushed work | Ask the user whether to push first. `--allow-unpushed` only if the task does not depend on that work. |
| limit reached | Tell the user what is running; start the rest later. |

## `/dispatch status`

Run `status` and show its lines as a table. States:

- `running`: still working.
- `done` / `failed` / `blocked` / `handoff`: the session's own report.
- `waiting-for-you`: it stopped on a prompt. Give the user the `claude attach <id>` shown.
- `stopped-no-report`: it ended without reporting. Only then look at its log, and only
  the end: `claude logs <id> | tail -c 3000` (the log is raw terminal output and large).
- Cloud tasks are judged by what reached GitHub: a pull request, a pushed branch, or
  nothing yet. Their live progress is only visible at the `view:` link.

## `/dispatch collect`

A session's own report says what it believes. Before the user reads it, a session that
did not write the change checks it.

1. **Review.** For each task that reports `done` and has no `<name>-review` task yet,
   run `review <name>`. It starts a new local session in its own worktree. That session
   gets the original brief and the branch's diff, not the writer's report or reasoning,
   and it marks each finding CONFIRMED (reproduced, or the failing lines are named) or
   PLAUSIBLE. Do not review tasks that report `failed` or `blocked`, and skip the step
   when the user says so ("collect, no review"). The review of a cloud task also runs
   on this machine, on the branch fetched from GitHub.
2. **Wait** for the reviews: `wait <name>-review ... --timeout 540`. If some are still
   running when it returns, report the rest now and say which reviews are pending.
3. **Report.** Run `result <name>` and `result <name>-review` for each task and give the
   user the tasks in order of the review's `RISK`, highest first:
   - `high`, or any CONFIRMED finding: what changed, branch or pull request, each
     confirmed finding with its file and line, and what the user must decide. Say that
     a person should read this one.
   - `normal`: two or three lines, and the PLAUSIBLE findings named as unconfirmed.
   - `low` and `clean`: one line.

   When the review's checks disagree with the writer's `TESTS` line, say so first.
4. **Findings are the user's call.** Do not fix them yourself and do not start a fix
   without being asked. If the user wants one, dispatch a new task whose brief names
   the branch to continue and quotes the confirmed findings.
5. **Clean up** when the user agrees: `claude rm <id>` removes a local session and its
   worktree (it refuses when the worktree holds unpushed work; do not force it), and
   `forget <name>` drops a task from the list. Do the same for the review tasks.

Never merge a branch or pull request yourself, whatever the review says.

`/dispatch review <name>` runs steps 1 to 3 for one task.

## `/dispatch handoff`

The user is about to close the laptop. Local sessions stop when it sleeps. Go through
every local task that `status` shows as `running`:

**Can move to the cloud** (GitHub remote, not marked `needs_local` in
`~/.claude/dispatch/tasks.jsonl`):

1. Ask the session to hand off: send it the message `Hand off now, as your brief
   describes.` (SendMessage to the session's name, if that tool is available). It
   commits its work in progress, pushes its branch and writes a report with
   `STATUS: handoff`. Wait up to two minutes for the report to appear.
2. If there is no way to message it, or no report appears: `claude stop <id>`, then in
   its worktree (`<repo>/.claude/worktrees/<name>`) commit everything to its branch with
   the message `wip: handoff` and push the branch. Never push the default branch.
3. Start the continuation: `cloud --name <name>-cloud --base <its branch>`, with a brief
   made of the original brief (`~/.claude/dispatch/briefs/<name>.md`, the part above the
   rules) plus the handoff report: what is done, what is left, what to try next.
4. `claude stop <id>` if it is still running. Keep the local worktree until the cloud
   task is collected.

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
  "local_only": ["infra", "some-repo-name"],
  "notify": "notify-send 'dispatch: {name} finished'",
  "extra_rules": "- Never touch files under deploy/.\n"
}
```

- `max_parallel`: local sessions allowed to run at once (default 4).
- `local_only`: repo directory names that must never go to the cloud.
- `notify`: a command each local session runs once when it reports.
- `extra_rules`: lines added to every brief's rules.

## Things to know

- A dispatched session uses the user's normal permission settings. If those skip
  permission prompts, nothing but the brief's rules holds the session back, so keep the
  brief narrow and never dispatch a task whose point is to deploy, pay or send.
- Sessions cost usage in parallel. Dispatching saves this session's context, not the
  account's limit. Say so if the user dispatches many tasks at once. A review is one
  more session per task, and it counts towards `max_parallel`.
- A review is a second opinion from one session, not a proof. `clean` means it found
  nothing, and a PLAUSIBLE finding may be wrong.
- A local task gets its own git worktree (`<repo>/.claude/worktrees/<name>`, branch
  `worktree-<name>`), so sessions in one repo cannot touch each other's changes. The
  worktree starts from the last commit: uncommitted changes in the repo are not in it.
- A cloud session works on a clone from GitHub. It never sees local files or secrets.
