#!/usr/bin/env python3
"""dispatch: start Claude Code sessions (local background or cloud) and track them.

Standard library only. State lives in ~/.claude/dispatch/ (never in a repo):

  config.json        optional local settings (see SKILL.md)
  tasks.jsonl        one line per dispatched task
  results/<name>.md  what each local session reports when it stops
  briefs/<name>.md   the exact brief each session was given

A review is a local task named <name>-review: a new session that did not write the
change reads the finished task's branch and reports a verdict.

Commands: local, cloud, review, wait, status, result, forget. Run with -h for flags.
"""

import argparse
import json
import os
import pty
import re
import select
import shutil
import subprocess
import sys
import time
from pathlib import Path

HOME = Path(os.environ.get("DISPATCH_HOME", Path.home() / ".claude" / "dispatch"))
TASKS = HOME / "tasks.jsonl"
RESULTS = HOME / "results"
BRIEFS = HOME / "briefs"
CONFIG = HOME / "config.json"

ANSI = re.compile(r"\x1b\[[0-9;?<>=]*[a-zA-Z]|\x1b\][^\x07]*\x07|\x1b[=>()][0-9A-B]?")
NAME_OK = re.compile(r"^[a-z0-9][a-z0-9-]{1,40}$")

RULES = """\
## Rules for this dispatched session

You were started by a manager session and nobody is watching you work.

- Work only on the task above, only in this repository.
- Put your work on the branch `{branch}`. Never commit to or push the default branch,
  never merge, never deploy, never spend money, never message or email anyone.
  If the task cannot be finished without one of those, stop and report `blocked`.
- Do not ask questions and wait: nobody will answer. If you need a decision, report
  `blocked` with the question.
- Before you report `done`, run the project's tests or build and say what happened.
{extra}"""

LOCAL_REPORT = """\
## Reporting (required, last step)

Write your result to `{result}` in exactly this shape, then stop:

    STATUS: done | failed | blocked
    BRANCH: <branch name, or none>
    PR: <url, or none>
    TESTS: <command you ran and whether it passed, or not run and why>
    SUMMARY: <2 to 5 lines: what changed, what is left, anything the owner must decide>

If you are told to hand off, write the same file with `STATUS: handoff` and put what is
done, what is left and what you were about to try under SUMMARY, commit your work in
progress to `{branch}`, push that branch, and stop.
{notify}"""

CLOUD_REPORT = """\
## Reporting (required, last step)

Push the branch `{branch}` and open a pull request from it (draft if unfinished). Start
the pull request description with these lines:

    STATUS: done | failed | blocked
    TESTS: <command you ran and whether it passed, or not run and why>
    SUMMARY: <2 to 5 lines: what changed, what is left, anything the owner must decide>

If you cannot open a pull request, push the branch and add the same text as
`DISPATCH_RESULT.md` in its last commit.
"""

RULES_MARK = "## Rules for this dispatched session"

REVIEW = """\
# Review another session's work

Another Claude Code session was given the task below and reports that it is finished.
You did not write this change and you have not seen that session's reasoning or its
report. Find out whether the change does what the task asked, and whether it breaks
anything.

## The task it was given

{brief}

## What to review

The change is `{ref}` compared with `{base}` in this repository:

    git diff --stat {base} {ref}
    git diff {base} {ref}

You are in your own git worktree. `git checkout --detach {ref}` puts the changed code
in it, so you can run things without touching anyone else's files.

## How

- Read the diff, then the code around it. A diff does not show the callers.
- Check each thing the task calls "done" yourself. Run the tests or the build that
  cover the change if they can run here, and keep the last lines of the real output.
- Look for: behaviour that changed and was not asked for, cases that now break for
  existing users or existing data, tests that are missing or prove nothing, work left
  half done, and secrets in the diff.
- Try to prove each finding wrong before you report it: reproduce it, or follow the
  code path from start to end. Mark it CONFIRMED when you reproduced it or can point
  at the exact lines that fail, and PLAUSIBLE when you could not settle it.
- Leave out style remarks and anything you cannot tie to a concrete failure.
- Do not fix anything. Never edit the change, commit, push, merge, deploy or message
  anyone. Do not ask questions and wait: nobody will answer.
{extra}
## Reporting (required, last step)

Write your result to `{result}` in exactly this shape, then stop:

    STATUS: done | failed
    VERDICT: clean | findings
    RISK: low | normal | high
    CHECKS: <each command you ran and the last lines of its output, or not run and why>
    FINDINGS:
    1. CONFIRMED | PLAUSIBLE  <file>:<line>  <what goes wrong, and with which input>

Write `FINDINGS: none` when there are none. RISK is about what the change touches, not
about its quality: `high` for money, a database schema, sign-in or permissions, deleting
data, or deploy configuration; `low` for documentation or UI text only; `normal` for
the rest. Use `STATUS: failed` only when you could not review at all, and say why under
CHECKS.
{notify}"""


