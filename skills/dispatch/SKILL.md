---
name: dispatch
description: Run tasks in other Claude Code sessions (local background sessions or cloud sessions) and manage them from this one, so the work does not fill this session's context. Use when the user wants to dispatch, delegate or fan out tasks to separate sessions, run something in the background or in the cloud, check on or collect dispatched work, bring a cloud session's files back, or move running work to the cloud before closing the laptop ("dispatch these", "run this in the cloud", "how are my sessions doing", "pull that cloud session", "I have to leave, hand it off").
argument-hint: "<tasks> | status | collect | review <name> | fix <name> | pull <name|id> | sessions | adopt <id> | envs | doctor | handoff"
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
python3 $D status [--repo <dir>]
python3 $D wait   [<name> ...] [--timeout <seconds>]
python3 $D result <name>
python3 $D review <name> [--local] [--model <id>]
python3 $D fix    <name> [--model <id>]
python3 $D pull   <name|session id> [--out <dir>] [--into-existing]
python3 $D sessions [--dispatch] [--all] [--json]
python3 $D adopt  <session id> --name <name> --repo <dir> [--brief-file <file>]
python3 $D envs   [--json]
python3 $D doctor
python3 $D clean  <name>
python3 $D forget <name>
```

If that path does not exist, the helper is the `scripts/dispatch.py` beside this file.
State lives in `~/.claude/dispatch/` (task list, briefs, reports, `config.json`), never in a
repo. Details that are not needed on every run are in `reference.md` beside this file:
read its section when this file points to it.

## `/dispatch <tasks>`

1. **Split** the request into independent tasks, one repository each. If two tasks would
   edit the same files, make them one task. A few minutes of work: do it yourself.
2. **Choose where each runs.**
   - **Local** is the default: the session has this machine's files, credentials,
     signed-in tools and network. It works in its own git worktree and branch.
   - **Cloud** for long work, when the user will close the laptop, or when this machine
     is short of resources, and only when the task needs nothing that exists only here
     (secrets, a cluster or VPN, a signed-in browser, local media, hardware). The folder
     is uploaded as it is, uncommitted changes included; GitHub is never used. The
     session sends back the files it changed, and `pull` or `review` brings them here.
   - `--needs-local` marks a local task that could never move to the cloud.
3. **Write a brief** per task to a temporary file. The session knows nothing about this
   conversation, so the brief must stand alone: the goal and why; where to look (files,
   docs, the command that shows the problem); what "done" means, as something checkable;
   what is out of scope. Do not add rules about branches, pushing or reporting: the
   helper appends those (work on its own branch; never push the default branch, merge,
   deploy, spend or message anyone; never wait on a question; run the tests; report in
   a fixed shape).
4. **Start them**, one helper call per task, each named with a short slug
   (`fix-flaky-login-test`). Its session is named `dispatch-<slug>` everywhere the user
   looks. The start line says where it runs, and for the cloud, on which environment.
5. **Tell the user** what was started as a table (task, where, environment) and stop. Do
   not wait for the sessions.

| The helper says | Do |
|---|---|
| not a trusted folder | Tell the user to run `claude` in that folder once and accept the prompt. Never accept it for them. |
| local_only | Run it with `local`. |
| not a git repository / no commit | Ask the user to `git init` and commit, or run it `local`. |
| limit reached | Tell the user what is running; start the rest later. |
| no environment named ... | Show the list it gives; ask which one, or use `--environment`. |

## `/dispatch status`

Run `status` and show its lines as a table. The states that need you:

- `waiting-for-you`: give the user the `claude attach <id>` (local) or the link (cloud).
- `stopped-no-report` (local): only then read the end of its log:
  `claude logs <id> | tail -c 3000`.
- `failed` / `blocked`: pass on what the session says; do not restart it unasked.
- `done` on a cloud task: its work is still in the cloud. `collect` brings it here.
- `quiet` or a `note:` line: claude.ai could not be read; run `doctor`.

Every state is described in reference.md, "Status states in full".

## `/dispatch collect`

A session's own report says what it believes. Before the user reads it, a session that
did not write the change checks it.

1. **Review.** Run `status` (or `--repo <dir>`). For each task that reports `done` (or
   `handoff` with nothing left) and has no `<name>-review` yet, run `review <name>`. It
   starts a new session with the original brief and the diff, never the writer's report
   or reasoning, **on a fresh cloud machine** by default, which also catches work that
   only runs here. A finished cloud task is first imported onto the local branch
   `cloud-<name>`. The start line says where the review runs and, if it is this
   machine, why. Skip `failed` and `blocked` tasks, and skip the step when the user says
   "no review". Findings are MAJOR (misses something the task calls done, or breaks
   what worked) or MINOR, and CONFIRMED (reproduced, or the failing lines named) or
   PLAUSIBLE.
2. **Wait**: `wait <name>-review ... --timeout 540`. It prints every status line when
   they have reported, starting with `still running after ...` if time ran out. Report
   what is ready and say what is pending.
3. **Send back what failed.** For each task whose newest review has a MAJOR CONFIRMED
   finding, run `fix <name>` (the task's own name): a new session in the task's
   worktree, on its branch, with the brief and only those findings. Then `wait`,
   `review <name>-fix<N>`, `wait`, and repeat while MAJOR CONFIRMED findings remain. The
   helper stops after `max_fix_rounds` (default 2, exit code 7). A round may find a
   finding false and change nothing: that ends the loop too. Never send MINOR or
   PLAUSIBLE findings back. Skip when the user says "no fixes".
4. **Report**, from `result <name>` and `result <name>-review`, highest `RISK` first:
   - `high`, or any MAJOR CONFIRMED finding left: what changed, the branch, each finding
     with file and line, what the user must decide. Say a person should read it.
   - `normal`: two or three lines, then the other findings a line each, PLAUSIBLE ones
     named as unconfirmed.
   - `low` / `clean`: one line.
   First, any check the writer says passed and the review ran and saw fail. A task with
   rounds is reported once, by its newest review, with what the rounds did. Then the
   tasks with no review, a line each, and why.
5. **The rest is the user's call.** Do not fix anything yourself. For other findings,
   dispatch a new task whose brief names the branch to continue and quotes them.
6. **Clean up** when the user agrees. Ask per branch: merge it, push it, or drop it; do
   that yourself only when told. `clean` reviews and fix rounds before their task, and a
   `<name>-cloud` continuation before the task it came from; then `clean <name>`. If it
   refuses over uncommitted files, show them and do not force it. More in reference.md,
   "Worktrees and branches".

Never merge a branch or pull request on your own judgement, whatever the review says.
`/dispatch review <name>` runs steps 1, 2 and 4 for one task; `/dispatch fix <name>` step 3.

## `/dispatch handoff`

The user is about to close the laptop; local sessions stop when it sleeps. For each local
task `status` shows as `running`:

**Not `needs_local`** (see `~/.claude/dispatch/tasks.jsonl`): move it.

1. Send the session `Hand off now, as your brief describes.` (SendMessage to
   `dispatch-<name>`). It commits its work in progress to its branch and reports
   `STATUS: handoff`. `wait <name> --timeout 300` (it reads messages between steps),
   then `result <name>`. If nothing is left, it is finished: stop it and say so.
2. No way to message it, or no report: `claude stop <id>`, then in its worktree
   (`<repo>/.claude/worktrees/<name>`) commit everything with the message
   `wip: handoff`. Push nothing.
3. Continue in the cloud from the worktree:
   `cloud --repo <repo>/.claude/worktrees/<name> --name <name>-cloud`, with a brief made of
   the original brief (`~/.claude/dispatch/briefs/<name>.md`, above the rules) plus the
   handoff report (done, left, what next), and anything the pacing or setup assumed
   about this machine.
4. `claude stop <id>`. Keep the worktree: `collect` reviews `<name>-cloud`, which imports
   its work onto `cloud-<name>-cloud` beside it.

**A fix round** moves like a task; its worktree is its task's. **A review**: `claude stop
<id>` and `forget` it; `collect` starts it again. **`needs_local`**: leave it running and
tell the user it only runs while the Mac is awake (lid open and plugged in, or
`sudo pmset -a disablesleep 1`, which they run and undo with `0`).

Finish with one table: each task, and whether it moved (with its link) or stays.

## Other commands

- `/dispatch pull <name|id>`: bring a cloud session's files here, for a dispatch task or
  any session. Show `PULLED.md`. Reference.md, "Cloud tasks: upload, pull, import".
- `/dispatch sessions`, `/dispatch adopt <id>`: list every cloud session of the account
  (teleport's list), and track one dispatch did not start. Reference.md, "Sessions,
  adopt, teleport".
- `/dispatch envs`: the cloud environments of this login. Reference.md, "Environments".
- `/dispatch doctor`: check the cloud parts after a Claude Code update.

## Things to know

- A dispatched session uses the user's normal permission settings. If those skip
  prompts, only the brief's rules hold it back: keep briefs narrow, and never dispatch a
  task whose point is to deploy, pay or send.
- Sessions cost usage in parallel. Dispatching saves this session's context, not the
  account's limit; say so when many tasks start at once.
- A review is a second opinion from one session, not a proof. `clean` means it found
  nothing; a PLAUSIBLE finding may be wrong.
- The cloud parts use undocumented Claude Code internals, found by reading its CLI. A
  Claude Code update can break them; `doctor` says which. Local tasks need none of them.
- Config keys (limits, models, environments, `local_only`, prefix): reference.md, "Config".