def die(msg, code=1):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


def load_config():
    if CONFIG.exists():
        try:
            return json.loads(CONFIG.read_text())
        except json.JSONDecodeError as e:
            die(f"{CONFIG} is not valid JSON: {e}")
    return {}


def load_tasks():
    if not TASKS.exists():
        return []
    return [json.loads(l) for l in TASKS.read_text().splitlines() if l.strip()]


def save_tasks(tasks):
    HOME.mkdir(parents=True, exist_ok=True)
    TASKS.write_text("".join(json.dumps(t) + "\n" for t in tasks))


def run(cmd, cwd=None, timeout=60):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)


def git(repo, *args):
    r = run(["git", "-C", str(repo), *args])
    return r.stdout.strip() if r.returncode == 0 else ""


def sessions():
    """Background and interactive sessions known to the CLI, keyed by short id."""
    r = run(["claude", "agents", "--json", "--all"])
    if r.returncode != 0:
        return {}
    try:
        return {s["id"]: s for s in json.loads(r.stdout) if s.get("id")}
    except (json.JSONDecodeError, TypeError):
        return {}


def check_common(args, tasks, cfg):
    repo = Path(args.repo).expanduser().resolve()
    if not repo.is_dir():
        die(f"{repo} is not a directory")
    if not NAME_OK.match(args.name):
        die("name must be lowercase letters, digits and dashes (2 to 41 characters)")
    if any(t["name"] == args.name for t in tasks):
        die(f"a task named {args.name} already exists; pick another name or `forget` it")
    brief = Path(args.brief_file).read_text().strip()
    if not brief:
        die("the brief is empty")
    if args.where == "local":
        check_limit(tasks, cfg)
    return repo, brief


def check_limit(tasks, cfg):
    limit = int(cfg.get("max_parallel", 4))
    live = sessions()
    running = [
        t for t in tasks
        if t["where"] == "local" and live.get(t["id"], {}).get("state") == "working"
    ]
    if len(running) >= limit:
        die(f"{len(running)} local tasks are already running (limit {limit}); wait or raise max_parallel")


def notify_text(cfg, name):
    if not cfg.get("notify"):
        return ""
    return (
        "\nAfter writing the file, notify the owner once with this command (skip it if "
        "your project instructions already made you send a notification):\n\n    "
        + cfg["notify"].replace("{name}", name) + "\n"
    )


def start_bg(repo, name, prompt, worktree):
    """Start a local background session and return its short id."""
    cmd = ["claude", "--bg", "-n", name]
    if worktree:
        cmd += ["-w", name]
    cmd.append(prompt)
    r = run(cmd, cwd=repo, timeout=120)
    out = ANSI.sub("", r.stdout + r.stderr)
    m = re.search(r"backgrounded\s*·\s*([0-9a-f]{6,})", out)
    if not m:
        if "not trusted" in out:
            die(f"{repo} is not a trusted folder. The owner must run `claude` there once "
                "and accept the trust prompt. Do not accept it for them.", 3)
        die("could not start the session:\n" + out.strip()[-600:])
    return m.group(1)


def cmd_local(args):
    cfg, tasks = load_config(), load_tasks()
    args.where = "local"
    repo, brief = check_common(args, tasks, cfg)
    is_git = bool(git(repo, "rev-parse", "--git-dir"))
    use_wt = is_git and not args.no_worktree
    branch = f"worktree-{args.name}" if use_wt else f"dispatch/{args.name}"
    result = RESULTS / f"{args.name}.md"
    notify = notify_text(cfg, args.name)
    extra = cfg.get("extra_rules", "")
    full = "\n\n".join([
        brief,
        RULES.format(branch=branch, extra=extra),
        LOCAL_REPORT.format(result=result, branch=branch, notify=notify),
    ])
    for d in (RESULTS, BRIEFS):
        d.mkdir(parents=True, exist_ok=True)
    (BRIEFS / f"{args.name}.md").write_text(full + "\n")
    result.unlink(missing_ok=True)

    base = git(repo, "rev-parse", "HEAD") if is_git else None
    task = {
        "name": args.name, "where": "local", "id": start_bg(repo, args.name, full, use_wt),
        "repo": str(repo), "branch": branch if is_git else None, "base": base or None,
        "worktree": use_wt, "needs_local": bool(args.needs_local),
        "started": int(time.time()),
    }
    save_tasks(tasks + [task])
    print(f"started local {args.name} id={task['id']} repo={repo.name} branch={task['branch']}")


def spawn_in_pty(cmd, cwd, timeout=90):
    """Run cmd on a pseudo-terminal (some claude flags refuse to run without one)."""
    master, slave = pty.openpty()
    p = subprocess.Popen(cmd, cwd=cwd, stdin=slave, stdout=slave, stderr=slave,
                         start_new_session=True)
    os.close(slave)
    buf, end = b"", time.time() + timeout
    while time.time() < end:
        ready, _, _ = select.select([master], [], [], 0.5)
        if ready:
            try:
                chunk = os.read(master, 65536)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            text = buf.decode("utf-8", "replace")
            if "trust this folder" in ANSI.sub(" ", text):
                p.kill()
                break
        elif p.poll() is not None:
            break
    if p.poll() is None:
        p.kill()
    os.close(master)
    return re.sub(r"[ \t]+", " ", ANSI.sub(" ", buf.decode("utf-8", "replace")))


def cmd_cloud(args):
    cfg, tasks = load_config(), load_tasks()
    args.where = "cloud"
    repo, brief = check_common(args, tasks, cfg)
    remote = git(repo, "remote", "get-url", "origin")
    if "github.com" not in remote:
        die(f"{repo.name} has no GitHub remote; a cloud session cannot see it. Run it locally.", 4)
    if any(repo.name == n or str(repo).endswith(n) for n in cfg.get("local_only", [])):
        die(f"{repo.name} is listed as local_only in {CONFIG}. Run it locally.", 4)
    dirty = git(repo, "status", "--porcelain")
    ahead = git(repo, "log", "--oneline", "@{u}..HEAD")
    if (dirty or ahead) and not args.allow_unpushed:
        die("the repo has "
            + ("uncommitted changes" if dirty else "")
            + (" and " if dirty and ahead else "")
            + ("unpushed commits" if ahead else "")
            + ". The cloud session only sees what is on GitHub. Push first, or pass "
              "--allow-unpushed if the task does not depend on them.", 5)
    branch = f"dispatch/{args.name}"
    base = args.base or git(repo, "branch", "--show-current") or "the default branch"
    full = "\n\n".join([
        brief,
        f"Start from the branch `{base}`.",
        RULES.format(branch=branch, extra=cfg.get("extra_rules", "")),
        CLOUD_REPORT.format(branch=branch),
    ])
    BRIEFS.mkdir(parents=True, exist_ok=True)
    (BRIEFS / f"{args.name}.md").write_text(full + "\n")

    out = spawn_in_pty(["claude", "--cloud", full, "-n", args.name], cwd=repo)
    if "trust this folder" in out:
        die(f"{repo} is not a trusted folder. The owner must run `claude` there once "
            "and accept the trust prompt. Do not accept it for them.", 3)
    m = re.search(r"(session_[A-Za-z0-9]+)", out)
    if "Created cloud session" not in out or not m:
        die("could not create the cloud session:\n" + out.strip()[-600:])
    url = re.search(r"https://claude\.ai/code/\S+", out)
    task = {
        "name": args.name, "where": "cloud", "id": m.group(1), "repo": str(repo),
        "branch": branch, "base": args.base or git(repo, "branch", "--show-current") or None,
        "url": url.group(0).split("?")[0] if url else None,
        "started": int(time.time()),
    }
    save_tasks(tasks + [task])
    print(f"started cloud {args.name} id={task['id']} repo={repo.name} branch={branch}")
    print(f"view: {task['url']}")


def resolves(repo, ref):
    return bool(ref and git(repo, "rev-parse", "--verify", "--quiet", ref + "^{commit}"))


def review_range(t, repo):
    """The two commits a review compares: where the task's branch left its base, and its tip."""
    if not t.get("branch"):
        die(f"{t['name']} has no branch (its folder is not a git repository); there is nothing to diff")
    cloud = t["where"] == "cloud"
    if cloud:
        run(["git", "-C", str(repo), "fetch", "--quiet", "origin", t["branch"]], timeout=120)
    ref = f"origin/{t['branch']}" if cloud else t["branch"]
    if not resolves(repo, ref):
        die(f"the branch {ref} does not exist" + (" on GitHub yet" if cloud else "")
            + f"; {t['name']} has nothing to review")
    head = git(repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    b = t.get("base")
    for cand in ((f"origin/{b}", b) if cloud else (b,)) + (head, "origin/main", "main", "origin/master", "master"):
        if resolves(repo, cand):
            base = git(repo, "merge-base", cand, ref)
            if base:
                break
    else:
        die(f"could not find the commit {ref} started from")
    tip = git(repo, "rev-parse", ref)
    if base == tip:
        die(f"{ref} has no commits of its own; {t['name']} has nothing to review")
    return base[:12], ref


def cmd_review(args):
    cfg, tasks = load_config(), load_tasks()
    t = next((t for t in tasks if t["name"] == args.name), None)
    if not t:
        die(f"no task named {args.name}")
    if t.get("kind") == "review":
        die(f"{args.name} is itself a review")
    name = f"{args.name}-review"
    if any(x["name"] == name for x in tasks):
        die(f"{args.name} already has a review; `result {name}` shows it, `forget {name}` allows another")
    repo = Path(t["repo"])
    base, ref = review_range(t, repo)
    check_limit(tasks, cfg)
    bf = BRIEFS / f"{args.name}.md"
    brief = bf.read_text().split(RULES_MARK)[0].strip() if bf.exists() else ""
    if not brief:
        die(f"the brief of {args.name} is missing ({bf}); a review needs to know what was asked")
    extra = cfg.get("extra_rules", "")
    result = RESULTS / f"{name}.md"
    full = REVIEW.format(brief=brief, base=base, ref=ref, result=result,
                         extra=extra + "\n" if extra else "", notify=notify_text(cfg, name))
    RESULTS.mkdir(parents=True, exist_ok=True)
    (BRIEFS / f"{name}.md").write_text(full + "\n")
    result.unlink(missing_ok=True)
    task = {
        "name": name, "where": "local", "kind": "review", "of": args.name,
        "id": start_bg(repo, name, full, True), "repo": str(repo), "branch": None,
        "worktree": True, "needs_local": True, "started": int(time.time()),
    }
    save_tasks(tasks + [task])
    print(f"started review {name} id={task['id']} repo={repo.name} diff={base}..{ref}")


def cmd_wait(args):
    """Block until the named local tasks (default: all of them) have reported or stopped."""
    tasks = [t for t in load_tasks() if t["where"] == "local"
             and (not args.names or t["name"] in args.names)]
    end = time.time() + args.timeout
    while True:
        live = sessions()
        left = [t["name"] for t in tasks
                if not (RESULTS / f"{t['name']}.md").exists()
                and live.get(t["id"], {}).get("state") == "working"]
        if not left or time.time() >= end:
            break
        time.sleep(10)
    if left:
        print(f"still running after {args.timeout}s: {', '.join(left)}")
    cmd_status(args)


def read_result(name):
    f = RESULTS / f"{name}.md"
    if not f.exists():
        return None, ""
    text = f.read_text()
    m = re.search(r"^STATUS:\s*(\w+)", text, re.M)
    status = m.group(1).lower() if m else "unknown"
    v = re.search(r"^VERDICT:\s*(\w+)", text, re.M)
    if v:  # a review: its note is the verdict, the risk and how many findings
        r = re.search(r"^RISK:\s*(\w+)", text, re.M)
        n = len(re.findall(r"^\s*\d+\.\s", text.split("FINDINGS:")[-1], re.M)) if "FINDINGS:" in text else 0
        c = len(re.findall(r"^\s*\d+\.\s+CONFIRMED", text, re.M))
        note = f"{v.group(1).lower()}, risk {r.group(1).lower() if r else 'unknown'}"
        if n:
            note += f", {n} finding{'s' if n != 1 else ''} ({c} confirmed)"
        return status, note
    s = re.search(r"^SUMMARY:\s*(.+)", text, re.M)
    return status, (s.group(1).strip() if s else "")


def cloud_state(t):
    """A cloud task is judged by what reached GitHub: its branch and pull request."""
    repo = t["repo"]
    if shutil.which("gh"):
        r = run(["gh", "pr", "list", "--head", t["branch"], "--state", "all",
                 "--json", "url,state,isDraft,body", "--limit", "1"], cwd=repo)
        if r.returncode == 0 and r.stdout.strip() not in ("", "[]"):
            pr = json.loads(r.stdout)[0]
            m = re.search(r"STATUS:\s*(\w+)", pr.get("body") or "")
            st = m.group(1).lower() if m else ("draft" if pr["isDraft"] else "pr-open")
            return st, pr["url"]
    if git(repo, "ls-remote", "--heads", "origin", t["branch"]):
        return "branch-pushed", f"branch {t['branch']} (no pull request yet)"
    return "running-or-no-output", t.get("url") or ""


def cmd_status(args):
    tasks = load_tasks()
    if not tasks:
        print("no dispatched tasks")
        return
    live = sessions()
    for t in tasks:
        age = int((time.time() - t["started"]) / 60)
        if t["where"] == "cloud":
            state, note = cloud_state(t)
        else:
            s = live.get(t["id"])
            rstat, summary = read_result(t["name"])
            cli = s.get("state") if s else "gone"
            # working: still going; blocked: waiting for a person; done: stopped
            if cli == "working":
                state, note = "running", ""
            elif rstat:
                state, note = rstat, summary
            elif cli == "blocked":
                state, note = "waiting-for-you", f"claude attach {t['id']}"
            elif cli == "done":
                state, note = "stopped-no-report", f"claude logs {t['id']}"
            else:
                state, note = "gone", "session no longer listed and left no report"
        where = "review" if t.get("kind") == "review" else t["where"]
        print(f"{t['name']:<28} {where:<6} {state:<20} {age:>4}m  {Path(t['repo']).name}  {note[:90]}")


def cmd_result(args):
    t = next((t for t in load_tasks() if t["name"] == args.name), None)
    if not t:
        die(f"no task named {args.name}")
    if t["where"] == "cloud":
        state, note = cloud_state(t)
        print(f"STATUS: {state}\n{note}\nview: {t.get('url')}\nbring it to this computer: claude --teleport {t['id']}")
        return
    f = RESULTS / f"{args.name}.md"
    print(f.read_text() if f.exists() else f"no report yet; look with: claude logs {t['id']}")


def cmd_forget(args):
    tasks = load_tasks()
    keep = [t for t in tasks if t["name"] != args.name]
    if len(keep) == len(tasks):
        die(f"no task named {args.name}")
    save_tasks(keep)
    print(f"forgot {args.name} (the session, its branch and its report file are untouched)")


def main():
    p = argparse.ArgumentParser(prog="dispatch.py", description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    for where, fn in (("local", cmd_local), ("cloud", cmd_cloud)):
        s = sub.add_parser(where, help=f"start a {where} session")
        s.add_argument("--repo", required=True, help="the project directory")
        s.add_argument("--name", required=True, help="short unique task name")
        s.add_argument("--brief-file", required=True, help="file holding the task brief")
        if where == "local":
            s.add_argument("--no-worktree", action="store_true",
                           help="work in the repo itself, not in a new git worktree")
            s.add_argument("--needs-local", action="store_true",
                           help="mark the task as unable to move to the cloud")
        else:
            s.add_argument("--base", help="branch the cloud session starts from")
            s.add_argument("--allow-unpushed", action="store_true")
        s.set_defaults(fn=fn)

    sub.add_parser("status", help="one line per task").set_defaults(fn=cmd_status)
    s = sub.add_parser("wait", help="block until local tasks report, then print status")
    s.add_argument("names", nargs="*", help="task names (default: every local task)")
    s.add_argument("--timeout", type=int, default=540, help="seconds to wait at most")
    s.set_defaults(fn=cmd_wait)
    for name, fn, text in (
        ("review", cmd_review, "start a new session that reviews a finished task's branch"),
        ("result", cmd_result, "print a task's report"),
        ("forget", cmd_forget, "drop a task from the list"),
    ):
        s = sub.add_parser(name, help=text)
        s.add_argument("name")
        s.set_defaults(fn=fn)

    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
